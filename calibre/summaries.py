"""On-demand public book descriptions. No catalog writes or persistent cache."""
from html.parser import HTMLParser
import json
import os
import re
import threading
import time
import unicodedata
from urllib.parse import urlencode, urlsplit
import requests
from .matcher import normalize, normalize_author
from .client import retry_after_seconds

OPEN_LIBRARY = 'https://openlibrary.org'
GOOGLE_BOOKS = 'https://www.googleapis.com/books/v1/volumes'
USER_AGENT = 'CWA-search/1.0 (https://github.com/boosted3svt/CWA-search; book descriptions)'
_request_lock = threading.Lock()
_next_request = 0.0
_cooldown_lock = threading.Lock()
_cooldowns = {}


class _Unavailable(Exception):
    pass


def _pace(deadline):
    """At most one public lookup request per second across this process."""
    global _next_request
    with _request_lock:
        delay = max(0, _next_request - time.monotonic())
        if time.monotonic() + delay >= deadline:
            raise _Unavailable()
        time.sleep(delay)
        _next_request = time.monotonic() + 1


class _JSON:
    def __init__(self, session, seconds):
        self.session = session
        self.deadline = time.monotonic() + seconds

    def get(self, url, params=None):
        host = urlsplit(url).hostname
        with _cooldown_lock:
            if _cooldowns.get(host, 0) > time.monotonic():
                raise _Unavailable()
        _pace(self.deadline)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable()
        response = None
        try:
            response = self.session.get(
                url, params=params or {}, stream=True, allow_redirects=False,
                timeout=min(5, remaining),
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
                proxies=requests.utils.get_environ_proxies(url),
                verify=os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE') or True)
            if response.status_code == 429:
                # Public API quotas can be shared by an IP or project. Do not
                # retry a throttled provider on every book/button click.
                delay = max(3600, retry_after_seconds(response.headers.get('Retry-After')))
                with _cooldown_lock:
                    _cooldowns[host] = max(_cooldowns.get(host, 0), time.monotonic() + delay)
            if response.status_code != 200:
                raise _Unavailable()
            if int(response.headers.get('Content-Length', '0')) > 1_000_000:
                raise _Unavailable()
            body = bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body) > 1_000_000 or time.monotonic() >= self.deadline:
                    raise _Unavailable()
            result = json.loads(body)
            if not isinstance(result, dict):
                raise _Unavailable()
            return result
        except (requests.RequestException, ValueError, OSError) as exc:
            raise _Unavailable() from exc
        finally:
            if response is not None:
                response.close()


def _fold(text, author=False):
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return normalize_author(text) if author else normalize(text)


def _title(text):
    # Remove explicit series labels, rather than arbitrary parenthesized titles.
    return re.sub(r'\s*[\[(][^\])]*\b(?:book|volume|series)\b[^\])]*[\])]\s*$',
                  '', text, flags=re.I).strip()


def _matching(book, title, authors):
    if not isinstance(title, str) or not isinstance(authors, list):
        return False
    wanted, actual = _title(book.title), _title(title)
    titles_match = _fold(wanted) == _fold(actual)
    # A provider may omit a subtitle. Different subtitles on both sides are
    # deliberately not treated as interchangeable (e.g. books in a series).
    if ':' not in actual:
        titles_match = titles_match or _fold(wanted.split(':', 1)[0]) == _fold(actual)
    if ':' not in wanted:
        titles_match = titles_match or _fold(wanted) == _fold(actual.split(':', 1)[0])
    names = {_fold(name, author=True) for name in book.authors if isinstance(name, str) and name.strip()}
    return bool(_fold(wanted)) and titles_match and any(
        isinstance(name, str) and _fold(name, author=True) in names for name in authors)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if not self.hidden and tag in ('p', 'br', 'li', 'div'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ('p', 'li', 'div'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_description(value):
    if isinstance(value, dict):
        value = value.get('value')
    if not isinstance(value, str):
        return ''
    parser = _Text()
    parser.feed(value[:100_000])
    text = '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines())
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text if len(text) <= 12000 else text[:12000] + '\n\n[Description shortened.]'


def _found(description, source, url, title, authors):
    return dict(status='found', description=description, source=source,
                source_url=url, matched_title=title, matched_authors=authors)


def _open_library(http, book):
    data = http.get(OPEN_LIBRARY + '/search.json', {
        'title': _title(book.title).split(':', 1)[0][:500], 'author': normalize_author(book.authors[0])[:200],
        'limit': 5, 'fields': 'key,title,author_name'})
    docs = data.get('docs')
    if not isinstance(docs, list):
        raise _Unavailable()
    tried = 0
    for item in docs[:5]:
        if not isinstance(item, dict) or not _matching(book, item.get('title'), item.get('author_name')):
            continue
        key = item.get('key', '')
        if not isinstance(key, str) or not re.fullmatch(r'/works/OL[0-9]+W', key):
            continue
        tried += 1
        work = http.get(OPEN_LIBRARY + key + '.json')
        description = plain_description(work.get('description'))
        if description and _matching(book, work.get('title', item['title']), item['author_name']):
            return _found(description, 'Open Library', OPEN_LIBRARY + key, item['title'], item['author_name'])
        if tried >= 2:
            break


def _google_books(http, book):
    # Query punctuation must not inject additional Google Books search operators.
    title = normalize(_title(book.title).split(':', 1)[0])[:500]
    author = normalize_author(book.authors[0])[:200]
    data = http.get(GOOGLE_BOOKS, {'q': f'intitle:"{title}" inauthor:"{author}"',
                                 'maxResults': 5, 'printType': 'books'})
    items = data.get('items', [])
    if not isinstance(items, list):
        raise _Unavailable()
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        info = item.get('volumeInfo', {})
        if not isinstance(info, dict):
            continue
        title, authors = info.get('title'), info.get('authors')
        if isinstance(title, str) and isinstance(info.get('subtitle'), str) and info['subtitle'].strip():
            title += ': ' + info['subtitle']
        identifier = item.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
            continue
        description = plain_description(info.get('description'))
        if description and _matching(book, title, authors):
            return _found(description, 'Google Books', 'https://books.google.com/books?' +
                          urlencode({'id': identifier}), title, authors)


def lookup_summary(book, session=None, seconds=15):
    if not book.title.strip() or not book.authors or not book.authors[0].strip():
        return dict(status='not_found', message='A title and author are needed to look up a matching summary.')
    owned = session is None
    session = session or requests.Session()
    if owned:
        # Never forward library credentials or implicit .netrc credentials.
        session.trust_env = False
    http = _JSON(session, seconds)
    unavailable = False
    try:
        for provider in (_open_library, _google_books):
            try:
                result = provider(http, book)
                if result:
                    return result
            except _Unavailable:
                unavailable = True
        if unavailable:
            return dict(status='unavailable', message='No verified summary was retrieved. A book information source is unavailable or rate-limited; try again later.')
        return dict(status='not_found', message='No summary matching both this title and author was found.')
    finally:
        if owned:
            session.close()
