from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import requests
from calibre.client import Client, Library, ClassifiedError, HttpError, classify_failure, retry_after_seconds
from calibre.library_state import LibraryState, library_key
from calibre.cache import CatalogManager, CatalogStore
from config import Settings
from test_refresh_runtime import configure, library_server, finish

@pytest.mark.parametrize('error,category', [
    (requests.exceptions.SSLError('secret URL'), 'configuration'),
    (requests.ConnectionError('secret URL'), 'connectivity'),
    (requests.exceptions.ProxyError('secret URL'), 'configuration'),
    (requests.Timeout(), 'connectivity'),
    (HttpError(401), 'configuration'), (HttpError(429), 'temporary'),
    (HttpError(503), 'temporary'), (HttpError(404), 'catalog'),
    (ClassifiedError('deadline', 'deadline'), 'deadline')])
def test_failure_categories(error, category):
    assert classify_failure(error) == category


def test_scan_retirement_interval_reset_and_restore(tmp_path, book):
    settings = Settings.load(configure(tmp_path, [book.source], library_scan_failure_limit=2))
    state = LibraryState(settings)
    library = settings.libraries()[0][1]
    state.reconcile()
    store = CatalogStore(settings.catalog_cache_dir)
    store.publish(library, [book], datetime.now(timezone.utc).isoformat())
    state.scan_outcome(library, category='connectivity', detail='timeout')
    state.scan_outcome(library, category='connectivity', detail='timeout')
    key = library_key(library)
    assert state.load()['active'][key]['scan_failure_count'] == 1
    state.scan_outcome(library, success=True)
    assert state.load()['active'][key]['scan_failure_count'] == 0
    state.scan_outcome(library, category='connectivity')
    state.scan_outcome(library, category='configuration')
    assert state.load()['active'][key]['scan_failure_count'] == 0
    state.scan_outcome(library, category='connectivity')
    data = state.load()
    data['active'][key]['last_counted_scan_failure'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    state.save(data)
    state.scan_outcome(library, category='connectivity', detail='refused')
    state.purge_pending()
    assert not settings.raw_libraries()
    assert store.read(library)[0] is None
    record = state.load()['history'][-1]
    assert record['reason'] == 'scan-connectivity' and record['scan_failure_count'] == 2
    state.restore(all_retired=True)
    assert state.load()['active'][key]['scan_failure_count'] == 0
    assert [line for line,_ in settings.raw_libraries()] == [book.source]


def test_worker_classification_reaches_parent_and_retires(tmp_path):
    with library_server() as fixture:
        url = fixture[0]
    # Closed local listening port: real subprocess sees refused connection.
    manager = CatalogManager(configure(tmp_path, [url], library_scan_failure_limit=1))
    try:
        assert manager.refresh(force=True)
        finish(manager)
        status = manager.status()
        assert status['libraries'] == []
        assert status['retired'][-1]['reason'] == 'scan-connectivity'
        assert status['batch']['failed'] == 1
    finally:
        manager.close()


def test_tls_diagnostic_does_not_expose_credentials():
    class Session:
        def get(self, *args, **kwargs):
            raise requests.exceptions.SSLError('https://secret:password@host/token')
    client = Client(Library('https://example.test'), session=Session())
    with pytest.raises(ClassifiedError) as caught:
        client.get('https://example.test/opds')
    assert caught.value.category == 'configuration'
    assert 'secret' not in str(caught.value) and 'password' not in str(caught.value)


def test_retry_after_date_and_seconds():
    assert retry_after_seconds('120') == 120
    assert retry_after_seconds('bad') == 0
    assert retry_after_seconds('-1') == 0
    from email.utils import format_datetime
    assert retry_after_seconds(format_datetime(datetime.now(timezone.utc)+timedelta(minutes=5))) > 290

@pytest.mark.parametrize('error,category', [
    (HttpError(401), 'configuration'),
    (ClassifiedError('Invalid catalog navigation', 'catalog'), 'catalog'),
    (ClassifiedError('TLS validation failed', 'configuration'), 'configuration'),
    (ClassifiedError('Scan deadline', 'deadline'), 'deadline'),
    (HttpError(503), 'temporary')])
def test_non_connectivity_scan_failures_remain_configured(tmp_path, error, category, monkeypatch):
    config = configure(tmp_path, ['http://127.0.0.1:9'], library_scan_failure_limit=1)
    manager = CatalogManager(config)
    def fail(*args):
        raise error
    monkeypatch.setattr(manager, 'run_worker', fail)
    try:
        manager.refresh(force=True)
        finish(manager)
        status = manager.status()
        assert len(status['libraries']) == 1 and not status['retired']
        assert status['libraries'][0]['health'] == category
        assert status['libraries'][0]['scan_failure_count'] == 0
        if category == 'temporary':
            settings = Settings.load(config)
            library = settings.libraries()[0][1]
            entry = CatalogStore(settings.catalog_cache_dir).entry(library)
            assert datetime.fromisoformat(entry['retry_not_before']) > datetime.now(timezone.utc)
            manager.refresh(force=True)
            finish(manager)
            assert manager.status()['batch']['skipped'] == 1
    finally:
        manager.close()


def test_connection_diagnostics_are_safe_and_specific():
    import errno
    from calibre.client import connection_diagnostic
    cause = ConnectionRefusedError(errno.ECONNREFUSED, 'secret password URL')
    error = requests.ConnectionError(cause)
    assert connection_diagnostic(error) == 'Library connection failed: connection refused'
