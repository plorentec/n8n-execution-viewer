#!/usr/bin/env bash
# n8n-execution-viewer installer
#
# Installs the viewer (frontend + backend + systemd service) on this host and
# optionally patches an Apache vhost so it can be reached under /n8n-viewer/.
#
# Safe to re-run: existing files are refreshed, the history database is
# always preserved, and the Apache block is managed (idempotent, with a
# timestamped backup and configtest before reload).
#
# Usage:
#   sudo bash install.sh                     # auto-detect n8n's SQLite
#   sudo bash install.sh --n8n-db /path/to/database.sqlite
#   sudo bash install.sh --apache-conf /etc/apache2/sites-available/foo.conf
#   sudo bash install.sh --dry-run           # show what would happen
#   sudo bash install.sh --uninstall         # remove (keeps the history DB)
#
# See the README for the manual install this script automates.

set -euo pipefail

# ---------- defaults / flags ----------
N8N_DB=""
HISTORY_DB="/var/lib/n8n-viewer/history.db"
BACKEND_DIR="/opt/n8n-execution-viewer"
WEB_ROOT="/var/www/n8n-viewer"
BIND="127.0.0.1"
PORT=8391
SYNC_INTERVAL=300
UNIT_NAME="n8n-viewer"
APACHE_CONF=""
VHOST_PORT=""
APACHE_PREFIX="/n8n-viewer"
DO_SYSTEMD=1
DRY=0
UNINSTALL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --n8n-db PATH        n8n's SQLite file (default: auto-detect)
  --history-db PATH    viewer history DB (default /var/lib/n8n-viewer/history.db)
  --backend-dir PATH   where the backend lives (default /opt/n8n-execution-viewer)
  --web-root PATH      where the frontend is served from (default /var/www/n8n-viewer)
  --bind ADDR          API bind address (default 127.0.0.1)
  --port PORT          API port (default 8391)
  --sync-interval SEC  seconds between automatic syncs (default 300)
  --unit-name NAME     systemd unit name (default n8n-viewer)
  --apache-conf FILE   Apache vhost conf to patch with a managed block
  --vhost-port PORT    which <VirtualHost> in that file to patch (default: the first)
  --no-systemd         install the files but do not create the service
  --dry-run            print the plan, change nothing
  --uninstall          remove service, files and the managed Apache block
  -h, --help           this help
EOF
}

log() { printf '\033[1;34m[n8n-viewer]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[n8n-viewer]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[n8n-viewer]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --n8n-db) N8N_DB="$2"; shift 2 ;;
    --history-db) HISTORY_DB="$2"; shift 2 ;;
    --backend-dir) BACKEND_DIR="$2"; shift 2 ;;
    --web-root) WEB_ROOT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --sync-interval) SYNC_INTERVAL="$2"; shift 2 ;;
    --unit-name) UNIT_NAME="$2"; shift 2 ;;
    --apache-conf) APACHE_CONF="$2"; shift 2 ;;
    --vhost-port) VHOST_PORT="$2"; shift 2 ;;
    --no-systemd) DO_SYSTEMD=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

run() {
  if [ "$DRY" -eq 1 ]; then
    printf '    (dry-run) %s\n' "$*"
  else
    "$@"
  fi
}

# ---------- uninstall ----------
if [ "$UNINSTALL" -eq 1 ]; then
  # (no pipefail-unsafe "cmd | grep -q": grep -q closes early, the upstream
  # gets SIGPIPE and pipefail would turn a real match into a miss)
  UNIT_LIST="$(systemctl list-unit-files 2>/dev/null || true)"
  if [ "$DO_SYSTEMD" -eq 1 ] && grep -q "^${UNIT_NAME}\.service" <<< "$UNIT_LIST"; then
    log "stopping and disabling ${UNIT_NAME}.service"
    run systemctl disable --now "$UNIT_NAME"
    run rm -f "/etc/systemd/system/${UNIT_NAME}.service"
    run systemctl daemon-reload
  fi
  if [ -n "$APACHE_CONF" ] && [ -f "$APACHE_CONF" ]; then
    if grep -q "BEGIN n8n-execution-viewer" "$APACHE_CONF"; then
      log "removing managed block from $APACHE_CONF"
      if [ "$DRY" -eq 0 ]; then
        cp "$APACHE_CONF" "${APACHE_CONF}.bak-n8n-viewer-$(date +%Y%m%d-%H%M%S)"
        python3 - "$APACHE_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
