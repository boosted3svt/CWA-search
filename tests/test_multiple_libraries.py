"""A single configured server enumerates independently scoped Calibre libraries."""
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit,parse_qs
import pytest
from config import Settings
from calibre.cache import CatalogManager,CatalogStore,library_key
from calibre.library_state import LibraryState
from test_refresh_runtime import configure,finish


@contextmanager
def multi_server():
    from test_web import PNG
    calls=Counter()
    state={'ids':['A','B'],'empty':set(),'broken':set()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            calls[self.path]+=1
            parsed=urlsplit(self.path)
            identifier=parse_qs(parsed.query).get('library_id',['A'])[0]
            if identifier in state['broken']:
                self.send_error(401);return
            if parsed.path=='/opds':
                entries=''.join('<entry><title>Library: '+state.get('names',{}).get(i,'Same name')+'</title><id>calibre-library:'+i+'</id><link type="application/atom+xml" href="/opds?library_id='+i+'"/></entry>' for i in state['ids'])
                body=('<feed xmlns="http://www.w3.org/2005/Atom"><link rel="search" href="/opds/search/{searchTerms}?library_id='+identifier+'"/><entry><title>By Title</title><link type="application/atom+xml" href="/opds/navcatalog/4f7469746c65?library_id='+identifier+'"/></entry>'+entries+'</feed>').encode()
            elif parsed.path.startswith('/opds/navcatalog/') or parsed.path.startswith('/opds/search/'):
                if identifier in state['empty']:
                    body=b'No books found';self.send_response(404);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
                # Deliberately reuse the numeric ID and UUID across databases.
                body=('<feed xmlns="http://www.w3.org/2005/Atom"><id>calibre-all:title</id><entry><id>urn:uuid:same</id><title>Book '+identifier+'</title><author><name>Fixture Author</name></author><link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/epub/1/'+identifier+'"/><link rel="http://opds-spec.org/image/thumbnail" type="image/png" href="/cover/'+identifier+'"/></entry></feed>').encode()
            elif parsed.path.startswith('/cover/'):
                body=PNG
            else:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port),state,calls
    finally:server.shutdown();server.server_close();thread.join()


def test_bare_server_scans_every_library_and_cached_search_combines_results(tmp_path):
    from web.service import Service
    from main import result_library
    with multi_server() as (url,state,calls):
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            settings=Settings.load(config)
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
            assert len(settings.libraries())==2
            status=manager.status()
            assert status['server_count']==1 and len(status['libraries'])==2
            assert {row['library_id'] for row in status['libraries']}=={'A','B'}
            assert status['batch']['succeeded']==2
            store=CatalogStore(settings.catalog_cache_dir)
            snapshots={library_key(lib):store.entry(lib)['snapshot'] for _,lib in settings.libraries()}
            books=[store.read(lib)[0][0] for _,lib in settings.libraries()]
            assert len({b.key for b in books})==2
            assert all((store.directory/'covers'/(b.key+'.img')).exists() for b in books)
            assert result_library(next(b for b in books if b.library_id=='B'),settings.libraries()).library_id=='B'
            service=Service(config)
            try:
                job=service.submit('search',{'author':'Fixture Author','all_libraries':True})
                for _ in range(200):
                    outcome=service.snapshot(job)
                    if outcome['status']!='running':break
                    time.sleep(.01)
                assert outcome['status']=='done' and len(outcome['results'])==2
            finally:service.executor.shutdown(wait=True);service.catalogs.close()
            manager.refresh(force=True);finish(manager)
            assert all(store.entry(lib)['snapshot']==snapshots[library_key(lib)] for _,lib in settings.libraries())
            assert any('reusing saved metadata' in event for event in manager.events)
        finally:manager.close()


def test_explicit_library_does_not_expand(tmp_path):
    with multi_server() as (url,state,calls):
        config=configure(tmp_path,[url+'?library_id=B'])
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            assert len(manager.status()['libraries'])==1
            assert all('library_id=A' not in path for path in calls)
            assert not (Path(Settings.load(config).catalog_cache_dir)/'servers.json').exists()
        finally:manager.close()


def test_empty_default_does_not_hide_nonempty_alternate(tmp_path):
    with multi_server() as (url,state,calls):
        state['empty'].add('A')
        manager=CatalogManager(configure(tmp_path,[url]))
        try:
            manager.refresh(force=True);finish(manager)
            status=manager.status()
            assert sorted(row['book_count'] for row in status['libraries'])==[0,1]
            assert status['batch']['succeeded']==2
        finally:manager.close()


def test_new_and_removed_advertised_libraries_reconcile_without_changing_server_line(tmp_path):
    with multi_server() as (url,state,calls):
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            settings=Settings.load(config);store=CatalogStore(settings.catalog_cache_dir)
            old=next(lib for _,lib in settings.libraries() if lib.library_id=='B')
            oldbook=store.read(old)[0][0]
            state['ids']=['A','C']
            manager.refresh(force=True);finish(manager)
            assert {row['library_id'] for row in manager.status()['libraries']}=={'A','C'}
            assert manager.status()['batch']['queued']==0
            assert store.read(old)[0] is None
            assert not (store.directory/'covers'/(oldbook.key+'.img')).exists()
            assert any(r['reason']=='library-unadvertised' for r in manager.status()['retired'])
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
            (tmp_path/'libraries.txt').write_text('')
            manager.maintain_libraries(settings)
            assert not store.manifest()['libraries']
            assert not list((store.directory/'covers').glob('*.img'))
        finally:manager.close()


