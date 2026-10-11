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
