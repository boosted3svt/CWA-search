import pytest
from calibre.parser import parse_feed, FeedError
from calibre.matcher import matches


def test_parse_metadata_links_and_pagination(xml):
    books, next_url = parse_feed(xml, 'http://books.test/opds/search/emily', 'http://books.test', 'Calibre_Library')
    book = books[0]
    assert book.authors == ['Emily Rath']
    assert book.book_id == '3170'
    assert book.identifier.startswith('urn:uuid:')
    assert book.publication_date == '2023-03-20'
    assert book.description == 'A hockey romance'
    assert book.cover_url == 'http://books.test/get/thumb/3170/Calibre_Library'
    assert [a.format for a in book.acquisitions] == ['epub', 'mobi']
    assert '&offset=30' in next_url and '&amp;' not in next_url


def test_matches_metadata_not_description(xml):
    books, _ = parse_feed(xml, 'http://books.test', 'http://books.test')
    assert matches(books[0], author=' EMILY  rath ')
    assert matches(books[0], title='Pucking Around', author='Emily Rath')
    assert not matches(books[1], author='Emily Rath')
    assert not matches(books[0], author='Emily Rat')
    assert not matches(books[0], title='Pucking Around', exact=True)
    assert matches(books[0], author='Emily Rath', exact=True)


@pytest.mark.parametrize('xml', [b'<html/>', b'broken', b'<!DOCTYPE feed [<!ENTITY x "unsafe">]><feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>'])
def test_bad_xml(xml):
    with pytest.raises(FeedError):
        parse_feed(xml, 'http://books.test', 'http://books.test')


def test_length_and_absolute_link(xml):
    xml = xml.replace(b'href="/get/mobi/', b'length="256" href="http://books.test/get/mobi/')
    book = parse_feed(xml, 'http://books.test', 'http://books.test')[0][0]
    assert book.acquisitions[1].length == 256
