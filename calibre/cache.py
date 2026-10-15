"""Dated full snapshots, atomic active pointers, and independent daily refresh."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
import subprocess
import sys
from pathlib import Path
import threading
import time
from config import Settings
from .client import ClientError, ClassifiedError, classify_failure
from .catalog import ScanCancelled
from .models import Book
from .state import atomic_text, state_lock, deduplicate_libraries, try_state_lock
from .library_state import LibraryState, library_key, source_lock
from .poll_worker import checkpoint_for


def now():
    return datetime.now(timezone.utc).isoformat()


def current_worker_id():
    name = threading.current_thread().name
    if name.startswith('catalog-worker_'):
        suffix = name.rsplit('_', 1)[1]
        if suffix.isdigit():
            return int(suffix) + 1
    return None


def is_stale(entry, hours):
    last = entry.get('last_success')
    return (not last or bool(entry.get('error')) or
            (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= hours * 3600)



class CatalogStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.index = self.directory / 'index.json'

    def manifest(self):
        return json.loads(self.index.read_text()) if self.index.exists() else {'libraries': {}}

    def entry(self, library):
        return self.manifest()['libraries'].get(library_key(library), {})

    def load(self, library):
        entry = self.entry(library)
        if not entry.get('snapshot'):
            return None, entry
        path = self.directory / entry['snapshot']
        if path.parent.resolve() != self.directory.resolve():
            raise ValueError('Invalid catalog snapshot path')
        data = json.loads(path.read_text())
        return [Book.from_dict(book) for book in data['books']], entry

    def update(self, library, fields):
        with state_lock(self.index):
            manifest = self.manifest()
            entry = manifest['libraries'].setdefault(library_key(library), {})
            entry.update(source=library.url, configured_library_id=library.library_id, **fields)
            atomic_text(self.index, json.dumps(manifest, indent=2))

    def publish(self, library, books, started, extra_fields=None):
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S%fZ')
        filename = f'{library_key(library)}_{stamp}.json'
        path = self.directory / filename
        # Readers take a shared lock while opening the active file. This keeps
        # deletion after publication from racing a reader of the old pointer.
        with state_lock(self.index):
            manifest = self.manifest()
            previous = manifest['libraries'].get(library_key(library), {})
            atomic_text(path, json.dumps({'refreshed_at': now(), 'books': [b.to_dict() for b in books]}, ensure_ascii=False))
            entry = {**previous, 'source': library.url, 'configured_library_id': library.library_id,
                     'snapshot': filename, 'book_count': len(books), 'last_success': now(),
                     'last_attempt': started, 'error': None, 'refresh_in_progress': False,
                     **(extra_fields or {})}
            manifest['libraries'][library_key(library)] = entry
            try:
                atomic_text(self.index, json.dumps(manifest, indent=2))
            except Exception:
                path.unlink(missing_ok=True)
                raise
            if previous.get('snapshot') and previous['snapshot'] != filename:
                old = self.directory / previous['snapshot']
                if old.parent.resolve() == self.directory.resolve():
                    try:
                        old.unlink(missing_ok=True)
                    except OSError:
                        logging.getLogger(__name__).warning('Old catalog snapshot could not be removed')
        return entry

    def read(self, library):
        with state_lock(self.index):
            return self.load(library)

    def due(self, library, hours):
        entry = self.entry(library)
        if entry.get('refresh_in_progress') or entry.get('covers_in_progress'):
            return True
        retry_at = entry.get('retry_not_before')
        if retry_at and datetime.now(timezone.utc) < datetime.fromisoformat(retry_at):
            return False
        if retry_at and entry.get('failure_category') == 'temporary':
            return True
        last = entry.get('last_attempt')
        if not last:
            return True
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= hours * 3600


def cleanup_covers(store, report=lambda message: None):
    """Retain images referenced by any saved catalog or pending page journal."""
    directory = store.directory / 'covers'
    if not directory.exists():
        return
    keep = set()
    with state_lock(store.index):
        entries = list(store.manifest()['libraries'].values())
    # Caller holds exclusive activity and lifecycle locks, so publication cannot remove these
    # snapshots while we inspect them. Keep the index free for GUI searches.
    for entry in entries:
        if entry.get('snapshot'):
            path = store.directory / entry['snapshot']
            if path.parent.resolve() != store.directory.resolve():
                raise ValueError('Invalid catalog snapshot path')
            for item in json.loads(path.read_text())['books']:
                book = Book.from_dict(item)
                if book.cover_url:
                    keep.add(book.key)
    for cursor in (store.directory / 'checkpoints').glob('*/cursor.json'):
        state = json.loads(cursor.read_text())
        for index in range(state['page_count']):
            for item in json.loads((cursor.parent / f'page_{index:06d}.json').read_text()):
                book = Book.from_dict(item)
                if book.cover_url:
                    keep.add(book.key)
    removed = 0
    for path in directory.glob('*.img'):
        if path.stem not in keep:
            path.unlink()
            path.with_suffix('.json').unlink(missing_ok=True)
            removed += 1
    for path in directory.glob('*.tmp'):
        path.unlink(missing_ok=True)
    if removed:
        report(f'Removed {removed} obsolete cover files')



class CatalogManager:
    def __init__(self, config):
        self.config = Path(config)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.cancel_scan = threading.Event()
        self.scheduler = None
        self.worker = None
        self.running = False
        self.paused_until = 0
        self.events = []
        self.active = None
        self.activities = {}
        self.batch = {}
        self.logger = logging.Logger('catalog-refresh')
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(logging.StreamHandler(sys.stdout))
        settings = Settings.load(self.config)
        log_path = Path(settings.catalog_cache_dir) / 'refresh.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.addHandler(RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3))

    def event(self, message):
        worker = current_worker_id()
        prefix = f'[worker {worker}] ' if worker is not None else ''
        message = f'{now()} {prefix}{str(message).replace(chr(10), " ").replace(chr(13), " ")}'
        with self.lock:
            self.events.append(message)
            self.events = self.events[-200:]
        self.logger.info(message)

    def maintain_libraries(self, settings):
        removed = deduplicate_libraries(settings)
        if removed:
            self.event(f'Removed {removed} duplicate library entries; original list saved as '
                       f'{Path(settings.libraries_file).name}.dedup.bak')
        LibraryState(settings, self.event).reconcile()

    def status(self):
        settings = Settings.load(self.config)
        store = CatalogStore(settings.catalog_cache_dir)
        libraries = settings.libraries() if Path(settings.libraries_file).exists() else []
        manifest = store.manifest()['libraries']
        from .servers import registry
        servers = registry(settings)
        entries = []
        for _, library in libraries:
            entry = manifest.get(library_key(library), {})
            entries.append({'source': library.url, 'library_id': (library.library_id if library.explicit_id else servers.get(library.url, {}).get('default_id', library.library_id)), 'book_count': entry.get('book_count', 0),
                            'last_success': entry.get('last_success'), 'last_attempt': entry.get('last_attempt'),
                            'error': entry.get('error'), 'cached': bool(entry.get('snapshot')),
                            'covers_error': entry.get('covers_error'),
                            'covers_pending': entry.get('covers_pending', False),
                            'covers_failed': entry.get('covers_failed'),
                            'stale': is_stale(entry, settings.catalog_refresh_interval_hours)})
        lifecycle = LibraryState(settings)
        counters = lifecycle.load()['active']
        for item, (_, library) in zip(entries, libraries):
            health = counters.get(library_key(library), {})
            item['download_timeout_count'] = health.get('timeout_count', 0)
            item['scan_failure_count'] = health.get('scan_failure_count', 0)
            item['health'] = health.get('scan_category', 'unknown')
            item['last_scan_outcome'] = health.get('last_scan_outcome')
        with self.lock:
            running, events = self.running, list(self.events)
            batch = dict(self.batch)
            activities = [dict(item) for item in self.activities.values()]
            if self.active:
                activities = [dict(self.active)] + [item for item in activities if item['source'] != self.active['source']]
        for item, (_, library) in zip(entries, libraries):
            item['batch_state'] = batch.get(library_key(library), 'not in latest batch')
        for item in activities:
            item['elapsed_seconds'] = round(time.monotonic() - item.pop('started_monotonic'), 1)
        return {'enabled': settings.catalog_cache_enabled, 'running': running, 'events': events,
                'batch': {name: list(batch.values()).count(name) for name in ('queued','checking','scanning','succeeded','failed','skipped','cancelled')},
                'server_count': len({lib.url for _, lib in settings.raw_libraries()}),
                'libraries': entries, 'interval_hours': settings.catalog_refresh_interval_hours,
                'active': activities[0] if activities else None, 'active_libraries': activities,
                'concurrency': settings.catalog_scan_concurrency,
                'library_timeout_seconds': settings.catalog_library_timeout_seconds,
                'covers_enabled': settings.catalog_cache_covers,
                'retired': lifecycle.load()['history'],
                'scan_failure_limit': settings.library_scan_failure_limit,
                'download_timeout_limit': settings.library_download_timeout_limit}

    def refresh(self, force=False, covers_only=False):
        settings = Settings.load(self.config)
        if not settings.catalog_cache_enabled:
            raise ValueError('Enable catalog_cache_enabled in config.json first')
        if covers_only and not settings.catalog_cache_covers:
            raise ValueError('Enable catalog_cache_covers in config.json first')
        if not force and time.monotonic() < self.paused_until:
            return False
        self.maintain_libraries(settings)
        libraries = settings.libraries()
        if not libraries:
            raise ValueError('No libraries configured')
        store = CatalogStore(settings.catalog_cache_dir)
        if not force and not covers_only and not any(store.due(lib, settings.catalog_refresh_interval_hours) for _, lib in libraries):
            return False
        with self.lock:
            if self.running or self.stop.is_set():
                return False
            self.running = True
            if force:
                self.paused_until = 0
            self.events = []
            self.cancel_scan.clear()
            self.worker = threading.Thread(target=self.run, args=(force, covers_only), daemon=True)
            self.worker.start()
        return True

    def run_worker(self, settings, library, covers_only):
        """A process boundary bounds headers, DNS, body reads, and retries alike."""
        started = time.monotonic()
        key = library_key(library)
        lifecycle = LibraryState(settings)
        entry = lifecycle.load()['active'].get(key)
        if not entry:
            raise ScanCancelled('Library removed before its worker started')
        generation = entry['generation']
        next_membership_check = started
        with self.lock:
            self.activities[key] = {'source': library.url, 'phase': 'starting', 'count': 0,
                           'worker_id': current_worker_id(), 'library_id': library.library_id,
                           'metadata_published': False,
                           'started_monotonic': started, 'last_activity': now(),
                           'deadline_seconds': settings.catalog_library_timeout_seconds}
        process = subprocess.Popen([sys.executable, '-m', 'calibre.poll_worker'],
                                   cwd=str(Path(__file__).resolve().parent.parent),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True)
        messages = queue.Queue()
        def read_messages():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()
        success, error, cover_summary = False, None, {}
        error_category, retry_after = 'catalog', 0
        heartbeat = started
        try:
            # Credentials are passed over stdin, never command arguments or files.
            json.dump({'settings': asdict(settings), 'library': asdict(library),
                       'key': library_key(library), 'covers_only': covers_only}, process.stdin)
            process.stdin.close()
            while True:
                if time.monotonic() >= next_membership_check:
                    if not lifecycle.allowed(library, generation):
                        raise ScanCancelled('Library removed; stopping worker before cache purge')
                    next_membership_check = time.monotonic() + 0.5
                if self.cancelled():
                    raise ScanCancelled('Refresh stopped; saved pages and completed covers retained for resume')
                elapsed = time.monotonic() - started
                if elapsed >= settings.catalog_library_timeout_seconds:
                    raise ClassifiedError(f'Library deadline reached after {elapsed:.1f}s; saved progress retained; moving on', 'deadline')
                try:
                    item = messages.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive() and messages.empty():
                        break
                else:
                    self.event(f'{library.url}'+(f' [library {library.library_id}]' if library.explicit_id else ' [default library]')+f': {item["message"]}')
                    if item.get('phase') == 'libraries-discovered' and (not library.explicit_id or library.parent_url):
                        from .servers import advertise
                        advertise(settings, library.url, item.get('library_ids', []),
                                  item.get('default_library_id'), authoritative=not library.explicit_id)
                        LibraryState(settings, self.event).reconcile()
                        with self.lock:
                            if not library.explicit_id:
                                self.activities[key]['library_id'] = item.get('default_library_id') or library.library_id
                            for _, discovered in settings.libraries():
                                self.batch.setdefault(library_key(discovered), 'queued')
                    if item.get('phase') == 'metadata-unchanged' and not covers_only:
                        with state_lock(lifecycle.gate):
                            if not lifecycle.allowed(library, generation):
                                raise ScanCancelled('Library removed during count check')
                            store = CatalogStore(settings.catalog_cache_dir)
                            if not store.entry(library).get('snapshot'):
                                raise ClientError('Saved catalog disappeared during count check')
                            store.update(library, {'last_success': now(), 'error': None,
                                'refresh_in_progress': False, 'failure_category': None,
                                'retry_not_before': None, 'temporary_failure_count': 0,
                                'covers_pending': settings.catalog_cache_covers,
                                'covers_in_progress': settings.catalog_cache_covers})
                        lifecycle.scan_outcome(library, success=True)
                        with self.lock:
                            self.activities[key]['metadata_published'] = True
                    if item.get('phase') == 'metadata-complete' and not covers_only:
                        published = self.publish_metadata(settings, library, generation)
                        if published:
                            lifecycle.scan_outcome(library, success=True)
                        with self.lock:
                            self.activities[key]['metadata_published'] = published
                    if item.get('phase') == 'covers-complete':
                        cover_summary = {name: item.get(name, 0) for name in ('total', 'failed', 'fetched', 'reused')}
                    with self.lock:
                        self.activities[key].update({k: v for k, v in item.items()
                                            if k not in ('message', 'error', 'success')})
                        self.activities[key]['last_activity'] = now()
                    success = success or item.get('success', False)
                    if item.get('error'):
                        error = item['message']
                        error_category = item.get('category', 'catalog')
                        retry_after = item.get('retry_after', 0)
                if time.monotonic() - heartbeat >= 30:
                    with self.lock:
                        activity = dict(self.activities[key])
                    self.event(f'{library.url}: still active ({activity["phase"]}, '
                               f'{elapsed:.0f}s elapsed, deadline {settings.catalog_library_timeout_seconds:g}s)')
                    heartbeat = time.monotonic()
            if process.returncode != 0 or not success:
                raise ClassifiedError(error or 'Catalog worker exited before completing', error_category, retry_after)
            with self.lock:
                published = self.activities[key]['metadata_published']
            return {**cover_summary, 'metadata_published': published}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if not process.stdin.closed:
                process.stdin.close()
            reader.join(timeout=1)
            process.stdout.close()

    def cleanup_covers(self, store):
        cleanup_covers(store, self.event)

    def publish_metadata(self, settings, library, generation=None):
        """Publish a complete checkpoint independently of the cover phase."""
        lifecycle = LibraryState(settings)
        checkpoint = checkpoint_for(settings, library, library_key(library))
        state, books = checkpoint.load()
        if not state or not state.get('complete'):
            return False
        with state_lock(lifecycle.gate):
            if not lifecycle.allowed(library, generation):
                raise ScanCancelled('Library removed before catalog publication')
            store = CatalogStore(settings.catalog_cache_dir)
            entry = store.entry(library)
            fields = {'metadata_checkpoint_stamp': state['saved_at'],
                      'covers_pending': settings.catalog_cache_covers,
                      'covers_in_progress': settings.catalog_cache_covers,
                      'cover_job_only': False, 'covers_error': None, 'failure_category': None,
                      'retry_not_before': None, 'temporary_failure_count': 0}
            if entry.get('metadata_checkpoint_stamp') != state['saved_at']:
                fields.update(covers_failed=None, covers_last_success=None)
                store.publish(library, list(books.values()), entry.get('last_attempt', now()), fields)
                self.event(f'{library.url}: published {len(books)} books; catalog searchable now'+
                           ('; cover fetching continues' if settings.catalog_cache_covers else ''))
            else:
                fields.update(error=None, refresh_in_progress=False)
                store.update(library, fields)
                self.event(f'{library.url}: complete catalog already published; reusing metadata')
        return True

    def run_one(self, settings, library, force, covers_only):
        key = library_key(library)
        with self.lock:
            self.batch[key] = 'checking'
        try:
            self.scan_one(settings, library, force, covers_only)
        except Exception:
            with self.lock:
                self.batch[key] = 'failed'
            raise
        finally:
            with self.lock:
                if self.batch.get(key) in ('checking', 'scanning'):
                    self.batch[key] = 'cancelled' if self.cancelled() else 'skipped'

    def scan_one(self, settings, library, force, covers_only):
        lifecycle = LibraryState(settings, self.event)
        key = library_key(library)
        store = CatalogStore(settings.catalog_cache_dir)
        def stopped():
            return self.cancelled() or not lifecycle.allowed(library)
        try:
            with state_lock(lifecycle.directory / 'activity', cancelled=stopped, shared=True), \
                    state_lock(source_lock(lifecycle.directory, library.url), cancelled=stopped, shared=True):
                with state_lock(lifecycle.directory / 'locks' / key, cancelled=stopped):
                    if stopped():
                        return
                    entry = store.entry(library)
                    if not force and settings.catalog_cache_covers and entry.get('covers_in_progress') and entry.get('cover_job_only'):
                        covers_only = True
                    if covers_only and not store.entry(library).get('snapshot'):
                        self.event(f'{library.url}: skipped cover-only refresh; no complete catalog yet')
                        return
                    if not force and not covers_only and not store.due(library, settings.catalog_refresh_interval_hours):
                        return
                    with self.lock:
                        self.batch[key] = 'scanning'
                    started = now()
                    retry_at = entry.get('retry_not_before')
                    if retry_at and datetime.now(timezone.utc) < datetime.fromisoformat(retry_at):
                        self.event(f'{library.url}: deferred until {retry_at} (server retry/backoff)')
                        with self.lock:
                            self.batch[key] = 'skipped'
                        return
                    with state_lock(lifecycle.gate):
                        if not lifecycle.allowed(library):
                            return
                        if not covers_only:
                            store.update(library, {'last_attempt': started, 'refresh_in_progress': True,
                                                   'covers_in_progress': False, 'cover_job_only': False})
                        else:
                            store.update(library, {'covers_in_progress': True, 'covers_pending': True,
                                                   'covers_error': None, 'cover_job_only': True})
                    self.event(f'{"Fetching missing covers for" if covers_only else "Refreshing"} {library.url}')
                    try:
                        summary = self.run_worker(settings, library, covers_only) or {}
                        if self.cancelled():
                            raise ScanCancelled('Refresh stopped; progress retained for resume')
                        if not covers_only and not summary.get('metadata_published') and not self.publish_metadata(settings, library):
                            raise ClientError('Worker did not save a complete catalog; old snapshot retained')
                        if not covers_only:
                            lifecycle.scan_outcome(library, success=True)
                        with state_lock(lifecycle.gate):
                            if not lifecycle.allowed(library):
                                self.event(f'{library.url}: removed; discarding worker completion')
                                return
                            fields = {'covers_pending': False, 'covers_in_progress': False,
                                      'covers_error': None}
                            if not covers_only:
                                fields.update(refresh_in_progress=False, failure_category=None, retry_not_before=None, temporary_failure_count=0)
                            if covers_only or settings.catalog_cache_covers:
                                fields.update(covers_last_success=now(), covers_failed=summary.get('failed', 0),
                                              covers_checked=summary.get('total', 0))
                            store.update(library, fields)
                            checkpoint = checkpoint_for(settings, library, key)
                            clear_checkpoint = not covers_only
                            if covers_only and checkpoint.cursor.exists():
                                cursor = json.loads(checkpoint.cursor.read_text())
                                clear_checkpoint = (cursor.get('complete') and
                                    cursor.get('saved_at') == store.entry(library).get('metadata_checkpoint_stamp'))
                            if clear_checkpoint:
                                try:
                                    checkpoint.cursor.unlink(missing_ok=True)
                                    checkpoint.clear()
                                except OSError:
                                    self.event(f'{library.url}: catalog saved, but old checkpoint files could not be removed')
                        with self.lock:
                            self.batch[key] = 'succeeded'
                    except Exception as exc:
                        with self.lock:
                            self.batch[key] = 'cancelled' if isinstance(exc, ScanCancelled) else 'failed'
                            published = covers_only or self.activities.get(key, {}).get('metadata_published', False)
                        if not published:
                            try:
                                # A deadline can race delivery of the metadata-complete event.
                                published = self.publish_metadata(settings, library)
                            except Exception:
                                pass
                        category = 'cancelled' if isinstance(exc, ScanCancelled) else classify_failure(exc)
                        if published and not covers_only:
                            lifecycle.scan_outcome(library, success=True)
                        elif not published:
                            lifecycle.scan_outcome(library, category=category, detail=str(exc), attempt_started=started)
                        with state_lock(lifecycle.gate):
                            present = lifecycle.allowed(library)
                            if present:
                                fields = ({'covers_error': str(exc), 'covers_pending': True,
                                           'covers_in_progress': isinstance(exc, ScanCancelled)} if published else
                                          {'error': str(exc), 'refresh_in_progress': isinstance(exc, ScanCancelled)})
                                if published and not covers_only:
                                    fields['refresh_in_progress'] = False
                                if not published:
                                    fields['failure_category'] = category
                                    from datetime import timedelta
                                    if category == 'temporary':
                                        attempts = store.entry(library).get('temporary_failure_count', 0) + 1
                                        fields['temporary_failure_count'] = attempts
                                        delay = min(settings.catalog_refresh_interval_hours * 3600, 60 * 2 ** min(attempts - 1, 10))
                                        fields['retry_not_before'] = (datetime.now(timezone.utc) + timedelta(seconds=max(delay, getattr(exc, 'retry_after', 0)))).isoformat()
                                store.update(library, fields)
                        message = ('library removed; worker stopped for cache cleanup — ' if not present else
                                   'cover phase interrupted; complete catalog remains searchable — ' if published else
                                   'refresh failed; previous cache retained — ')
                        self.event(f'{library.url}: '+message+str(exc))
        except RuntimeError as exc:
            if not stopped():
                raise
        finally:
            with self.lock:
                self.activities.pop(key, None)
                self.active = None
            lifecycle.purge_pending()

    def run_server(self, settings, server, force, covers_only):
        if server.explicit_id:
            return self.run_one(settings, server, force, covers_only)
        done = set()
        known = {library_key(lib) for _, lib in settings.libraries() if lib.url == server.url}
        try:
            while not self.cancelled():
                candidates = [lib for _, lib in settings.libraries() if lib.url == server.url]
                known.update(library_key(lib) for lib in candidates)
                choices = [lib for lib in candidates if library_key(lib) not in done]
                if not choices:
                    break
                library = choices[0]
                done.add(library_key(library))
                self.run_one(settings, library, force, covers_only)
        finally:
            with self.lock:
                for key in known:
                    if self.batch.get(key) == 'queued':
                        self.batch[key] = 'cancelled' if self.cancelled() else 'skipped'

    def run(self, force, covers_only=False):
        try:
            settings = Settings.load(self.config)
            libraries = settings.libraries()
            with self.lock:
                self.batch = {library_key(lib): 'queued' for _, lib in libraries}
            self.event(f'Starting batch with up to {settings.catalog_scan_concurrency} parallel libraries')
            with ThreadPoolExecutor(max_workers=settings.catalog_scan_concurrency, thread_name_prefix='catalog-worker') as pool:
                bare = {lib.url for _, lib in settings.raw_libraries() if not lib.explicit_id}
                servers = [lib for _, lib in settings.raw_libraries() if not lib.explicit_id or lib.url not in bare]
                futures = [pool.submit(self.run_server, settings, library, force, covers_only)
                           for library in servers]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        self.event(f'Library worker failed: {exc}')
            LibraryState(settings, self.event).purge_pending()
            self.cleanup_if_idle(settings)
        except Exception as exc:
            self.event(f'Catalog refresh could not run: {exc}')
        finally:
            with self.lock:
                self.running = False
            self.event('Batch finished')

    def cleanup_if_idle(self, settings):
        # An exclusive activity lock excludes workers in other app instances too.
        with try_state_lock(Path(settings.catalog_cache_dir) / 'activity') as acquired:
            if acquired:
                with state_lock(Path(settings.catalog_cache_dir) / 'lifecycle'):
                    self.cleanup_covers(CatalogStore(settings.catalog_cache_dir))

    def download_outcome(self, library, success=False, timed_out=False):
        settings = Settings.load(self.config)
        outcome = LibraryState(settings, self.event).outcome(library, success, timed_out)
        if outcome and outcome['retired']:
            self.cleanup_if_idle(settings)
        return outcome

    def restore(self, record_id=None, all_retired=False):
        settings = Settings.load(self.config)
        result = LibraryState(settings, self.event).restore(record_id, all_retired)
        self.maintain_libraries(settings)
        return result

    def clear_retirement_history(self):
        LibraryState(Settings.load(self.config), self.event).clear_history()

    def start(self):
        def schedule():
            while not self.stop.is_set():
                try:
                    settings = Settings.load(self.config)
                    self.maintain_libraries(settings)
                    if settings.catalog_cache_enabled:
                        self.refresh()
                except Exception as exc:
                    self.event(f'Scheduler: {exc}')
                self.stop.wait(60)
        settings = Settings.load(self.config)
        self.maintain_libraries(settings)
        self.cleanup_if_idle(settings)
        self.scheduler = threading.Thread(target=schedule, daemon=True)
        self.scheduler.start()

    def cancelled(self):
        return self.stop.is_set() or self.cancel_scan.is_set()

    def cancel(self):
        self.cancel_scan.set()
        self.paused_until = time.monotonic() + Settings.load(self.config).catalog_refresh_interval_hours * 3600
        self.event('Stopping refresh and interrupting active network requests; saved progress retained')

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join(timeout=2)
        if self.worker:
            self.worker.join(timeout=5)
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
