"""Bounded thumbnail retrieval; never serve SVG/HTML as images."""
import requests
from .client import ClientError


def image_type(body):
    if body.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if body.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if body[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        return 'image/webp'
    raise ClientError('Unsupported cover image')


def fetch_image(client, url):
    response, _ = client.get(url)
    try:
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise ClientError('Cover exceeds size limit')
        body = bytes(body)
        return body, image_type(body)
    except requests.RequestException as exc:
        raise ClientError('Cover transfer failed') from exc
    finally:
        response.close()
