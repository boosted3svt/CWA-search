"""Isolated network worker: the parent can terminate stalled requests safely."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from config import Settings
from .client import Client, Library, classify_failure
from .catalog import full_catalog, catalog_count
from .checkpoint import Checkpoint
from .images import fetch_image, image_type
from .state import atomic_text


def emit(message, **fields):
    print(json.dumps({'message': message, **fields}), flush=True)


def checkpoint_for(settings, library, key):
    identity = {'source': library.url, 'library_id': library.library_id,
                'explicit_id': library.explicit_id, 'opds_path': settings.opds_path,
                'max_pages': settings.catalog_max_pages, 'max_books': settings.catalog_max_books}
    return Checkpoint(Path(settings.catalog_cache_dir) / 'checkpoints' / key,
                      identity, settings.catalog_checkpoint_max_age_hours)


def cover_available(directory, book):
    path = directory / (book.key + '.img')
    try:
        image_type(path.read_bytes())
        metadata = path.with_suffix('.json')
        return not metadata.exists() or json.loads(metadata.read_text())['url'] == book.cover_url
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def save_cover(directory, book, body):
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=book.key + '-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / (book.key + '.img'))
        atomic_text(directory / (book.key + '.json'), json.dumps({'url': book.cover_url}))
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_library(settings, library, key, covers_only=False, report=emit):
    from .cache import CatalogStore
    checkpoint = checkpoint_for(settings, library, key)
    client = Client(library, timeout=settings.timeout, retries=settings.retries,
                    auto_discover=True, opds_path=settings.opds_path)
    try:
        reuse = False
        if covers_only:
            books, entry = CatalogStore(settings.catalog_cache_dir).read(library)
            if books is None:
                raise ValueError('No complete catalog yet; refresh metadata first')
            report('Using saved metadata; no catalog requests', phase='covers', count=0)
        else:
            books, entry = CatalogStore(settings.catalog_cache_dir).read(library)
            reuse = False
            pending = {}
            if checkpoint.cursor.exists():
                try:
                    pending = json.loads(checkpoint.cursor.read_text())
                except (OSError, ValueError):
                    pending = {}
            resume_complete = pending.get('complete') and entry.get('covers_pending')
            if books is not None and not resume_complete:
                report('Checking remote book count against saved metadata', phase='metadata', count=0)
                remote_count = catalog_count(client, pause=lambda: time.sleep(settings.catalog_request_delay))
                reuse = remote_count == len(books) if remote_count is not None else False
                if reuse:
                    report(f'Book count unchanged: {remote_count}; reusing saved metadata',
                           phase='metadata-unchanged', count=remote_count)
                else:
                    if remote_count is not None and pending.get('complete'):
                        checkpoint.clear()
                    report(f'Book count {remote_count if remote_count is not None else "unavailable"}; saved {len(books)}; scanning metadata', phase='metadata', count=0)
            if not reuse:
                report('Reading catalog pages (resuming saved pages if available)', phase='metadata', count=0)
                books = full_catalog(client, settings.catalog_max_pages, settings.catalog_max_books,
                                     pause=lambda: time.sleep(settings.catalog_request_delay),
                                     checkpoint=checkpoint,
                                     progress=lambda count: report(f'{count} books read', phase='metadata', count=count))
                report(f'Metadata complete: {len(books)} books', phase='metadata-complete', count=len(books))
        if settings.catalog_cache_covers or covers_only:
            directory = Path(settings.catalog_cache_dir) / 'covers'
            candidates = [book for book in books if book.cover_url]
            total = len(candidates)
            fetched = reused = failed = 0
            # Only covers belonging to these book records count; unrelated files
            # cannot conceal missing covers, and books without image links need none.
            expected = {book.key for book in candidates}
            available = {path.stem for path in directory.glob('*.img')
                         if path.stem in expected and path.is_file() and path.stat().st_size > 0} if reuse else set()
            if reuse and all(book.key in available for book in candidates):
                report(f'All {total} expected covers already saved; skipping cover downloads',
                       phase='covers-complete', count=total, total=total, failed=0, fetched=0, reused=total)
                report('Worker finished', phase='complete', success=True)
                return
            report(f'Fetching missing covers: 0/{total}', phase='covers', count=0, total=total)
            last_report = time.monotonic()
            for index, book in enumerate(candidates, 1):
                if cover_available(directory, book):
                    reused += 1
                    metadata = directory / (book.key + '.json')
                    if not metadata.exists():
                        atomic_text(metadata, json.dumps({'url': book.cover_url}))
                else:
                    time.sleep(settings.catalog_request_delay)
                    try:
                        body, _ = fetch_image(client, book.cover_url)
                        save_cover(directory, book, body)
                        fetched += 1
                    except Exception:
                        failed += 1
                if index % 20 == 0 or index == total or time.monotonic() - last_report >= 5:
                    report(f'Covers: {index}/{total} checked; {fetched} fetched, {reused} reused, {failed} unavailable',
                           phase='covers', count=index, total=total, fetched=fetched, reused=reused, failed=failed)
                    last_report = time.monotonic()
            report(f'Covers complete: {fetched} fetched, {reused} reused, {failed} unavailable',
                   phase='covers-complete', count=total, total=total, failed=failed,
                   fetched=fetched, reused=reused)
        report('Worker finished', phase='complete', success=True)
    finally:
        client.close()


def main():
    try:
        request = json.load(sys.stdin)
        run_library(Settings(**request['settings']), Library(**request['library']),
                    request['key'], request['covers_only'])
    except Exception as exc:
        emit(str(exc), phase='error', error=True, category=classify_failure(exc), retry_after=getattr(exc, 'retry_after', 0))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
