from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import pytest
from calibre.cache import CatalogManager,CatalogStore,library_key,now
from calibre.cover_jobs import CoverJob
from calibre.client import HttpError
from calibre.images import EmptyCoverResponse, fetch_image
from calibre.language import library_language
from calibre.poll_worker import run_library,checkpoint_for
from config import Settings
from test_refresh_runtime import configure,finish
from test_multiple_libraries import multi_server
from test_count_refresh import FakeClient


@pytest.mark.parametrize('name,expected',[('English Books','english'),('Calibre','unknown'),
    ('My Books','english'),('Bücher','non-english'),('Bibliothèque','non-english'),
    ('Calibre_书库','non-english'),('Имидж каталог','non-english')])
def test_library_name_policy(name,expected):
    assert library_language(Settings(),'http://fixture.test','ID',name)[0]==expected


def test_policy_overrides_are_scoped_and_explicit():
    settings=Settings(library_language_overrides={'ID':'non-english','http://fixture.test?library_id=ID':'english'})
    assert library_language(settings,'http://fixture.test','ID','Bücher')[0]=='english'
    assert library_language(settings,'http://other.test','ID','English Books')[0]=='non-english'


def test_language_counter_and_retirement_do_not_remove_english_sibling(tmp_path):
    with multi_server() as (url,state,calls):
        state['names']={'A':'Bücher','B':'English Books'}
        config=configure(tmp_path,[url],library_language_failure_limit=2)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            status=manager.status();a=next(r for r in status['libraries'] if r['library_id']=='A')
            assert a['health']=='language' and a['language_failure_count']==1 and not a['cached']
            assert status['batch']['failed']==1 and status['batch']['succeeded']==1
            assert not any('navcatalog' in path and 'library_id=A' in path for path in calls)
            manager.refresh(force=True);finish(manager)
            assert next(r for r in manager.status()['libraries'] if r['library_id']=='A')['language_failure_count']==1
            from calibre.library_state import LibraryState
            lifecycle=LibraryState(Settings.load(config));data=lifecycle.load()
            default=next(lib for _,lib in Settings.load(config).libraries() if not lib.explicit_id)
            data['active'][library_key(default)]['last_counted_language_failure']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
            lifecycle.save(data)
            manager.refresh(force=True);finish(manager)
            assert [r['library_id'] for r in manager.status()['libraries']]==['B']
            assert manager.status()['retired'][-1]['reason']=='library-language'
            assert (tmp_path/'libraries.txt').read_text()==url+'\n'
        finally:manager.close()


def test_saved_complete_empty_default_still_discovers_sibling(tmp_path):
    with multi_server() as (url,state,calls):
        state['empty'].add('A')
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        settings=Settings.load(config);library=settings.libraries()[0][1]
        checkpoint=checkpoint_for(settings,library,library_key(library))
        checkpoint.save({'url':None,'seen':[],'page_count':1,'complete':True,
                         'resolved_library_id':'A','acquisition_feed':True},[])
        store=CatalogStore(settings.catalog_cache_dir)
        stamp=json.loads(checkpoint.cursor.read_text())['saved_at']
        store.publish(library,[],now(),{'covers_pending':True,'metadata_checkpoint_stamp':stamp})
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            assert {r['library_id'] for r in manager.status()['libraries']}=={'A','B'}
            assert next(r for r in manager.status()['libraries'] if r['library_id']=='B')['book_count']==1
        finally:manager.close()


