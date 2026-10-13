"""Local HTTP fixtures exercise the actual isolated refresh process, never live libraries."""
from collections import Counter
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
import pytest
from config import Settings
from calibre.cache import CatalogManager, CatalogStore, library_key, now
from calibre.client import Library
from calibre.poll_worker import checkpoint_for
from calibre.state import deduplicate_libraries


@contextmanager
def library_server():
    from test_web import PNG
    calls = Counter()
    entered, release = threading.Event(), threading.Event()
    state = {'stall_page': False, 'stall_cover': False, 'drip': False, 'removed': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            route = self.path
            calls[route] += 1
            root = '<feed xmlns="http://www.w3.org/2005/Atom">'
            if route.startswith('/opds'):
                body = (root+'<link rel="search" href="/opds/search/{searchTerms}?library_id=Actual"/>'
                        '<entry><title>By Title</title><link type="application/atom+xml" href="/all?offset=0"/></entry></feed>').encode()
            elif route == '/all?offset=0':
                body = (root+'<id>calibre-all:title</id><link rel="next" href="/all?offset=30"/>'+
                        self.entry('first')+'</feed>').encode()
            elif route == '/all?offset=30':
                if state['stall_page']:
                    entered.set()
                    if state['drip']:
                        self.send_response(200);self.end_headers()
                        try:
                            while not release.wait(.1):
                                self.wfile.write(b' ');self.wfile.flush()
                        except (BrokenPipeError,ConnectionResetError): pass
                        return
                    release.wait(10)
                body = (root+'<id>calibre-all:title</id>'+
                        ('' if state['removed'] else self.entry('last'))+'</feed>').encode()
            elif route.startswith('/cover/'):
                if state.get('cover_missing'):
                    self.send_error(404);return
                if state['stall_cover']:
                    entered.set();release.wait(10)
                body = PNG
            else:
                self.send_error(404);return
            self.send_response(200)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def entry(self, identifier):
            return ('<entry><id>urn:uuid:'+identifier+'</id><title>Book '+identifier+'</title>'
                    '<author><name>Fixture Author</name></author>'
                    '<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/'+identifier+'"/>'
                    '<link rel="http://opds-spec.org/image/thumbnail" type="image/png" href="/cover/'+identifier+'"/></entry>')
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads = True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}',state,calls,entered,release
    finally:
        release.set();server.shutdown();server.server_close();thread.join(2)


def configure(tmp_path, urls, **options):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'catalog_cache_enabled':True,'catalog_request_delay':0,
                                'timeout':20,'retries':0,**options}))
    (tmp_path/'libraries.txt').write_text('\n'.join(urls)+'\n')
    return path


def finish(manager, timeout=20):
    manager.worker.join(timeout)
    assert not manager.worker.is_alive(), manager.status()


@pytest.mark.parametrize('drip', [False,True])
def test_library_deadline_interrupts_headers_or_drip_and_continues(tmp_path,book,drip):
    with library_server() as stalled, library_server() as healthy:
        url,state,_,entered,_=stalled
        state.update(stall_page=True,drip=drip)
        config=configure(tmp_path,[url,healthy[0]],catalog_library_timeout_seconds=1.5)
        manager=CatalogManager(config)
        store=CatalogStore(Settings.load(config).catalog_cache_dir)
        old=replace(book,source=url)
        store.publish(Library(url),[old],now())
        try:
            start=time.monotonic();assert manager.refresh(force=True)
            assert entered.wait(4);finish(manager)
            assert time.monotonic()-start < 6
            books,entry=store.read(Library(url))
            assert books==[old] and 'deadline' in entry['error']
            assert store.entry(Library(healthy[0]))['book_count']==2
            assert 'deadline' in (store.directory/'refresh.log').read_text()
        finally:manager.close()


def test_cancel_interrupts_request_then_resumes_completed_pages(tmp_path):
    with library_server() as fixture:
        url,state,calls,entered,release=fixture
        state['stall_page']=True
        config=configure(tmp_path,[url])
        manager=CatalogManager(config)
        try:
            assert manager.refresh(force=True);assert entered.wait(4)
            store=CatalogStore(Settings.load(config).catalog_cache_dir)
            assert store.read(Library(url))[0] is None
            start=time.monotonic();manager.cancel();finish(manager,3)
            assert time.monotonic()-start < 2
            assert checkpoint_for(Settings.load(config),Library(url),library_key(Library(url))).load()[0]
            state['stall_page']=False;release.set()
            assert manager.refresh(force=True);finish(manager)
            assert calls['/all?offset=0']==1 # Resume uses the saved next page.
            assert store.entry(Library(url))['book_count']==2
        finally:manager.close()


