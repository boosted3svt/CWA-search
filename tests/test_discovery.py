import pytest
from calibre.client import Client, Library, ClientError
from calibre.parser import FeedError
from conftest import Session, Response


def catalog(template):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">'
            f'<link rel="search" href="{template}"/></feed>').encode()


def test_discover_id_overrides_shared_default(xml):
    session = Session([Response(catalog('/opds/search/{searchTerms}?library_id=Different')), Response(xml)])
    client = Client(Library('http://books.test', 'WrongDefault'), max_pages=1, session=session)
    books = client.search('Kate Battistelli')
    assert session.calls[0][0] == 'http://books.test/opds'
    assert session.calls[1][0] == 'http://books.test/opds/search/Kate%20Battistelli?library_id=Different'
    assert all(book.library_id == 'Different' for book in books)


def test_explicit_library_id_preserved(xml):
    session = Session([Response(catalog('/custom/{searchTerms}')), Response(xml)])
    client = Client(Library('http://books.test', 'Chosen', explicit_id=True), max_pages=1, session=session)
    assert client.search('Emily Rath')[0].library_id == 'Chosen'
    assert session.calls[0][0] == 'http://books.test/opds?library_id=Chosen'
    assert session.calls[1][0] == 'http://books.test/custom/Emily%20Rath?library_id=Chosen'


def test_discovery_404_falls_back(xml):
    session = Session([Response(status=404), Response(xml)])
    client = Client(Library('http://books.test', 'Known'), max_pages=1, session=session)
    assert client.search('Emily')[0].library_id == 'Known'
    assert session.calls[1][0].endswith('/opds/search/Emily?library_id=Known')


def test_discovery_does_not_hide_auth_error():
    session = Session([Response(status=401)])
    with pytest.raises(ClientError, match='401'):
        Client(Library('http://books.test'), session=session).search('Emily')
    assert len(session.calls) == 1


def test_external_advertised_search_rejected():
    session = Session([Response(catalog('http://evil.test/{searchTerms}'))])
    with pytest.raises(ClientError):
        Client(Library('http://books.test'), session=session).search('Emily')
    assert len(session.calls) == 1


def test_follow_single_library_navigation(xml):
    root = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <link type="application/atom+xml;profile=opds-catalog" href="/opds?library_id=Hidden"/>
    </entry></feed>'''
    session = Session([Response(root), Response(catalog('/opds/search/{searchTerms}?library_id=Hidden')), Response(xml)])
    client = Client(Library('http://books.test'), max_pages=1, session=session)
    assert client.search('Kate')[0].library_id == 'Hidden'
    assert session.calls[1][0] == 'http://books.test/opds?library_id=Hidden'


def test_ambiguous_libraries_require_explicit_selection():
    root = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <link type="application/atom+xml" href="/opds?library_id=One"/>
    <link type="application/atom+xml" href="/opds?library_id=Two"/>
    </entry></feed>'''
    with pytest.raises(ClientError, match='Multiple library'):
        Client(Library('http://books.test'), session=Session([Response(root)])).search('Kate')


def test_discovery_loop_rejected():
    root = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <link type="application/atom+xml" href="/opds?library_id=One"/>
    </entry></feed>'''
    with pytest.raises(FeedError, match='discovery loop'):
        Client(Library('http://books.test'), session=Session([Response(root), Response(root)])).search('Kate')


def test_encoded_template_and_escaped_parameters(xml):
    session = Session([Response(catalog('/find/%7BsearchTerms%7D?library_id=Other&amp;sort=title')), Response(xml)])
    client = Client(Library('http://books.test'), max_pages=1, session=session)
    client.search('Kate Battistelli')
    assert session.calls[1][0] == 'http://books.test/find/Kate%20Battistelli?library_id=Other&sort=title'
