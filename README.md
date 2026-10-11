# Calibre OPDS search and download

A Python 3.8+ CLI and LAN browser interface for searching multiple configured ebook libraries, filtering title/author metadata, selecting a book, and safely downloading its advertised acquisition link. It defaults to MOBI, then EPUB, AZW3, AZW, PDF. Use libraries you are authorized to access.

The browser interface displays cover thumbnails and uses the same search, validation, and JSON history as the CLI. No Docker, database, scheduling, or CWA-specific integration is required.

## Ubuntu setup

```bash
sudo apt-get install python3 python3-venv
cd CWA-search
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp config.example.json config.json
cp libraries.example.txt libraries.txt
.venv/bin/python -m pytest -q
```

For runtime only, install `requirements.txt` instead. No service is needed. The tests use mocked HTTP responses and a temporary local HTTP fixture server, never the example remote library.

Python 3.8 is supported, including Ubuntu's 3.8.10 interpreter. The test dependency is pinned to pytest 8.3.5 because pytest 8.4 requires Python 3.9+. If pip reports no matching pytest version after an update, run `git pull` (or download the current ZIP) and retry the installation. Check the virtual environment's interpreter with `.venv/bin/python --version`; upgrading pip does not change that interpreter. Dependency installation and the test suite were verified on Python 3.8.20 and 3.12.14.

Edit `libraries.txt` to list one server per line. Blank lines and lines beginning with `#` are ignored. Addresses and library identifiers are not hardcoded:

```text
http://204.111.163.115:8080?library_id=Calibre_Library
192.168.1.25:8080
https://books.example.org?library_id=Other_Library
```

The first line illustrates the server in the handoff; it is not included as an enabled default. Bare `HOST:PORT` uses HTTP. Do not put passwords in URLs.

Before searching, the client reads each server's `/opds` catalog and uses its advertised search URL, including its own `library_id`. A single advertised library navigation link is followed when necessary, with bounded depth and loop checks. You can normally list bare addresses without knowing their IDs:

```text
204.111.163.115:8080
205.209.236.19:8080
```

An explicit `?library_id=ID` in a library-list URL takes precedence and scopes discovery to that library. The shared `library_id` setting is a fallback, not a value forced onto every discovered server. If a catalog advertises multiple libraries without a single default search, specify the desired ID in that server's line instead of guessing. If `/opds` returns 404 or has no supported search template, the client reports the fallback and uses `search_path` with the configured default ID. Authentication errors and invalid XML remain errors. Set `auto_discover` to false to use the configured search template directly, or set `opds_path` for a different discovery endpoint.

Each attempted server is reported on stderr, followed by its detected library ID and catalog/eligible result counts. A 404 remains an HTTP error, rather than an assumed empty result; searching continues to the next server. JSON result output remains on stdout.

## Web interface on your local network

Update and install from your existing project folder. Keep your current `config.json` and `libraries.txt`; do not overwrite them with the example files:

```bash
git pull --ff-only
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python web_app.py --host 0.0.0.0 --port 8090
```

Leave that terminal running. On the server, use `hostname -I` to find its LAN address. On another device connected to the same LAN, open `http://SERVER_LAN_IP:8090` in a browser (for example, `http://192.168.1.25:8090`). Use the machine's address, not `0.0.0.0`, in the browser. If a firewall is enabled, allow TCP port 8090 from your LAN subnet. The default without `--host` accepts connections only from the same machine.

Enter an author, title, or both, then click **Search libraries**. Progress shows each server and its discovered ID. The default stops at the first server with eligible results; **Search all libraries** searches the entire list. Cover thumbnails, author metadata, and descriptions help you select a book. Choose a format and click **Download to server**. This explicitly starts the acquisition and saves it on the Ubuntu server, not on your phone or browser device. No ebook is automatically downloaded during search.

Download outcomes are shown on the affected result: a green check means the file was saved on the server; a red X means the download failed. The failure panel names the book, format, and server, explicitly states whether a new file was saved, and keeps raw errors under **Technical details**. **Acknowledge** dismisses the prompt while preserving the result badge. **Find another server** repeats the last search with the failed server skipped for that search only and previous attempts excluded. It does not remove the server from `libraries.txt` or label it offline: a catalog-size mismatch is a file validation failure, not proof that the server is unreachable. Other formats on the same server can still be selected deliberately. If there is no other configured server, the UI says so. A saved file followed by a history-write error is reported separately, with its saved path, rather than claiming the download failed.

The page adapts to phone, tablet, and desktop widths, with single-column results, stacked actions, larger touch targets, and wrapping for long metadata on small screens. Browser tests exercise the download failure prompt, acknowledgment, recovery to another server, and widths of 320, 390, and 768 pixels.

**Download folder** saves an absolute server-side path in your existing `config.json`, preserving the other settings. Use a folder writable by the account running the application; it can be your local library's ingest folder. A browser cannot select a folder on another device's filesystem. **Reject this format** marks the selected acquisition bad. **Include previous attempts** enables deliberate retries, including ones recorded as failed. Existing ebook files are never overwritten.

