"""Concurrency, lifecycle races, and persisted timeout accounting."""
from dataclasses import replace
import json
import time
import pytest
from config import Settings
from calibre.cache import CatalogManager, CatalogStore, library_key, now
from calibre.client import Library, LibraryTimeout
from calibre.library_state import LibraryState
from calibre.state import History
from test_refresh_runtime import library_server, configure, finish
from test_web import PNG, post, wait_job, search, website


@pytest.mark.parametrize('concurrency', [1, 2])
def test_parallel_pool_limit_and_independent_progress(tmp_path, concurrency):
    with library_server() as a, library_server() as b, library_server() as c:
        for fixture in (a,b,c): fixture[1]['stall_page'] = True
        manager=CatalogManager(configure(tmp_path,[a[0],b[0],c[0]],catalog_scan_concurrency=concurrency))
        try:
            assert manager.refresh(force=True)
            assert a[3].wait(4)
            if concurrency==2:
                assert b[3].wait(4)
                status=manager.status()
                assert len(status['active_libraries'])==2
                assert {item['source'] for item in status['active_libraries']}=={a[0],b[0]}
                assert not c[3].wait(.2)
            else:
                assert not b[3].wait(.2)
                assert len(manager.status()['active_libraries'])==1
            a[4].set()
            assert (c if concurrency==2 else b)[3].wait(4)
            manager.cancel();finish(manager,4)
        finally:manager.close()


def test_two_app_instances_never_scan_same_library_simultaneously(tmp_path):
    with library_server() as fixture:
        fixture[1]['stall_page']=True
        config=configure(tmp_path,[fixture[0]])
        first,second=CatalogManager(config),CatalogManager(config)
        try:
            first.refresh(force=True);assert fixture[3].wait(4)
            second.refresh(force=True)
            time.sleep(.3)
            assert fixture[2]['/all?offset=0']==1
            second.cancel();finish(second,3)
            fixture[4].set();finish(first)
            assert CatalogStore(Settings.load(config).catalog_cache_dir).entry(Library(fixture[0]))['book_count']==2
        finally:first.close();second.close()


def seed(manager, settings, library, book):
    manager.maintain_libraries(settings)
    book=replace(book,source=library.url)
    store=CatalogStore(settings.catalog_cache_dir)
    store.publish(library,[book],now())
    covers=store.directory/'covers';covers.mkdir(exist_ok=True)
    (covers/(book.key+'.img')).write_bytes(PNG)
    (covers/(book.key+'.json')).write_text(json.dumps({'url':book.cover_url}))
    return store,book


def test_consecutive_timeouts_reset_retire_purge_restore_and_clear(tmp_path,book):
    config=configure(tmp_path,[book.source],library_download_timeout_limit=2)
    manager=CatalogManager(config);settings=Settings.load(config);library=settings.libraries()[0][1]
    store,cached=seed(manager,settings,library,book)
    ingest=tmp_path/'downloads';ingest.mkdir();(ingest/'existing.epub').write_bytes(b'keep')
    history=History(settings.history_file);history.remember([cached],{})
    try:
        assert manager.download_outcome(library,timed_out=True)['timeout_count']==1
        manager.download_outcome(library,success=True)
        assert LibraryState(settings).load()['active'][library_key(library)]['timeout_count']==0
        assert manager.download_outcome(library,timed_out=True)['timeout_count']==1
        assert manager.download_outcome(library,timed_out=False) is None
        outcome=manager.download_outcome(library,timed_out=True)
        assert outcome['retired'] and outcome['timeout_count']==2
        assert not (tmp_path/'libraries.txt').read_text()
        assert (tmp_path/'libraries.txt.bak').read_text()==book.source+'\n'
        assert store.read(library)[0] is None
        assert not (store.directory/'covers'/(cached.key+'.img')).exists()
        assert (ingest/'existing.epub').read_bytes()==b'keep'
        assert History(settings.history_file).book(cached.key)==cached
        record=manager.status()['retired'][0]
        assert record['reason']=='download-timeouts' and record['timeout_count']==2
        manager.restore(record['id'])
        assert Settings.load(config).libraries()[0][1].url==library.url
        assert LibraryState(settings).load()['active'][library_key(library)]['timeout_count']==0
        assert store.read(library)[0] is None
        manager.clear_retirement_history()
        assert manager.status()['retired']==[]
        assert (tmp_path/'libraries.txt').read_text()
    finally:manager.close()


