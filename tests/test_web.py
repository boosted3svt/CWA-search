from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
from pathlib import Path
import threading
import time
import requests
import pytest
from web.service import Service
from web_app import make_server

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
HEADERS = {'X-OPDS-UI': '1'}


@pytest.mark.parametrize('failure', [BrokenPipeError, ConnectionResetError])
@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_disconnect_during_reply_does_not_attempt_an_error_response(website, failure, stage):
    _,service,_=website
    server=make_server(service,port=0)
    handler=object.__new__(server.RequestHandlerClass)
    statuses=[]
    handler.send_response=lambda status:statuses.append(status)
    handler.send_header=lambda *args:None
    def disconnected(*args):raise failure('Client disconnected')
    handler.end_headers=disconnected if stage=='headers' else lambda:None
    class Writer:
        write=disconnected
    handler.wfile=Writer()
    try:
        handler.reply(200,b'page')
        assert handler.close_connection and statuses==[200]
    finally:server.server_close()


def test_real_browser_disconnect_is_quiet_and_server_still_responds(website, monkeypatch):
    import socket,struct
    from web_app import STATIC
    _,service,_=website
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    original=Path.read_bytes
    def delayed(path):
        if path==STATIC/'index.html':
            entered.set();assert release.wait(3)
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',delayed)
    server=make_server(service,port=0)
    errors=[]
    server.handle_error=lambda *args:errors.append(args)
    original_finish=server.RequestHandlerClass.finish
    def finish_handler(handler):
        try:original_finish(handler)
        finally:finished.set()
    server.RequestHandlerClass.finish=finish_handler
    with serving(server) as url:
        client=socket.create_connection(server.server_address,timeout=2)
        try:
            client.sendall(f'GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n\r\n'.encode())
            assert entered.wait(2)
            client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
            client.close()
        finally:client.close();release.set()
        assert finished.wait(2)
        assert not errors
        assert requests.get(url+'/api/settings',timeout=3).status_code==200


