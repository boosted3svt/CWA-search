"""One background operation at a time; shared CLI history remains locked."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import threading
import uuid
from config import Settings
from calibre.state import History, state_lock, atomic_text, prune_library
from calibre.client import ClientError, LibraryTimeout
from calibre.parser import FeedError
from calibre.matcher import matches
from calibre.downloader import download, SUPPORTED_FORMATS
from main import make_client, choose_format, result_library


class BusyError(ValueError):
    pass


class Service:
    def __init__(self, config):
        self.config = Path(config).resolve()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None
        self.cover_slots = threading.BoundedSemaphore(2)

    def settings(self):
        settings = Settings.load(self.config)
        try:
            count = len(settings.libraries())
        except FileNotFoundError:
            count = 0
        with self.lock:
            active = self.active
        return {'download_dir': settings.download_dir, 'library_count': count, 'active_job': active,
                'prune_timeouts': settings.prune_timeouts}

    def submit(self, action, data):
        if action not in ('search', 'download', 'mark-bad'):
            raise ValueError('Unknown operation')
        if action == 'search' and not (data.get('author', '').strip() or data.get('title', '').strip()):
            raise ValueError('Enter a title or author')
        with self.lock:
            if self.active:
                raise BusyError('Another operation is running. Wait for it to finish.')
            key = uuid.uuid4().hex
            self.jobs[key] = {'id': key, 'action': action, 'status': 'running', 'events': [], 'results': [],
                              'include_history': bool(data.get('include_history', False)), 'cancelled': False}
            if action == 'search':
                self.jobs[key]['query'] = dict(data)
            self.active = key
            if len(self.jobs) > 32:
                del self.jobs[next(iter(self.jobs))]
        self.executor.submit(self.run, key, action, dict(data))
        return key

    def event(self, key, message):
        with self.lock:
            self.jobs[key]['events'].append(message)
            self.jobs[key]['events'] = self.jobs[key]['events'][-300:]

    def snapshot(self, key):
        with self.lock:
            return json.loads(json.dumps(self.jobs[key]))

    def cancel(self, key):
        with self.lock:
            job = self.jobs.get(key)
            if not job or job['status'] != 'running' or job['action'] != 'search':
                raise ValueError('Only an active search can be stopped')
            job['cancelled'] = True
        self.event(key, 'Stopping after the current library finishes…')

    def run(self, key, action, data):
        try:
            settings = Settings.load(self.config)
            self.event(key, 'Waiting for access to search and download history…')
            with state_lock(settings.history_file):
                history = History(settings.history_file)
                if action == 'search':
                    result = self.search(key, data, settings, history)
                else:
                    book = history.book(data['id'])
                    with self.lock:
                        self.jobs[key]['selection'] = {'id': book.key, 'title': book.title,
                                                      'source': book.source, 'format': data.get('format')}
                    acquisition = choose_format(book, settings, history, data.get('format'),
                                                data.get('include_history', False))
                    if action == 'mark-bad':
                        history.record(book, acquisition, 'bad')
                        result = {'message': 'Marked this format bad. Future searches skip it.'}
                    else:
                        client = make_client(settings, result_library(book, settings.libraries()))
                        self.event(key, f'Downloading {book.title} ({acquisition.format.upper()})…')
                        with self.lock:
                            self.jobs[key].update(file_saved=False)
                        try:
                            path = download(client, book, acquisition, settings.download_dir,
                                            settings.max_download_bytes,
                                            on_warning=lambda message: self.event(key, message))
                        except (ClientError, OSError):
                            history.record(book, acquisition, 'failed')
                            raise
                        finally:
                            client.close()
                        with self.lock:
                            self.jobs[key].update(file_saved=True, saved_path=str(path))
                        history.record(book, acquisition, 'downloaded', path)
                        result = {'message': f'Saved on the server: {path}'}
            with self.lock:
                self.jobs[key].update(result, status='done')
        except Exception as exc:
            # Requests errors are translated by the client; never expose tracebacks.
            message = 'Unknown result; search again.' if isinstance(exc, KeyError) else str(exc)
            with self.lock:
                self.jobs[key].update(status='error', error=message)
                if action == 'download':
                    saved = self.jobs[key].get('file_saved', False)
                    self.jobs[key]['user_message'] = (
                        'The book was saved, but a later step failed. Check the saved path before retrying.' if saved else
                        'Download failed. No new ebook was saved; existing files were left unchanged. '
                        'You can acknowledge this failure and look for a copy on another server.')
        finally:
            with self.lock:
                self.active = None

    def search(self, key, data, settings, history):
        title, author = data.get('title', '').strip(), data.get('author', '').strip()
        term = ' '.join(value for value in (author, title) if value)
        libraries = settings.libraries()
        if not libraries:
            raise ValueError('Add library URLs to libraries.txt on the server first.')
        results, failures, successes = [], 0, 0
        for index, (line, library) in enumerate(libraries, 1):
            if self.snapshot(key)['cancelled']:
                break
            if library.url == data.get('skip_source'):
                self.event(key, f'Skipping {library.url} for this search after the failed download')
                continue
            self.event(key, f'[{index}/{len(libraries)}] Searching {library.url}')
            client = make_client(settings, library)
            try:
                books = client.search(term)
                successes += 1
                selected = [b for b in books if matches(b, title, author, data.get('exact', False))
                            and any(a.format in SUPPORTED_FORMATS for a in
                                    history.available(b, data.get('include_history', False)))]
                if client.discovery_message:
                    self.event(key, client.discovery_message)
                self.event(key, f'{len(books)} catalog results; {len(selected)} eligible matches')
                if books and not selected:
                    for book in books[:5]:
                        reason = ('title/author mismatch' if not matches(book, title, author, data.get('exact', False))
                                  else 'no untried supported format (check download history)')
                        self.event(key, f'Excluded: {book.title} — {", ".join(book.authors)}: {reason}')
                if client.truncated:
                    self.event(key, 'Configured page/result limit reached.')
                known = {b.key for b in results}
                results.extend(b for b in selected if b.key not in known)
                if selected and not data.get('all_libraries', False):
                    break
            except LibraryTimeout:
                failures += 1
                self.event(key, f'Timed out: {library.url}')
                if (settings.prune_timeouts and not data.get('keep_timeouts', False)
                        and not self.snapshot(key)['cancelled']):
                    prune_library(settings.libraries_file, line)
                    self.event(key, 'Removed timed-out library; previous list saved to .bak')
            except (ClientError, FeedError) as exc:
                failures += 1
                self.event(key, f'Search failed: {exc}')
            finally:
                client.close()
        history.remember(results, {'author': author, 'title': title})
        cancelled = self.snapshot(key)['cancelled']
        if not successes and not cancelled and failures:
            raise ValueError('No library could be searched successfully. See search activity.')
        output = []
        for book in results:
            item = book.to_dict()
            available = history.available(book, data.get('include_history', False))
            item['formats'] = list(dict.fromkeys(a.format for a in available if a.format in SUPPORTED_FORMATS))
            item['preferred_format'] = choose_format(book, settings, history,
                                                    include_history=data.get('include_history', False)).format
            # Never send arbitrary acquisition URLs to the browser.
            item.pop('acquisitions', None)
            item['cover_url'] = f'/api/cover/{book.key}' if book.cover_url else None
            output.append(item)
        if not successes and not failures and not cancelled:
            return {'results': [], 'failed_libraries': 0,
                    'message': 'No other library is configured. Add another server or retry a different format.'}
        return {'results': output, 'failed_libraries': failures,
                'message': ('Search stopped. ' if cancelled else '') +
                           f'{len(output)} eligible results. {failures} library failures.'}

    def save_folder(self, folder):
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError('Enter a download folder on the server')
        with self.lock:
            if self.active:
                raise BusyError('Wait for the current operation before changing settings.')
            settings = Settings.load(self.config)
            with state_lock(settings.history_file):
                path = Path(folder).expanduser()
                if not path.is_absolute():
                    path = self.config.parent / path
                path = path.resolve()
                path.mkdir(parents=True, exist_ok=True)
                # Probe write access without modifying an existing file.
                import tempfile
                with tempfile.TemporaryFile(dir=path):
                    pass
                data = json.loads(self.config.read_text()) if self.config.exists() else {}
                data['download_dir'] = str(path)
                atomic_text(self.config, json.dumps(data, indent=2) + '\n')
        return self.settings()

    def cover(self, key):
        settings = Settings.load(self.config)
        # History updates use atomic replacement, so an unlocked read is consistent.
        book = History(settings.history_file).book(key)
        if not book.cover_url:
            raise ValueError('No cover available')
        client = make_client(settings, result_library(book, settings.libraries()))
        response = None
        with self.cover_slots:
            try:
                response, _ = client.get(book.cover_url)
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise ValueError('Cover exceeds size limit')
                body = bytes(body)
                if body.startswith(b'\xff\xd8\xff'):
                    media = 'image/jpeg'
                elif body.startswith(b'\x89PNG\r\n\x1a\n'):
                    media = 'image/png'
                elif body[:6] in (b'GIF87a', b'GIF89a'):
                    media = 'image/gif'
                elif body[:4] == b'RIFF' and body[8:12] == b'WEBP':
                    media = 'image/webp'
                else:
                    raise ValueError('Unsupported cover image')
                return body, media
            finally:
                if response is not None:
                    response.close()
                client.close()
