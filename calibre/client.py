"""Bounded, sequential HTTP requests with origin checks on every redirect."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit, parse_qs, parse_qsl
import requests
from .parser import parse_feed, catalog_links, FeedError, library_names


class ClientError(RuntimeError):
    pass


class LibraryTimeout(ClientError):
    pass


def is_timeout_error(error):
    """Requests wraps streamed read timeouts inside ConnectionError arguments."""
    from urllib3.exceptions import TimeoutError as UrllibTimeout
    seen, pending = set(), [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (requests.Timeout, UrllibTimeout, TimeoutError)):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__, *current.args)
                       if isinstance(value, BaseException))
    return False


class HttpError(ClientError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Library returned HTTP {status}')


class ClassifiedError(ClientError):
    def __init__(self, message, category, retry_after=0):
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


def classify_failure(exc):
    """Safe diagnostics: never serialize request URLs, headers, or credentials."""
    if isinstance(exc, ClassifiedError):
        return exc.category
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, requests.exceptions.SSLError):
            return 'configuration'
        if isinstance(current, (LibraryTimeout, requests.Timeout)):
            return 'connectivity'
        if isinstance(current, requests.exceptions.ProxyError):
            return 'configuration'
        if isinstance(current, requests.ConnectionError):
            return 'connectivity'
        if isinstance(current, HttpError):
            if current.status in (401, 403):
                return 'configuration'
            if current.status == 429 or current.status >= 500:
                return 'temporary'
            return 'catalog'
        current = current.__cause__
    if isinstance(exc, FeedError):
        return 'catalog'
    return 'catalog'


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
    explicit_id: bool = False
    verify_tls: bool = True
    parent_url: str = ''


class Client:
    def __init__(self, library, timeout=10, retries=1, max_pages=10, max_results=100,
                 max_feed_bytes=5_000_000, session=None, auto_discover=True, opds_path='/opds'):
        self.library = library
        self.base_origin = origin(library.url)
        self.timeout, self.retries = timeout, retries
        self.max_pages, self.max_results = max_pages, max_results
        self.max_feed_bytes = max_feed_bytes
        self.session = session or requests.Session()
        # Avoid implicit .netrc credentials on additional hosts.
        self.session.trust_env = False
        self.truncated = False
        self.auto_discover, self.opds_path = auto_discover, opds_path
        self.resolved_library_id = library.library_id
        self.discovery_message = ''
        self.on_libraries = None
        self.advertised_library_ids = set()
        self.library_names = {}

    def close(self):
        self.session.close()

    def is_library_origin(self, target):
        # Advertised HTTPS links may use the TLS port rather than the HTTP port.
        return target == self.base_origin or (self.base_origin[0] == 'http' and
                target[0] == 'https' and target[1] == self.base_origin[1])

    def check_url(self, url):
        target = origin(url)
        if not self.is_library_origin(target) and target[1] not in self.library.allowed_hosts:
            raise ClassifiedError('Link or redirect points outside the configured server/allowed hosts', 'configuration')
        if self.base_origin[0] == 'https' and target[0] != 'https':
            raise ClassifiedError('HTTPS downgrade refused', 'configuration')

    def get(self, url):
        for redirect in range(6):
            self.check_url(url)
            auth = None
            if self.is_library_origin(origin(url)) and self.library.username is not None:
                auth = (self.library.username, self.library.password or '')
            response = None
            for attempt in range(self.retries + 1):
                try:
                    response = self.session.get(url, timeout=self.timeout, stream=True,
                                                allow_redirects=False, auth=auth,
                                                verify=self.library.verify_tls if self.is_library_origin(origin(url)) else True,
                                                proxies=requests.utils.get_environ_proxies(url),
                                                headers={'Accept-Encoding': 'identity'})
                except requests.Timeout as exc:
                    if attempt == self.retries:
                        raise LibraryTimeout('Library request timed out') from exc
                except requests.exceptions.SSLError as exc:
                    raise ClassifiedError('TLS certificate verification or negotiation failed; check certificate and HTTPS configuration', 'configuration') from exc
                except requests.exceptions.ProxyError as exc:
                    raise ClassifiedError('Proxy connection failed; check host proxy configuration', 'configuration') from exc
                except requests.ConnectionError as exc:
                    raise ClassifiedError(connection_diagnostic(exc), 'connectivity') from exc
                except requests.RequestException as exc:
                    raise ClassifiedError('Library request configuration failed', 'configuration') from exc
                else:
                    if response.status_code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                        break
                    response.close()
                time.sleep(max(min(0.5 * 2 ** attempt, 4), retry_after_seconds(response.headers.get('Retry-After')) if response is not None else 0))
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                response.close()
                if not location:
                    raise ClientError('Redirect has no Location')
                next_url = urljoin(url, location)
                if origin(url)[0] == 'https' and origin(next_url)[0] != 'https':
                    raise ClassifiedError('HTTPS downgrade refused', 'configuration')
                url = next_url
                continue
            if response.status_code != 200:
                status = response.status_code
                empty_catalog = False
                if status == 404 and '/opds/navcatalog/' in urlsplit(url).path:
                    try:
                        empty_catalog = next(response.iter_content(64), b'').strip() == b'No books found'
                    except requests.RequestException:
                        pass
                response.close()
                error = HttpError(status)
                error.empty_catalog = empty_catalog
                error.retry_after = retry_after_seconds(response.headers.get('Retry-After'))
                raise error
            return response, url
        raise ClientError('Too many redirects')

    def read_feed(self, url):
        try:
            response, final_url = self.get(url)
        except HttpError as exc:
            if getattr(exc, 'empty_catalog', False):
                return b'<feed xmlns="http://www.w3.org/2005/Atom"><id>calibre-all:title</id></feed>', url
            raise
        try:
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_feed_bytes:
                    raise FeedError('OPDS feed exceeds size limit')
                chunks.append(chunk)
            return b''.join(chunks), final_url
        except requests.RequestException as exc:
            # Preserve a safe category for the isolated metadata polling worker.
            raise ClassifiedError('OPDS feed transfer timed out' if is_timeout_error(exc) else 'OPDS feed transfer failed', classify_failure(exc)) from exc
        finally:
            response.close()

    def configured_search(self):
        if '{searchTerms}' not in self.library.search_path:
            raise ClientError('search_path must contain {searchTerms}')
        url = urljoin(self.library.url.rstrip('/') + '/',
                      self.library.search_path)
        if self.library.library_id:
            url += ('&' if '?' in url else '?') + urlencode({'library_id': self.library.library_id})
        return url

    def discover_search(self):
        """Use the server's advertised template; explicit IDs remain authoritative."""
        fallback = self.configured_search()
        if not self.auto_discover:
            return fallback
        url = urljoin(self.library.url.rstrip('/') + '/', self.opds_path)
        if self.library.explicit_id:
            url += ('&' if '?' in url else '?') + urlencode({'library_id': self.library.library_id})
        visited = set()
        for _ in range(4):
            if url in visited:
                raise FeedError('Library discovery loop detected')
            visited.add(url)
            try:
                xml, final_url = self.read_feed(url)
            except HttpError as exc:
                if exc.status != 404:
                    raise
                self.discovery_message = 'OPDS discovery endpoint returned 404; using configured search template'
                return fallback
            searches, libraries = catalog_links(xml, final_url)
            self.library_names.update(library_names(xml))
            if self.on_libraries:
                advertised = []
                for link in libraries + searches:
                    self.check_url(link)
                    identifier = parse_qs(urlsplit(link).query).get('library_id', [''])[0]
                    if identifier:
                        advertised.append(identifier)
                default_id = (parse_qs(urlsplit(searches[0]).query).get('library_id', [''])[0]
                              if len(searches) == 1 and not self.library.explicit_id else None)
                self.advertised_library_ids.update(advertised)
                self.on_libraries(sorted(self.advertised_library_ids), default_id)
            if self.library.explicit_id:
                searches = [template for template in searches if
                            parse_qs(urlsplit(template).query).get('library_id', [self.library.library_id])[0]
                            == self.library.library_id]
                libraries = [link for link in libraries if
                             parse_qs(urlsplit(link).query)['library_id'][0] == self.library.library_id]
            if len(searches) == 1:
                template = searches[0]
                self.check_url(template)
                parsed = urlsplit(template)
                discovered_id = parse_qs(parsed.query).get('library_id', [''])[0]
                if self.library.explicit_id and not discovered_id:
                    query = parse_qsl(parsed.query, keep_blank_values=True)
                    query.append(('library_id', self.library.library_id))
                    template = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                          urlencode(query, safe='{}'), ''))
                    discovered_id = self.library.library_id
                self.resolved_library_id = discovered_id
                self.discovery_message = f'Library ID: {discovered_id or "server default"} (advertised search)'
                return template
            if len(searches) > 1 or len(libraries) > 1:
                ids = sorted({parse_qs(urlsplit(link).query).get('library_id', [''])[0]
                              for link in searches + libraries} - {''})
                choices = ', '.join(repr(value) for value in ids) or 'unspecified IDs'
                if self.on_libraries and not self.library.explicit_id and libraries:
                    url = next((link for link in libraries if parse_qs(urlsplit(link).query).get('library_id') == [ids[0]]), libraries[0])
                    self.check_url(url)
                    continue
                raise ClientError(f'Multiple library catalogs advertised ({choices}); '
                                  'select one with ?library_id=ID in libraries.txt')
            if libraries:
                url = libraries[0]
                continue
            self.discovery_message = 'No path search template advertised; using configured search template'
            return fallback
        raise FeedError('Library discovery depth limit reached')

    def search(self, term):
        template = self.discover_search()
        url = template.replace('{searchTerms}', quote(term, safe=''))
        seen_urls, seen_books, books = set(), set(), []
        self.truncated = False
        for _ in range(self.max_pages):
            if url in seen_urls:
                raise FeedError('Pagination loop detected')
            seen_urls.add(url)
            xml, final_url = self.read_feed(url)
            page, next_url = parse_feed(xml, final_url, self.library.url, self.resolved_library_id)
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


def retry_after_seconds(value):
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone
    try:
        import math
        delay = float(value)
        return max(0, delay) if math.isfinite(delay) else 0
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def connection_diagnostic(exc):
    import errno
    import socket
    seen, pending = set(), [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, socket.gaierror) or type(current).__name__ == 'NameResolutionError':
            return 'Library connection failed: DNS name resolution failed'
        if isinstance(current, OSError) and current.errno == errno.ECONNREFUSED:
            return 'Library connection failed: connection refused'
        if isinstance(current, OSError) and current.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return 'Library connection failed: network or host unreachable'
        if isinstance(current, requests.exceptions.ProxyError):
            return 'Library connection failed: proxy connection failed'
        pending.extend(value for value in (current.__cause__, current.__context__, *current.args)
                       if isinstance(value, BaseException))
    return 'Library connection failed: connection interrupted or could not be established'
