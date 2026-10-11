"""Page journals are resumable work, never searchable catalogs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from .models import Book
from .state import atomic_text


class Checkpoint:
    def __init__(self, directory, identity, max_age_hours=72):
        self.directory = Path(directory)
        self.cursor = self.directory / 'cursor.json'
        self.identity = identity
        self.max_age_hours = max_age_hours

    def clear(self):
        if self.directory.exists():
            shutil.rmtree(self.directory)

    def load(self):
        if not self.cursor.exists():
            return None, {}
        try:
            state = json.loads(self.cursor.read_text())
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state['saved_at'])).total_seconds()
            if state['identity'] != self.identity or age > self.max_age_hours * 3600:
                self.clear()
                return None, {}
            books = {}
            for index in range(state['page_count']):
                page = json.loads((self.directory / f'page_{index:06d}.json').read_text())
                for item in page:
                    book = Book.from_dict(item)
                    books[book.key] = book
            return state, books
        except (OSError, ValueError, KeyError, TypeError):
            self.clear()
            return None, {}

    def save(self, state, page):
        index = state['page_count'] - 1
        atomic_text(self.directory / f'page_{index:06d}.json',
                    json.dumps([b.to_dict() for b in page], ensure_ascii=False))
        atomic_text(self.cursor, json.dumps({**state, 'identity': self.identity,
                                           'saved_at': datetime.now(timezone.utc).isoformat()}))
