#!/usr/bin/env python3
"""Browser GUI served with Python's standard library; no debug server."""
import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ipaddress
from urllib.parse import urlsplit
from web.service import Service, BusyError

STATIC = Path(__file__).parent / 'web' / 'static'


def make_server(service, host='127.0.0.1', port=8090, password=None, allowed_hosts=()):
    names = {'localhost', socket.gethostname().lower(), socket.getfqdn().lower(), *allowed_hosts}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, media='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            try:
                hostname = urlsplit('//' + self.headers.get('Host', '')).hostname
                if not hostname:
                    raise ValueError()
                if hostname.lower() not in names:
                    ipaddress.ip_address(hostname)
            except ValueError:
                self.reply(403, {'error': 'Unrecognized server hostname; use the server IP address'})
                return False
            if not password:
                return True
            try:
                scheme, token = self.headers.get('Authorization', '').split(' ', 1)
                credentials = base64.b64decode(token, validate=True).decode()
                expected = 'calibre:' + password
                valid = scheme.lower() == 'basic' and secrets.compare_digest(credentials.encode(), expected.encode())
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Calibre search"')
                self.send_header('Content-Length', '0')
                self.end_headers()
            return valid

        def do_GET(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            try:
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path in assets:
                    name, media = assets[path]
                    self.reply(200, (STATIC / name).read_bytes(), media)
                elif path == '/api/settings':
                    self.reply(200, service.settings())
                elif path.startswith('/api/jobs/'):
                    self.reply(200, service.snapshot(path.rsplit('/', 1)[1]))
                elif path.startswith('/api/cover/'):
                    body, media = service.cover(path.rsplit('/', 1)[1])
                    self.reply(200, body, media)
                else:
                    self.reply(404, {'error': 'Not found'})
            except KeyError:
                self.reply(404, {'error': 'Result or job not found; search again'})
            except Exception:
                self.reply(400, {'error': 'Unable to load this resource. Check server configuration.'})

        def do_POST(self):
            if not self.authorized():
                return
            # Browser requests must originate from this UI; no CORS is enabled.
            origin = self.headers.get('Origin')
            if (self.headers.get('X-OPDS-UI') != '1' or
                    (origin and origin != 'http://' + self.headers.get('Host', ''))):
                self.reply(403, {'error': 'Use the application page to perform this action'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Expected a small JSON request')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                path = urlsplit(self.path).path
                if path == '/api/settings':
                    self.reply(200, service.save_folder(data.get('download_dir')))
                elif path == '/api/cancel':
                    if not isinstance(data.get('id'), str):
                        raise ValueError('Expected a search job ID')
                    service.cancel(data.get('id'))
                    self.reply(200, {'message': 'Stopping search'})
                elif path in ('/api/search', '/api/download', '/api/mark-bad'):
                    if any(not isinstance(data.get(name, ''), str) for name in ('author', 'title', 'id', 'format', 'skip_source')):
                        raise ValueError('Search and selection fields must be text')
                    if any(name in data and not isinstance(data[name], bool) for name in
                           ('exact', 'all_libraries', 'include_history', 'keep_timeouts')):
                        raise ValueError('Search options must be true or false')
                    key = service.submit(path.rsplit('/', 1)[1], data)
                    self.reply(202, {'id': key})
                else:
                    self.reply(404, {'error': 'Not found'})
            except BusyError as exc:
                self.reply(409, {'error': str(exc)})
            except (ValueError, OSError) as exc:
                self.reply(400, {'error': str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def cli():
    parser = argparse.ArgumentParser(description='Run the Calibre search browser GUI on your trusted LAN.')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--host', default='127.0.0.1', help='Use 0.0.0.0 to accept connections from your LAN')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--allowed-host', action='append', default=[], help='Additional LAN DNS name for this server')
    args = parser.parse_args()
    service = Service(args.config)
    server = make_server(service, args.host, args.port, os.environ.get('CALIBRE_WEB_PASSWORD'), args.allowed_host)
    print(f'Calibre search listening on {args.host}:{server.server_port}', flush=True)
    print('Downloads are saved on this server. Press Ctrl+C to stop.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.executor.shutdown(wait=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