def test_manual_removal_purges_and_bulk_restore_excludes_it(tmp_path,book):
    config=configure(tmp_path,[book.source]);settings=Settings.load(config)
    manager=CatalogManager(config);library=settings.libraries()[0][1]
    store,cached=seed(manager,settings,library,book)
    try:
        (tmp_path/'libraries.txt').write_text('# intentionally empty\n')
        manager.maintain_libraries(settings)
        assert store.read(library)[0] is None
        assert not (store.directory/'covers'/(cached.key+'.img')).exists()
        record=manager.status()['retired'][0]
        assert record['reason']=='manual-removal'
        with pytest.raises(ValueError,match='No retired'):manager.restore(all_retired=True)
        # Manual re-addition starts fresh and marks the old history as restored.
        (tmp_path/'libraries.txt').write_text(book.source+'\n')
        manager.maintain_libraries(settings)
        assert LibraryState(settings).load()['active'][library_key(library)]['timeout_count']==0
        assert manager.status()['retired'][0]['restored_at']
    finally:manager.close()


@pytest.mark.parametrize('reason', ['manual', 'timeout'])
def test_removal_stops_active_scan_and_cannot_recreate_cache(tmp_path,book,reason):
    with library_server() as removed, library_server() as remaining:
        removed[1]['stall_page']=True;remaining[1]['stall_page']=True
        config=configure(tmp_path,[removed[0],remaining[0]],library_download_timeout_limit=1)
        settings=Settings.load(config);manager=CatalogManager(config)
        library=settings.libraries()[0][1]
        store,cached=seed(manager,settings,library,book)
        try:
            manager.refresh(force=True)
            assert removed[3].wait(4) and remaining[3].wait(4)
            if reason=='manual':
                (tmp_path/'libraries.txt').write_text(remaining[0]+'\n')
                manager.maintain_libraries(settings)
            else:manager.download_outcome(library,timed_out=True)
            for _ in range(60):
                if not LibraryState(settings).load()['pending']:break
                time.sleep(.05)
            assert not LibraryState(settings).load()['pending']
            assert store.read(library)[0] is None
            assert not (store.directory/'covers'/(cached.key+'.img')).exists()
            assert not (store.directory/'checkpoints'/library_key(library)).exists()
            # Another source remains busy while the removed source is purged.
            assert any(item['source']==remaining[0] for item in manager.status()['active_libraries'])
            removed[4].set();remaining[4].set();finish(manager)
            assert store.read(library)[0] is None
            assert store.entry(Library(remaining[0]))['book_count']==2
        finally:manager.close()


def test_retirement_disabled_still_counts_and_polling_deadlines_do_not_count(tmp_path):
    with library_server() as fixture:
        fixture[1]['stall_page']=True
        config=configure(tmp_path,[fixture[0]],library_download_timeout_limit=0,catalog_library_timeout_seconds=1.5)
        settings=Settings.load(config);manager=CatalogManager(config);library=settings.libraries()[0][1]
        try:
            manager.refresh(force=True);finish(manager)
            assert LibraryState(settings).load()['active'][library_key(library)]['timeout_count']==0
            for _ in range(4): assert not manager.download_outcome(library,timed_out=True)['retired']
            assert (tmp_path/'libraries.txt').read_text()
        finally:manager.close()


def test_streamed_read_timeout_is_classified_and_partial_file_removed(tmp_path,book,monkeypatch):
    import requests
    from urllib3.exceptions import ReadTimeoutError
    from calibre.downloader import download, DownloadError
    from conftest import Response
    class Client:
        def get(self,url):return Response(error=requests.ConnectionError(ReadTimeoutError(None,url,'read timed out'))),url
    with pytest.raises(LibraryTimeout):download(Client(),book,book.acquisitions[0],tmp_path)
    assert not list(tmp_path.iterdir())
    class Disconnected:
        def get(self,url):return Response(error=requests.ConnectionError('Disconnected')),url
    with pytest.raises(DownloadError):download(Disconnected(),book,book.acquisitions[0],tmp_path)


