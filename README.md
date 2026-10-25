# CWA Search

Search multiple Calibre OPDS libraries from a browser on your local network. Preview covers, choose a book and format, and download it into a server folder—including a Calibre-Web Automated ingest folder.

Docker Compose runs the service in the background. Book metadata is cached locally; downloads connect to the original library. Ensure you only use libraries you have permission to access.

## Install

You need Git, Docker, and Docker Compose on your Linux server. No host Python installation is required.

For a **new installation**:

```bash
git clone https://github.com/boosted3svt/CWA-search.git
cd CWA-search
cp config.example.json config.json
cp libraries.example.txt libraries.txt
cp .env.example .env
```

For an existing installation, keep your current configuration and library list; use the update instructions below.

### 1. Configure the container

Edit `.env`. Set `CWA_UID` and `CWA_GID` to the account that can write to your project and download folders. Find those numbers with:

```bash
id -u
id -g
```

The default web port is `8090`. To use port `8118`, set:

```dotenv
CWA_PORT=8118
```

Optionally set `CALIBRE_WEB_PASSWORD` in `.env` to require a browser login; the username is `calibre`. Keep the service on your trusted local network.

### 2. Choose the download folder

**Default:** downloads go into `downloads/` inside the project folder. No additional mount is needed.

**NAS or another host folder:** edit `compose.yaml` and add a mount under the service's existing `volumes:` section:

```yaml
    volumes:
      - "${CWA_DATA_DIR:-.}:/data"
      - "/media/NAS/cwa-book-ingest:/ingest"
```

Replace `/media/NAS/cwa-book-ingest` with your actual **host folder**. `/ingest` is the corresponding **folder inside the container**. Keep the `/data` mount: it stores configuration, catalogs, covers, and download history.

Ensure the host folder exists, the NAS is mounted, and the configured UID/GID can write to it before starting the container.

In `config.json`, set:

```json
"download_dir": "/ingest"
```

You can also save `/ingest` using **Download folder** in the GUI. The GUI requires the container path, not the host's NAS path. Adding or changing a Docker mount requires recreating the container with `docker compose up -d`.

### 3. Add libraries and enable caching

Edit `libraries.txt`, with one address per line:

```text
192.168.1.20:8080
http://192.168.1.21:9090
https://books.example.org
http://192.168.1.22:8080?library_id=My_Library
```

Bare `HOST:PORT` entries use HTTP. A bare server address discovers and scans all advertised libraries separately. Use `?library_id=...` to scan only that library. Discovered IDs are stored in `state/catalogs/servers.json`, not appended to your text file. Each library has its own catalog, count check, covers, and health; the GUI combines their search results. Libraries on the same configured server are scanned sequentially, while different server entries use the parallel workers. Lines beginning with `#` are comments. Duplicate entries are removed automatically. Do not put credentials in URLs.

On startup and during scheduler maintenance, the app converts the list into an editable register:

```text
url                         | total | fails | scrapes | active
http://192.168.1.20:8080     | 756   | 3     | 37      | false
http://192.168.1.21:9090     | 775   | 0     | 71      | true

# New additions: paste one URL or HOST:PORT per line below.
192.168.1.30:8080
```

Paste new addresses below **New additions**; the app moves unique entries into the table. Matching host/IP and port combinations are duplicates, even with different HTTP/HTTPS spelling on the same port. Different ports remain separate. Explicit `?library_id=...` selections remain independent. An existing table row takes precedence over a pasted address, including when `active` is `false`.

`total` is the last known book count, summed across discovered libraries for a bare server. `fails` counts failed metadata scan operations; `scrapes` counts successful metadata scans or successful remote count checks. Each sub-library counts separately; cover-only jobs and cancelled scans do not increment these counters. They are lifetime counters from this upgrade onward and are separate from the consecutive failures used for retirement. Existing catalog counts are imported; historical lifetime counters that were never recorded start at zero.

Inactive entries are kept for comparison and are not scanned. Their memory is also saved in `state/catalogs/library-register.json`, so clearing the retirement log or pasting an old address again does not reactivate it. Use **Restore server**, or deliberately change the table row's final `active` column from `false` to `true`, to retry an inactive server. Preserve the column separators when editing. The original list is backed up as `libraries.txt.original.bak` during conversion.

In `config.json`, change these existing settings:

| Setting | Recommended starting value | Purpose |
|---|---|---|
| `catalog_cache_enabled` | `true` | Search locally cached book metadata |
| `catalog_cache_covers` | `true` | Save covers for result previews |
| `catalog_scan_concurrency` | `3` | Scan up to three libraries at once |
| `catalog_refresh_interval_hours` | `24` | Refresh interval for each library |

To accept self-signed or otherwise unverified HTTPS certificates for every configured library, add `"verify_tls": false` to `config.json`. Verification defaults to `true`. HTTPS stays encrypted, but with verification disabled the server identity is not checked. HTTP entries automatically follow same-host HTTPS redirects and HTTPS links advertised by the library; you do not need to change the library-list entry for those upgrades.

