"""Small JSON history, atomically written; a lock protects CLI state mutations."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from .models import Book


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {'books': {}, 'attempts': {}, 'searches': []}

    def save(self):
        atomic_text(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))

    def remember(self, books, query):
        for book in books:
            self.data['books'][book.key] = book.to_dict()
        self.data['searches'].append({'query': query, 'results': [b.key for b in books], 'at': self.now()})
        self.data['searches'] = self.data['searches'][-100:]
        self.save()

    def available(self, book, include_history=False):
        if include_history:
            return book.acquisitions
        return [a for a in book.acquisitions if self.data['attempts'].get(book.key, {}).get(a.url, {}).get('status')
                not in ('failed', 'bad', 'downloaded')]

    def record(self, book, acquisition, status, path=None):
        self.data['books'][book.key] = book.to_dict()
        self.data['attempts'].setdefault(book.key, {})[acquisition.url] = {
            'status': status, 'at': self.now(), 'path': str(path) if path else None}
        self.save()

    def book(self, key):
        return Book.from_dict(self.data['books'][key])

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()


@contextmanager
def state_lock(path):
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
        fcntl.flock(handle, fcntl.LOCK_UN)


def prune_library(path, line):
    """Remove only the timed-out entry; preserve comments and create a backup."""
    path = Path(path)
    original = path.read_text()
    backup = path.with_name(path.name + '.bak')
    atomic_text(backup, original)
    atomic_text(path, ''.join(item for item in original.splitlines(keepends=True)
                             if item.strip() != line.strip()))
