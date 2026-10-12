"""Optional Chromium check: CWA_BROWSER_TEST=1, with Playwright installed."""
import os
import pytest
from test_web import website

pytestmark = pytest.mark.skipif(os.environ.get('CWA_BROWSER_TEST') != '1', reason='Optional browser check')


def test_browser_search_preview_select_and_save(website):
    playwright = pytest.importorskip('playwright.sync_api')
    url, _, directory = website
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                          args=['--no-sandbox'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.get_by_label('Author', exact=True).fill('Emily Rath')
        page.get_by_role('button', name='Search libraries', exact=True).click()
        page.get_by_role('heading', name='1 matching book', exact=True).wait_for()
        image = page.get_by_role('img', name='Cover of Pucking Around: A Why Choose Hockey Romance')
        image.wait_for()
        page.wait_for_function('() => document.querySelector(".cover").naturalWidth > 0')
        assert page.get_by_role('heading', name='Pucking Around: A Why Choose Hockey Romance').is_visible()
        folder = directory / 'browser-ingest'
        page.get_by_label('Server folder').fill(str(folder))
        page.get_by_role('button', name='Save folder', exact=True).click()
        page.wait_for_function('() => document.getElementById("notice").textContent === "Download folder saved."')
        page.get_by_role('button', name='Download to server', exact=True).click()
        page.wait_for_function('() => document.getElementById("notice").textContent.startsWith("Saved on the server:")')
        assert len(list(folder.glob('*.mobi'))) == 1
        page.set_viewport_size({'width':390, 'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors, errors
        browser.close()


def test_browser_download_failure_recovery_and_mobile(website, monkeypatch):
    from calibre.downloader import DownloadError
    playwright = pytest.importorskip('playwright.sync_api')
    url, _, directory = website
    original = (directory / 'libraries.txt').read_text()
    (directory / 'libraries.txt').write_text(original + original.replace('127.0.0.1', 'localhost'))
    def fail(*args, **kwargs):
        raise DownloadError('OPDS advertised 242650 bytes; downloaded 250266 bytes; download size differs from the OPDS acquisition length')
    monkeypatch.setattr('web.service.download', fail)
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':320,'height':720})
        page.goto(url)
        page.get_by_label('Author', exact=True).fill('Emily Rath')
        page.get_by_role('button', name='Search libraries', exact=True).click()
        page.get_by_role('heading', name='1 matching book', exact=True).wait_for()
        page.get_by_role('button', name='Download to server', exact=True).click()
        page.get_by_role('heading', name='✕ Download failed', exact=True).wait_for()
        assert 'No new ebook was saved' in page.locator('#failure-outcome').inner_text()
        assert '✕ Download failed' in page.locator('.download-status').inner_text()
        assert page.locator('.book.download-failed').count() == 1
        for width in (320,390,768):
            page.set_viewport_size({'width':width,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.get_by_role('button', name='Acknowledge', exact=True).click()
        assert page.locator('#download-error').is_hidden()
        assert page.locator('.book.download-failed').count() == 1
        # Repeat deliberately to check the recovery button from the prompt.
        page.get_by_label('Include previous attempts', exact=True).check()
        page.get_by_role('button', name='Search libraries', exact=True).click()
        page.get_by_role('heading', name='1 matching book', exact=True).wait_for()
        page.get_by_role('button', name='Download to server', exact=True).click()
        page.get_by_role('heading', name='✕ Download failed', exact=True).wait_for()
        page.get_by_role('button', name='Find another server', exact=True).click()
        page.wait_for_function('() => document.querySelector(".source")?.textContent.includes("localhost")')
        assert 'Skipping' in page.locator('#activity').inner_text()
        assert page.locator('.book.download-failed').count() == 0
        assert not list((directory / 'downloads').glob('*.mobi'))
        browser.close()


def test_browser_cached_results_show_freshness(website,book):
    import json
    from dataclasses import replace
    from calibre.cache import CatalogStore,now
    from config import Settings
    playwright=pytest.importorskip('playwright.sync_api')
    url,service,directory=website
    config=json.loads(service.config.read_text());config['catalog_cache_enabled']=True
    service.config.write_text(json.dumps(config))
    settings=Settings.load(service.config);library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[replace(book,source=library.url,library_id='Actual')],now())
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        page=browser.new_page()
        page.goto(url)
        page.get_by_role('heading',name='Local catalogs',exact=True).wait_for()
        assert '2 cached books' not in page.locator('#catalog-summary').inner_text()
        assert '1 cached books' in page.locator('#catalog-summary').inner_text()
        assert page.get_by_role('button',name='Fetch missing covers',exact=True).is_disabled()
        page.get_by_label('Author',exact=True).fill('Emily Rath')
        page.get_by_role('button',name='Search libraries',exact=True).click()
        page.get_by_role('heading',name='1 matching book',exact=True).wait_for()
        assert 'Catalog refreshed:' in page.locator('.book').inner_text()
        assert page.locator('.book img').count()==0
        browser.close()


def test_browser_cover_progress_is_visible_and_missing_covers_action_works(website,book,monkeypatch):
    import json,time,threading
    from dataclasses import replace
    from calibre.cache import CatalogStore,now
    from config import Settings
    playwright=pytest.importorskip('playwright.sync_api')
    url,service,directory=website
    config=json.loads(service.config.read_text())
    config.update(catalog_cache_enabled=True,catalog_cache_covers=True)
    service.config.write_text(json.dumps(config))
    settings=Settings.load(service.config);library=settings.libraries()[0][1]
    CatalogStore(settings.catalog_cache_dir).publish(library,[replace(book,source=library.url)],now())
    release=threading.Event()
    def worker(settings,library,covers_only):
        assert covers_only
        with service.catalogs.lock:
            service.catalogs.active={'source':library.url,'phase':'covers','count':20,'total':100,
                                     'started_monotonic':time.monotonic(),'last_activity':now(),
                                     'deadline_seconds':3600}
        assert release.wait(15)
    monkeypatch.setattr(service.catalogs,'run_worker',worker)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':390,'height':844})
        page.goto(url)
        page.get_by_role('button',name='Fetch missing covers',exact=True).click()
        try:
            page.wait_for_function('() => document.querySelector("#catalog-progress").textContent.includes("20/100")')
            text=page.locator('#catalog-progress').inner_text()
            assert 'covers' in text and 'Last progress:' in text and '3600s limit' in text
            assert page.get_by_role('button',name='Stop refresh',exact=True).is_visible()
            assert page.get_by_role('button',name='Fetch missing covers',exact=True).is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        finally:
            release.set();service.catalogs.worker.join(3);browser.close()


def test_browser_parallel_progress_and_retirement_controls(website,monkeypatch):
    import json,time
    from config import Settings
    from calibre.cache import now
    playwright=pytest.importorskip('playwright.sync_api')
    url,service,directory=website
    config=json.loads(service.config.read_text())
    config.update(catalog_cache_enabled=True,library_download_timeout_limit=1)
    service.config.write_text(json.dumps(config))
    settings=Settings.load(service.config);library=settings.libraries()[0][1]
    service.catalogs.download_outcome(library,timed_out=True)
    with service.catalogs.lock:
        for index in (1,2):
            service.catalogs.activities[str(index)]={'source':f'http://fixture{index}.test','phase':'covers',
                 'count':20*index,'total':100,'started_monotonic':time.monotonic(),
                 'last_activity':now(),'deadline_seconds':3600}
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':390,'height':844})
        page.goto(url)
        page.get_by_role('heading',name='Retired servers',exact=True).wait_for()
        page.wait_for_function('() => document.querySelectorAll("#catalog-progress p").length === 2')
        assert '20/100' in page.locator('#catalog-progress').inner_text()
        assert '40/100' in page.locator('#catalog-progress').inner_text()
        page.get_by_role('button',name='Restore server',exact=True).click()
        page.wait_for_function('() => document.querySelector("#retired-entries").textContent.includes("Restored")')
        assert settings.libraries()[0][1].url==library.url
        page.on('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='Clear retirement history',exact=True).click()
        page.wait_for_function('() => document.querySelector("#retired-panel").hidden')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        browser.close()
    with service.catalogs.lock:service.catalogs.activities.clear()