s = open(path).read()
begin, end = "# BEGIN n8n-execution-viewer", "# END n8n-execution-viewer"
s = s[:s.index(begin)] + s[s.index(end) + len(end):]
open(path, "w").write(s)
PYEOF
        if command -v apache2ctl >/dev/null 2>&1; then
          if apache2ctl configtest >/dev/null 2>&1; then
            run systemctl reload apache2
          else
            die "Apache configtest failed after removing the block; restore from the .bak-n8n-viewer-* backup"
          fi
        fi
      fi
    fi
  fi
  log "removing $BACKEND_DIR and $WEB_ROOT"
  run rm -rf "$BACKEND_DIR" "$WEB_ROOT"
  log "done. The history database ($HISTORY_DB) was left in place on purpose."
  exit 0
fi

# ---------- preflight ----------
if [ "$(id -u)" -ne 0 ] && [ -z "${NVE_SKIP_ROOT_CHECK:-}" ]; then
  die "run with sudo (the service installs to system paths)"
fi
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found (Python >= 3.8 required, stdlib only — no pip)"
fi
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "python3 is $PYVER; 3.8 or newer is required"

# ---------- find n8n's database ----------
detect_n8n_db() {
  local candidate src
  candidate="/var/lib/docker/volumes/n8n_n8n_n8n_data/_data/database.sqlite"
  if [ -f "$candidate" ]; then
    echo "$candidate"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker ps -q >/dev/null 2>&1; then
    for src in $(docker ps --format '{{.Names}} {{.Image}}' | awk '/n8n/ {print $1}' |
                while read -r c; do
                  docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$c" 2>/dev/null
                done); do
      for dest in "$src/database.sqlite" "$src/_data/database.sqlite"; do
        if [ -f "$dest" ]; then
          echo "$dest"
          return 0
        fi
      done
    done
  fi
  return 1
}

if [ -z "$N8N_DB" ]; then
  N8N_DB="$(detect_n8n_db || true)"
fi
if [ -z "$N8N_DB" ] && [ -t 0 ]; then
  printf 'Could not find the n8n SQLite database. Enter its path (e.g. /var/lib/docker/volumes/.../database.sqlite): '
  read -r N8N_DB
fi
[ -n "$N8N_DB" ] || die "cannot find n8n's SQLite database; pass it with --n8n-db PATH"
[ -f "$N8N_DB" ] || die "n8n database not found: $N8N_DB"
log "n8n database: $N8N_DB"

# ---------- plan ----------
log "plan:"
log "  frontend  -> $WEB_ROOT"
log "  backend   -> $BACKEND_DIR"
log "  history   -> $HISTORY_DB (preserved on re-runs)"
log "  service   -> ${UNIT_NAME}.service (bind $BIND:$PORT, sync every ${SYNC_INTERVAL}s)"
[ -n "$APACHE_CONF" ] && log "  apache    -> managed block in $APACHE_CONF"

if [ "$DRY" -eq 1 ]; then
  log "dry-run: nothing was changed"
  exit 0
fi

# ---------- frontend ----------
run mkdir -p "$WEB_ROOT"
run cp "$SCRIPT_DIR/frontend/"* "$WEB_ROOT/"
run chmod 755 "$WEB_ROOT"
log "frontend installed in $WEB_ROOT"

# ---------- backend ----------
run mkdir -p "$BACKEND_DIR"
run cp "$SCRIPT_DIR/backend/app.py" "$SCRIPT_DIR/backend/sync.py" "$SCRIPT_DIR/backend/schema.py" "$BACKEND_DIR/"
log "backend installed in $BACKEND_DIR"

# ---------- systemd ----------
if [ "$DO_SYSTEMD" -eq 1 ]; then
  run mkdir -p "$(dirname "$HISTORY_DB")"
  cat > "$BACKEND_DIR/${UNIT_NAME}.service" <<EOF
[Unit]
Description=n8n execution viewer backend (persistent history + API)
After=network.target
Wants=network.target

