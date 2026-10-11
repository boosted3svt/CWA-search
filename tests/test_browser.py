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
