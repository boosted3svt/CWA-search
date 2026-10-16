"""Persist advertised library selections without editing the user's server list."""
from dataclasses import replace
import json
from pathlib import Path
from .state import atomic_text, state_lock


def registry_path(settings):
    return Path(settings.catalog_cache_dir) / 'servers.json'


def registry(settings):
    path = registry_path(settings)
    return json.loads(path.read_text()) if path.exists() else {}


def advertise(settings, source, ids, default_id=None, authoritative=True, names=None):
    ids = sorted(set(ids) - {''})
    if not ids:
        return
    path = registry_path(settings)
    with state_lock(path):
        data = json.loads(path.read_text()) if path.exists() else {}
        previous = data.get(source, {})
        if not authoritative:
            ids = sorted(set(ids + previous.get('ids', [])))
        default_id = default_id or previous.get('default_id') or ids[0]
        ids = sorted(set(ids + [default_id]))
        data[source] = {'ids': ids, 'default_id': default_id,
                        'excluded': previous.get('excluded', []),
                        'names': {**previous.get('names', {}), **(names or {})}}
        atomic_text(path, json.dumps(data, indent=2))


def expand(settings, entries):
    data = registry(settings)
    result, seen = [], set()
    for line, library in entries:
        info = data.get(library.url, {}) if not library.explicit_id else {}
        choices = []
        if not info:
            choices = [library]
        else:
            default = info['default_id']
            if default not in info.get('excluded', []):
                # Keep the existing default key and checkpoint identity unchanged.
                choices.append(library)
            choices.extend(replace(library, library_id=identifier, explicit_id=True,
                                   parent_url=library.url)
                           for identifier in info['ids']
                           if identifier != default and identifier not in info.get('excluded', []))
        for choice in choices:
            identity = (choice.url, choice.library_id if choice.explicit_id else info.get('default_id', ''))
            if identity not in seen:
                seen.add(identity)
                result.append((line, choice))
    return result


def exclude(settings, library):
    """Return (handled, remove_server). Only retire the parent after all children."""
    path = registry_path(settings)
    with state_lock(path):
        data = json.loads(path.read_text()) if path.exists() else {}
        info = data.get(library.url)
        if not info or (library.explicit_id and not library.parent_url):
            return False, False
        identifier = library.library_id if library.explicit_id else info['default_id']
        info['excluded'] = sorted(set(info.get('excluded', []) + [identifier]))
        atomic_text(path, json.dumps(data, indent=2))
        return True, set(info['ids']).issubset(info['excluded'])


def restore_selection(settings, record):
    path = registry_path(settings)
    with state_lock(path):
        data = json.loads(path.read_text()) if path.exists() else {}
        info = data.get(record['source'])
        if info and (record.get('parent_url') or not record.get('explicit_id')):
            identifier = record['library_id'] if record.get('explicit_id') else info['default_id']
            info['excluded'] = [value for value in info.get('excluded', []) if value != identifier]
            atomic_text(path, json.dumps(data, indent=2))


def search_discovery(settings, library, entries):
    """Append newly advertised siblings to an ongoing live-search queue."""
    if library.explicit_id and not library.parent_url:
        return None
    def observed(ids, default_id):
        if not ids:
            return
        from .library_state import LibraryState, library_key
        advertise(settings, library.url, ids, default_id, authoritative=not library.explicit_id)
        LibraryState(settings).reconcile()
        known = {library_key(lib) for _, lib in entries}
        for line, lib in settings.libraries():
            if lib.url == library.url and library_key(lib) not in known:
                entries.append((line, lib))
                known.add(library_key(lib))
    return observed


def reactivate_readded_servers(settings, active, pending):
    """A manually re-added bare server starts fresh after prior cleanup finished."""
    sources = {lib.url for _, lib in settings.raw_libraries() if not lib.explicit_id}
    occupied = {entry['source'] for entry in list(active.values()) + list(pending.values())}
    candidates = sources - occupied
    if not candidates:
        return
    path = registry_path(settings)
    if not path.exists():
        return
    with state_lock(path):
        data = json.loads(path.read_text())
        changed = False
        for source in candidates:
            info = data.get(source)
            if info and info.get('excluded'):
                info['excluded'] = []
                changed = True
        if changed:
            atomic_text(path, json.dumps(data, indent=2))