[Service]
WorkingDirectory=$BACKEND_DIR
ExecStart=/usr/bin/python3 $BACKEND_DIR/app.py
Restart=always
RestartSec=5
Environment=N8N_DB_PATH=$N8N_DB
Environment=N8N_VIEWER_DB=$HISTORY_DB
Environment=N8N_VIEWER_BIND=$BIND
Environment=N8N_VIEWER_PORT=$PORT
Environment=N8N_VIEWER_SYNC_INTERVAL=$SYNC_INTERVAL
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  run cp "$BACKEND_DIR/${UNIT_NAME}.service" "/etc/systemd/system/${UNIT_NAME}.service"
  run systemctl daemon-reload
  run systemctl enable --now "$UNIT_NAME"
  log "service ${UNIT_NAME} is running (first sync = backfill; watch: journalctl -u ${UNIT_NAME} -f)"
fi

# ---------- apache (optional, managed block) ----------
apache_block() {
  cat <<EOF
    # BEGIN n8n-execution-viewer (managed block — do not edit inside)
    ProxyPass ${APACHE_PREFIX}/api/ http://127.0.0.1:${PORT}/api/
    ProxyPassReverse ${APACHE_PREFIX}/api/ http://127.0.0.1:${PORT}/api/
    ProxyPass ${APACHE_PREFIX} !
    Alias ${APACHE_PREFIX} ${WEB_ROOT}
    <Directory ${WEB_ROOT}>
      # restrict to your LAN / VPN networks before exposing publicly:
      Require all granted
    </Directory>
    # END n8n-execution-viewer
EOF
}

if [ -n "$APACHE_CONF" ]; then
  [ -f "$APACHE_CONF" ] || die "apache conf not found: $APACHE_CONF"
  if ! command -v apache2ctl >/dev/null 2>&1; then
    warn "apache2ctl not found; printing the snippet instead of patching"
  else
    log "patching $APACHE_CONF (backup + configtest + reload)"
    BACKUP="${APACHE_CONF}.bak-n8n-viewer-$(date +%Y%m%d-%H%M%S)"
    cp "$APACHE_CONF" "$BACKUP"
    apache_block > /tmp/.n8n-viewer-block
    # Insert (or replace) the managed block just before the chosen
    # </VirtualHost> (first one, or the one listening on --vhost-port).
    python3 - "$APACHE_CONF" /tmp/.n8n-viewer-block "$VHOST_PORT" <<'PYEOF'
import re, sys
path, block_file, want_port = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
begin, end = "# BEGIN n8n-execution-viewer", "# END n8n-execution-viewer"

# existing block? -> remove it first (idempotent replacement)
if begin in text:
    i, j = text.index(begin), text.index(end) + len(end)
    text = text[:i] + text[j:]
lines = text.splitlines(keepends=True)

# find the </VirtualHost> to insert before: first vhost, or the one
# whose header mentions :want_port (e.g. "<VirtualHost *:443>")
port_re = re.compile(rf":{want_port}(?=[\s/>])") if want_port else None
close = None
header = None
for n, line in enumerate(lines):
    if "<VirtualHost" in line:
        header = line
    elif "</VirtualHost>" in line and header is not None:
        if port_re is None or port_re.search(header):
            close = n
            break
        header = None

block = open(block_file).read()
if close is None:
    lines = lines + [block]          # no vhost / port not found: append
else:
    lines = lines[:close] + [block] + lines[close:]
open(path, "w").write("".join(lines))
PYEOF
    if apache2ctl configtest >/dev/null 2>&1; then
      run systemctl reload apache2
    else
      cp "$BACKUP" "$APACHE_CONF"
      die "Apache configtest failed with the new block; $APACHE_CONF was restored from $BACKUP"
    fi
  fi
else
  cat <<EOF

The web server was not touched (no --apache-conf given). To expose the
viewer under ${APACHE_PREFIX}/, add this to your vhost:

$(apache_block)

nginx equivalent:
    location ${APACHE_PREFIX}/api/ { proxy_pass http://127.0.0.1:${PORT}/api/; }
    location ${APACHE_PREFIX}/     { alias ${WEB_ROOT}/; }

Then: apache2ctl configtest && systemctl reload apache2
EOF
fi

# ---------- summary ----------
cat <<EOF

Done.

  UI      http://<this-host>${APACHE_PREFIX}/      (once the vhost is in place)
  API     http://${BIND}:${PORT}/api/stats        (direct, loopback)
  Watch   journalctl -u ${UNIT_NAME} -f

First sync backfills everything n8n still retains; later syncs run every
${SYNC_INTERVAL}s. History database: $HISTORY_DB
EOF
