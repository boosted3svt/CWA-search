from pathlib import Path
import os
import re
import struct
import tempfile
import logging
import zipfile
import requests
from .client import ClientError


SUPPORTED_FORMATS = ('epub', 'mobi', 'azw', 'azw3', 'pdf')


class DownloadError(ClientError):
    pass


def safe_name(title):
    name = re.sub(r'[^\w .()-]', '_', title).strip(' .')[:100].rstrip(' .')
    # Filesystem limits apply to bytes, not Unicode character counts.
    name = name.encode('utf-8')[:100].decode('utf-8', errors='ignore').rstrip(' .')
    return name or 'book'


def book_filename(book, fmt):
    """Use catalog metadata, not remote filenames, with a scoped ID for uniqueness."""
    from .matcher import normalize
    title = safe_name(book.title)
    # Preserve readable capitalization while normalizing explicit surname-first names.
    names = []
    for author in book.authors:
        parts = author.split(',')
        if len(parts) == 2 and all(part.strip() for part in parts) and normalize(parts[1]) not in ('jr', 'sr', 'ii', 'iii', 'iv'):
            author = parts[1].strip() + ' ' + parts[0].strip()
        if normalize(author) and not any(normalize(author) == normalize(name) for name in names):
            names.append(author)
    absent = [name for name in names if f' {normalize(name)} ' not in f' {normalize(title)} ']
    if absent:
        title += ' - ' + safe_name(', '.join(absent))
    return f'{title} [{book.key}].{fmt}'


def validate(path, fmt):
    with open(path, 'rb') as handle:
        header = handle.read(256)
    if fmt == 'epub':
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > 500_000_000:
                    raise DownloadError('EPUB exceeds unpacked size/file limit')
                info = archive.getinfo('mimetype')
                if info.file_size > 100 or archive.read(info) != b'application/epub+zip':
                    raise DownloadError('Invalid EPUB mimetype')
                if 'META-INF/container.xml' not in archive.namelist():
                    raise DownloadError('EPUB lacks container metadata')
                if archive.testzip() is not None:
                    raise DownloadError('Corrupt EPUB archive')
        except (zipfile.BadZipFile, KeyError, RuntimeError, OSError, NotImplementedError) as exc:
            raise DownloadError('Invalid EPUB archive') from exc
    elif fmt in ('mobi', 'azw', 'azw3'):
        # Palm database BOOK/MOBI signature and a MOBI header in the first record.
        if len(header) < 86 or header[60:68] != b'BOOKMOBI':
            raise DownloadError('Invalid MOBI/Palm database signature')
        records = struct.unpack('>H', header[76:78])[0]
        offset = struct.unpack('>I', header[78:82])[0]
        if not records or offset < 78 + records * 8 or offset + 20 > path.stat().st_size:
            raise DownloadError('Invalid MOBI record table')
        with open(path, 'rb') as handle:
            handle.seek(offset + 16)
            if handle.read(4) != b'MOBI':
                raise DownloadError('Missing MOBI record header')
    elif fmt == 'pdf':
        if not header.startswith(b'%PDF-'):
            raise DownloadError('Invalid PDF signature')
    else:
        raise DownloadError(f'Content validation is unsupported for format {fmt}')


def download(client, book, acquisition, destination, max_bytes=100_000_000, on_warning=None):
    if acquisition not in book.acquisitions:
        raise DownloadError('Acquisition is not associated with the selected book')
    if acquisition.format not in SUPPORTED_FORMATS:
        raise DownloadError('Unsupported download format')
    directory = Path(destination).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    filename = book_filename(book, acquisition.format)
    target = directory / filename
    if target.exists():
        raise DownloadError('Destination already exists; refusing to overwrite')
    response, _ = client.get(acquisition.url)
    temporary = None
    try:
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type or 'application/xhtml' in content_type:
            raise DownloadError('Server returned a web page instead of an ebook')
        length = response.headers.get('Content-Length')
        expected = int(length) if length and length.isdigit() else None
        if expected is not None and expected > max_bytes:
            raise DownloadError('Download exceeds size limit')
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.part', delete=False) as handle:
            temporary = Path(handle.name)
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise DownloadError('Download exceeds size limit')
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not size or (expected is not None and size != expected):
            raise DownloadError('Empty or incomplete download')
        validate(temporary, acquisition.format)
        if acquisition.length is not None and size != acquisition.length:
            message = (f'OPDS advertised {acquisition.length} bytes; downloaded {size} bytes')
            if acquisition.format != 'epub':
                raise DownloadError(message + '; download size differs from the OPDS acquisition length')
            # Calibre can update EPUB metadata during delivery. The catalog length
            # may describe the stored file, not the complete delivered ZIP.
            warning = message + '. EPUB ZIP integrity and container checks passed; keeping the file.'
            logging.getLogger(__name__).warning(warning)
            if on_warning:
                on_warning(warning)
        # Hard-link publication is atomic and fails if another process created target.
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise DownloadError('Destination already exists; refusing to overwrite') from exc
        return target
    except requests.RequestException as exc:
        raise DownloadError('Download transfer failed') from exc
    finally:
        response.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
