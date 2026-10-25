from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from config import Settings
from calibre.cache import CatalogManager, CatalogStore, now
from calibre.library_list import rows, sync, register_path
from calibre.library_state import LibraryState, library_key
from test_refresh_runtime import configure, finish, library_server


@pytest.mark.parametrize('registered_first', [True, False])
def test_registered_inactive_row_wins_over_pasted_aliases(tmp_path, registered_first):
    settings = Settings.load(configure(tmp_path, []))
    existing = 'http://80.47.157.190:8070 | 1428 | 5 | 16 | FALSE\n'
    additions = '80.47.157.190:8070\nhttps://80.47.157.190:8070/\n'
    path = Path(settings.libraries_file)
    path.write_text((existing + additions if registered_first else additions + existing)
                    + '80.47.157.190:8080\n80.47.157.190:8080\n')
    assert sync(settings) == 3
    assert rows(settings) == [
        dict(url='http://80.47.157.190:8070', total=1428, fails=5, scrapes=16, active=False),
        dict(url='80.47.157.190:8080', total=0, fails=0, scrapes=0, active=True),
    ]
    assert [lib.url for _,lib in settings.raw_libraries()] == ['http://80.47.157.190:8080']
    assert sync(settings) == 0
    assert '# New additions:' in path.read_text()


def test_inactive_memory_survives_clearing_history_and_replacing_the_text_file(tmp_path):
    settings = Settings.load(configure(tmp_path, ['books.test:8080']))
    lifecycle = LibraryState(settings)
    lifecycle.reconcile()
    library = settings.libraries()[0][1]
    for number in range(3):
        stamp = (datetime.now(timezone.utc)+timedelta(days=number)).isoformat()
        lifecycle.scan_outcome(library, category='connectivity', attempt_started=stamp)
    lifecycle.purge_pending()
    assert rows(settings)[0]['active'] is False
    assert rows(settings)[0]['fails'] == 3
    lifecycle.clear_history()
    Path(settings.libraries_file).write_text('http://BOOKS.test:8080/\nnew.test:8080\n')
    sync(settings)
    assert [(row['url'],row['active']) for row in rows(settings)] == [
        ('http://BOOKS.test:8080/',False), ('new.test:8080',True)]
    assert [lib.url for _,lib in settings.libraries()] == ['http://new.test:8080']
    assert register_path(settings).exists()


def test_metadata_counters_are_lifetime_and_deduplicate_attempt_callbacks(tmp_path, book):
    settings = Settings.load(configure(tmp_path, [book.source]))
    lifecycle = LibraryState(settings)
    lifecycle.reconcile()
    library = settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[book],now())
    attempt = now()
    lifecycle.scan_outcome(library,success=True,attempt_started=attempt)
    lifecycle.scan_outcome(library,success=True,attempt_started=attempt)
    lifecycle.scan_outcome(library,category='configuration',attempt_started='2026-01-01T00:00:00+00:00')
    lifecycle.scan_outcome(library,category='configuration',attempt_started='2026-01-01T00:00:00+00:00')
    lifecycle.scan_outcome(library,category='catalog',attempt_started='2026-01-02T00:00:00+00:00')
    lifecycle.scan_outcome(library,category='cancelled')
    lifecycle.scan_outcome(library,success=True,attempt_started='2026-01-03T00:00:00+00:00')
    assert rows(settings) == [dict(url=book.source,total=1,fails=2,scrapes=2,active=True)]
    assert lifecycle.load()['active'][library_key(library)]['scan_failure_count'] == 0


def test_legacy_retirement_is_imported_and_not_reactivated_by_a_pasted_url(tmp_path, book):
    settings = Settings.load(configure(tmp_path,[book.source]))
    lifecycle = LibraryState(settings)
    entry = lifecycle.new_entry(book.source,settings.library(book.source))
    lifecycle.save({'active':{},'pending':{},'history':[
        dict(entry,id='old-retirement',key=library_key(settings.library(book.source)),
             reason='scan-connectivity',retired_at=now())]})
    assert settings.raw_libraries() == []
    sync(settings)
    assert rows(settings)[0]['active'] is False
    lifecycle.reconcile()
    assert not lifecycle.load()['active']
    lifecycle.restore(all_retired=True)
    assert rows(settings)[0]['active'] is True
    assert settings.raw_libraries()


def test_completed_and_count_reused_scans_count_once_and_cover_only_does_not(tmp_path):
    with library_server() as fixture:
        config = configure(tmp_path,[fixture[0]],catalog_cache_covers=True)
        settings = Settings.load(config)
        manager = CatalogManager(config)
        try:
            manager.refresh(force=True); finish(manager)
            first = rows(settings)[0]
            assert first['total'] == 2 and first['scrapes'] == 1 and first['fails'] == 0
            manager.refresh(force=True); finish(manager)
            assert rows(settings)[0]['scrapes'] == 2
            manager.refresh(force=True,covers_only=True); finish(manager)
            assert rows(settings)[0]['scrapes'] == 2
            assert manager.status()['registered_servers'] == rows(settings)
        finally:
            manager.close()


def test_manual_false_edit_purges_cache_and_true_edit_restores_selection(tmp_path, book):
    settings = Settings.load(configure(tmp_path,[book.source]))
    manager = CatalogManager(tmp_path/'config.json')
    manager.maintain_libraries(settings)
    library = settings.libraries()[0][1]
    store = CatalogStore(settings.catalog_cache_dir)
    store.publish(library,[book],now())
    try:
        path = Path(settings.libraries_file)
        path.write_text(path.read_text().replace('true','false'))
        manager.maintain_libraries(settings)
        assert not settings.libraries() and store.read(library)[0] is None
        path.write_text(path.read_text().replace('false','true'))
        manager.maintain_libraries(settings)
        assert len(settings.libraries()) == 1
    finally:
        manager.close()