def test_cover_only_reuses_metadata_and_images_and_cleans_removed_books(tmp_path):
    with library_server() as fixture:
        url,state,calls,_,_=fixture
        config=configure(tmp_path,[url])
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            metadata_calls=sum(value for route,value in calls.items() if not route.startswith('/cover/'))
            data=json.loads(config.read_text());data['catalog_cache_covers']=True
            config.write_text(json.dumps(data))
            store=CatalogStore(Settings.load(config).catalog_cache_dir)
            before=store.entry(Library(url))
            manager.refresh(force=True,covers_only=True);finish(manager)
            assert metadata_calls==sum(value for route,value in calls.items() if not route.startswith('/cover/'))
            assert len(list((store.directory/'covers').glob('*.img')))==2
            assert store.entry(Library(url))['last_success']==before['last_success']
            manager.refresh(force=True,covers_only=True);finish(manager)
            assert calls['/cover/first']==calls['/cover/last']==1
            assert any('reused' in item for item in manager.events)
            store.update(Library(url),{'covers_error':'Previous cover-only failure'})
            state['removed']=True
            manager.refresh(force=True);finish(manager)
            assert store.entry(Library(url))['book_count']==1
            assert store.entry(Library(url))['covers_error'] is None
            assert len(list((store.directory/'covers').glob('*.img')))==1
        finally:manager.close()


def test_cancel_covers_preserves_complete_pages_and_resumes_without_catalog_fetch(tmp_path):
    with library_server() as fixture:
        url,state,calls,entered,release=fixture
        state['stall_cover']=True
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);assert entered.wait(4)
            for _ in range(50):
                active=manager.status()['active']
                if active and active['phase']=='covers':break
                time.sleep(.02)
            assert active['phase']=='covers' and active['total']==2
            store=CatalogStore(Settings.load(config).catalog_cache_dir)
            for _ in range(40):
                if store.entry(Library(url)).get('snapshot'):break
                time.sleep(.025)
            before=store.entry(Library(url))
            assert before['book_count']==2 and before['covers_pending']
            manager.cancel();finish(manager,3)
            assert store.entry(Library(url))['snapshot']==before['snapshot']
            assert not store.entry(Library(url))['error']
            state['stall_cover']=False;release.set()
            manager.refresh(force=True);finish(manager)
            assert calls['/all?offset=0']==calls['/all?offset=30']==1
            assert len(list((tmp_path/'state/catalogs/covers').glob('*.img')))==2
            after=store.entry(Library(url))
            assert after['snapshot']==before['snapshot'] and not after['covers_pending']
        finally:manager.close()


def test_first_catalog_is_searchable_during_cover_fetch_and_cover_deadline(tmp_path):
    from web.service import Service
    with library_server() as fixture:
        url,state,calls,entered,release=fixture
        state['stall_cover']=True
        config=configure(tmp_path,[url],catalog_cache_covers=True,catalog_library_timeout_seconds=4)
        manager=CatalogManager(config);service=Service(config)
        store=CatalogStore(Settings.load(config).catalog_cache_dir)
        try:
            manager.refresh(force=True);assert entered.wait(4)
            for _ in range(50):
                if store.entry(Library(url)).get('snapshot'):break
                time.sleep(.02)
            before=store.entry(Library(url))
            assert before['book_count']==2
            key=service.submit('search',{'author':'Fixture Author'})
            for _ in range(100):
                job=service.snapshot(key)
                if job['status']!='running':break
                time.sleep(.01)
            assert job['status']=='done' and len(job['results'])==2
            assert all(book['cover_url']=='/placeholder-cover.svg' for book in job['results'])
            finish(manager)
            after=store.entry(Library(url))
            assert after['snapshot']==before['snapshot'] and after['error'] is None
            assert after['covers_pending'] and 'deadline' in after['covers_error']
            assert not store.due(Library(url),24)
            state['stall_cover']=False;release.set()
            manager.refresh(force=True);finish(manager)
            assert calls['/all?offset=0']==calls['/all?offset=30']==1
            assert store.entry(Library(url))['snapshot']==before['snapshot']
        finally:
            manager.close();service.executor.shutdown(wait=True);service.catalogs.close()


def test_unavailable_covers_do_not_invalidate_complete_catalog(tmp_path):
    with library_server() as fixture:
        url,state,_,_,_=fixture;state['cover_missing']=True
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            entry=CatalogStore(Settings.load(config).catalog_cache_dir).entry(Library(url))
            assert entry['book_count']==2 and entry['error'] is None and entry['covers_error'] is None
            assert entry['covers_failed']==2 and not entry['covers_pending']
            assert any('catalog searchable now' in item for item in manager.events)
        finally:manager.close()