Keep the file valid JSON. Configuration edits apply to subsequent operations; restart the container to apply them consistently. Library-list edits are picked up automatically, normally within a minute.

### 4. Start and open the app

From the project folder:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs -f cwa-search
```

Open `http://YOUR_SERVER_LAN_IP:8090` from any device on your local network, or use your chosen port—for example, `http://192.168.1.10:8118`. Allow that port through your server's firewall if necessary.

You can close the terminal. The container runs in the background and restarts after a server reboot unless you explicitly stopped it.

## Use the app

1. **Let the initial catalog refresh run.** The Local catalogs panel shows active workers, latest scan outcomes, and batch progress. Expand **Library freshness and refresh activity** for individual library details.
2. **Search by author, title, or both.** Enable **Search all libraries** to collect matches across libraries; otherwise the search stops at the first library with eligible matches.
3. **Check the title, author, and cover.** Select an available format and download the book. Files are saved in your configured server folder, with readable title and author names.
4. **Check the result.** A green check indicates a saved download. A red X indicates a failed attempt; use **Find another server** to try another source. **Include previous attempts** allows deliberate retries of previously attempted formats.

Refreshes compare the remote book count with the saved catalog when a usable count can be established from the first and last pages. Equal counts reuse metadata; changed or unavailable counts trigger a metadata scan. With unchanged metadata, a complete set of saved covers also skips cover downloads. Same-count book replacements and metadata-only edits will not be detected by this shortcut.

Completed metadata becomes searchable before covers finish. Missing covers use a shared placeholder. Cover jobs save their position and continue after a timeout or restart; progress totals carry over between runs. Unavailable images have recorded reasons in the GUI and progress logs. Empty responses (`empty-response`, including HTTP 200 with zero bytes) and HTTP 404/410 wait at least seven days before another attempt. Other failures start at `catalog_cover_failure_retry_hours` (default 24 hours), increasing with repeated failures. **Fetch missing covers** respects this backoff. A new image URL bypasses the old failure record. Existing `invalid-image` records are reclassified only when retried. You can search during a refresh.

**Searchable catalogs** counts libraries with saved metadata, including older catalogs retained after a failed refresh. It is separate from the current batch's success/failure counts.

- **Refresh catalogs now:** force a metadata refresh of configured libraries, resuming saved progress where available. Server backoff can defer attempts.
- **Fetch missing covers:** use saved metadata to fetch missing covers without rescanning book lists.
- **Stop refresh:** retain saved progress and pause automatic refresh for one interval, until a manual refresh, or until restart.

Repeated connectivity failures can retire a library, marking it inactive and removing its cached data while retaining its address and lifetime counters. The default scan threshold is three failures, counted at most once per library per refresh interval; successful metadata scans reset that consecutive counter. Ebook download timeouts have a separate counter. Authentication, certificate, catalog compatibility errors, and the overall scan time limit do not trigger connectivity retirement. Clear non-English library names have a separate `library_language_failure_limit` (default 3, 0 disables retirement), counted at most once per refresh interval. Those libraries are excluded from cached search while awaiting retirement; healthy siblings remain available. Use **Retired servers** to inspect or restore automatically retired sources. Older automatic retirements still present in retirement history are imported as inactive entries.

Deleting an active bare server entry from `libraries.txt`, or setting it inactive, removes all its discovered libraries and their cached metadata and unshared covers. Its address remains as an inactive reference row. A retired sub-library is excluded independently; the bare server row becomes inactive automatically only when every advertised library has been retired. Inactive rows are retained even if omitted from a later pasted list. The GUI separates server counts from discovered library counts and reports retained inactive entries. Downloaded ebooks are preserved.

Library-name language checks recognize non-Latin lettering and common non-English library wording. They do not establish the language of the books. Ambiguous names such as “Calibre” are kept by default; `reject_unknown_library_language: true` also rejects uncertain names. To override a classification, add `library_language_overrides` to `config.json`, mapping a library ID—or a scoped `SERVER_URL?library_id=ID`—to `english` or `non-english`. For example:

```json
"library_language_overrides": {
  "http://192.168.1.20:8080?library_id=My_Library": "english"
}
```

## Start, stop, and inspect

Run these commands from the project folder:

| Action | Command |
|---|---|
| Start an existing container | `docker compose start` |
| Stop | `docker compose stop` |
| Restart | `docker compose restart` |
| Check container status | `docker compose ps` |
| Follow service logs | `docker compose logs -f cwa-search` |
| Show recent logs | `docker compose logs --tail 100 cwa-search` |
| Apply Compose or environment changes | `docker compose up -d` |

Stopping or recreating the container preserves data in the host mounts. Keep your `.env`, `config.json`, `libraries.txt`, and `state/` folder when moving or backing up the installation.

## Update

From the project folder:

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

Keep your existing `config.json`, `libraries.txt`, `.env`, and custom Compose mount settings. Do not copy the example configuration over them during updates. If Git reports conflicting local edits to `compose.yaml`, preserve your mounts while resolving the conflict rather than discarding the file.

`docker compose restart` alone does not install updated application code; updates require the rebuild command above.
