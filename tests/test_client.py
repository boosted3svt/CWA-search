import requests
import pytest
from calibre.client import Client as RealClient, Library, ClientError, LibraryTimeout
from calibre.parser import FeedError
from conftest import Session, Response

def Client(*args, **kwargs):
    # These tests isolate search/pagination from catalog discovery.
    kwargs.setdefault('auto_discover', False)
    return RealClient(*args, **kwargs)


EMPTY = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'


def test_search_encoding_pagination_and_dedup(xml):
    session = Session([Response(xml), Response(EMPTY)])
    client = Client(Library('http://books.test', 'Calibre_Library'), session=session)
    books = client.search('Emily Rath / #')
    assert len(books) == 2
    assert session.calls[0][0] == 'http://books.test/opds/search/Emily%20Rath%20%2F%20%23?library_id=Calibre_Library'
    assert session.calls[1][0].endswith('&offset=30')
    assert not client.truncated


def test_pagination_loop(xml):
    session = Session([Response(xml), Response(xml)])
    with pytest.raises(FeedError, match='loop'):
        Client(Library('http://books.test'), session=session).search('Emily Rath')


def test_limits(xml):
    client = Client(Library('http://books.test'), max_results=1, session=Session([Response(xml)]))
    assert len(client.search('Emily')) == 1
    assert client.truncated
    client = Client(Library('http://books.test'), max_pages=1, session=Session([Response(xml)]))
    assert len(client.search('Emily')) == 2
    assert client.truncated


@pytest.mark.parametrize('url', ['http://evil.test/a', 'http://books.test:99/a', 'file:///tmp/a', 'http://user:password@books.test/a'])
def test_restrict_links(url):
    session = Session([])
    with pytest.raises(ClientError):
        Client(Library('http://books.test'), session=session).get(url)
    assert not session.calls


def test_redirect_checked_before_request_and_no_auth_leak():
    session = Session([Response(status=302, headers={'Location': 'http://cdn.test/book'}), Response(b'book')])
    client = Client(Library('http://books.test', allowed_hosts=['cdn.test'], username='u', password='p'), session=session)
    client.get('http://books.test/a')
    assert session.calls[0][1]['auth'] == ('u', 'p')
    assert session.calls[1][1]['auth'] is None
    session = Session([Response(status=302, headers={'Location': 'http://evil.test/book'})])
    with pytest.raises(ClientError):
        Client(Library('http://books.test'), session=session).get('http://books.test/a')
    assert len(session.calls) == 1


def test_timeout_and_retry(monkeypatch):
    monkeypatch.setattr('calibre.client.time.sleep', lambda _: None)
    session = Session([requests.Timeout(), Response(EMPTY)])
    assert Client(Library('http://books.test'), session=session).search('x') == []
    with pytest.raises(LibraryTimeout):
        Client(Library('http://books.test'), retries=0, session=Session([requests.Timeout()])).search('x')


def test_http_error_and_transient_retry(monkeypatch):
    monkeypatch.setattr('calibre.client.time.sleep', lambda _: None)
    with pytest.raises(ClientError, match='HTTP 401'):
        Client(Library('http://books.test'), session=Session([Response(status=401)])).search('x')
    assert Client(Library('http://books.test'), session=Session([Response(status=503), Response(EMPTY)])).search('x') == []


def test_feed_size_bound():
    with pytest.raises(FeedError, match='size limit'):
        Client(Library('http://books.test'), max_feed_bytes=4, session=Session([Response(EMPTY)])).search('x')


def test_https_downgrade_rejected():
    session = Session([Response(status=302, headers={'Location': 'http://books.test/book'})])
    with pytest.raises(ClientError):
        Client(Library('https://books.test'), session=session).get('https://books.test/search')
    assert len(session.calls) == 1


def test_external_pagination_blocked(xml):
    xml = xml.replace(b'/opds/search/emily%20rath?', b'http://evil.test/opds/search/emily%20rath?')
    session = Session([Response(xml)])
    with pytest.raises(ClientError):
        Client(Library('http://books.test'), session=session).search('Emily')
    assert len(session.calls) == 1
