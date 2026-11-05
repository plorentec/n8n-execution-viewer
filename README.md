# n8n-execution-viewer

A permanent, searchable history for your n8n executions.

n8n prunes old executions (the default retention keeps only recent runs).
This project keeps a growing copy: a small **read-only** Python service
imports your n8n executions into its own SQLite database and serves a fast
web viewer — filters, full-text search, an n8n-style execution diagram, and
per-node input/output inspection.

- **Read-only against n8n.** No credentials, no n8n settings touched, no
  writes to n8n's database (plain `SELECT`s against the SQLite file).
- **No dependencies.** Python 3 stdlib only (no `pip install`), and the
  viewer is plain HTML/JS with no build step.
- **Incremental.** The first run backfills everything n8n still has; every
  later sync (automatic, every 5 minutes by default) imports only the delta.
  A small re-scan window picks up late `running → finished` transitions and
  soft-deletes.
- **Loopback by default.** The API binds to `127.0.0.1:8391`; expose it
  through your existing web server (Apache/nginx notes included).

![scope](https://img.shields.io/badge/n8n-SQLite%20instances-informational)

> **Requires an n8n instance using the default SQLite database.** n8n on
> Postgres is not supported (see [Limitations](#limitations)).

## How it works

```
n8n (Docker)                          n8n-execution-viewer
┌─────────────────────────┐           ┌──────────────────────────────┐
│ database.sqlite (WAL)   │  SELECT   │ sync engine (every 5 min)    │
│  execution_entity       │ ────────► │  decode + import (delta)     │
│  execution_data         │  read-only│        ▼                     │
│  workflow_entity        │           │ history.db (SQLite, yours)   │
└─────────────────────────┘           │        ▼                     │
                                      │ API on 127.0.0.1:8391        │
        Apache / nginx ◄──────────────│        ▼                     │
              (Alias + ProxyPass)     │   static frontend            │
                                      └──────────────────────────────┘
```

The history database is a separate file that only this service writes, so
n8n's pruning never touches it: executions you imported last month are still
here after n8n has forgotten them.

## Quick start (one command)

On the machine where n8n runs:

```sh
curl -fsSL https://raw.githubusercontent.com/plorentec/n8n-execution-viewer/main/install.sh | sudo bash -s --
```

The installer will:

1. **Find n8n's SQLite database** (official Docker volume path, then
   `docker inspect` of a running n8n container, then it asks). Pass
   `--n8n-db PATH` to skip detection.
2. Copy the frontend to `/var/www/n8n-viewer/` and the backend to
   `/opt/n8n-execution-viewer/`.
3. Create and start the `n8n-viewer.service` systemd unit (loopback API,
   first run backfills the history, then it syncs every 5 minutes).
4. Print the exact two lines to add to your Apache vhost (or patch it
   itself, with a timestamped backup, a managed block and a configtest,
   if you pass `--apache-conf /path/to/vhost.conf` — optional `--vhost-port`).

Re-running it updates the code and never touches the history database.
Remove it later with `sudo bash install.sh --uninstall` (the history
stays on disk on purpose). Other options: `--dry-run`, `--port`,
`--bind`, `--sync-interval`, `--no-systemd`, `--help`.

Prefer to do it by hand? The manual install below is exactly what the
script does, step by step.

## Deploying beside an existing Docker n8n

If your company already runs n8n in Docker (compose, Portainer, plain
`docker run`), you have three ways to install the viewer. All three read
the database the same way — pick by how you manage your containers.

**How the viewer reaches the database (all options).** SQLite is not a
server: there is no port, no protocol and no credentials. It is a file.
Docker lets two containers mount the same volume, so the viewer mounts
n8n's data volume **read-only** (`:ro`) and opens
`database.sqlite` with SQLite's own read-only mode. n8n does not
participate and is never contacted: no token, no webhook, no API. The
`:ro` flag is enforced by the kernel, not by convention, so even a
buggy viewer cannot write to n8n's database. The viewer keeps its own
separate history database in its own volume — that is the only thing
it ever writes.

Prerequisites for every option:

- n8n uses **SQLite** (the official image's default; check for
  `database.sqlite` in its data volume). Postgres setups are not
  supported.
- The viewer runs on the **same host/kernel** as n8n. Sharing a SQLite
  file over NFS or a network mount is not supported (SQLite is known
  to corrupt over network filesystems).
- Build the image once on that host (no registry image yet):
  `docker build -t n8n-execution-viewer .`
- Find the volume name and mount point first — every option needs it:

```sh
docker inspect <n8n-container> --format \
  '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{println}}{{end}}'
# the official image mounts volume n8n_data at /home/node/.n8n,
# so the database file is /home/node/.n8n/database.sqlite
```

### Option A — sidecar in your existing compose stack (recommended)

Best when n8n is managed by a `docker-compose.yml`: the viewer becomes
one more service in the same file, lifecycle-managed together with n8n
(`docker compose up -d` brings both up after a host reboot).

```yaml
services:
  n8n-viewer:
    image: n8n-execution-viewer
    restart: unless-stopped
    environment:
      - N8N_DB_PATH=/data/database.sqlite   # path inside the volume
    volumes:
      - n8n_data:/data:ro                   # the SAME volume n8n uses
      - n8n-viewer_history:/var/lib/n8n-viewer
    ports:
      - "127.0.0.1:8391:8391"               # loopback; front it with your proxy

volumes:
  n8n-viewer_history:
  # do NOT redeclare n8n_data here if your n8n service already defines
  # it in this file — reuse its name
```

Then `docker compose up -d n8n-viewer`.

### Option B — standalone container (Portainer UI or docker run)

Best when you manage n8n from Portainer's UI or with `docker run`
commands and prefer not to touch your n8n stack at all. Create a new
container that attaches n8n's *existing* volume as external:

```sh
docker volume create n8n-viewer_history
docker run -d --name n8n-viewer --restart unless-stopped \
  -e N8N_DB_PATH=/data/database.sqlite \
  -p 127.0.0.1:8391:8391 \
  -v n8n_data:/data:ro \
  -v n8n-viewer_history:/var/lib/n8n-viewer \
  n8n-execution-viewer
```

In Portainer: Volumes shows your n8n volume's name; Containers →
Add container → set the image, the env var, the port mapping, and add
**two** volumes under "Advanced settings": the n8n volume (read-only
toggle on) and a new one for the history. Updating later is
`docker rm -f n8n-viewer` + the same command — the history volume
survives.

### Option C — no second container (native service)

Best when policy says one process per container, or you prefer a
systemd service. `install.sh` detects the running n8n container,
reads its volume path under `/var/lib/docker/volumes/...`, and installs
the Python backend (stdlib only, no packages) as a system service
reading that file read-only:

```sh
curl -fsSL https://raw.githubusercontent.com/plorentec/n8n-execution-viewer/main/install.sh | sudo bash -s -- \
  --n8n-db /var/lib/docker/volumes/n8n_data/_data/database.sqlite
```

(auto-detection covers the official volume path and a running container;
`--dry-run` shows every step first.) The frontend is copied to
`/var/www/n8n-viewer/` and wired into Apache if present.

### Serving the UI and the proxy (all options)

The viewer serves only its API on loopback. Point any web server (or
Portainer's own proxy) at it — for Apache under a subpath:

```apache
Alias /n8n-viewer /var/www/n8n-viewer
ProxyPass /n8n-viewer/api/ http://127.0.0.1:8391/api/
ProxyPassReverse /n8n-viewer/api/ http://127.0.0.1:8391/api/
ProxyPass /n8n-viewer !
```

The `frontend/` files are plain static assets — any nginx, Caddy or
Traefik static route works the same way.

### Why the container runs as root

n8n's volume is owned by uid 1000 with `0600` files, so a non-root
user could not open the database. The image contains exactly three
small Python files and nothing else, and the n8n mount is read-only.

## Manual install

### 1. Frontend

Copy `frontend/` to a directory your web server can serve, e.g.
`/var/www/n8n-viewer/`. Optionally set `N8N_URL` at the top of `app.js` to
your n8n web UI base URL (e.g. `https://n8n.example.com`) to enable the
"open in n8n" links; leave it empty to hide them.

### 2. Backend

Copy `backend/` to `/opt/n8n-viewer/` (or anywhere; the paths below assume
that).

### 3. Service

Copy `deploy/n8n-viewer.service` to `/etc/systemd/system/` and adjust the
paths if needed:

```ini
Environment=N8N_DB_PATH=/var/lib/docker/volumes/n8n_n8n_n8n_data/_data/database.sqlite
Environment=N8N_VIEWER_DB=/var/lib/n8n-viewer/history.db
```

`N8N_DB_PATH` is where n8n's SQLite file lives. For the official n8n Docker
image with default volumes it is
`/var/lib/docker/volumes/n8n_n8n_n8n_data/_data/database.sqlite`; find
yours with `docker inspect <n8n-container> | grep -A3 Mounts` if your
volume name differs.

Then:

```sh
systemctl daemon-reload
systemctl enable --now n8n-viewer
journalctl -u n8n-viewer -f        # watch the first (backfill) sync
```

### 4. Web server

Point the static files and the API at the viewer, e.g. Apache (the same
works with nginx; see `deploy/apache-notes.conf`):

```apache
ProxyPass /n8n-viewer/api/ http://127.0.0.1:8391/api/
ProxyPassReverse /n8n-viewer/api/ http://127.0.0.1:8391/api/
ProxyPass /n8n-viewer !
Alias /n8n-viewer /var/www/n8n-viewer
```

> **Trap (measured):** the `ProxyPass` target must keep the `/api/` path —
> Apache maps the remainder of the URL onto the target, so a bare
> `http://127.0.0.1:8391/` target rewrites `/n8n-viewer/api/stats` to
> `/stats` and the backend answers 404.

Open `https://your-host/n8n-viewer/`.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `N8N_DB_PATH` | n8n Docker default volume path | n8n's SQLite file (read-only) |
| `N8N_VIEWER_DB` | `/var/lib/n8n-viewer/history.db` | this service's history database |
| `N8N_VIEWER_BIND` | `127.0.0.1` | API bind address |
| `N8N_VIEWER_PORT` | `8391` | API port |
| `N8N_VIEWER_SYNC_INTERVAL` | `300` | seconds between automatic syncs |
| `N8N_VIEWER_PAGE_SIZE` | `50` | executions per page |
| `N8N_VIEWER_RETENTION_DAYS` | `0` | keep the history this many days; `0` = keep forever. After each sync, older rows are dropped from the history (never from n8n's own database). Also `--retention-days N` in `install.sh` |
| `N8N_VIEWER_MAX_BLOB_MB` | `50` | skip the payload of any execution whose stored data is larger than this. The execution still appears in the list, marked as having no data, instead of being loaded and re-serialized in memory |

### Branding (logo and title)

Two ways, in order of precedence:

1. **Settings panel** (recommended): gear icon → *Marca* → title,
   subtitle and logo upload. Saved via the API to `config.json` next to
   the history database (survives restarts and image rebuilds).
2. **Static file**: drop a `brand.json` next to `index.html` (in your web
   root). Used only when the settings panel has not saved a brand:

```json
{ "title": "Mi Empresa", "subtitle": "historial de n8n", "logo": "logo.png", "favicon": "favicon.png" }
```

- `title` replaces the heading, `subtitle` the small caption below it, and
  the browser tab becomes `"title · subtitle"`.
- `logo` replaces the default icon (any image file, shown at 42 px).
- `favicon` replaces the tab icon.
- No file, or any field missing → the current default. Nothing else needs to
  change; it is plain static files, served by the same web server.

### Disk usage

`GET /api/stats` returns a `storage` block: current size of the history
(`dbBytes`, `percentOfDisk`), free space on that disk, and an honest
estimate of the growth (`estBytesPerDay`, from the average imported payload
per day) with a 30-day projection (`projectedBytes30d`). The UI shows the
current percentage in the fifth card, details in the tooltip, and the active
retention in the card label.

## API (what the viewer consumes)

- `GET /api/stats` — totals, byStatus, deleted, importIssues, date range
- `GET /api/workflows` — id/name/active/deleted + total + error counts
- `GET /api/executions?workflow=&status=&q=&from=&to=&before=` — filtered
  list; `q` is an exact execution id (digits) or full-text search over node
  data; cursor pagination via `nextBefore`
- `GET /api/executions/:id` — detail with nodes/positions/connections
  (diagram data) and the error box
- `GET /api/executions/:id/nodes/:name?run=N` — node input (derived from
  the source run, or `inputOverride`), output, parameters, error
- `GET /api/sync` — running/step/last run result (`retentionDeleted` rows dropped by the
  retention window, `retentionSkipped` executions too old to import again,
  `oversizedBlobs` payloads skipped by `N8N_VIEWER_MAX_BLOB_MB`)
- `POST /api/sync` (header `x-viewer-action: sync`) — trigger a sync now;
  `409` while one is running
- `GET /api/config` — current `retentionDays` + `brand` (title/subtitle/logo)
- `POST /api/config` (header `x-viewer-action: config`, JSON body
  `{retentionDays?, brand?}`) — update them; persists to `config.json` next
  to the history DB; `400` on invalid values, `403` without the header

The **settings panel** (gear icon in the header) edits both through that
endpoint: the retention period (how long executions are kept in the
history) and the brand (title, subtitle, logo upload). The workflow
sidebar is sorted by execution count (most executed first).

Dates are passed through verbatim as n8n stores them
(`YYYY-MM-DD HH:mm:ss.mmm`); the viewer renders them in your local
timezone.

## Reading the n8n execution data (n8n ≥ 2.x)

n8n does **not** store the execution payload as a plain JSON object. The
`execution_data.data` column holds a **flatted array** (measured on
n8n 2.29.10, decoded against the parser n8n ships itself in
`@n8n/backend-common`):

- It is a JSON list of unique values (dicts, lists **and strings**);
  entry `0` is the root.
- Every string appearing inside the structure is the *string form of the
  index* of the real value in the list (a shared object, or a primitive).
- Numbers, booleans and null stay inline.
- `backend/sync.py` → `flatted_resolve` is a faithful Python port of that
  parser (shared references and cycles included).

Other 2.x specifics handled by the importer (all measured on 2.29.10):

- `execution_entity` has **no** `lastNodeExecuted` / `executionDataId`;
  the last node is read from the decoded payload.
- The workflow snapshot is a **separate column** (`workflowData`), merged
  into the stored blob, so the diagram still works for rows whose run data
  n8n has pruned.
- `startData` sits at the top level of the payload (legacy nested form is
  also accepted).
- A run's input provenance is `run["source"]` — a **list** of source
  objects (`previousNode` / `previousNodeOutput` / `previousNodeRun`);
  legacy runs (`inputSource` / `source` string) are still understood.
- `workflow_entity` marks removed workflows with `isArchived` (legacy
  `deletedAt` is probed too).

If you upgrade n8n to a version that changes this schema, the sync fails
loudly with `n8n schema not recognized: …` and the already-imported history
keeps being served. File an issue — the fix is usually a few lines in
`backend/sync.py`.

## Tests

The whole test suite runs on a **synthetic n8n database that serializes the
real 2.x flatted format**, so it exercises the production shape, not a
simplification:

```sh
cd backend
python3 -m unittest discover -s tests -v
```

## Limitations

- **SQLite only.** n8n configured with Postgres/MySQL is not supported;
  the sync engine reads the SQLite file directly (that is what makes it
  dependency-free and safe).
- **Binary data is shown as references** (`fileName`, `mimeType`) — the
  viewer never copies the binary blobs.
- **UI language is Spanish** for now; the API is language-neutral.
- The history database grows (~a few hundred MB per day on a busy
  instance); it is a plain SQLite file you can delete/rotate at will.

## Security notes

- The backend opens n8n's database **read-only** (`file:...?mode=ro`) and
  never writes to it; with n8n's WAL mode, read-only connections do not
  block the running instance.
- The API is loopback-only by default. If you expose it, keep it behind a
  reverse proxy that restricts access (the notes in `deploy/` show the
  minimal Apache changes on a LAN/Tailscale-only host).
- No credentials are needed or stored anywhere.
