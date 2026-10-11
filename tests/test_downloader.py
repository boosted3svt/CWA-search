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
    with pytest.raises(DownloadError, match='OPDS'):
        download(client(mobi), book, book.acquisitions[1], tmp_path)


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
