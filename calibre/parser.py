"""Parse untrusted OPDS XML without resolving entities or external resources."""
from urllib.parse import urljoin, urlsplit, parse_qs
import re
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import ParseError
from .models import Acquisition, Book

ATOM = '{http://www.w3.org/2005/Atom}'
DC = '{http://purl.org/dc/terms/}'
FORMATS = {
    'application/epub+zip': 'epub',
    'application/x-mobipocket-ebook': 'mobi',
    'application/pdf': 'pdf',
    'application/vnd.amazon.ebook': 'azw',
    'application/x-azw3': 'azw3',
}


class FeedError(ValueError):
    pass


def atom_root(xml):
    try:
        root = ET.fromstring(xml)
    except ParseError as exc:
        # Calibre can emit invalid '--' inside comments copied from descriptions.
        # Comments carry no catalog metadata. Preserve CDATA and valid comments;
        # retry only after removing complete, malformed comments. The retry uses
        # the same hardened parser and never repairs tags, entities, or DTDs.
        raw = xml.encode('utf-8') if isinstance(xml, str) else xml
        def clean(match):
            token = match.group(0)
            if token.startswith(b'<!--'):
                body = token[4:-3]
                if b'--' in body or body.endswith(b'-'):
                    return b''
            return token
        repaired = re.sub(rb'<!\[CDATA\[.*?\]\]>|<!--.*?-->', clean, raw, flags=re.DOTALL)
        if repaired == raw:
            raise FeedError('Invalid or unsafe OPDS XML') from exc
        try:
            root = ET.fromstring(repaired)
        except Exception as retry_exc:
            raise FeedError('Invalid or unsafe OPDS XML') from retry_exc
    except Exception as exc:
        raise FeedError('Invalid or unsafe OPDS XML') from exc
    if root.tag != ATOM + 'feed':
        raise FeedError('Expected an Atom OPDS feed')
    return root


def catalog_links(xml, feed_url):
    """Return advertised search templates and library navigation links."""
    root = atom_root(xml)
    searches, libraries = [], []
    for link in root.findall(ATOM + 'link'):
        if link.get('rel') == 'search' and link.get('href'):
            url = urljoin(feed_url, link.get('href'))
            url = re.sub(r'%7bsearchTerms%7d', '{searchTerms}', url, flags=re.IGNORECASE)
            if '{searchTerms}' in url:
                searches.append(url)
    for entry in root.findall(ATOM + 'entry'):
        for link in entry.findall(ATOM + 'link'):
            href = link.get('href', '')
            url = urljoin(feed_url, href)
            media = link.get('type', '')
            if ('application/atom+xml' in media and
                    link.get('rel', 'subsection') in ('subsection', 'alternate') and
                    parse_qs(urlsplit(url).query).get('library_id')):
                libraries.append(url)
    return list(dict.fromkeys(searches)), list(dict.fromkeys(libraries))


def parse_feed(xml: bytes, feed_url: str, source: str, library_id: str = '', include_formatless=False):
    root = atom_root(xml)
    books = []
    for entry in root.findall(ATOM + 'entry'):
        def value(tag):
            element = entry.find(tag)
            return ''.join(element.itertext()).strip() if element is not None else ''
        book = Book(
            title=value(ATOM + 'title'),
            authors=[e.text.strip() for e in entry.findall(ATOM + 'author/' + ATOM + 'name') if e.text],
            identifier=value(ATOM + 'id'), source=source, library_id=library_id,
            updated=value(ATOM + 'updated'), published=value(ATOM + 'published'),
            publication_date=value(DC + 'date'),
            description=value(ATOM + 'content') or value(ATOM + 'summary'),
        )
        thumbnail = None
        cover = None
        for link in entry.findall(ATOM + 'link'):
            href = link.get('href', '')
            if not href:
                continue
            url = urljoin(feed_url, href)
            rel = link.get('rel', '')
            if rel in ('http://opds-spec.org/image/thumbnail', 'http://opds-spec.org/thumbnail'):
                thumbnail = url
            elif rel in ('http://opds-spec.org/image', 'http://opds-spec.org/cover'):
                cover = url
            if rel != 'http://opds-spec.org/acquisition':
                continue
            media_type = link.get('type', '').split(';')[0].strip().lower()
            path = urlsplit(url).path
            match = re.search(r'/get/([\w]+)/([0-9]+)(?:/|$)', path)
            fmt = FORMATS.get(media_type) or (match.group(1).lower() if match else None)
            if not fmt:
                continue
            if match:
                book.book_id = match.group(2)
            raw_length = link.get('length')
            length = int(raw_length) if raw_length and raw_length.isdigit() else None
            book.acquisitions.append(Acquisition(fmt, url, media_type, length))
        book.cover_url = thumbnail or cover
        if book.title and (book.acquisitions or include_formatless):
            books.append(book)
    next_url = None
    for link in root.findall(ATOM + 'link'):
        if link.get('rel') == 'next' and link.get('href'):
            next_url = urljoin(feed_url, link.get('href'))
            break
    return books, next_url
