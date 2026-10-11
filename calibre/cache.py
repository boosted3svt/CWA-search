"""Dated full snapshots, atomic active pointers, and independent daily refresh."""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from config import Settings
from .client import Client
from .catalog import full_catalog, ScanCancelled
from .models import Book
from .state import atomic_text, state_lock
from .images import fetch_image


def now():
    return datetime.now(timezone.utc).isoformat()


def is_stale(entry, hours):
    last = entry.get('last_success')
    return (not last or bool(entry.get('error')) or
            (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= hours * 3600)


def library_key(library):
    # Shared defaults can change; explicit per-line selection defines identity.
    return hashlib.sha256((library.url + '\n' +
                           (library.library_id if library.explicit_id else '')).encode()).hexdigest()[:24]


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

    def publish(self, library, books, started):
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
                     'last_attempt': started, 'error': None, 'refresh_in_progress': False}
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

    def event(self, message):
        with self.lock:
            self.events.append(message)
            self.events = self.events[-200:]

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
                            'stale': is_stale(entry, settings.catalog_refresh_interval_hours)})
        with self.lock:
            running, events = self.running, list(self.events)
        return {'enabled': settings.catalog_cache_enabled, 'running': running, 'events': events,
                'libraries': entries, 'interval_hours': settings.catalog_refresh_interval_hours}

    def refresh(self, force=False):
        settings = Settings.load(self.config)
        if not settings.catalog_cache_enabled:
            raise ValueError('Enable catalog_cache_enabled in config.json first')
        if not force and time.monotonic() < self.paused_until:
            return False
        libraries = settings.libraries()
        if not libraries:
            raise ValueError('No libraries configured')
        store = CatalogStore(settings.catalog_cache_dir)
        if not force and not any(store.due(lib, settings.catalog_refresh_interval_hours) for _, lib in libraries):
            return False
        with self.lock:
            if self.running or self.stop.is_set():
                return False
            self.running = True
            if force:
                self.paused_until = 0
            self.events = []
            self.cancel_scan.clear()
            self.worker = threading.Thread(target=self.run, args=(force,), daemon=True)
            self.worker.start()
        return True

    def run(self, force):
        try:
            settings = Settings.load(self.config)
            store = CatalogStore(settings.catalog_cache_dir)
            for _, library in settings.libraries():
                if self.cancelled():
                    break
                if not force and not store.due(library, settings.catalog_refresh_interval_hours):
                    continue
                # Cross-process refresh lock prevents overlapping scans of the same
                # store; it does not lock history or block local searches.
                with state_lock(Path(settings.catalog_cache_dir) / 'refresh'):
                    if not force and not store.due(library, settings.catalog_refresh_interval_hours):
                        continue
                    started = now()
                    store.update(library, {'last_attempt': started, 'refresh_in_progress': True})
                    self.event(f'Refreshing {library.url}')
                    client = Client(library, timeout=settings.timeout, retries=settings.retries,
                                    auto_discover=True, opds_path=settings.opds_path)
                    try:
                        def pause():
                            if self.stop.wait(settings.catalog_request_delay) or self.cancel_scan.is_set():
                                raise ScanCancelled('Refresh stopped; old snapshot retained')
                        books = full_catalog(client, settings.catalog_max_pages, settings.catalog_max_books,
                                             pause=pause, cancelled=self.cancelled,
                                             progress=lambda count: self.event(f'{library.url}: {count} books read'))
                        if self.cancelled():
                            raise ScanCancelled('Refresh stopped; old snapshot retained')
                        if settings.catalog_cache_covers:
                            cover_dir = Path(settings.catalog_cache_dir) / 'covers'
                            cover_dir.mkdir(parents=True, exist_ok=True)
                            for book in books:
                                if self.cancelled():
                                    raise ScanCancelled('Refresh stopped; old snapshot retained')
                                if not book.cover_url:
                                    continue
                                pause()
                                try:
                                    body, _ = fetch_image(client, book.cover_url)
                                    fd, temporary = tempfile.mkstemp(dir=cover_dir)
                                    try:
                                        with os.fdopen(fd, 'wb') as handle:
                                            handle.write(body)
                                            handle.flush()
                                            os.fsync(handle.fileno())
                                        os.replace(temporary, cover_dir / (book.key + '.img'))
                                    finally:
                                        Path(temporary).unlink(missing_ok=True)
                                except Exception:
                                    self.event(f'{library.url}: cover unavailable for {book.title}')
                        store.publish(library, books, started)
                        self.event(f'{library.url}: published {len(books)} books')
                    except Exception as exc:
                        store.update(library, {'error': str(exc), 'refresh_in_progress': False})
                        self.event(f'{library.url}: refresh failed; previous cache retained — {exc}')
                    finally:
                        client.close()
        except Exception as exc:
            self.event(f'Catalog refresh could not run: {exc}')
        finally:
            with self.lock:
                self.running = False

    def start(self):
        def schedule():
            while not self.stop.is_set():
                try:
                    settings = Settings.load(self.config)
                    if settings.catalog_cache_enabled:
                        self.refresh()
                except Exception as exc:
                    self.event(f'Scheduler: {exc}')
                self.stop.wait(60)
        self.scheduler = threading.Thread(target=schedule, daemon=True)
        self.scheduler.start()

    def cancelled(self):
        return self.stop.is_set() or self.cancel_scan.is_set()

    def cancel(self):
        self.cancel_scan.set()
        self.paused_until = time.monotonic() + Settings.load(self.config).catalog_refresh_interval_hours * 3600
        self.event('Stopping catalog refresh after the current request; old snapshots retained')

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join(timeout=2)
        if self.worker:
            self.worker.join(timeout=2)
