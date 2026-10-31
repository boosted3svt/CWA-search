from dataclasses import replace
import json
import pytest
import requests
from calibre.summaries import lookup_summary, _matching, plain_description
from calibre.state import state_lock
from config import Settings
from conftest import Response, Session
from test_web import website, search, post


@pytest.fixture(autouse=True)
def no_provider_delay(monkeypatch):
    from calibre import summaries
    summaries._cooldowns.clear()
    monkeypatch.setattr('calibre.summaries._pace', lambda deadline: None)
    yield
    summaries._cooldowns.clear()


def response(data, **kwargs):
    return Response(json.dumps(data).encode(), **kwargs)


def ol_doc(book, **changes):
    return {'key': '/works/OL123W', 'title': book.title, 'author_name': book.authors, **changes}


def google_doc(book, **changes):
    return {'id': 'volume_123', 'volumeInfo': {
        'title': book.title, 'authors': book.authors, 'description': 'Google description.', **changes}}


def test_open_library_description_is_matched_and_converted_to_text(book):
    responses = [response({'docs': [ol_doc(book)]}), response({
        'title': book.title, 'description': {'value': '<p>A story &amp; adventure.</p><script>hidden()</script><p>Second paragraph.</p>'}})]
    session = Session(responses.copy())
    result = lookup_summary(book, session=session)
    assert result['status'] == 'found' and result['source'] == 'Open Library'
    assert result['source_url'] == 'https://openlibrary.org/works/OL123W'
    assert result['description'] == 'A story & adventure.\n\nSecond paragraph.'
    assert len(session.calls) == 2 and all(item.closed for item in responses)
    assert session.calls[0][1]['params']['author'] == 'emily rath'
    assert session.calls[0][1]['params']['title'] == 'Pucking Around'
    assert all('auth' not in kwargs and kwargs['allow_redirects'] is False and kwargs['verify']
               for _, kwargs in session.calls)
    assert book.source not in repr(session.calls)


def test_mismatched_author_is_skipped_and_google_is_used(book):
    session = Session([response({'docs': [ol_doc(book, author_name=['Wrong Author'])]}),
                       response({'items': [google_doc(book)]})])
    result = lookup_summary(book, session=session)
    assert result['status'] == 'found' and result['source'] == 'Google Books'
    assert result['source_url'] == 'https://books.google.com/books?id=volume_123'
    assert len(session.calls) == 2
    assert session.calls[1][1]['params']['q'] == 'intitle:"pucking around" inauthor:"emily rath"'


def test_no_title_and_author_match_does_not_return_an_unrelated_summary(book):
    session = Session([response({'docs': [ol_doc(book, title='Another book')]}),
                       response({'items': [google_doc(book, authors=['Someone Else'])]})])
    assert lookup_summary(book, session=session)['status'] == 'not_found'


@pytest.mark.parametrize('failure', [Response(status=429), requests.Timeout(), Response(b'<html>failure</html>')])
def test_provider_failure_falls_back_to_another_source(book, failure):
    session = Session([failure, response({'items': [google_doc(book)]})])
    result = lookup_summary(book, session=session)
    assert result['status'] == 'found' and result['source'] == 'Google Books'


def test_unavailable_sources_return_a_clear_retry_message(book):
    session = Session([Response(status=503), requests.Timeout()])
    result = lookup_summary(book, session=session)
    assert result['status'] == 'unavailable' and 'try again later' in result['message']
    assert 'description' not in result


def test_rate_limited_google_is_skipped_on_later_clicks_then_retried_after_cooldown(book):
    from calibre import summaries
    first = Session([response({'docs': []}), Response(status=429)])
    assert lookup_summary(book, session=first)['status'] == 'unavailable'
    second = Session([response({'docs': []})])
    assert lookup_summary(book, session=second)['status'] == 'unavailable'
    assert len(second.calls) == 1 and second.calls[0][0].startswith('https://openlibrary.org/')
    summaries._cooldowns['www.googleapis.com'] = 0
    third = Session([response({'docs': []}), response({'items': [google_doc(book)]})])
    assert lookup_summary(book, session=third)['source'] == 'Google Books'


@pytest.mark.parametrize('retry_after,minimum', [(None,3600), ('60',3600), ('7200',7200)])
def test_rate_limit_cooldown_is_at_least_an_hour_and_honors_retry_after(book,retry_after,minimum):
    import time
    from calibre import summaries
    headers = {'Retry-After':retry_after} if retry_after else {}
    failed = Response(status=429,headers=headers)
    session = Session([failed,response({'items':[]})])
    assert lookup_summary(book,session=session)['status'] == 'unavailable'
    assert summaries._cooldowns['openlibrary.org']-time.monotonic() >= minimum-2
    assert failed.closed


