"""Editable server table plus a durable register of previously seen destinations."""
import hashlib
import json
from pathlib import Path
from .client import origin
from .state import atomic_text, state_lock

HEADER = ('url', 'total', 'fails', 'scrapes', 'active')
ADDITIONS = '# New additions: paste one URL or HOST:PORT per line below.'


def identity(settings, url):
    library = settings.library(url)
    _, host, port = origin(library.url)
    # Keep deliberately selected sub-libraries independent. The protocol and
    # spelling of the hostname do not make another destination on the same port.
    value = [host, port, library.library_id if library.explicit_id else '']
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def parse(line):
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    fields = [part.strip() for part in line.split('|')]
    if fields == list(HEADER):
        return None
    if len(fields) == 1:
        return {'url': line, '_registered': False}
    if len(fields) != 5 or fields[4].lower() not in ('true', 'false'):
        raise ValueError('Library rows must be URL | total | fails | scrapes | active (true/false)')
    numbers = [int(value) for value in fields[1:4]]
    if min(numbers) < 0:
        raise ValueError('Library totals and counters cannot be negative')
    return dict(zip(HEADER, [fields[0], *numbers, fields[4].lower() == 'true']), _registered=True)


def register_path(settings):
    return Path(settings.catalog_cache_dir) / 'library-register.json'


def load(settings):
    path = register_path(settings)
    if path.exists():
        return json.loads(path.read_text())
    # Import old automatic retirements once. Retired siblings must not disable
    # their still-active parent server.
    known = {}
    lifecycle = Path(settings.catalog_cache_dir) / 'libraries.json'
    if lifecycle.exists():
        data = json.loads(lifecycle.read_text())
        occupied = {identity(settings, item['line']) for item in data.get('active', {}).values()}
        for item in data.get('history', []):
            if item.get('restored_at') or item['reason'] not in ('scan-connectivity', 'download-timeouts', 'library-language', 'manual-removal'):
                continue
            key = identity(settings, item['line'])
            if key not in occupied:
                known[key] = dict(url=item['line'], total=0, fails=0, scrapes=0, active=False)
    return known


def collect(settings, text, known):
    rows, comments, removed = {}, [], 0
    for line in text.splitlines():
        row = parse(line)
        if row is None:
            if line.lstrip().startswith('#') and line.strip() != ADDITIONS:
                comments.append(line)
            continue
        key = identity(settings, row['url'])
        if key in rows:
            removed += 1
            if not row['_registered'] or rows[key]['_registered']:
                continue
        if row['_registered']:
            selected = row
        else:
            selected = {**dict(total=0, fails=0, scrapes=0, active=True),
                        **known.get(key, {}), **row}
        rows[key] = selected
    # Retired addresses from older app versions become visible reference rows.
    for key, row in known.items():
        if not row['active'] and key not in rows:
            rows[key] = {**row, '_registered': True}
    return rows, comments, removed


def rows(settings):
    text = Path(settings.libraries_file).read_text()
    result, _, _ = collect(settings, text, load(settings))
    return [{name: row[name] for name in HEADER} for row in result.values()]


def write(settings, original, entries, comments, known):
    values = [[str(row[name]).lower() if name == 'active' else str(row[name])
               for name in HEADER] for row in entries.values()]
    widths = [max([len(name)] + [len(row[index]) for row in values])
              for index, name in enumerate(HEADER)]
    def render(row):
        return ' | '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
    text = '\n'.join([*comments, render(HEADER), *(render(row) for row in values), '', ADDITIONS, ''])
    path = Path(settings.libraries_file)
    if text != original:
        # Preserve the first pre-upgrade list, and the latest edited input.
        initial = path.with_name(path.name + '.original.bak')
        if not initial.exists():
            atomic_text(initial, original)
        atomic_text(path.with_name(path.name + '.dedup.bak'), original)
        atomic_text(path, text)
    known.update({key: {name: row[name] for name in HEADER} for key, row in entries.items()})
    encoded = json.dumps(known, indent=2)
    target = register_path(settings)
    if not target.exists() or target.read_text() != encoded:
        atomic_text(target, encoded)


def sync(settings):
    path = Path(settings.libraries_file)
    if not path.exists():
        return 0
    with state_lock(path):
        original = path.read_text()
        known = load(settings)
        entries, comments, removed = collect(settings, original, known)
        # Populate totals from existing complete catalogs when upgrading.
        index = Path(settings.catalog_cache_dir) / 'index.json'
        manifest = json.loads(index.read_text()).get('libraries', {}) if index.exists() else {}
        for row in entries.values():
            library = settings.library(row['url'])
            matching = [entry for entry in manifest.values() if entry['source'] == library.url
                        and (not library.explicit_id or entry.get('configured_library_id') == library.library_id)]
            if matching:
                row['total'] = sum(entry.get('book_count', 0) for entry in matching)
        write(settings, original, entries, comments, known)
        return removed


def update(settings, url, active=None, fails=0, scrapes=0, total=None):
    path = Path(settings.libraries_file)
    with state_lock(path):
        original = path.read_text()
        known = load(settings)
        entries, comments, _ = collect(settings, original, known)
        key = identity(settings, url)
        if key not in entries:
            entries[key] = {**dict(url=url, total=0, fails=0, scrapes=0, active=True), **known.get(key, {})}
        row = entries[key]
        if active is not None:
            row['active'] = active
        row['fails'] += fails
        row['scrapes'] += scrapes
        if total is not None:
            row['total'] = total
        write(settings, original, entries, comments, known)