def test_child_retirement_preserves_parent_then_restore_uses_single_line(tmp_path):
    with multi_server() as (url,state,calls):
        config=configure(tmp_path,[url],library_scan_failure_limit=1)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            settings=Settings.load(config);lifecycle=LibraryState(settings)
            default=next(lib for _,lib in settings.libraries() if not lib.explicit_id)
            child=next(lib for _,lib in settings.libraries() if lib.explicit_id)
            lifecycle.scan_outcome(child,category='connectivity');lifecycle.purge_pending()
            assert len(settings.libraries())==1
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
            lifecycle.restore(all_retired=True)
            assert len(settings.libraries())==2
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
            lifecycle.scan_outcome(child,category='connectivity');lifecycle.purge_pending()
            lifecycle.scan_outcome(default,category='connectivity');lifecycle.purge_pending()
            assert not (tmp_path/'libraries.txt').read_text()
            lifecycle.restore(all_retired=True)
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
            assert len(settings.libraries())==2
        finally:manager.close()


def test_discovery_keeps_existing_default_snapshot_and_failing_child_does_not_block_sibling(tmp_path):
    with multi_server() as (url,state,calls):
        state['ids']=['A']
        config=configure(tmp_path,[url])
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            settings=Settings.load(config);store=CatalogStore(settings.catalog_cache_dir)
            default=settings.libraries()[0][1]
            previous=store.entry(default)['snapshot']
            state['ids']=['A','B']
            manager.refresh(force=True);finish(manager)
            assert store.entry(default)['snapshot']==previous
            assert len(settings.libraries())==2
            state['broken'].add('A')
            manager.refresh(force=True);finish(manager)
            status=manager.status()
            assert status['batch']['failed']==1 and status['batch']['succeeded']==1
            assert next(row for row in status['libraries'] if row['library_id']=='B')['health']=='healthy'
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
        finally:manager.close()


def test_manual_readd_after_every_child_retired_starts_fresh(tmp_path):
    with multi_server() as (url,state,calls):
        config=configure(tmp_path,[url],library_scan_failure_limit=1)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            settings=Settings.load(config);lifecycle=LibraryState(settings)
            for _,lib in list(settings.libraries()):
                lifecycle.scan_outcome(lib,category='connectivity');lifecycle.purge_pending()
            assert not (tmp_path/'libraries.txt').read_text()
            (tmp_path/'libraries.txt').write_text(url+'\n')
            manager.maintain_libraries(settings)
            assert len(settings.libraries())==2
            assert all(entry['scan_failure_count']==0 for entry in lifecycle.load()['active'].values())
        finally:manager.close()


def test_live_search_discovers_siblings_without_a_prior_refresh(tmp_path):
    from web.service import Service
    with multi_server() as (url,state,calls):
        config=configure(tmp_path,[url],catalog_cache_enabled=False)
        service=Service(config)
        try:
            job=service.submit('search',{'author':'Fixture Author','all_libraries':True})
            for _ in range(200):
                outcome=service.snapshot(job)
                if outcome['status']!='running':break
                time.sleep(.01)
            assert outcome['status']=='done' and len(outcome['results'])==2
            settings=Settings.load(config)
            assert len(settings.libraries())==2
            assert not CatalogStore(settings.catalog_cache_dir).manifest()['libraries']
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
        finally:service.executor.shutdown(wait=True);service.catalogs.close()


def test_empty_catalog_signal_is_narrowly_scoped():
    from calibre.client import Client,Library,HttpError
    from calibre.parser import atom_root
    from conftest import Session,Response
    client=Client(Library('http://fixture.test'),session=Session([Response(b'No books found',status=404)]))
    xml,_=client.read_feed('http://fixture.test/opds/navcatalog/4f7469746c65')
    assert atom_root(xml) is not None
    for path,body in [('/opds/search/example',b'No books found'),('/opds/navcatalog/4f7469746c65',b'Not found')]:
        client=Client(Library('http://fixture.test'),session=Session([Response(body,status=404)]))
        with pytest.raises(HttpError):client.read_feed('http://fixture.test'+path)


def test_library_selection_feed_without_default_is_enumerated():
    from calibre.client import Client,Library
    from conftest import Session,Response
    root=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link type="application/atom+xml" href="/opds?library_id=A"/><link type="application/atom+xml" href="/opds?library_id=B"/></entry></feed>'
    selected=b'<feed xmlns="http://www.w3.org/2005/Atom"><link rel="search" href="/opds/search/{searchTerms}?library_id=A"/></feed>'
    client=Client(Library('http://fixture.test'),session=Session([Response(root),Response(selected)]))
    observed=[]
    client.on_libraries=lambda ids,default:observed.append((ids,default))
    assert 'library_id=A' in client.discover_search()
    assert observed[-1][0]==['A','B']
