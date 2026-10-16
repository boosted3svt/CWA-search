from dataclasses import replace
import io
import zipfile
import pytest
import requests
from calibre.client import Client, Library
from calibre.downloader import download, DownloadError
from conftest import Session, Response


def client(body, headers=None, error=None):
    return Client(Library('http://books.test'), session=Session([Response(body, headers=headers, error=error)]))


def test_mobi_download_atomic_and_no_overwrite(tmp_path, book, mobi):
    book.title = '../../Unsafe / book'
    acquisition = book.acquisitions[1]
    acquisition.length = len(mobi)
    path = download(client(mobi, {'Content-Length': str(len(mobi))}), book, acquisition, tmp_path)
    assert path.parent == tmp_path
    assert path.read_bytes() == mobi
    assert not list(tmp_path.glob('*.part'))
    with pytest.raises(DownloadError, match='overwrite'):
        download(client(mobi), book, acquisition, tmp_path)


@pytest.mark.parametrize('body,headers', [(b'<html>login</html>', {}), (b'x', {'Content-Type': 'text/html'}),
                                        (b'', {}), (b'bad', {'Content-Length': '100'})])
def test_invalid_download_cleans_temp(tmp_path, book, body, headers):
    with pytest.raises(DownloadError):
        download(client(body, headers), book, book.acquisitions[1], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_size_limits_and_advertised_length(tmp_path, book, mobi):
    with pytest.raises(DownloadError, match='size limit'):
        download(client(mobi), book, book.acquisitions[1], tmp_path, max_bytes=20)
    book.acquisitions[1].length = 900
    path = download(client(mobi), book, book.acquisitions[1], tmp_path)
    assert path.read_bytes() == mobi


def test_epub_validation(tmp_path, book):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container/>')
    path = download(client(buffer.getvalue()), book, book.acquisitions[0], tmp_path)
    assert zipfile.is_zipfile(path)


def test_valid_epub_with_stale_catalog_length(tmp_path, book, caplog):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container/>')
    body = buffer.getvalue()
    book.acquisitions[0].length = len(body) + 100
    path = download(client(body, {'Content-Length': str(len(body))}), book, book.acquisitions[0], tmp_path)
    assert path.read_bytes() == body
    assert f'OPDS advertised {len(body) + 100} bytes; downloaded {len(body)} bytes' in caplog.text
    assert 'checks passed' in caplog.text


def test_epub_http_length_mismatch_still_fails(tmp_path, book):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container/>')
    body = buffer.getvalue()
    with pytest.raises(DownloadError, match='incomplete'):
        download(client(body, {'Content-Length': str(len(body) + 100)}), book, book.acquisitions[0], tmp_path)
    assert not list(tmp_path.iterdir())


def test_invalid_epub_with_stale_length_still_fails(tmp_path, book):
    book.acquisitions[0].length = 1000
    with pytest.raises(DownloadError, match='Invalid EPUB'):
        download(client(b'not a ZIP archive'), book, book.acquisitions[0], tmp_path)
    assert not list(tmp_path.iterdir())


def test_transfer_failure(tmp_path, book):
    with pytest.raises(DownloadError, match='transfer'):
        download(client(b'', error=requests.ConnectionError()), book, book.acquisitions[1], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_unassociated_link(tmp_path, book, mobi):
    with pytest.raises(DownloadError, match='associated'):
        download(client(mobi), book, replace(book.acquisitions[1], url='http://evil.test/book'), tmp_path)


def test_filename_contains_title_and_author_from_catalog(tmp_path, book, mobi):
    book.title = 'The God Dare'
    book.authors = ['Battistelli, Kate']
    path = download(client(mobi), book, book.acquisitions[1], tmp_path)
    assert path.name == f'The God Dare - Kate Battistelli [{book.key}].mobi'


def test_filename_does_not_repeat_author_already_in_title(book):
    from calibre.downloader import book_filename
    book.title = 'The God Dare by Kate Battistelli'
    book.authors = ['Battistelli, Kate', 'Kate Battistelli']
    assert book_filename(book, 'epub').count('Kate Battistelli') == 1


def test_filename_multiple_missing_and_unsafe_authors(book):
    from calibre.downloader import book_filename
    book.title = 'Example Book'
    book.authors = ['Emily Rath', 'Other Author', 'Emily Rath']
    name = book_filename(book, 'epub')
    assert 'Emily Rath' in name and 'Other Author' in name
    assert name.count('Emily Rath') == 1
    book.authors = ['../../Bad / Name']
    assert '/' not in book_filename(book, 'epub')
    book.authors = []
    assert book_filename(book, 'epub') == f'Example Book [{book.key}].epub'


def test_unicode_filename_fits_filesystem_limit(book):
    from calibre.downloader import book_filename
    book.title = '書' * 100
    book.authors = ['作' * 100]
    assert len(book_filename(book, 'epub').encode('utf-8')) <= 255


@pytest.mark.parametrize('fmt',['mobi','azw','azw3'])
@pytest.mark.parametrize('delta',[-100,100])
def test_valid_palm_download_with_stale_catalog_size_is_saved_with_warning(tmp_path,book,mobi,fmt,delta):
    acquisition=replace(book.acquisitions[1],format=fmt,length=len(mobi)+delta)
    book.acquisitions=[acquisition]
    warnings=[]
    path=download(client(mobi,{'Content-Length':str(len(mobi))}),book,acquisition,tmp_path,on_warning=warnings.append)
    assert path.read_bytes()==mobi
    assert warnings and 'record-table and header checks passed; keeping the file' in warnings[0]


def test_mobi_http_length_mismatch_is_still_rejected(tmp_path,book,mobi):
    book.acquisitions[1].length=len(mobi)+100
    with pytest.raises(DownloadError,match='incomplete'):
        download(client(mobi,{'Content-Length':str(len(mobi)+1)}),book,book.acquisitions[1],tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('offsets',[(100,9999),(140,120),(100,110)])
def test_invalid_palm_record_table_rejected_despite_stale_catalog_length(tmp_path,book,mobi,offsets):
    import struct
    data=bytearray(mobi)
    struct.pack_into('>H',data,76,2)
    for index,offset in enumerate(offsets):struct.pack_into('>I',data,78+index*8,offset)
    data[offsets[0]+16:offsets[0]+20]=b'MOBI'
    book.acquisitions[1].length=999
    with pytest.raises(DownloadError):
        download(client(bytes(data)),book,book.acquisitions[1],tmp_path)
    assert not list(tmp_path.iterdir())
