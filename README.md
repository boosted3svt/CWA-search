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

Bare `HOST:PORT` entries use HTTP. The app discovers the default library ID when possible; use `?library_id=...` to select a particular library. Lines beginning with `#` are comments. Duplicate entries are removed automatically. Do not put credentials in URLs.

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

Completed metadata becomes searchable before covers finish. Missing covers use a shared placeholder. You can search during a refresh.

**Searchable catalogs** counts libraries with saved metadata, including older catalogs retained after a failed refresh. It is separate from the current batch's success/failure counts.

- **Refresh catalogs now:** force a metadata refresh of configured libraries, resuming saved progress where available. Server backoff can defer attempts.
- **Fetch missing covers:** use saved metadata to fetch missing covers without rescanning book lists.
- **Stop refresh:** retain saved progress and pause automatic refresh for one interval, until a manual refresh, or until restart.

Repeated connectivity failures can retire a library, removing its address and cached data. The default scan threshold is three failures, counted at most once per library per refresh interval; successful metadata scans reset that counter. Ebook download timeouts have a separate counter. Authentication, certificate, catalog compatibility errors, and the overall scan time limit do not trigger scan retirement. Use **Retired servers** to inspect or restore automatically retired sources.

Deleting a library from `libraries.txt` also removes its cached metadata and unshared covers. Downloaded ebooks are preserved.

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