Only one search, download, or rejection operation runs at a time across all browser clients. The UI stays responsive and polls for progress while requests run in the background. Another device (or a refreshed page) can follow an active operation. **Stop search** stops after the current library completes, retaining results already found; it does not interrupt a file download. The CLI uses the same history lock, so running a CLI command during a browser operation may wait. Thumbnails use at most two simultaneous requests, with origin checks and a 2 MB limit. Missing/unavailable covers show a placeholder. No database or new runtime dependency is needed.

Keep this service on a trusted LAN rather than exposing port 8090 to the Internet. By default, everyone able to reach the service can search, trigger downloads, reject formats, and change the server download folder. Optional shared access protection uses the `CALIBRE_WEB_PASSWORD` environment variable; supply it securely before starting the service, then log in at the browser prompt with username `calibre`. HTTP Basic authentication over HTTP is unencrypted; use a properly configured HTTPS deployment for untrusted networks. No CORS access is enabled, and browser mutations require same-origin UI requests. LAN IP addresses, localhost, and the machine hostname are accepted; for a custom LAN DNS name, add `--allowed-host books.lan` to the startup command.

Press Ctrl+C to stop the service. It waits for a running background operation to finish; stop long searches in the UI first. The terminal command is the initial deployment workflow; automatic startup and Docker are not included. Jobs/progress are held in memory and disappear when the server restarts; configuration, history, downloads, and timeout-removal backups remain on disk. Repeat the startup command after a restart.

## Search, select, and download

```bash
.venv/bin/python main.py search --author "Emily Rath" --select
```

The search uses `/opds/search/Emily%20Rath?library_id=Calibre_Library`, not the unsuccessful `?query=` syntax. Only entries whose **author metadata** matches Emily Rath are retained; mentions in descriptions do not qualify. Select a result number, then press Enter to choose MOBI if available, or type another listed format. Press Enter at the result prompt to cancel.

An example interaction, using the metadata supplied in the handoff:

```text
1. Pucking Around: A Why Choose Hockey Romance
   Author: Emily Rath
   Result ID: <server-and-library-scoped key> | Calibre ID: 3170
   ...
   Formats: EPUB, MOBI
Select result number (Enter to cancel): 1
Formats: epub, mobi
Format [mobi]:
Downloaded MOBI: <configured folder>/<safe title>-<key>.mobi
```

The automated end-to-end test executed this interaction against a **local synthetic fixture**, including a structural MOBI fixture. A deliberate live search from the cloud environment returned HTTP 403; the proxy response was `Domain forbidden`, so live search and live ebook download are not claimed as verified. No real ebook was downloaded during development.

For noninteractive use:

```bash
.venv/bin/python main.py search --title "Pucking Around" --author "Emily Rath" --json
.venv/bin/python main.py download --id RESULT_KEY_FROM_SEARCH --format mobi
```

`--id` is the opaque result key, not the numeric Calibre ID. Numeric IDs are not globally unique. Downloads must come from a stored search result and a library still configured in the list.

Search is sequential, with bounded retries and backoff. It stops at the first library with eligible matches. It never downloads automatically. Use `--all-libraries` to search every configured library. `--exact` requires normalized full title/author equality; the default uses contiguous, word-boundary partial matching, with case, punctuation, and whitespace normalization. When both title and author are supplied, both must match.

Author matching also recognizes explicit surname-first metadata: `Battistelli, Kate` matches a search for `Kate Battistelli`, including with `--exact`. The search text sent to the server is unchanged; this normalization is applied only when filtering returned author metadata. Title matching does not reorder words. If a server returns books but none are eligible, stderr shows up to five excluded titles/authors and whether they failed metadata matching, lacked supported formats, or were excluded by download history.

## Remember bad or failed downloads

Searches, book metadata, and per-acquisition outcomes are stored in a JSON file, without a database. Failed, rejected, and successfully downloaded acquisitions are skipped by default. Another format of the same book remains eligible; when all its acquisitions have been tried, searching proceeds to the next eligible book/library.

```bash
.venv/bin/python main.py mark-bad --id RESULT_KEY --format mobi
# Omit --format to reject every acquisition of the selected book.
.venv/bin/python main.py search --author "Emily Rath"
# Explicitly restore eligibility for a retry:
.venv/bin/python main.py search --author "Emily Rath" --include-history
.venv/bin/python main.py download --id RESULT_KEY --format mobi --include-history
```

Marking a book bad does not delete the downloaded file. Existing destination files are never overwritten, including on a retry; move the existing file yourself before retrying. A failed download is not automatically replaced by another download: run another search and deliberately select the next candidate.

## Library removal

By default, a library that times out while establishing/requesting a feed is removed from `libraries.txt` after bounded retries. The previous list is backed up to `libraries.txt.bak` (the backup is replaced on the next removal). HTTP errors, connection refusals, malformed XML, body-transfer errors, and successful empty/nonmatching responses **do not remove** libraries. Errors and removals appear on stderr.

Use `--keep-timeouts` for a single search or set `"prune_timeouts": false` to disable removal. A transient timeout can remove a healthy server; restore an entry from the backup when appropriate. Downloads do not remove library entries.

