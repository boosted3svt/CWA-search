from dataclasses import replace
from pathlib import Path
import pytest
from calibre.catalog import catalog_count
from calibre.client import Library
from calibre.cache import CatalogStore, library_key, now
from calibre.poll_worker import run_library
from config import Settings
from test_refresh_runtime import configure

ROOT=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>By Title</title><link type="application/atom+xml" href="/all"/></entry></feed>'

def page(ids, links=''):
    entries=''.join('<entry><id>urn:uuid:'+str(i)+'</id><title>Book '+str(i)+'</title><link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/'+str(i)+'"/></entry>' for i in ids)
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><id>calibre-all:title</id>'+entries+links+'</feed>').encode()

class FakeClient:
    def __init__(self, pages):
        self.library=Library('http://fixture.test')
        self.resolved_library_id=''
        self.opds_path='/opds'
        self.pages=pages
        self.calls=[]
    def discover_search(self):pass
    def read_feed(self,url):
        self.calls.append(url)
        return self.pages[url],url
    def close(self):pass


def test_count_uses_final_page_not_all_middle_pages():
    client=FakeClient({'http://fixture.test/opds':ROOT,
        'http://fixture.test/all':page([1,2],'<link rel="next" href="/all?offset=2"/><link rel="last" href="/all?offset=100"/>'),
        'http://fixture.test/all?offset=100':page([101])})
    assert catalog_count(client)==101
    assert len(client.calls)==3


def test_count_rejects_inaccurate_last_link_with_more_pages():
    client=FakeClient({'http://fixture.test/opds':ROOT,
        'http://fixture.test/all':page([1,2],'<link rel="next" href="/all?offset=2"/><link rel="last" href="/all?offset=2"/>'),
        'http://fixture.test/all?offset=2':page([3,4],'<link rel="next" href="/all?offset=4"/>')})
    assert catalog_count(client) is None


@pytest.mark.parametrize('remote', [0,1,2,None])
def test_refresh_count_match_skips_metadata_other_counts_scan(tmp_path,book,monkeypatch,remote):
    import calibre.poll_worker as worker
    settings=Settings.load(configure(tmp_path,[book.source],catalog_cache_covers=False))
    library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[book],now())
    monkeypatch.setattr(worker,'Client',lambda *a,**k:FakeClient({}))
    monkeypatch.setattr(worker,'catalog_count',lambda *a,**k:remote)
    scans=[]
    def full(*a,**k):
        scans.append(True)
        return [book]
    monkeypatch.setattr(worker,'full_catalog',full)
    events=[]
    run_library(settings,library,library_key(library),report=lambda message,**fields:events.append((message,fields)))
    assert bool(scans)==(remote!=1)
    assert any(fields.get('phase')=='metadata-unchanged' for _,fields in events)==(remote==1)


@pytest.mark.parametrize('saved', [True,False])
def test_matching_cover_inventory_skips_network_otherwise_fetches_missing(tmp_path,book,monkeypatch,saved):
    import calibre.poll_worker as worker
    from test_web import PNG
    settings=Settings.load(configure(tmp_path,[book.source],catalog_cache_covers=True))
    library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[book],now())
    directory=Path(settings.catalog_cache_dir)/'covers';directory.mkdir()
    # An unrelated cover must not hide the missing expected one.
    (directory/((book.key if saved else 'unrelated')+'.img')).write_bytes(PNG)
    monkeypatch.setattr(worker,'Client',lambda *a,**k:FakeClient({}))
    monkeypatch.setattr(worker,'catalog_count',lambda *a,**k:1)
    fetched=[]
    def fetch(*args):
        fetched.append(True)
        return PNG,'image/png'
    monkeypatch.setattr(worker,'fetch_image',fetch)
    events=[]
    run_library(settings,library,library_key(library),report=lambda m,**f:events.append((m,f)))
    assert bool(fetched)==(not saved)
    assert any('skipping cover downloads' in m for m,_ in events)==saved

@pytest.mark.parametrize('changed_count',[1,3])
def test_real_worker_reuses_snapshot_then_publishes_changed_count(tmp_path,changed_count):
    from collections import Counter
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from calibre.cache import CatalogManager
    from test_refresh_runtime import finish
    calls=Counter()
    state={'count':2}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            calls[self.path]+=1
            if self.path=='/opds':
                body=ROOT
            else:
                from urllib.parse import urlsplit,parse_qs
                offset=int(parse_qs(urlsplit(self.path).query).get('offset',['0'])[0])
                links=''
                if offset+1<state['count']:
                    links='<link rel="next" href="/all?offset='+str(offset+1)+'"/>'
                if offset==0 and state['count']>1:
                    links+='<link rel="last" href="/all?offset='+str(state['count']-1)+'"/>'
                body=page([offset+1],links)
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    config=configure(tmp_path,[url],catalog_cache_covers=True)
    manager=CatalogManager(config);settings=Settings.load(config)
    library=settings.libraries()[0][1];store=CatalogStore(settings.catalog_cache_dir)
    try:
        manager.refresh(force=True);finish(manager)
        before=store.entry(library)
        assert before['book_count']==2
        manager.refresh(force=True);finish(manager)
        assert store.entry(library)['snapshot']==before['snapshot']
        assert manager.status()['batch']['succeeded']==1
        assert any('reusing saved metadata' in e for e in manager.events)
        assert any('skipping cover downloads' in e for e in manager.events)
        state['count']=changed_count
        manager.refresh(force=True);finish(manager)
        assert store.entry(library)['book_count']==changed_count
        assert store.entry(library)['snapshot']!=before['snapshot']
        assert manager.status()['libraries'][0]['health']=='healthy'
    finally:
        manager.close();server.shutdown();server.server_close();thread.join()
