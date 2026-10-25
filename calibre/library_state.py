"""Persistent lifecycle and timeout state; never stores authentication values."""
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
import re
from pathlib import Path
import shutil
import uuid
from urllib.parse import urlencode
from .client import Library
from .models import Book
from .state import atomic_text, state_lock, try_state_lock


def library_key(library):
    return hashlib.sha256((library.url + '\n' +
                           (library.library_id if library.explicit_id else '')).encode()).hexdigest()[:24]


def now():
    return datetime.now(timezone.utc).isoformat()


def source_lock(directory, source):
    return Path(directory) / 'source-locks' / hashlib.sha256(source.encode()).hexdigest()


class LibraryState:
    def __init__(self, settings, report=lambda message: None):
        self.settings = settings
        self.directory = Path(settings.catalog_cache_dir)
        self.path = self.directory / 'libraries.json'
        self.gate = self.directory / 'lifecycle'
        self.report = report

    def load(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else {'active': {}, 'history': [], 'pending': {}}
        keys = list(data['active']) + list(data['pending']) + [item['key'] for item in data['history']]
        if any(not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{24}', key) for key in keys):
            raise ValueError('Invalid library identity in lifecycle state')
        return data

    def save(self, data):
        atomic_text(self.path, json.dumps(data, indent=2))

    def configured(self):
        # A missing file must not be interpreted as an intentional empty list.
        return {library_key(lib): (line, lib) for line, lib in self.settings.libraries()}

    def new_entry(self, line, library):
        return {'line': line, 'source': library.url, 'library_id': library.library_id,
                'explicit_id': library.explicit_id, 'parent_url': library.parent_url, 'timeout_count': 0, 'scan_failure_count': 0, 'language_failure_count': 0, 'generation': uuid.uuid4().hex}

    def remove_entry(self, data, key, reason):
        entry = data['active'].pop(key)
        record = {**entry, 'id': uuid.uuid4().hex, 'key': key, 'retired_at': now(), 'reason': reason}
        data['history'].append(record)
        data['pending'][key] = record
        self.report(f'Deactivated {entry["source"]} [library {entry["library_id"] or "default"}]: {reason}; {entry["timeout_count"]} download timeouts; {entry.get("scan_failure_count", 0)} connectivity scan failures')

    def reconcile(self):
        with state_lock(self.gate):
            data = self.load()
            from .servers import reactivate_readded_servers
            reactivate_readded_servers(self.settings, data['active'], data['pending'])
            configured = self.configured()
            # Adopt old manifest entries on upgrade, including absent libraries.
            from .cache import CatalogStore
            for key, entry in CatalogStore(self.directory).manifest()['libraries'].items():
                if not re.fullmatch(r'[0-9a-f]{24}', key):
                    raise ValueError('Invalid library identity in catalog index')
                if key in data['active'] or key in data['pending']:
                    continue
                source = entry['source']
                library = Library(source, entry.get('configured_library_id', ''))
                explicit = Library(source, library.library_id, explicit_id=True)
                if library.library_id and library_key(explicit) == key:
                    library = explicit
                line = source + ('?' + urlencode({'library_id': library.library_id}) if library.explicit_id else '')
                if key in configured:
                    line, library = configured[key]
                data['active'][key] = self.new_entry(line, library)
            # An interrupted first scan can have a checkpoint but no manifest entry.
            for cursor in (self.directory / 'checkpoints').glob('*/cursor.json'):
                key = cursor.parent.name
                if key in data['active'] or key in data['pending']:
                    continue
                identity = json.loads(cursor.read_text()).get('identity', {})
                if not identity.get('source'):
                    continue
                library = Library(identity['source'], identity.get('library_id', ''),
                                  explicit_id=identity.get('explicit_id', False))
                if library_key(library) != key:
                    raise ValueError('Invalid library identity in checkpoint')
                line = library.url + ('?' + urlencode({'library_id': library.library_id}) if library.explicit_id else '')
                if key in configured:
                    line, library = configured[key]
                data['active'][key] = self.new_entry(line, library)
            for key in list(data['active']):
                if key not in configured:
                    raw_sources = {lib.url for _, lib in self.settings.raw_libraries()}
                    reason = 'library-unadvertised' if data['active'][key].get('parent_url') and data['active'][key]['source'] in raw_sources else 'manual-removal'
                    if reason == 'manual-removal':
                        from .library_list import update
                        update(self.settings, data['active'][key]['line'], active=False)
                    self.remove_entry(data, key, reason)
            for key, (line, library) in configured.items():
                if key not in data['active'] and key not in data['pending']:
                    data['active'][key] = self.new_entry(line, library)
                    for record in data['history']:
                        if record['key'] == key and not record.get('restored_at'):
                            record['restored_at'] = now()
                elif key in data['active']:
                    data['active'][key]['line'] = line
            self.save(data)
        self.purge_pending()

    def allowed(self, library, generation=None):
        key = library_key(library)
        entry = self.load()['active'].get(key)
        if not entry or (generation and entry['generation'] != generation):
            return False
        return key in self.configured()

    def purge_pending(self):
        from .cache import CatalogStore
        for key, retired in list(self.load()['pending'].items()):
            with try_state_lock(self.directory / 'locks' / key) as acquired, \
                    try_state_lock(source_lock(self.directory, retired['source'])) as source_idle:
                if not acquired or not source_idle:
                    continue
                with state_lock(self.gate):
                    data = self.load()
                    if key not in data['pending']:
                        continue
                    store = CatalogStore(self.directory)
                    with state_lock(store.index):
                        manifest = store.manifest()
                        source = retired['source']
                        candidates, keep = set(), set()
                        for candidate_key, entry in manifest['libraries'].items():
                            if entry['source'] != source or not entry.get('snapshot'):
                                continue
                            path = self.directory / entry['snapshot']
                            if path.parent.resolve() != self.directory.resolve():
                                raise ValueError('Invalid catalog snapshot path')
                            target = candidates if candidate_key == key else keep
                            target.update(Book.from_dict(item).key for item in json.loads(path.read_text())['books'])
                        for cursor in (self.directory / 'checkpoints').glob('*/cursor.json'):
                            state = json.loads(cursor.read_text())
                            if state.get('identity', {}).get('source') != source:
                                continue
                            target = candidates if cursor.parent.name == key else keep
                            for index in range(state['page_count']):
                                target.update(Book.from_dict(item).key for item in
                                              json.loads((cursor.parent / f'page_{index:06d}.json').read_text()))
                        manifest['libraries'].pop(key, None)
                        atomic_text(store.index, json.dumps(manifest, indent=2))
                        for path in self.directory.glob(key + '_*.json'):
                            path.unlink(missing_ok=True)
                        for suffix in ('.json','.jsonl'):
                            (self.directory/'cover-jobs'/(key+suffix)).unlink(missing_ok=True)
                        checkpoint = self.directory / 'checkpoints' / key
                        if checkpoint.exists():
                            shutil.rmtree(checkpoint)
                        for cover_key in candidates - keep:
                            (self.directory / 'covers' / (cover_key + '.img')).unlink(missing_ok=True)
                            (self.directory / 'covers' / (cover_key + '.json')).unlink(missing_ok=True)
                    data['pending'].pop(key)
                    self.save(data)
                    self.report(f'Purged cached catalog, checkpoints, and {len(candidates - keep)} unshared covers for {source}')

    def remove_configured(self, key):
        from .servers import exclude
        selection = self.configured().get(key)
        if selection:
            library = selection[1]
            handled, remove_server = exclude(self.settings, library)
            if handled and not remove_server:
                return
            if handled:
                key = library_key(replace(library, library_id='', explicit_id=False, parent_url=''))
        from .library_list import update
        for line, library in self.settings.raw_libraries():
            if library_key(library) == key:
                update(self.settings, line, active=False)
                break

    def scan_outcome(self, library, success=False, category=None, detail=None, attempt_started=None, count_scrape=True):
        """At most one connectivity count per refresh interval, including manual scans."""
        key = library_key(library)
        with state_lock(self.gate):
            data = self.load()
            entry = data['active'].get(key)
            if not entry or key not in self.configured():
                return
            # Metadata publication and worker completion can report the same
            # attempt several times. Count one terminal metadata outcome only.
            attempt = attempt_started or uuid.uuid4().hex
            if count_scrape and category != 'cancelled' and entry.get('last_scrape_attempt') != attempt:
                from .library_list import update
                from .cache import CatalogStore
                manifest = CatalogStore(self.directory).manifest()['libraries']
                selection = self.settings.library(entry['line'])
                matching = [row for row in manifest.values() if row['source'] == selection.url
                            and (not selection.explicit_id or row.get('configured_library_id') == selection.library_id)]
                total = sum(row.get('book_count', 0) for row in matching) if success and matching else None
                update(self.settings, entry['line'], fails=int(not success), scrapes=int(success), total=total)
                entry['last_scrape_attempt'] = attempt
            entry['scan_category'] = 'healthy' if success else category
            entry['scan_detail'] = None if success else detail
            entry['last_scan_outcome'] = now()
            if success:
                entry['scan_failure_count'] = 0
                entry['language_failure_count'] = 0
                entry.pop('last_counted_language_failure', None)
                entry.pop('last_counted_scan_failure', None)
            elif category == 'connectivity':
                stamp = entry.get('last_counted_scan_failure')
                attempt_time = datetime.fromisoformat(attempt_started) if attempt_started else datetime.now(timezone.utc)
                elapsed = ((attempt_time - datetime.fromisoformat(stamp)).total_seconds()
                           if stamp else float('inf'))
                if elapsed >= self.settings.catalog_refresh_interval_hours * 3600:
                    entry['scan_failure_count'] = entry.get('scan_failure_count', 0) + 1
                    entry['last_counted_scan_failure'] = attempt_time.isoformat()
            elif category not in ('cancelled', 'deadline'):
                # A server response breaks the sequence of unreachable scans.
                entry['scan_failure_count'] = 0
                entry.pop('last_counted_scan_failure', None)
            if category == 'language':
                stamp = entry.get('last_counted_language_failure')
                attempt_time = datetime.fromisoformat(attempt_started) if attempt_started else datetime.now(timezone.utc)
                elapsed = (attempt_time - datetime.fromisoformat(stamp)).total_seconds() if stamp else float('inf')
                if elapsed >= self.settings.catalog_refresh_interval_hours * 3600:
                    entry['language_failure_count'] = entry.get('language_failure_count',0) + 1
                    entry['last_counted_language_failure'] = attempt_time.isoformat()
                language_limit = self.settings.library_language_failure_limit
                if language_limit and entry.get('language_failure_count',0) >= language_limit:
                    self.remove_configured(key)
                    self.remove_entry(data,key,'library-language')
            limit = self.settings.library_scan_failure_limit
            if category == 'connectivity' and limit and entry.get('scan_failure_count', 0) >= limit:
                self.remove_configured(key)
                self.remove_entry(data, key, 'scan-connectivity')
            self.save(data)

    def outcome(self, library, success=False, timed_out=False):
        """One count per terminal operation, and success resets consecutive timeouts."""
        if not success and not timed_out:
            return None
        self.reconcile()
        key = library_key(library)
        with state_lock(self.gate):
            data = self.load()
            entry = data['active'].get(key)
            if not entry or key not in self.configured():
                return None
            entry['timeout_count'] = 0 if success else entry['timeout_count'] + 1
            self.report(f'{library.url}: consecutive download timeouts = {entry["timeout_count"]}')
            limit = self.settings.library_download_timeout_limit
            retired = not success and limit > 0 and entry['timeout_count'] >= limit
            if retired:
                self.remove_configured(key)
                self.remove_entry(data, key, 'download-timeouts')
            count = entry['timeout_count']
            self.save(data)
        self.purge_pending()
        return {'timeout_count': count, 'retired': retired, 'limit': limit}

    def restore(self, record_id=None, all_retired=False):
        self.purge_pending()
        with state_lock(self.gate):
            data = self.load()
            records = [item for item in data['history'] if not item.get('restored_at') and
                       ((all_retired and item['reason'] in ('download-timeouts', 'scan-connectivity', 'library-language')) or item['id'] == record_id)]
            if not records:
                raise ValueError('No retired entries selected')
            if any(item['reason'] == 'library-unadvertised' for item in records):
                raise ValueError('Library no longer advertised; refresh its server to discover it again')
            if any(item['key'] in data['pending'] for item in records):
                raise ValueError('A removed scan is still stopping; retry restoration shortly')
            from .library_list import update
            from .servers import restore_selection
            for item in records:
                restore_selection(self.settings, item)
                update(self.settings, item['line'], active=True)
                library = replace(self.settings.library(item['line']), library_id=item['library_id'],
                                  explicit_id=item['explicit_id'], parent_url=item.get('parent_url',''))
                data['active'][item['key']] = self.new_entry(item['line'], library)
                item['restored_at'] = now()
            self.save(data)
        self.report(f'Restored {len(records)} retired entries with counters reset')
        return len(records)

    def clear_history(self):
        with state_lock(self.gate):
            data = self.load()
            data['history'] = []
            self.save(data)
        self.report('Cleared retirement history; no servers re-added')
