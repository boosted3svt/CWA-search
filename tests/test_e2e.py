"""CLI integration against a local fixture server; never contacts a remote library."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading


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
