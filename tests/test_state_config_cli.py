import json
from argparse import Namespace
import pytest
from config import Settings
from calibre.client import LibraryTimeout, ClientError
from calibre.state import History, prune_library
from main import search, choose_format


def test_config_and_library_ids(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'library_id': 'default'}))
    settings = Settings.load(path)
    assert settings.library('127.0.0.1:8080').library_id == 'default'
    assert settings.library('http://books.test?library_id=Other').library_id == 'Other'
    assert settings.download_dir == str(tmp_path / 'downloads')
    with pytest.raises(ClientError):
        settings.library('http://user:password@books.test')


def test_history_format_fallback_and_bad(tmp_path, book):
    settings = Settings()
    history = History(tmp_path / 'history.json')
    history.remember([book], {'author': 'Emily Rath'})
    assert choose_format(book, settings, history).format == 'mobi'
    history.record(book, book.acquisitions[1], 'failed')
    assert choose_format(book, settings, history).format == 'epub'
    history.record(book, book.acquisitions[0], 'bad')
    assert not history.available(book)
    assert choose_format(book, settings, history, include_history=True).format == 'mobi'
    assert History(history.path).book(book.key) == book


def test_ids_scoped_to_source_and_library(book):
    from dataclasses import replace
    assert book.key != replace(book, source='http://other.test').key
    assert book.key != replace(book, library_id='Other').key


def test_prune_preserves_other_entries_and_backup(tmp_path):
    path = tmp_path / 'libraries.txt'
    original = '# comment\nbooks.test:80\nother.test:80\n'
    path.write_text(original)
    prune_library(path, 'books.test:80')
    assert path.read_text() == '# comment\nother.test:80\n'
    assert path.with_name('libraries.txt.bak').read_text() == original


def test_search_skips_failed_match_and_stops_at_next(monkeypatch, tmp_path, book):
    from dataclasses import replace
    entries = tmp_path / 'libraries.txt'
    entries.write_text('first.test:80\nsecond.test:80\nthird.test:80\n')
    settings = Settings(libraries_file=str(entries))
    history = History(tmp_path / 'history.json')
    first = replace(book, source='http://first.test:80', library_id='')
    for acquisition in first.acquisitions:
        history.record(first, acquisition, 'bad')
    calls = []
    class Fake:
        truncated = False
        def __init__(self, library): self.library = library
        def search(self, term):
            calls.append(self.library.url)
            return [replace(book, source=self.library.url, library_id='')]
        def close(self): pass
    monkeypatch.setattr('main.make_client', lambda settings, library: Fake(library))
    args = Namespace(author='Emily Rath', title=None, exact=False, include_history=False,
                     all_libraries=False, keep_timeouts=False, json=True, select=False)
    assert search(args, settings, history) == 0
    assert calls == ['http://first.test:80', 'http://second.test:80']
    assert len(history.data['searches'][-1]['results']) == 1


@pytest.mark.parametrize('error,removed', [(LibraryTimeout('timeout'), True), (ClientError('HTTP 401'), False)])
def test_only_timeouts_pruned(monkeypatch, tmp_path, error, removed):
    entries = tmp_path / 'libraries.txt'
    entries.write_text('books.test:80\n')
    settings = Settings(libraries_file=str(entries))
    class Fake:
        def search(self, term): raise error
        def close(self): pass
    monkeypatch.setattr('main.make_client', lambda *args: Fake())
    args = Namespace(author='Emily Rath', title=None, exact=False, include_history=False,
                     all_libraries=False, keep_timeouts=False, json=True, select=False)
    assert search(args, settings, History(tmp_path / 'history.json')) == 1
    assert (not entries.read_text()) == removed


def test_empty_search_preserves_library(monkeypatch, tmp_path):
    path = tmp_path / 'libraries.txt'
    path.write_text('books.test:80\n')
    class Fake:
        truncated = False
        def search(self, term): return []
        def close(self): pass
    monkeypatch.setattr('main.make_client', lambda *args: Fake())
    args = Namespace(author='Emily Rath', title=None, exact=False, include_history=False,
                     all_libraries=False, keep_timeouts=False, json=True, select=False)
    assert search(args, Settings(libraries_file=str(path)), History(tmp_path / 'history.json')) == 0
    assert path.read_text() == 'books.test:80\n'


def test_unsupported_match_continues_next_library(monkeypatch, tmp_path, book):
    from dataclasses import replace
    path = tmp_path / 'libraries.txt'
    path.write_text('first.test:80\nsecond.test:80\n')
    calls = []
    class Fake:
        truncated = False
        def __init__(self, library): self.library = library
        def search(self, term):
            calls.append(self.library.url)
            acquisitions = ([replace(book.acquisitions[0], format='txt')]
                            if len(calls) == 1 else book.acquisitions)
            return [replace(book, source=self.library.url, acquisitions=acquisitions)]
        def close(self): pass
    monkeypatch.setattr('main.make_client', lambda settings, library: Fake(library))
    args = Namespace(author='Emily Rath', title=None, exact=False, include_history=False,
                     all_libraries=False, keep_timeouts=False, json=True, select=False)
    assert search(args, Settings(libraries_file=str(path)), History(tmp_path / 'history.json')) == 0
    assert len(calls) == 2
