"""Persistent lifecycle and timeout state; never stores authentication values."""
from datetime import datetime, timezone
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
                'explicit_id': library.explicit_id, 'timeout_count': 0, 'generation': uuid.uuid4().hex}

    def remove_entry(self, data, key, reason):
        entry = data['active'].pop(key)
        record = {**entry, 'id': uuid.uuid4().hex, 'key': key, 'retired_at': now(), 'reason': reason}
        data['history'].append(record)
        data['pending'][key] = record
        self.report(f'Removed {entry["source"]}: {reason}; {entry["timeout_count"]} consecutive download timeouts')

    def reconcile(self):
        with state_lock(self.gate):
            configured = self.configured()
            data = self.load()
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
                data['active'][key] = self.new_entry(line, library)
            for key in list(data['active']):
                if key not in configured:
                    self.remove_entry(data, key, 'manual-removal')
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
                        checkpoint = self.directory / 'checkpoints' / key
                        if checkpoint.exists():
                            shutil.rmtree(checkpoint)
                        for cover_key in candidates - keep:
                            (self.directory / 'covers' / (cover_key + '.img')).unlink(missing_ok=True)
                            (self.directory / 'covers' / (cover_key + '.json')).unlink(missing_ok=True)
                    data['pending'].pop(key)
                    self.save(data)
                    self.report(f'Purged cached catalog, checkpoints, and {len(candidates - keep)} unshared covers for {source}')

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
                path = Path(self.settings.libraries_file)
                with state_lock(path):
                    original = path.read_text()
                    kept = []
                    for line in original.splitlines(keepends=True):
                        if line.strip() and not line.lstrip().startswith('#'):
                            try:
                                if library_key(self.settings.library(line)) == key:
                                    continue
                            except ValueError:
                                pass
                        kept.append(line)
                    atomic_text(path.with_name(path.name + '.bak'), original)
                    atomic_text(path, ''.join(kept))
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
                       ((all_retired and item['reason'] == 'download-timeouts') or item['id'] == record_id)]
            if not records:
                raise ValueError('No retired entries selected')
            if any(item['key'] in data['pending'] for item in records):
                raise ValueError('A removed scan is still stopping; retry restoration shortly')
            path = Path(self.settings.libraries_file)
            with state_lock(path):
                original = path.read_text()
                configured = self.configured()
                additions = []
                for item in records:
                    if item['key'] not in configured:
                        additions.append(item['line'])
                        library = self.settings.library(item['line'])
                        data['active'][item['key']] = self.new_entry(item['line'], library)
                        configured[item['key']] = (item['line'], library)
                    else:
                        if item['key'] not in data['active']:
                            line, library = configured[item['key']]
                            data['active'][item['key']] = self.new_entry(line, library)
                        else:
                            data['active'][item['key']]['timeout_count'] = 0
                    item['restored_at'] = now()
                if additions:
                    atomic_text(path, original + ('\n' if original and not original.endswith('\n') else '') +
                                '\n'.join(additions) + '\n')
            self.save(data)
        self.report(f'Restored {len(records)} retired entries with counters reset')
        return len(records)

    def clear_history(self):
        with state_lock(self.gate):
            data = self.load()
            data['history'] = []
            self.save(data)
        self.report('Cleared retirement history; no servers re-added')
