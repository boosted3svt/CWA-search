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
from .client import ClientError
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
        if entry.get('refresh_in_progress'):
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
        entries = []
        for _, library in libraries:
            entry = manifest.get(library_key(library), {})
            entries.append({'source': library.url, 'book_count': entry.get('book_count', 0),
                            'last_success': entry.get('last_success'), 'last_attempt': entry.get('last_attempt'),
                            'error': entry.get('error'), 'cached': bool(entry.get('snapshot')),
                            'covers_error': entry.get('covers_error'),
                            'stale': is_stale(entry, settings.catalog_refresh_interval_hours)})
        lifecycle = LibraryState(settings)
        counters = lifecycle.load()['active']
        for item, (_, library) in zip(entries, libraries):
            item['download_timeout_count'] = counters.get(library_key(library), {}).get('timeout_count', 0)
        with self.lock:
            running, events = self.running, list(self.events)
            activities = [dict(item) for item in self.activities.values()]
            if self.active:
                activities = [dict(self.active)] + [item for item in activities if item['source'] != self.active['source']]
        for item in activities:
            item['elapsed_seconds'] = round(time.monotonic() - item.pop('started_monotonic'), 1)
        return {'enabled': settings.catalog_cache_enabled, 'running': running, 'events': events,
                'libraries': entries, 'interval_hours': settings.catalog_refresh_interval_hours,
                'active': activities[0] if activities else None, 'active_libraries': activities,
                'concurrency': settings.catalog_scan_concurrency,
                'library_timeout_seconds': settings.catalog_library_timeout_seconds,
                'covers_enabled': settings.catalog_cache_covers,
                'retired': lifecycle.load()['history'],
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
                           'worker_id': current_worker_id(),
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
        success, error = False, None
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
                    raise ClientError(f'Library deadline reached after {elapsed:.1f}s; saved progress retained; moving on')
                try:
                    item = messages.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive() and messages.empty():
                        break
                else:
                    self.event(f'{library.url}: {item["message"]}')
                    with self.lock:
                        self.activities[key].update({k: v for k, v in item.items()
                                            if k not in ('message', 'error', 'success')})
                        self.activities[key]['last_activity'] = now()
                    success = success or item.get('success', False)
                    if item.get('error'):
                        error = item['message']
                if time.monotonic() - heartbeat >= 30:
                    with self.lock:
                        activity = dict(self.activities[key])
                    self.event(f'{library.url}: still active ({activity["phase"]}, '
                               f'{elapsed:.0f}s elapsed, deadline {settings.catalog_library_timeout_seconds:g}s)')
                    heartbeat = time.monotonic()
            if process.returncode != 0 or not success:
                raise ClientError(error or 'Catalog worker exited before completing')
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

    def run_one(self, settings, library, force, covers_only):
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
                    if covers_only and not store.entry(library).get('snapshot'):
                        self.event(f'{library.url}: skipped cover-only refresh; no complete catalog yet')
                        return
                    if not force and not covers_only and not store.due(library, settings.catalog_refresh_interval_hours):
                        return
                    started = now()
                    with state_lock(lifecycle.gate):
                        if not lifecycle.allowed(library):
                            return
                        if not covers_only:
                            store.update(library, {'last_attempt': started, 'refresh_in_progress': True})
                    self.event(f'{"Fetching missing covers for" if covers_only else "Refreshing"} {library.url}')
                    try:
                        self.run_worker(settings, library, covers_only)
                        if self.cancelled():
                            raise ScanCancelled('Refresh stopped; progress retained for resume')
                        with state_lock(lifecycle.gate):
                            if not lifecycle.allowed(library):
                                self.event(f'{library.url}: removed; discarding worker completion')
                                return
                            if covers_only:
                                store.update(library, {'covers_last_success': now(), 'covers_error': None})
                            else:
                                checkpoint = checkpoint_for(settings, library, key)
                                state, books = checkpoint.load()
                                if not state or not state['complete']:
                                    raise ClientError('Worker did not save a complete catalog; old snapshot retained')
                                with self.lock:
                                    activity = self.activities.get(key) or self.active
                                    if activity:
                                        activity.update(phase='saving', last_activity=now())
                                self.event(f'{library.url}: saving complete JSON snapshot ({len(books)} books)')
                                covers_fields = ({'covers_last_success': now(), 'covers_error': None}
                                                 if settings.catalog_cache_covers else {})
                                store.publish(library, list(books.values()), started, covers_fields)
                                try:
                                    checkpoint.cursor.unlink(missing_ok=True)
                                    checkpoint.clear()
                                except OSError:
                                    self.event(f'{library.url}: catalog saved, but old checkpoint files could not be removed')
                                self.event(f'{library.url}: published {len(books)} books')
                    except Exception as exc:
                        with state_lock(lifecycle.gate):
                            if lifecycle.allowed(library):
                                fields = ({'covers_error': str(exc)} if covers_only else
                                          {'error': str(exc), 'refresh_in_progress': isinstance(exc, ScanCancelled)})
                                store.update(library, fields)
                        self.event(f'{library.url}: refresh failed; previous cache retained — {exc}')
        except RuntimeError as exc:
            if not stopped():
                raise
        finally:
            with self.lock:
                self.activities.pop(key, None)
                self.active = None
            lifecycle.purge_pending()

    def run(self, force, covers_only=False):
        try:
            settings = Settings.load(self.config)
            libraries = settings.libraries()
            self.event(f'Starting batch with up to {settings.catalog_scan_concurrency} parallel libraries')
            with ThreadPoolExecutor(max_workers=settings.catalog_scan_concurrency, thread_name_prefix='catalog-worker') as pool:
                futures = [pool.submit(self.run_one, settings, library, force, covers_only)
                           for _, library in libraries]
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
