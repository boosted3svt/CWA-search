"""JSON settings and line-delimited library URLs; no credentials stored on disk."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs
from calibre.client import Library, origin


@dataclass
class Settings:
    libraries_file: str = 'libraries.txt'
    download_dir: str = 'downloads'
    history_file: str = 'state/history.json'
    library_id: str = ''
    search_path: str = '/opds/search/{searchTerms}'
    auto_discover: bool = True
    opds_path: str = '/opds'
    preferred_formats: tuple = ('mobi', 'epub', 'azw3', 'azw', 'pdf')
    timeout: float = 10
    retries: int = 1
    max_pages: int = 10
    max_results: int = 100
    max_download_bytes: int = 100_000_000
    prune_timeouts: bool = True
    allowed_hosts: tuple = ()
    username_env: str = 'CALIBRE_USERNAME'
    password_env: str = 'CALIBRE_PASSWORD'
    catalog_cache_enabled: bool = False
    catalog_refresh_interval_hours: float = 24
    catalog_cache_dir: str = 'state/catalogs'
    catalog_max_pages: int = 10000
    catalog_max_books: int = 500000
    catalog_request_delay: float = 0.25
    catalog_cache_covers: bool = False

    @classmethod
    def load(cls, filename):
        path = Path(filename).expanduser().resolve()
        data = json.loads(path.read_text()) if path.exists() else {}
        settings = cls(**data)
        for name in ('libraries_file', 'download_dir', 'history_file', 'catalog_cache_dir'):
            value = Path(getattr(settings, name)).expanduser()
            setattr(settings, name, str(value if value.is_absolute() else path.parent / value))
        if settings.timeout <= 0 or not 0 <= settings.retries <= 5:
            raise ValueError('timeout must be positive; retries must be between 0 and 5')
        if min(settings.max_pages, settings.max_results, settings.max_download_bytes) < 1:
            raise ValueError('Page, result, and download limits must be positive')
        if '{searchTerms}' not in settings.search_path:
            raise ValueError('search_path must contain {searchTerms}')
        if not settings.preferred_formats:
            raise ValueError('preferred_formats cannot be empty')
        if settings.catalog_refresh_interval_hours <= 0 or settings.catalog_request_delay < 0:
            raise ValueError('Catalog interval must be positive and request delay cannot be negative')
        if min(settings.catalog_max_pages, settings.catalog_max_books) < 1:
            raise ValueError('Catalog limits must be positive')
        if not isinstance(settings.catalog_cache_enabled, bool) or not isinstance(settings.catalog_cache_covers, bool):
            raise ValueError('Catalog enable/cover flags must be JSON true or false')
        return settings

    def library(self, line):
        url = line.strip()
        if '://' not in url:
            url = 'http://' + url
        parsed = urlsplit(url)
        origin(url)
        query = parse_qs(parsed.query)
        unknown = set(query) - {'library_id'}
        if unknown or parsed.fragment:
            raise ValueError('Library URLs may only include a library_id query parameter')
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))
        return Library(url, query.get('library_id', [self.library_id])[0],
                       self.search_path, list(self.allowed_hosts),
                       os.environ.get(self.username_env) or None, os.environ.get(self.password_env) or None,
                       explicit_id='library_id' in query)

    def libraries(self):
        path = Path(self.libraries_file)
        lines = path.read_text().splitlines()
        return [(line, self.library(line)) for line in lines if line.strip() and not line.lstrip().startswith('#')]