A successful empty search exits 0; if every attempted library fails, search exits 1. Partial search failures are reported on stderr and in JSON, even when another library succeeds. CLI state mutations are serialized with an advisory Linux file lock; a second invocation waits for the first.

## Configuration

Use `--config /path/to/config.json` **before** the subcommand. Configuration paths are relative to the JSON file's directory. If it is absent, defaults apply relative to its intended directory; a library list is still required.

| Setting | Purpose |
| --- | --- |
| `libraries_file` | Line-delimited library URLs |
| `download_dir` | Folder for downloads; can be your local library's ingest folder |
| `history_file` | JSON book/search/download history |
| `library_id` | Fallback library identifier when discovery is unavailable |
| `search_path` | Fallback template containing `{searchTerms}` |
| `auto_discover`, `opds_path` | Enable catalog discovery (default true), and its endpoint (default `/opds`) |
| `preferred_formats` | Default format order |
| `timeout`, `retries` | Per-request connect/read timeout in seconds, and bounded transient retries |
| `max_pages`, `max_results` | Per-library raw OPDS page/result limits, before matching |
| `max_download_bytes` | Download size limit |
| `prune_timeouts` | Remove timed-out entries, with backup |
| `allowed_hosts` | Explicit additional hosts for links/redirects; empty restricts to server origin |
| `username_env`, `password_env` | Names of optional HTTP Basic authentication environment variables |

Set the named authentication variables through your deployment's secure environment mechanism. The default names are `CALIBRE_USERNAME` and `CALIBRE_PASSWORD`. Authentication is sent only to the configured server origin, never an additional host. This initial version uses one authentication pair and search template across the list; separate configurations can handle groups of servers with different credentials or endpoint templates. HTTP Basic authentication over HTTP is unencrypted; use HTTPS for authenticated deployments.

Original Calibre content-server behavior is implemented. Configurable templates can handle compatible OPDS servers; Calibre-Web/CWA-specific adapters have not been implemented or validated. Discovery supports Atom-advertised `{searchTerms}` templates and library navigation links; separate OpenSearch description documents are not supported.

## Download checks and boundaries

Acquisition URLs come from the feed. Each URL and redirect is checked before requesting it. Relative URLs and XML-escaped pagination parameters are handled. External hosts require explicit permission in `allowed_hosts`; a hostname entry permits that host's ports, so keep this list narrow. HTTPS-to-HTTP downgrade redirects are refused. Environment HTTP proxies are respected; implicit `.netrc` authentication is disabled.

Downloads stream into a temporary file in the destination, enforce byte limits, compare HTTP transfer lengths when present, and validate EPUB ZIP/mimetype/container metadata (with unpacked size/file limits), MOBI/Palm record signatures, or PDF signatures. For EPUB only, a mismatch with the OPDS catalog length becomes a warning after ZIP integrity, mimetype, and container checks pass: servers can rewrite EPUB metadata during delivery or advertise a stale stored-file size. HTTP transfer length mismatches still fail, as do corrupt EPUBs; catalog length mismatches for MOBI/AZW/PDF remain errors. A validated file is published atomically without overwriting an existing path. Failed partial files are removed. Filename input cannot escape the download directory. Structural validation detects common error pages and truncated/corrupt files; it does not establish literary correctness, reader compatibility, or comprehensive file safety. Open/preview the book and mark bad selections yourself.

Feeds use entity-safe XML parsing and a 5 MB response limit. Pagination has page/result limits and loop detection. Unknown downloadable formats are listed if identifiable, but downloading formats without validators is refused. Redirect count and retries are bounded. There is no concurrency against remote libraries.

## Modules and validation

- `calibre/client.py`: HTTP access, encoded path search, origin checks, pagination.
- `calibre/parser.py`, `models.py`: OPDS metadata, acquisitions, source-scoped identities.
- `calibre/matcher.py`: title/author matching.
- `calibre/downloader.py`: streaming, validation, safe publication.
- `calibre/state.py`: JSON history, locks, library-list removal.
- `config.py`, `main.py`: settings and CLI.
- `web_app.py`, `web/service.py`, `web/static/`: LAN HTTP interface, background operations, and browser UI.

Run `.venv/bin/python -m pytest -q`. Tests cover parsing, matching, encoding, pagination/limits, unsafe XML, auth/redirect boundaries, HTTP errors/retries/timeouts, download validation/cleanup/overwrite prevention, history/failover, timeout-only pruning, library discovery/explicit overrides, and local CLI flows including a first-server 404 followed by discovery and download from a second server with a different ID. Remote integration is deliberate and optional, not part of the suite.

Web tests cover real HTTP search/cover/download flows against a local fixture library, saved folders, multi-client busy handling, optional authentication, cross-origin rejection, timeout removal, and stopping searches. `tests/test_browser.py` is optional and skipped by default; it was executed with Chromium and Playwright on Python 3.12 to validate form submission, thumbnail display, format selection, a server download, and a mobile viewport. The standard Python 3.8 suite does not require Playwright.
