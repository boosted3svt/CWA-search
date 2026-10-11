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


def test_web_timeout_prunes_but_continues(website, monkeypatch):
    from calibre.client import LibraryTimeout
    url, _, directory = website
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
    response = post(url, '/api/search', {'author':'Emily'})
    state = wait_job(url, response.json()['id'])
    assert state['status'] == 'done' and state['failed_libraries'] == 1
    assert path.read_text() == 'working.test:80\n'
    assert path.with_name('libraries.txt.bak').exists()


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