@contextmanager
def serving(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture
def website(tmp_path, xml, mobi, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '*')
    class LibraryHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/opds':
                body = b'<feed xmlns="http://www.w3.org/2005/Atom"><link rel="search" href="/opds/search/{searchTerms}?library_id=Actual"/></feed>'
            elif self.path.startswith('/opds/search/'):
                body = xml.replace(b'Calibre_Library', b'Actual').replace(b' <link rel="next" href="/opds/search/emily%20rath?library_id=Actual&amp;offset=30"/>', b'')
            elif self.path == '/get/mobi/3170/Actual':
                body = mobi
            elif self.path == '/get/thumb/3170/Actual':
                body = PNG
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    with serving(ThreadingHTTPServer(('127.0.0.1', 0), LibraryHandler)) as library:
        config = tmp_path / 'config.json'
        config.write_text('{"library_id": "Fallback"}')
        (tmp_path / 'libraries.txt').write_text(library + '\n')
        service = Service(config)
        with serving(make_server(service, port=0)) as url:
            yield url, service, tmp_path
        service.executor.shutdown(wait=True)
        service.catalogs.close()


def post(url, endpoint, data):
    return requests.post(url + endpoint, json=data, headers=HEADERS, timeout=5)


def wait_job(url, key):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = requests.get(url + '/api/jobs/' + key, timeout=5).json()
        if state['status'] != 'running':
            return state
        time.sleep(0.01)
    pytest.fail('Background job did not finish')


def search(url):
    response = post(url, '/api/search', {'author': 'Emily Rath'})
    assert response.status_code == 202
    state = wait_job(url, response.json()['id'])
    assert state['status'] == 'done', state
    return state['results'][0]


def test_http_search_cover_download_and_settings(website, mobi):
    url, service, directory = website
    page = requests.get(url, timeout=5)
    assert page.status_code == 200 and 'Find a book' in page.text
    assert "script-src 'self'" in page.headers['Content-Security-Policy']
    assert requests.get(url + '/app.js', timeout=5).status_code == 200
    book = search(url)
    assert book['library_id'] == 'Actual'
    assert book['preferred_format'] == 'mobi'
    assert 'acquisitions' not in book
    cover = requests.get(url + book['cover_url'], timeout=5)
    assert cover.content == PNG and cover.headers['Content-Type'] == 'image/png'
    new_folder = directory / 'ingest'
    saved = post(url, '/api/settings', {'download_dir': str(new_folder)})
    assert saved.status_code == 200
    config = json.loads(service.config.read_text())
    assert config['library_id'] == 'Fallback' and config['download_dir'] == str(new_folder)
    response = post(url, '/api/download', {'id': book['key'], 'format': 'mobi'})
    state = wait_job(url, response.json()['id'])
    assert state['status'] == 'done', state
    assert next(new_folder.glob('*.mobi')).read_bytes() == mobi
    response = post(url, '/api/mark-bad', {'id': book['key'], 'format': 'mobi', 'include_history': True})
    assert wait_job(url, response.json()['id'])['status'] == 'done'
    assert search(url)['formats'] == ['epub']


def test_cross_origin_and_invalid_requests_rejected(website):
    url, _, _ = website
    assert requests.post(url + '/api/search', json={'author':'Emily'}, timeout=5).status_code == 403
    assert requests.post(url + '/api/search', json={'author':'Emily'},
                         headers={**HEADERS, 'Origin':'http://evil.example'}, timeout=5).status_code == 403
    assert requests.get(url, headers={'Host':'evil.example'}, timeout=5).status_code == 403
    assert post(url, '/api/search', {}).status_code == 400
    assert post(url, '/api/search', {'author': [], 'title':'x'}).status_code == 400
    assert requests.get(url + '/api/jobs/missing', timeout=5).status_code == 404
    assert requests.get(url + '/../../config.json', timeout=5).status_code == 404


def test_second_client_can_read_progress_but_not_start_another_job(website, monkeypatch):
    url, service, _ = website
    entered, release = threading.Event(), threading.Event()
    def blocked(key, *args):
        entered.set()
        assert release.wait(5)
        return {'results': []}
    monkeypatch.setattr(service, 'search', blocked)
    response = post(url, '/api/search', {'author':'Emily'})
    key = response.json()['id']
    try:
        assert entered.wait(2)
        assert requests.get(url + '/api/settings', timeout=5).json()['active_job'] == key
        assert requests.get(url + '/api/jobs/' + key, timeout=5).json()['status'] == 'running'
        assert post(url, '/api/search', {'author':'Other'}).status_code == 409
        assert post(url, '/api/settings', {'download_dir':'other'}).status_code == 409
    finally:
        release.set()
    assert wait_job(url, key)['status'] == 'done'


def test_optional_password(website):
    _, service, _ = website
    with serving(make_server(service, port=0, password='test-password')) as url:
        assert requests.get(url, timeout=5).status_code == 401
        assert requests.get(url, auth=('calibre', 'wrong'), timeout=5).status_code == 401
        assert requests.get(url, auth=('calibre', 'test-password'), timeout=5).status_code == 200


def test_stop_search_skips_remaining_libraries(website, monkeypatch, book):
    from dataclasses import replace
    url, service, directory = website
    settings = service.settings()
    (directory / 'libraries.txt').write_text('first.test:80\nsecond.test:80\n')
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Fake:
        discovery_message = ''
        truncated = False
        def __init__(self, library): self.library = library
        def search(self, term):
            calls.append(self.library.url)
            entered.set()
            assert release.wait(5)
            return []
        def close(self): pass
    monkeypatch.setattr('web.service.make_client', lambda settings, library: Fake(library))
    response = post(url, '/api/search', {'author':'Emily', 'all_libraries':True})
    key = response.json()['id']
    try:
        assert entered.wait(2)
        assert post(url, '/api/cancel', {'id':key}).status_code == 200
    finally:
        release.set()
    state = wait_job(url, key)
    assert state['status'] == 'done' and state['cancelled']
    assert calls == ['http://first.test:80']
    assert 'Search stopped' in state['message']


def test_web_timeout_retains_until_connectivity_limit_but_continues(website, monkeypatch):
    from calibre.client import LibraryTimeout
    from calibre.library_state import LibraryState
    from calibre.library_list import rows
    from config import Settings
    from datetime import datetime, timedelta, timezone
    url, service, directory = website
    path = directory / 'libraries.txt'
    path.write_text('offline.test:80\nworking.test:80\n')
    class Fake:
        discovery_message = ''
        truncated = False
        def __init__(self, library): self.library = library
        def search(self, term):
            if 'offline' in self.library.url:
                raise LibraryTimeout('Library request timed out')
            return []
        def close(self): pass
    monkeypatch.setattr('web.service.make_client', lambda settings, library: Fake(library))
    settings=Settings.load(service.config)
    lifecycle=LibraryState(settings)
    for attempt in range(3):
        if attempt:
            data=lifecycle.load()
            for entry in data['active'].values():
                entry['last_counted_scan_failure']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
            lifecycle.save(data)
        response = post(url, '/api/search', {'author':'Emily'})
        state = wait_job(url, response.json()['id'])
        assert state['status'] == 'done' and state['failed_libraries'] == 1
        assert len(settings.raw_libraries()) == (2 if attempt < 2 else 1)
    assert [line for line,_ in settings.raw_libraries()] == ['working.test:80']
    assert next(row for row in rows(settings) if row['url']=='offline.test:80')['active'] is False
    assert path.with_name('libraries.txt.original.bak').exists()


def test_cover_cannot_proxy_arbitrary_server(website):
    from calibre.state import History
    from config import Settings
    url, service, _ = website
    book = search(url)
    history = History(Settings.load(service.config).history_file)
    history.data['books'][book['key']]['cover_url'] = 'http://unconfigured.test/cover.png'
    history.save()
    assert requests.get(url + book['cover_url'], timeout=5).status_code == 400


def test_download_failure_explains_outcome_and_can_skip_server(website, monkeypatch):
    from calibre.downloader import DownloadError
    from calibre.state import History
    from config import Settings
    url, service, directory = website
    book = search(url)
    def fail(*args, **kwargs):
        raise DownloadError('OPDS advertised 242650 bytes; downloaded 250266 bytes; download size differs from the OPDS acquisition length')
    monkeypatch.setattr('web.service.download', fail)
    response = post(url, '/api/download', {'id':book['key'], 'format':'mobi'})
    state = wait_job(url, response.json()['id'])
    assert state['status'] == 'error'
    assert not state['file_saved']
    assert state['selection']['source'] == book['source']
    assert 'No new ebook was saved' in state['user_message']
    assert '242650' in state['error']
    assert not list((directory / 'downloads').glob('*.mobi'))
    history = History(Settings.load(service.config).history_file)
    assert history.data['attempts'][book['key']]
    before = (directory / 'libraries.txt').read_text()
    response = post(url, '/api/search', {'author':'Emily Rath', 'skip_source':book['source']})
    next_state = wait_job(url, response.json()['id'])
    assert next_state['status'] == 'done' and next_state['results'] == []
    assert 'No other library' in next_state['message']
    assert (directory / 'libraries.txt').read_text() == before


def test_history_write_failure_does_not_claim_saved_file_is_missing(website, monkeypatch):
    from calibre.state import History
    url, _, directory = website
    book = search(url)
    original = History.record
    def record(self, book, acquisition, status, path=None):
        if status == 'downloaded':
            raise OSError('History update failed')
        return original(self, book, acquisition, status, path)
    monkeypatch.setattr(History, 'record', record)
    response = post(url, '/api/download', {'id':book['key'], 'format':'mobi'})
    state = wait_job(url, response.json()['id'])
    assert state['status'] == 'error' and state['file_saved']
    assert Path(state['saved_path']).exists()
    assert 'book was saved' in state['user_message']


def test_cached_search_uses_no_live_library_and_download_still_does(website, monkeypatch, book, mobi):
    from dataclasses import replace
    from calibre.cache import CatalogStore, now
    from config import Settings
    url, service, directory = website
    settings = Settings.load(service.config)
    library = settings.libraries()[0][1]
    data = json.loads(service.config.read_text())
    data['catalog_cache_enabled'] = True
    service.config.write_text(json.dumps(data))
    cached = replace(book, source=library.url, library_id='Actual',
                     acquisitions=[replace(a,url=a.url.replace('http://books.test',library.url).replace('Calibre_Library','Actual')) for a in book.acquisitions])
    CatalogStore(settings.catalog_cache_dir).publish(library,[cached],now())
    from web.service import make_client as original
    calls = []
    def tracked(*args,**kwargs):
        calls.append('network-client')
        return original(*args,**kwargs)
    monkeypatch.setattr('web.service.make_client',tracked)
    result = search(url)
    assert calls == [] and result['cached_at']
    assert result['cover_url'] == '/placeholder-cover.svg'  # No live thumbnail fetch in cached mode.
    response = post(url,'/api/download',{'id':result['key'],'format':'mobi'})
    state = wait_job(url,response.json()['id'])
    assert state['status'] == 'done' and calls == ['network-client']
    assert next((directory/'downloads').glob('*.mobi')).read_bytes() == mobi


def test_all_missing_covers_use_one_shared_cached_placeholder(website,monkeypatch,book):
    from dataclasses import replace
    from calibre.cache import CatalogStore,now
    from config import Settings
    url,service,directory=website
    data=json.loads(service.config.read_text());data['catalog_cache_enabled']=True
    service.config.write_text(json.dumps(data))
    with (directory/'libraries.txt').open('a') as handle:handle.write('http://second.test:8080\n')
    settings=Settings.load(service.config);store=CatalogStore(settings.catalog_cache_dir)
    for _,library in settings.libraries():
        store.publish(library,[replace(book,source=library.url,cover_url=None)],now())
    def no_network(*args,**kwargs):raise AssertionError('Cached search must not fetch remote covers')
    monkeypatch.setattr('web.service.make_client',no_network)
    job=wait_job(url,post(url,'/api/search',{'author':'Emily Rath','all_libraries':True}).json()['id'])
    assert len(job['results'])==2 and {book['cover_url'] for book in job['results']}=={'/placeholder-cover.svg'}
    placeholder=requests.get(url+'/placeholder-cover.svg',timeout=3)
    assert placeholder.status_code==200 and placeholder.headers['Content-Type']=='image/svg+xml'
    assert 'max-age=86400' in placeholder.headers['Cache-Control']
    assert not list(store.directory.glob('covers/*.img'))
    keys=[book['key'] for book in job['results']]
    assert post(url,'/api/cover-availability',{'ids':keys}).json()=={}
    (store.directory/'covers').mkdir()
    (store.directory/'covers'/(keys[0]+'.img')).write_bytes(PNG)
    assert post(url,'/api/cover-availability',{'ids':keys}).json()=={keys[0]:f'/api/cover/{keys[0]}'}
    assert post(url,'/api/cover-availability',{'ids':['../../config.json']}).status_code==400


def test_refresh_api_publishes_full_catalog_while_local_search_remains_available(website, monkeypatch, book):
    from dataclasses import replace
    from config import Settings
    url,service,directory = website
    config = json.loads(service.config.read_text()); config['catalog_cache_enabled']=True
    service.config.write_text(json.dumps(config))
    library = Settings.load(service.config).libraries()[0][1]
    entered,release = threading.Event(),threading.Event()
    def scan(*args,**kwargs):
        from calibre.poll_worker import checkpoint_for
        entered.set(); assert release.wait(5)
        from calibre.cache import library_key
        checkpoint=checkpoint_for(args[0],args[1],library_key(library))
        checkpoint.save({'url':None,'seen':[],'acquisition_feed':True,'page_count':1,
                         'complete':True,'resolved_library_id':'Actual'},[replace(book,source=library.url,library_id='Actual')])
    monkeypatch.setattr(service.catalogs,'run_worker',scan)
    response = post(url,'/api/catalogs/refresh',{})
    assert response.status_code == 202
    try:
        assert entered.wait(2)
        assert requests.get(url+'/api/catalogs',timeout=5).json()['running']
        assert requests.get(url+'/api/settings',timeout=5).status_code == 200
        assert post(url,'/api/catalogs/refresh',{}).status_code == 409
    finally:
        release.set(); service.catalogs.worker.join(3)
    assert requests.get(url+'/api/catalogs',timeout=5).json()['libraries'][0]['book_count'] == 1
    assert search(url)['cached_at']
    service.catalogs.close()


def test_cover_only_api_requires_enabled_covers_and_exposes_progress(website,monkeypatch,book):
    from dataclasses import replace
    from calibre.cache import CatalogStore,now
    from config import Settings
    url,service,directory=website
    config=json.loads(service.config.read_text());config['catalog_cache_enabled']=True
    service.config.write_text(json.dumps(config))
    assert post(url,'/api/catalogs/covers',{}).status_code==400
    config['catalog_cache_covers']=True;service.config.write_text(json.dumps(config))
    entered,release=threading.Event(),threading.Event()
    settings=Settings.load(service.config);library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[replace(book,source=library.url)],now())
    def worker(settings,library,covers_only):
        assert covers_only
        with service.catalogs.lock:
            service.catalogs.active={'source':library.url,'phase':'covers','count':1,'total':2,
                                     'started_monotonic':time.monotonic(),'last_activity':now(),
                                     'deadline_seconds':3600}
        entered.set();assert release.wait(5)
    monkeypatch.setattr(service.catalogs,'run_worker',worker)
    assert post(url,'/api/catalogs/covers',{}).status_code==202
    try:
        assert entered.wait(2)
        state=requests.get(url+'/api/catalogs').json()
        assert state['active']['phase']=='covers' and state['active']['total']==2
        assert post(url,'/api/catalogs/covers',{}).status_code==409
    finally:
        release.set();service.catalogs.worker.join(3)
    assert requests.get(url+'/api/catalogs').json()['libraries'][0]['book_count']==1