@pytest.mark.parametrize('bad', [Response(b'x' * 1_000_001),
                                Response(headers={'Content-Length': '2000000'}),
                                Response(status=302, headers={'Location': 'http://private.test'}),
                                response(['not', 'an', 'object'])])
def test_bounded_responses_and_redirects_are_rejected(book, bad):
    session = Session([bad, response({'items': []})])
    assert lookup_summary(book, session=session)['status'] == 'unavailable'
    assert bad.closed and len(session.calls) == 2


def test_provider_cannot_make_the_app_fetch_an_arbitrary_work_url(book):
    session = Session([response({'docs': [ol_doc(book, key='https://private.test/secret')]}),
                       response({'items': []})])
    assert lookup_summary(book, session=session)['status'] == 'not_found'
    assert [url for url, _ in session.calls] == [
        'https://openlibrary.org/search.json', 'https://www.googleapis.com/books/v1/volumes']


def test_google_source_url_is_constructed_not_taken_from_provider(book):
    item = google_doc(book)
    item['volumeInfo']['infoLink'] = 'javascript:alert(1)'
    session = Session([response({'docs': []}), response({'items': [item]})])
    assert lookup_summary(book, session=session)['source_url'].startswith('https://books.google.com/')


@pytest.mark.parametrize('title,authors,expected', [
    ('Pucking Around', ['Rath, Emily'], True),
    ('Pucking Around (Jacksonville Rays Book 1)', ['EMILY RATH'], True),
    ('Pucking Around: A Different Book', ['Emily Rath'], False),
    ('Pucking Around', ['Emily Rathson'], False),
    ('Pucking', ['Emily Rath'], False),
    ('', ['Emily Rath'], False),
])
def test_title_and_author_matching_is_conservative(book, title, authors, expected):
    assert _matching(book, title, authors) is expected


def test_accents_and_surname_first_metadata_are_normalized(book):
    wanted = replace(book, title='A Book', authors=['Smith, Renée'])
    assert _matching(wanted, 'A Book: A Subtitle', ['Renee Smith'])


def test_missing_author_does_not_make_any_external_requests(book):
    session = Session([])
    assert lookup_summary(replace(book, authors=[]), session=session)['status'] == 'not_found'
    assert not session.calls


def test_plain_descriptions_are_bounded_and_never_return_markup():
    assert plain_description(None) == ''
    assert '<img' not in plain_description('<img src="x" onerror="bad()"><p>Words</p>')
    assert len(plain_description('A' * 50000)) < 12100


def test_summary_endpoint_is_on_demand_read_only_and_uses_saved_metadata(website, monkeypatch):
    url, service, directory = website
    calls = []
    def lookup(book):
        calls.append(book)
        return {'status': 'found', 'description': 'External summary', 'source': 'Open Library'}
    monkeypatch.setattr('web.service.lookup_summary', lookup)
    book = search(url)
    assert calls == []
    settings = Settings.load(service.config)
    previous = (directory/'state/history.json').read_bytes()
    # A summary must not wait for a search/download's exclusive history lock.
    with state_lock(settings.history_file):
        result = post(url, '/api/summary', {'id': book['key'], 'title': 'Wrong', 'author': 'Wrong'})
    assert result.status_code == 200 and result.json()['description'] == 'External summary'
    assert calls[0].title == book['title'] and calls[0].authors == book['authors']
    assert service.active is None
    assert (directory/'state/history.json').read_bytes() == previous
    assert post(url, '/api/summary', {'id': book['key']}).status_code == 200
    assert len(calls) == 2  # Opening it again performs a fresh lookup.
    assert (directory/'state/history.json').read_bytes() == previous


def test_summary_endpoint_rejects_unknown_ids_and_limits_concurrency(website, monkeypatch):
    url, service, _ = website
    calls = []
    monkeypatch.setattr('web.service.lookup_summary', lambda book: calls.append(book) or {'status': 'not_found'})
    for key in (None, 'http://private.test', 'a'*24):
        assert post(url, '/api/summary', {'id': key}).status_code == 400
    assert not calls
    book = search(url)
    service.summary_slots.acquire(); service.summary_slots.acquire()
    try:
        assert post(url, '/api/summary', {'id': book['key']}).status_code == 409
    finally:
        service.summary_slots.release(); service.summary_slots.release()
    assert post(url, '/api/summary', {'id': book['key']}).status_code == 200
    assert len(calls) == 1
    assert requests.post(url+'/api/summary', json={'id': book['key']}, timeout=5).status_code == 403
