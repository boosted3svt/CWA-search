"""Calibre description comment recovery preserves catalog data and XML defenses."""
import pytest
from calibre.parser import atom_root, parse_feed, FeedError


def feed(description):
    return ('''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Example Book</title><id>urn:uuid:example</id><author><name>Example Author</name></author>
<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">'''+description+'''</div></content>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/epub/1/Library"/>
</entry><link rel="next" href="/opds/all?offset=570"/></feed>''').encode()


def test_invalid_double_hyphen_comment_does_not_drop_book_or_pagination():
    xml=feed('<p><!--[CDATA[Description -- with invalid comment.<br--><br/>Visible description.</p>')
    books,next_url=parse_feed(xml,'http://example.test/opds/all','http://example.test','Library')
    assert len(books)==1 and books[0].title=='Example Book'
    assert books[0].authors==['Example Author'] or books[0].authors==('Example Author',)
    assert books[0].acquisitions[0].url=='http://example.test/get/epub/1/Library'
    assert next_url=='http://example.test/opds/all?offset=570'
    assert 'Visible description.' in books[0].description


def test_cdata_comment_text_preserved_even_during_recovery():
    xml=feed('<p><![CDATA[<!--literal -- text-->]]></p><!--bad -- comment-->')
    root=atom_root(xml)
    assert '<!--literal -- text-->' in ''.join(root.itertext())


@pytest.mark.parametrize('description', ['<p>unescaped & value</p><!--bad -- comment-->',
                                         '<p>broken</div><!--bad -- comment-->',
                                         '<!--unclosed -- comment'])
def test_other_malformed_xml_is_still_rejected(description):
    with pytest.raises(FeedError):
        atom_root(feed(description))


def test_entity_declarations_still_rejected():
    xml=b'<!DOCTYPE feed [<!ENTITY secret "expanded">]>'+feed('<p>&secret;</p><!--bad -- comment-->')
    with pytest.raises(FeedError):
        atom_root(xml)

@pytest.mark.parametrize('end', ['?>', '/??>'])
def test_office_namespace_fragment_does_not_drop_description_or_book(end):
    artifact = '<?xml:namespace prefix = o ns = "urn:schemas-microsoft-com:office:office" '+end
    xml=feed('<p>'+artifact+'Visible description.</p>')
    books,_=parse_feed(xml,'http://example.test/opds/all','http://example.test','Library')
    assert len(books)==1 and 'Visible description.' in books[0].description
    assert books[0].title=='Example Book'


def test_office_fragment_in_cdata_is_preserved():
    literal='<?xml:namespace prefix = o ns = "office" /??>'
    root=atom_root(feed('<p><![CDATA['+literal+']]></p><!--bad -- comment-->'))
    assert literal in ''.join(root.itertext())


def test_unrelated_invalid_processing_instruction_is_not_repaired():
    with pytest.raises(FeedError):
        atom_root(feed('<p><?other:namespace invalid /??></p><!--bad -- comment-->'))
