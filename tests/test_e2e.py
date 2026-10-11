"""CLI integration against a local fixture server; never contacts a remote library."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import pytest


def test_cli_search_select_mobi_history(tmp_path, xml, mobi, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '*')
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith('/opds/search/'):
                body = xml.replace(b' <link rel="next" href="/opds/search/emily%20rath?library_id=Calibre_Library&amp;offset=30"/>', b'')
                media = 'application/atom+xml'
            elif self.path == '/get/mobi/3170/Calibre_Library':
                body, media = mobi, 'application/x-mobipocket-ebook'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        (tmp_path / 'libraries.txt').write_text(f'http://127.0.0.1:{server.server_port}?library_id=Calibre_Library\n')
        config = tmp_path / 'config.json'
        config.write_text('{}')
        def run(*args, input=None):
            return subprocess.run([sys.executable, 'main.py', '--config', str(config), *args],
                                  text=True, input=input, capture_output=True, timeout=10)
        found = run('search', '--author', 'Emily Rath', '--json')
        assert found.returncode == 0, found.stderr
        data = json.loads(found.stdout)
        assert len(data['results']) == 1  # Excludes incidental description match.
        key = data['results'][0]['key']
        selected = run('search', '--author', 'Emily Rath', '--select', input='1\n\n')
        assert selected.returncode == 0, selected.stderr
        assert 'Downloaded MOBI:' in selected.stdout
        downloads = list((tmp_path / 'downloads').glob('*.mobi'))
        assert len(downloads) == 1 and downloads[0].read_bytes() == mobi
        assert requests[-1] == '/get/mobi/3170/Calibre_Library'
        rejected = run('mark-bad', '--id', key)
        assert rejected.returncode == 0
        assert json.loads(run('search', '--author', 'Emily Rath', '--json').stdout)['results'] == []
        restored = run('search', '--author', 'Emily Rath', '--include-history', '--json')
        assert len(json.loads(restored.stdout)['results']) == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('interactive', [False, True])
def test_cli_discovers_different_id_after_first_server_404(tmp_path, xml, mobi, monkeypatch, interactive):
    monkeypatch.setenv('NO_PROXY', '*')
    calls = []
    class First(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(('first', self.path))
            if self.path == '/opds':
                body = b'<feed xmlns="http://www.w3.org/2005/Atom"><link rel="search" href="/opds/search/{searchTerms}?library_id=First"/></feed>'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        def log_message(self, *args): pass
    class Second(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(('second', self.path))
            if self.path == '/opds':
                body = b'<feed xmlns="http://www.w3.org/2005/Atom"><link rel="search" href="/opds/search/{searchTerms}?library_id=Second_Library"/></feed>'
            elif self.path in ('/opds/search/Kate%20Battistelli?library_id=Second_Library',
                               '/opds/search/Kate%20Battistelli%20The%20God%20Dare?library_id=Second_Library'):
                body = (xml.replace(b'Emily Rath', b'Kate Battistelli')
                        .replace(b'<name>Kate Battistelli</name>', b'<name>Battistelli, Kate</name>')
                        .replace(b'Pucking Around: A Why Choose Hockey Romance',
                                 b'The God Dare: Will You Choose to Believe the Impossible?')
                        .replace(b'Calibre_Library', b'Second_Library')
                        .replace(b' <link rel="next" href="/opds/search/emily%20rath?library_id=Second_Library&amp;offset=30"/>', b''))
            elif self.path == '/get/mobi/3170/Second_Library':
                body = mobi
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    servers = [ThreadingHTTPServer(('127.0.0.1', 0), handler) for handler in (First, Second)]
    workers = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for worker in workers: worker.start()
    try:
        (tmp_path / 'libraries.txt').write_text(''.join(f'127.0.0.1:{s.server_port}\n' for s in servers))
        config = tmp_path / 'config.json'
        config.write_text('{"library_id": "Wrong_Shared_Default"}')
        command = [sys.executable, 'main.py', '--config', str(config)]
        found = subprocess.run(command + ['search', '--author', 'Kate Battistelli', '--json'],
                               capture_output=True, text=True, timeout=10)
        assert found.returncode == 0, found.stderr
        results = json.loads(found.stdout)['results']
        assert len(results) == 1 and results[0]['library_id'] == 'Second_Library'
        assert results[0]['authors'] == ['Battistelli, Kate']
        assert 'HTTP 404' in found.stderr
        assert f'Searching: http://127.0.0.1:{servers[1].server_port}' in found.stderr
        assert 'Library ID: Second_Library' in found.stderr
        fetch_args = (['search', '--author', 'Kate Battistelli', '--title', 'The God Dare', '--select'] if interactive else
                      ['download', '--id', results[0]['key'], '--format', 'mobi'])
        fetched = subprocess.run(command + fetch_args, input='1\n\n' if interactive else None,
                                 capture_output=True, text=True, timeout=10)
        assert fetched.returncode == 0, fetched.stderr
        assert calls[-1] == ('second', '/get/mobi/3170/Second_Library')
        assert len(list((tmp_path / 'downloads').glob('*.mobi'))) == 1
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for worker in workers: worker.join()
