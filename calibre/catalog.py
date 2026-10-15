"""Enumerate a Calibre OPDS acquisition catalog without guessing search syntax."""
from urllib.parse import urljoin, urlencode
from .parser import atom_root, ATOM, parse_feed
from .client import ClientError


class ScanCancelled(ClientError):
    pass


def full_catalog(client, max_pages=10000, max_books=500000, pause=lambda: None,
                 cancelled=lambda: False, progress=lambda count: None, checkpoint=None):
    # Discovery establishes the selected library; explicit per-line IDs win.
    state, books = checkpoint.load() if checkpoint else (None, {})
    if state:
        client.resolved_library_id = state['resolved_library_id']
        progress(len(books))
        if state['complete']:
            return list(books.values())
        url, seen = state['url'], set(state['seen'])
        acquisition_feed, page_count = state['acquisition_feed'], state['page_count']
    else:
        client.discover_search()
        url = urljoin(client.library.url.rstrip('/') + '/', client.opds_path)
        if client.resolved_library_id:
            url += ('&' if '?' in url else '?') + urlencode({'library_id': client.resolved_library_id})
        seen, acquisition_feed, page_count = set(), False, 0
    for _ in range(page_count, max_pages):
        if cancelled():
            raise ScanCancelled('Catalog refresh stopped; previous snapshot retained')
        if url in seen:
            if checkpoint:
                checkpoint.clear()
            raise ClientError('Catalog pagination/navigation loop detected')
        seen.add(url)
        pause()
        prefetched = getattr(client, '_count_probe_pages', {}).pop(url, None)
        xml, final_url = prefetched if prefetched is not None else client.read_feed(url)
        root = atom_root(xml)
        entries = root.findall(ATOM + 'entry')
        page, next_url = parse_feed(xml, final_url, client.library.url, client.resolved_library_id,
                                    include_formatless=True)
        # A feed may contain formatless book records. They are not downloadable,
        # but keep their metadata and count them in the complete catalog.
        is_books = any(book.acquisitions for book in page) or root.findtext(ATOM + 'id', '').startswith('calibre-all:')
        if not acquisition_feed and not is_books:
            navigation = []
            for entry in entries:
                title = entry.findtext(ATOM + 'title', '').strip().casefold()
                if title not in ('by title', 'all books', 'all titles'):
                    continue
                for link in entry.findall(ATOM + 'link'):
                    if 'application/atom+xml' in link.get('type', '') and link.get('href'):
                        navigation.append(urljoin(final_url, link.get('href')))
            if len(navigation) != 1:
                raise ClientError('No unambiguous full catalog advertised (expected By Title / All Books)')
            url = navigation[0]
            page_count += 1
            if checkpoint:
                checkpoint.save({'url': url, 'seen': sorted(seen), 'acquisition_feed': False,
                                 'page_count': page_count, 'complete': False,
                                 'resolved_library_id': client.resolved_library_id}, [])
            continue
        acquisition_feed = True
        if entries and not is_books:
            raise ClientError('Pagination changed to a navigation feed; previous snapshot retained')
        if len(page) != len(entries):
            raise ClientError('Catalog contains an entry without a usable title; previous snapshot retained')
        for book in page:
            if not book.identifier:
                raise ClientError('Catalog contains a book without an identifier')
            books[book.key] = book
        if len(books) > max_books:
            raise ClientError('Catalog exceeds configured book limit; previous snapshot retained')
        progress(len(books))
        page_count += 1
        if checkpoint:
            checkpoint.save({'url': next_url, 'seen': sorted(seen), 'acquisition_feed': True,
                             'page_count': page_count, 'complete': not next_url,
                             'resolved_library_id': client.resolved_library_id}, page)
        if not next_url:
            if cancelled():
                raise ScanCancelled('Catalog refresh stopped; previous snapshot retained')
            return list(books.values())
        url = next_url
    raise ClientError('Catalog exceeds configured page limit; previous snapshot retained')


def catalog_count(client, pause=lambda: None):
    """Count a complete title feed using its first and final advertised pages."""
    from urllib.parse import urlsplit, parse_qs
    client._count_probe_pages = {}
    def read(url):
        data = client.read_feed(url)
        client._count_probe_pages[url] = data
        return data
    client.discover_search()
    url = urljoin(client.library.url.rstrip('/') + '/', client.opds_path)
    if client.resolved_library_id:
        url += ('&' if '?' in url else '?') + urlencode({'library_id': client.resolved_library_id})
    seen = set()
    for _ in range(10):
        if url in seen:
            return None
        seen.add(url)
        pause()
        xml, final_url = read(url)
        root = atom_root(xml)
        entries = root.findall(ATOM + 'entry')
        books, next_url = parse_feed(xml, final_url, client.library.url,
                                     client.resolved_library_id, include_formatless=True)
        is_books = any(book.acquisitions for book in books) or root.findtext(ATOM+'id', '').startswith('calibre-all:')
        if not is_books:
            links = [urljoin(final_url, link.get('href')) for entry in entries
                     if entry.findtext(ATOM+'title', '').strip().casefold() in ('by title','all books','all titles')
                     for link in entry.findall(ATOM+'link')
                     if link.get('href') and 'application/atom+xml' in link.get('type','')]
            if len(links) != 1:
                return None
            url = links[0]
            continue
        if len(books) != len(entries):
            return None
        first = urlsplit(final_url)
        query = parse_qs(first.query)
        if query.get('offset', ['0']) != ['0']:
            return None
        if not next_url:
            return len(books)
        # A final-page link is useful only if it really ends the same catalog.
        links = [urljoin(final_url, link.get('href')) for link in root.findall(ATOM+'link')
                 if link.get('rel') == 'last' and link.get('href')]
        if len(links) != 1 or not books:
            return None
        last = urlsplit(links[0])
        last_query = parse_qs(last.query)
        try:
            offset = int(last_query.get('offset', ['-1'])[0])
        except ValueError:
            return None
        without_offset = lambda q: {k:v for k,v in q.items() if k != 'offset'}
        if ((last.scheme,last.netloc,last.path) != (first.scheme,first.netloc,first.path)
                or without_offset(query) != without_offset(last_query)
                or offset <= 0 or offset % len(books)):
            return None
        following = urlsplit(next_url)
        if (following.scheme,following.netloc,following.path) != (first.scheme,first.netloc,first.path):
            return None
        if parse_qs(following.query).get('offset') != [str(len(books))]:
            return None
        pause()
        last_xml, last_url = read(links[0])
        last_root = atom_root(last_xml)
        tail, more = parse_feed(last_xml,last_url,client.library.url,
                                client.resolved_library_id,include_formatless=True)
        if more or not tail or len(tail) > len(books) or len(tail) != len(last_root.findall(ATOM+'entry')):
            return None
        return offset + len(tail)
    return None
