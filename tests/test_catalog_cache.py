from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import threading
import pytest
from calibre.client import Client, Library, ClientError
from calibre.catalog import full_catalog, ScanCancelled
from calibre.cache import CatalogStore, CatalogManager, library_key, now
from conftest import Session, Response
from calibre.parser import ATOM
import xml.etree.ElementTree as ET

ROOT = b'''<feed xmlns="http://www.w3.org/2005/Atom">
<link rel="search" href="/opds/search/{searchTerms}?library_id=Actual"/>
<entry><title>By Title</title><link type="application/atom+xml" href="/all?library_id=Actual"/></entry>
</feed>'''


def pages(xml):
    root = ET.fromstring(xml)
    ET.SubElement(root, ATOM+'id').text = 'calibre-all:title'
    for link in root.findall(ATOM+'link'):
        link.set('href','/all?library_id=Actual&offset=2')
    first = ET.tostring(root)
    second = ET.Element(ATOM+'feed')
    ET.SubElement(second, ATOM+'id').text = 'calibre-all:title'
    last = ET.SubElement(second, ATOM+'entry')
    ET.SubElement(last, ATOM+'id').text = 'urn:uuid:last'
    ET.SubElement(last, ATOM+'title').text = 'Zones of Thought'
    # This book has no downloadable format, but still belongs in the catalog.
    return first, ET.tostring(second)


def scan_client(xml):
    first,last = pages(xml)
    return Client(Library('http://books.test'), session=Session([Response(ROOT),Response(ROOT),Response(first),Response(last)]))


def test_full_catalog_includes_final_page_and_formatless_book(xml):
    client = scan_client(xml)
    books = full_catalog(client)
    assert len(books) == 3
    assert books[-1].title == 'Zones of Thought' and books[-1].acquisitions == []
    assert all(b.library_id == 'Actual' for b in books)
    assert client.session.calls[-1][0].endswith('&offset=2')


def test_full_catalog_limit_fails_instead_of_publishing_partial(xml):
    with pytest.raises(ClientError, match='page limit'):
        full_catalog(scan_client(xml), max_pages=2)
    with pytest.raises(ClientError, match='book limit'):
        full_catalog(scan_client(xml), max_books=1)


def test_full_catalog_cancelled(xml):
    with pytest.raises(ScanCancelled):
        full_catalog(scan_client(xml), cancelled=lambda: True)


def test_manual_stop_pauses_scheduler_but_manual_refresh_resumes(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text('{"catalog_cache_enabled":true}')
    (tmp_path / 'libraries.txt').write_text('http://books.test\n')
    manager = CatalogManager(config)
    manager.cancel()
    assert not manager.refresh()
    monkeypatch.setattr(manager, 'run', lambda *args: None)
    assert manager.refresh(force=True)
    manager.worker.join(3)
    assert manager.paused_until == 0
    manager.close()


def test_snapshot_replacement_adds_and_removes_books(tmp_path, book):
    store = CatalogStore(tmp_path / 'catalogs')
    library = Library(book.source)
    old = store.publish(library, [book], now())
    replacement = replace(book, identifier='urn:uuid:replacement', title='Replacement title')
    new = store.publish(library, [replacement], now())
    books,entry = store.read(library)
    assert books == [replacement]
    assert entry['book_count'] == 1
    assert new['snapshot'] != old['snapshot']
    assert not (store.directory / old['snapshot']).exists()
    assert (store.directory / new['snapshot']).exists()
    store.publish(library, [], now())
    assert store.read(library)[0] == []


def test_failed_refresh_retains_old_snapshot_and_records_error(tmp_path, book, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text('{"catalog_cache_enabled":true,"catalog_request_delay":0}')
    (tmp_path / 'libraries.txt').write_text(book.source+'\n')
    store = CatalogStore(tmp_path / 'state/catalogs')
    library = Library(book.source)
    old = store.publish(library, [book], now())
    def broken(*a, **k): raise ClientError('Page 2 failed')
    monkeypatch.setattr(CatalogManager, 'run_worker', broken)
    manager = CatalogManager(config)
    assert manager.refresh(force=True)
    manager.worker.join(3)
    books, entry = store.read(library)
    assert books == [book] and entry['snapshot'] == old['snapshot']
    assert entry['error'] == 'Page 2 failed'
    assert (tmp_path / 'libraries.txt').read_text() == book.source+'\n'
    manager.close()


def test_interval_survives_restart_and_new_library_is_due(tmp_path, book):
    store = CatalogStore(tmp_path)
    library = Library(book.source)
    assert store.due(library,24)
    store.publish(library,[book],now())
    assert not CatalogStore(tmp_path).due(library,24)
    store.update(library, {'last_attempt':(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()})
    assert store.due(library,24)
    assert store.due(Library('http://new.test'),24)


def test_failed_pointer_write_keeps_previous_snapshot(tmp_path, book, monkeypatch):
    store = CatalogStore(tmp_path)
    library = Library(book.source)
    old = store.publish(library,[book],now())
    from calibre.cache import atomic_text as original
    def fail(path,text):
        if path == store.index: raise OSError('Disk full')
        original(path,text)
    monkeypatch.setattr('calibre.cache.atomic_text',fail)
    with pytest.raises(OSError): store.publish(library,[],now())
    assert store.read(library)[0] == [book]
    assert len(list(tmp_path.glob(library_key(library)+'_*.json'))) == 1
    assert (tmp_path / old['snapshot']).exists()


def test_scheduler_retries_interrupted_scan_and_skips_recent_complete(tmp_path,book):
    store=CatalogStore(tmp_path)
    library=Library(book.source)
    store.publish(library,[book],now())
    assert not store.due(library,24)
    store.update(library,{'refresh_in_progress':True,'last_attempt':now()})
    assert CatalogStore(tmp_path).due(library,24)


def test_navigation_and_pagination_loop_preserves_completeness(xml):
    first,_=pages(xml)
    session=Session([Response(ROOT),Response(ROOT),Response(first),Response(first)])
    client=Client(Library('http://books.test'),session=session)
    with pytest.raises(ClientError,match='loop'):
        full_catalog(client)


def test_stale_state_flags_age_and_failure():
    from calibre.cache import is_stale
    assert not is_stale({'last_success':now()},24)
    assert is_stale({'last_success':now(),'error':'Offline'},24)
    old=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    assert is_stale({'last_success':old},24)


def test_cache_covers_is_local_and_bounded(tmp_path,book,monkeypatch):
    from test_web import PNG
    from config import Settings
    from calibre.poll_worker import run_library
    config=tmp_path/'config.json'
    config.write_text('{"catalog_cache_enabled":true,"catalog_cache_covers":true,"catalog_request_delay":0}')
    (tmp_path/'libraries.txt').write_text(book.source+'\n')
    class Fake:
        resolved_library_id = ''
        def discover_search(self): pass
        def close(self): pass
    monkeypatch.setattr('calibre.poll_worker.Client',lambda *a,**k:Fake())
    monkeypatch.setattr('calibre.poll_worker.full_catalog',lambda *a,**k:[book])
    monkeypatch.setattr('calibre.poll_worker.fetch_image',lambda *a:(PNG,'image/png'))
    events=[]
    run_library(Settings.load(config),Library(book.source),library_key(Library(book.source)),
                report=lambda message,**fields:events.append((message,fields)))
    assert (tmp_path/'state/catalogs/covers'/ (book.key+'.img')).read_bytes()==PNG
    assert any(fields.get('fetched')==1 for _,fields in events)
