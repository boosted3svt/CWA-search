from pathlib import Path
import struct
import pytest
from calibre.parser import parse_feed


class Response:
    def __init__(self, body=b'', status=200, headers=None, error=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}
        self.error, self.closed = error, False

    def iter_content(self, size):
        if self.error:
            raise self.error
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


@pytest.fixture
def xml():
    return Path('tests/fixtures/search.xml').read_bytes()


@pytest.fixture
def book(xml):
    return parse_feed(xml, 'http://books.test/opds/search/emily', 'http://books.test', 'Calibre_Library')[0][0]


@pytest.fixture
def mobi():
    # Minimal structural fixture, not a readable book or a real published work.
    data = bytearray(256)
    data[60:68] = b'BOOKMOBI'
    data[76:78] = struct.pack('>H', 1)
    data[78:82] = struct.pack('>I', 86)
    data[102:106] = b'MOBI'
    return bytes(data)