def test_cover_only_completion_clears_consumed_metadata_checkpoint(tmp_path):
    with library_server() as fixture:
        url,state,calls,entered,release=fixture;state['stall_cover']=True
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        manager=CatalogManager(config);store=CatalogStore(Settings.load(config).catalog_cache_dir)
        try:
            manager.refresh(force=True);assert entered.wait(6)
            for _ in range(50):
                if store.entry(Library(url)).get('snapshot'):break
                time.sleep(.02)
            before=store.entry(Library(url));assert before.get('snapshot')
            manager.cancel();finish(manager,4)
            state['stall_cover']=False;release.set()
            manager.refresh(force=True,covers_only=True);finish(manager)
            assert store.entry(Library(url))['snapshot']==before['snapshot']
            assert not (store.directory/'checkpoints'/library_key(Library(url))).exists()
            manager.refresh(force=True);finish(manager)
            assert calls['/all?offset=0']==calls['/all?offset=30']==2
        finally:manager.close()


def test_dedup_normalizes_endpoints_preserves_selection_and_comments(tmp_path):
    config=configure(tmp_path,[])
    path=tmp_path/'libraries.txt'
    original=('# My servers\n172.1.2.3:8080\nhttp://172.1.2.3:8080/\n'
              'https://172.1.2.3:8080\nhttp://BOOKS.test\nbooks.test:80/\n'
              'http://172.1.2.3:8080?library_id=Other\n'
              '172.1.2.3:8080?library_id=Other\n172.1.2.3:8080?library_id=Another\n\n')
    path.write_text(original)
    settings=Settings.load(config)
    assert deduplicate_libraries(settings)==4
    assert path.with_name('libraries.txt.dedup.bak').read_text()==original
    assert path.read_text().startswith('# My servers\n172.1.2.3:8080\nhttp://BOOKS.test\n')
    assert '?library_id=Other' in path.read_text() and '?library_id=Another' in path.read_text()
    assert deduplicate_libraries(settings)==0


def test_startup_dedup_runs_even_when_catalog_caching_disabled(tmp_path):
    config=configure(tmp_path,['books.test:80','http://books.test/'],catalog_cache_enabled=False)
    manager=CatalogManager(config)
    try:
        manager.start()
        assert (tmp_path/'libraries.txt').read_text()=='books.test:80\n'
        with (tmp_path/'libraries.txt').open('a') as handle:handle.write('books.test:80\n')
        manager.maintain_libraries(Settings.load(config))
        assert (tmp_path/'libraries.txt').read_text()=='books.test:80\n'
    finally:manager.close()


def test_expired_or_incompatible_checkpoints_restart_and_ignore_uncommitted_pages(tmp_path,book):
    from datetime import datetime,timezone,timedelta
    from calibre.checkpoint import Checkpoint
    checkpoint=Checkpoint(tmp_path/'checkpoint',{'source':'fixture'},72)
    state={'page_count':1,'complete':False,'url':'/next','seen':['/first'],
           'acquisition_feed':True,'resolved_library_id':'Actual'}
    checkpoint.save(state,[book])
    (checkpoint.directory/'page_000001.json').write_text('invalid uncommitted page')
    saved,books=checkpoint.load()
    assert saved['page_count']==1 and list(books.values())==[book]
    saved['saved_at']=(datetime.now(timezone.utc)-timedelta(hours=73)).isoformat()
    checkpoint.cursor.write_text(json.dumps(saved))
    assert checkpoint.load()==(None,{})
    assert not checkpoint.directory.exists()
    checkpoint.save(state,[book])
    assert Checkpoint(checkpoint.directory,{'source':'different'},72).load()==(None,{})


def test_cleanup_keeps_old_snapshot_and_pending_checkpoint_covers(tmp_path,book):
    from test_web import PNG
    config=configure(tmp_path,[])
    settings=Settings.load(config)
    manager=CatalogManager(config)
    store=CatalogStore(settings.catalog_cache_dir)
    library=Library(book.source)
    pending=replace(book,identifier='pending')
    orphan=replace(book,identifier='orphan')
    store.publish(library,[book],now())
    checkpoint=checkpoint_for(settings,Library('http://pending.test'),library_key(Library('http://pending.test')))
    checkpoint.save({'page_count':1,'complete':False},[pending])
    covers=store.directory/'covers';covers.mkdir()
    for item in (book,pending,orphan):
        (covers/(item.key+'.img')).write_bytes(PNG)
        (covers/(item.key+'.json')).write_text('{}')
    try:
        manager.cleanup_covers(store)
        assert (covers/(book.key+'.img')).exists()
        assert (covers/(pending.key+'.img')).exists()
        assert not (covers/(orphan.key+'.img')).exists()
        assert not (covers/(orphan.key+'.json')).exists()
    finally:manager.close()
