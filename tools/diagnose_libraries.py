"""Read-only OPDS diagnostics. Run via stdin inside the existing Docker image."""
import argparse
import json
import sys
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from config import Settings
from calibre.client import Client
from calibre.catalog import full_catalog, ScanCancelled
from calibre.parser import atom_root
from defusedxml import ElementTree as ET


def safe_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    if parts.port:
        host += ':' + str(parts.port)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                       if k in ('library_id', 'offset')])
    return urlunsplit((parts.scheme, host, parts.path, query, ''))


def diagnose(settings, library, seconds=3600, pages=10000):
    result = {'source': safe_url(library.url), 'requests': [], 'comment_recoveries': [],
              'books_read': 0, 'outcome': None,
              'limits': {'seconds': seconds, 'pages': pages}}
    started = time.monotonic()
    client = Client(library, timeout=min(settings.timeout, 10), retries=0,
                    opds_path=settings.opds_path)
    last_page = {}
    request_get, check_url, read_feed = client.session.get, client.check_url, client.read_feed

    def checked(url):
        try:
            check_url(url)
        except Exception:
            result['rejected_link'] = safe_url(url)
            raise

    def get(url, **kwargs):
        item = {'url': safe_url(url)}
        result['requests'].append(item)
        response = request_get(url, **kwargs)
        item.update(status=response.status_code,
                    content_type=response.headers.get('Content-Type'),
                    server=response.headers.get('Server'))
        if response.headers.get('Location'):
            from urllib.parse import urljoin
            item['redirect_to'] = safe_url(urljoin(url, response.headers['Location']))
        return response

    def read(url):
        data, final_url = read_feed(url)
        last_page.update(url=safe_url(final_url), bytes=len(data))
        # Use the application's current parser, including narrowly scoped recovery.
        try:
            atom_root(data)
        except Exception as exc:
            cause = exc.__cause__
            result['failed_page'] = {**last_page,
                                    'parser_reason': str(cause) if cause else str(exc),
                                    'body': data[:1_000_000].decode('utf-8', errors='replace'),
                                    'body_truncated': len(data) > 1_000_000}
            raise
        try:
            ET.fromstring(data)
        except Exception:
            result['comment_recoveries'].append(dict(last_page))
        return data, final_url

    def progress(count):
        result['books_read'] = count
        if count % 300 == 0:
            print(f'{library.url}: {count} books checked', file=sys.stderr, flush=True)

    client.check_url, client.session.get, client.read_feed = checked, get, read
    try:
        print(f'Checking {library.url}', file=sys.stderr, flush=True)
        books = full_catalog(client, max_pages=pages, max_books=500000,
                             pause=lambda: time.sleep(.25), progress=progress,
                             cancelled=lambda: time.monotonic() - started > seconds)
        result.update(outcome='complete', books_read=len(books))
    except Exception as exc:
        result.update(outcome='diagnostic limit reached' if isinstance(exc, ScanCancelled) else 'failed',
                      error=str(exc), last_page=dict(last_page))
    finally:
        client.close()
    result['elapsed_seconds'] = round(time.monotonic() - started, 1)
    print(f'{library.url}: {result["outcome"]}; {result["books_read"]} books',
          file=sys.stderr, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/data/config.json')
    parser.add_argument('--seconds', type=int, default=3600, help='Per-library time limit (default: 3600 seconds)')
    parser.add_argument('--max-pages', type=int, default=10000, help='Per-library page limit (default: 10000)')
    parser.add_argument('servers', nargs='+')
    args = parser.parse_args()
    if args.seconds < 1 or args.max_pages < 1:
        parser.error('--seconds and --max-pages must be positive')
    settings = Settings.load(args.config)
    configured = settings.libraries()
    results = []
    for address in args.servers:
        target = settings.library(address)
        matches = [lib for _, lib in configured if lib.url == target.url]
        if len(matches) > 1 and not target.explicit_id:
            raise SystemExit('Multiple libraries on this server; specify ?library_id=...')
        if matches and not target.explicit_id:
            target = matches[0]
        results.append(diagnose(settings, target, seconds=args.seconds, pages=args.max_pages))
    json.dump({'diagnostics': results}, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == '__main__':
    main()