@pytest.mark.parametrize('failure',[HttpError(404),EmptyCoverResponse()])
def test_cover_cursor_and_backoff_avoid_repeating_first_failed_covers(tmp_path,book,monkeypatch,failure):
    import calibre.poll_worker as worker
    settings=Settings.load(configure(tmp_path,[book.source],catalog_cache_covers=True))
    library=settings.libraries()[0][1]
    books=[replace(book,identifier=str(i),cover_url=book.source+'/cover/'+str(i)) for i in range(40)]
    CatalogStore(settings.catalog_cache_dir).publish(library,books,now())
    monkeypatch.setattr(worker,'Client',lambda *a,**k:FakeClient({}))
    monkeypatch.setattr(worker,'catalog_count',lambda *a,**k:40)
    calls=[]
    def interrupted(client,url):
        calls.append(url)
        if len(calls)==21:raise SystemExit('simulate terminated process')
        raise failure
    monkeypatch.setattr(worker,'fetch_image',interrupted)
    with pytest.raises(SystemExit):run_library(settings,library,library_key(library),report=lambda *a,**k:None)
    job=CoverJob(settings,library_key(library),books)
    assert job.index==20 and len(job.failures)==20
    calls.clear()
    def fail(client,url):calls.append(url);raise failure
    monkeypatch.setattr(worker,'fetch_image',fail)
    run_library(settings,library,library_key(library),report=lambda *a,**k:None)
    assert len(calls)==20 and calls[0].endswith('/20')
    calls.clear();events=[]
    run_library(settings,library,library_key(library),report=lambda m,**f:events.append((m,f)))
    assert calls==[]
    assert events[-2][1]['deferred']==40
    reason='empty-response' if isinstance(failure,EmptyCoverResponse) else 'http-404'
    assert events[-2][1]['unavailable_reasons']=={reason:40}
    assert any('reasons:' in message and reason in message for message,_ in events)


def test_empty_http_200_is_backed_off_until_retry_then_recovers(tmp_path,book,monkeypatch):
    import calibre.poll_worker as worker
    from calibre.client import Client, Library
    from conftest import Response, Session
    from test_web import PNG
    settings=Settings.load(configure(tmp_path,[book.source],catalog_cache_covers=True))
    library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[book],now())
    session=Session([Response(b'',headers={'Content-Type':'image/jpeg','Content-Length':'0'}),
                     Response(PNG,headers={'Content-Type':'image/png'})])
    client=Client(library,session=session)
    client.discover_search=lambda:None
    monkeypatch.setattr(worker,'Client',lambda *a,**k:client)
    monkeypatch.setattr(worker,'catalog_count',lambda *a,**k:1)
    clock=[1000]
    monkeypatch.setattr('calibre.cover_jobs.time.time',lambda:clock[0])
    key=library_key(library)
    events=[]
    report=lambda m,**f:events.append((m,f))
    run_library(settings,library,key,report=report)
    failure=CoverJob(settings,key,[book]).failures[book.key]
    assert failure['reason']=='empty-response'
    assert failure['retry_at']==1000+7*24*3600
    assert len(session.calls)==1
    assert events[-2][1]['unavailable_reasons']=={'empty-response':1}
    assert not (Path(settings.catalog_cache_dir)/'covers'/(book.key+'.img')).exists()
    clock[0]+=24*3600
    run_library(settings,library,key,covers_only=True,report=report)
    assert len(session.calls)==1 and events[-2][1]['deferred']==1
    changed=replace(book,cover_url=book.cover_url+'?updated=1')
    assert not CoverJob(settings,key,[changed]).deferred(changed)
    clock[0]=failure['retry_at']
    run_library(settings,library,key,covers_only=True,report=report)
    assert len(session.calls)==2
    assert (Path(settings.catalog_cache_dir)/'covers'/(book.key+'.img')).read_bytes()==PNG
    assert not CoverJob(settings,key,[book]).failures
    assert events[-2][1]['failed']==0 and events[-2][1]['fetched']==1


def test_nonempty_invalid_image_keeps_its_original_failure_reason(tmp_path,book):
    from calibre.client import Client, ClientError, Library
    from conftest import Response, Session
    settings=Settings.load(configure(tmp_path,[book.source]))
    response=Response(b'<html>No thumbnail</html>',headers={'Content-Type':'image/jpeg'})
    client=Client(Library(book.source),session=Session([response]))
    with pytest.raises(ClientError,match='Unsupported cover image') as raised:
        fetch_image(client,book.cover_url)
    assert response.closed
    job=CoverJob(settings,'test',[book])
    assert job.unavailable(book,raised.value)=='invalid-image'