def test_gui_timeout_retirement_and_restore_api(website,monkeypatch):
    from calibre.downloader import DownloadError
    url,service,directory=website
    data=json.loads(service.config.read_text());data['library_download_timeout_limit']=2
    service.config.write_text(json.dumps(data))
    result=search(url)
    def timeout(*args,**kwargs):raise LibraryTimeout('Request timed out')
    monkeypatch.setattr('web.service.download',timeout)
    for expected in (1,2):
        response=post(url,'/api/download',{'id':result['key'],'format':'mobi','include_history':True})
        job=wait_job(url,response.json()['id'])
        assert job['retirement']['timeout_count']==expected
    assert job['retirement']['retired'] and 'retired' in job['user_message']
    state=service.catalogs.status();record=state['retired'][0]
    assert post(url,'/api/retired/restore-all',{}).status_code==400
    assert post(url,'/api/retired/restore',{'id':record['id']}).status_code==200
    def corrupt(*args,**kwargs):raise DownloadError('Size mismatch')
    monkeypatch.setattr('web.service.download',corrupt)
    job=wait_job(url,post(url,'/api/download',{'id':result['key'],'format':'mobi','include_history':True}).json()['id'])
    assert 'retirement' not in job
    assert service.catalogs.status()['libraries'][0]['download_timeout_count']==0
    assert post(url,'/api/retired/clear',{'confirm':True}).status_code==200


def test_gui_counts_internal_retries_once_and_real_success_resets(website,monkeypatch):
    import requests
    from calibre.client import Client
    from conftest import Session
    from web.service import make_client as original
    url,service,directory=website
    result=search(url)
    session=Session([requests.Timeout(),requests.Timeout()])
    monkeypatch.setattr('calibre.client.time.sleep',lambda delay:None)
    monkeypatch.setattr('web.service.make_client',lambda settings,library:Client(library,retries=1,session=session))
    job=wait_job(url,post(url,'/api/download',{'id':result['key'],'format':'mobi'}).json()['id'])
    assert len(session.calls)==2 and job['retirement']['timeout_count']==1
    monkeypatch.setattr('web.service.make_client',original)
    job=wait_job(url,post(url,'/api/download',{'id':result['key'],'format':'mobi','include_history':True}).json()['id'])
    assert job['status']=='done' and job['file_saved']
    assert service.catalogs.status()['libraries'][0]['download_timeout_count']==0


def test_purge_preserves_covers_referenced_by_other_library_selection(tmp_path,book):
    config=configure(tmp_path,[book.source,book.source+'?library_id='+book.library_id])
    settings=Settings.load(config);manager=CatalogManager(config)
    first,second=[lib for _,lib in settings.libraries()]
    store,cached=seed(manager,settings,first,book)
    store.publish(second,[cached],now())
    try:
        (tmp_path/'libraries.txt').write_text(book.source+'?library_id='+book.library_id+'\n')
        manager.maintain_libraries(settings)
        manager.cleanup_if_idle(settings)
        assert store.read(first)[0] is None
        assert store.read(second)[0]==[cached]
        assert (store.directory/'covers'/(cached.key+'.img')).exists()
    finally:manager.close()


def test_upgrade_purges_unlisted_checkpoint_without_a_snapshot(tmp_path,book):
    from calibre.poll_worker import checkpoint_for
    config=configure(tmp_path,[]);settings=Settings.load(config);library=Library(book.source)
    checkpoint=checkpoint_for(settings,library,library_key(library))
    checkpoint.save({'page_count':1},[book])
    covers=checkpoint.directory.parent.parent/'covers';covers.mkdir()
    (covers/(book.key+'.img')).write_bytes(PNG)
    manager=CatalogManager(config)
    try:
        manager.maintain_libraries(settings)
        assert not checkpoint.directory.exists()
        assert not (covers/(book.key+'.img')).exists()
        assert manager.status()['retired'][0]['reason']=='manual-removal'
    finally:manager.close()


def test_missing_list_is_not_treated_as_manual_deletion(tmp_path,book):
    config=configure(tmp_path,[book.source]);settings=Settings.load(config);manager=CatalogManager(config)
    library=settings.libraries()[0][1];store,cached=seed(manager,settings,library,book)
    try:
        (tmp_path/'libraries.txt').unlink()
        with pytest.raises(FileNotFoundError):manager.maintain_libraries(settings)
        assert store.read(library)[0]==[cached]
        assert (store.directory/'covers'/(cached.key+'.img')).exists()
    finally:manager.close()
