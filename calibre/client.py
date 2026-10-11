"""Bounded, sequential HTTP requests with origin checks on every redirect."""
from dataclasses import dataclass, field
import time
from urllib.parse import quote, urlencode, urljoin, urlsplit
import requests
from .parser import parse_feed, FeedError


class ClientError(RuntimeError):
    pass


class LibraryTimeout(ClientError):
    pass


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ClientError('URLs must use HTTP(S), without embedded credentials')
    try:
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        raise ClientError('Invalid URL port') from exc


@dataclass
class Library:
    url: str
    library_id: str = ''
    search_path: str = '/opds/search/{searchTerms}'
    allowed_hosts: list[str] = field(default_factory=list)
    username: str | None = None
    password: str | None = field(default=None, repr=False)


class Client:
    def __init__(self, library, timeout=10, retries=1, max_pages=10, max_results=100,
                 max_feed_bytes=5_000_000, session=None):
        self.library = library
        self.base_origin = origin(library.url)
        self.timeout, self.retries = timeout, retries
        self.max_pages, self.max_results = max_pages, max_results
        self.max_feed_bytes = max_feed_bytes
        self.session = session or requests.Session()
        # Avoid implicit .netrc credentials on additional hosts.
        self.session.trust_env = False
        self.truncated = False

    def close(self):
        self.session.close()

    def check_url(self, url):
        target = origin(url)
        if target != self.base_origin and target[1] not in self.library.allowed_hosts:
            raise ClientError('Link or redirect points outside the configured server/allowed hosts')
        if self.base_origin[0] == 'https' and target[0] != 'https':
            raise ClientError('HTTPS downgrade refused')

    def get(self, url):
        for redirect in range(6):
            self.check_url(url)
            auth = None
            if origin(url) == self.base_origin and self.library.username is not None:
                auth = (self.library.username, self.library.password or '')
            response = None
            for attempt in range(self.retries + 1):
                try:
                    response = self.session.get(url, timeout=self.timeout, stream=True,
                                                allow_redirects=False, auth=auth,
                                                proxies=requests.utils.get_environ_proxies(url),
                                                headers={'Accept-Encoding': 'identity'})
                except requests.Timeout as exc:
                    if attempt == self.retries:
                        raise LibraryTimeout('Library request timed out') from exc
                except requests.RequestException as exc:
                    raise ClientError('Library connection failed') from exc
                else:
                    if response.status_code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                        break
                    response.close()
                time.sleep(min(0.5 * 2 ** attempt, 4))
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                response.close()
                if not location:
                    raise ClientError('Redirect has no Location')
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise ClientError(f'Library returned HTTP {status}')
            return response, url
        raise ClientError('Too many redirects')

    def read_feed(self, url):
        response, final_url = self.get(url)
        try:
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_feed_bytes:
                    raise FeedError('OPDS feed exceeds size limit')
                chunks.append(chunk)
            return b''.join(chunks), final_url
        except requests.RequestException as exc:
            # A body read failure does not justify removing a library from the list.
            raise ClientError('OPDS feed transfer failed') from exc
        finally:
            response.close()

    def search(self, term):
        if '{searchTerms}' not in self.library.search_path:
            raise ClientError('search_path must contain {searchTerms}')
        url = urljoin(self.library.url.rstrip('/') + '/',
                      self.library.search_path.replace('{searchTerms}', quote(term, safe='')))
        if self.library.library_id:
            url += ('&' if '?' in url else '?') + urlencode({'library_id': self.library.library_id})
        seen_urls, seen_books, books = set(), set(), []
        self.truncated = False
        for _ in range(self.max_pages):
            if url in seen_urls:
                raise FeedError('Pagination loop detected')
            seen_urls.add(url)
            xml, final_url = self.read_feed(url)
            page, next_url = parse_feed(xml, final_url, self.library.url, self.library.library_id)
            for book in page:
                if book.key not in seen_books:
                    books.append(book)
                    seen_books.add(book.key)
                    if len(books) >= self.max_results:
                        self.truncated = bool(next_url) or book is not page[-1]
                        return books
            if not next_url:
                return books
            url = next_url
        self.truncated = True
        return books