def test_language_rejection_cannot_be_overridden_by_an_old_complete_checkpoint(tmp_path):
    with multi_server() as (url,state,calls):
        state['names']={'A':'English Books','B':'English Books'}
        config=configure(tmp_path,[url],catalog_cache_covers=True)
        manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            settings=Settings.load(config);library=next(lib for _,lib in settings.libraries() if not lib.explicit_id)
            store=CatalogStore(settings.catalog_cache_dir);books,entry=store.read(library)
            checkpoint=checkpoint_for(settings,library,library_key(library))
            checkpoint.save({'url':None,'seen':[],'page_count':1,'complete':True,
                             'resolved_library_id':'A','acquisition_feed':True},books)
            store.update(library,{'covers_pending':True})
            state['names']['A']='Bücher'
            manager.refresh(force=True);finish(manager)
            row=next(r for r in manager.status()['libraries'] if r['library_id']=='A')
            assert row['health']=='language' and row['language_failure_count']==1 and not row['cached']
            assert store.entry(library)['snapshot']==entry['snapshot']
            assert not store.due(library,24)
        finally:manager.close()


def test_cover_negative_cache_expires_and_new_url_bypasses_it(tmp_path,book,monkeypatch):
    settings=Settings.load(configure(tmp_path,[book.source]))
    monkeypatch.setattr('calibre.cover_jobs.time.time',lambda:1000)
    job=CoverJob(settings,'test',[book])
    job.unavailable(book,HttpError(404));job.save(1)
    assert job.failures[book.key]['retry_at']==1000+7*24*3600
    monkeypatch.setattr('calibre.cover_jobs.time.time',lambda:1000+24*3600)
    assert CoverJob(settings,'test',[book]).deferred(book)
    changed=replace(book,cover_url=book.cover_url+'?updated=1')
    fresh=CoverJob(settings,'test',[changed])
    assert fresh.index==0 and not fresh.deferred(changed)
    monkeypatch.setattr('calibre.cover_jobs.time.time',lambda:1000+8*24*3600)
    assert not CoverJob(settings,'test',[book]).deferred(book)


def test_cover_journal_recovers_after_a_truncated_last_line(tmp_path,book):
    settings=Settings.load(configure(tmp_path,[book.source]))
    second=replace(book,identifier='second',cover_url=book.cover_url+'?second')
    job=CoverJob(settings,'test',[book,second]);job.unavailable(book,HttpError(404));job.save(1)
    with job.journal.open('a') as handle:handle.write('{"interrupted')
    next_job=CoverJob(settings,'test',[book,second]);next_job.unavailable(second,HttpError(403));next_job.save(2)
    recovered=CoverJob(settings,'test',[book,second])
    assert len(recovered.failures)==2
    assert recovered.reasons()=={'http-404':1,'http-403':1}


def test_explicit_english_override_resets_language_counter(tmp_path):
    with multi_server() as (url,state,calls):
        state['names']={'A':'Bücher','B':'English Books'}
        config=configure(tmp_path,[url]);manager=CatalogManager(config)
        try:
            manager.refresh(force=True);finish(manager)
            assert next(r for r in manager.status()['libraries'] if r['library_id']=='A')['language_failure_count']==1
            data=json.loads(config.read_text());data['library_language_overrides']={'A':'english'};config.write_text(json.dumps(data))
            manager.refresh(force=True);finish(manager)
            row=next(r for r in manager.status()['libraries'] if r['library_id']=='A')
            assert row['health']=='healthy' and row['language_failure_count']==0
        finally:manager.close()


def test_local_cover_write_failure_is_not_cached_as_remote_unavailable(tmp_path,book,monkeypatch):
    import calibre.poll_worker as worker
    from calibre.client import ClientError
    from test_web import PNG
    settings=Settings.load(configure(tmp_path,[book.source],catalog_cache_covers=True))
    library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[book],now())
    monkeypatch.setattr(worker,'Client',lambda *a,**k:FakeClient({}))
    monkeypatch.setattr(worker,'catalog_count',lambda *a,**k:1)
    monkeypatch.setattr(worker,'fetch_image',lambda *a:(PNG,'image/png'))
    def denied(*args):raise PermissionError('permission denied')
    monkeypatch.setattr(worker,'save_cover',denied)
    with pytest.raises(ClientError,match='disk space and permissions'):
        run_library(settings,library,library_key(library),report=lambda *a,**k:None)
    assert not CoverJob(settings,library_key(library),[book]).failures
