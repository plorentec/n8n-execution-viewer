"""n8n execution viewer backend.

Serves the /api/* contract consumed by the static viewer (see
odd/tasks/n8n-viewer-backend.md, spec S3) and keeps a persistent copy of the
n8n execution history in its own SQLite database via the incremental sync
engine in sync.py. Python 3.12 stdlib only.

Environment:
  N8N_DB_PATH              n8n's SQLite file (read-only)
  N8N_VIEWER_DB            this service's history database
  N8N_VIEWER_PORT          listen port (default 8391)
  N8N_VIEWER_BIND          bind address (default 127.0.0.1)
  N8N_VIEWER_SYNC_INTERVAL seconds between automatic syncs (default 300)
  N8N_VIEWER_PAGE_SIZE     executions per page (default 50)
  N8N_VIEWER_RETENTION_DAYS keep history this many days; 0 = keep forever (default)
  N8N_VIEWER_MAX_BLOB_MB   cap for one execution payload in MB (default 50):
                           bigger blobs are not copied, the execution is
                           stored as data_missing with a parse_error reason
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import schema
import sync as syncmod

CONFIG = {
    "n8n_db": os.environ.get(
        "N8N_DB_PATH",
        "/var/lib/docker/volumes/n8n_n8n_n8n_data/_data/database.sqlite"),
    "history_db": os.environ.get("N8N_VIEWER_DB", "/var/lib/n8n-viewer/history.db"),
    "port": int(os.environ.get("N8N_VIEWER_PORT", "8391")),
    "bind": os.environ.get("N8N_VIEWER_BIND", "127.0.0.1"),
    "sync_interval": int(os.environ.get("N8N_VIEWER_SYNC_INTERVAL", "300")),
    "page_size": int(os.environ.get("N8N_VIEWER_PAGE_SIZE", "50")),
    "retention_days": int(os.environ.get("N8N_VIEWER_RETENTION_DAYS", "0")),
    "max_blob_mb": int(os.environ.get("N8N_VIEWER_MAX_BLOB_MB", "50")),
    "brand": None,  # set from config.json at startup, or via POST /api/config
}

LOG = logging.getLogger("n8n-viewer")

# S6: space reclamation after a retention pass.
#
# Two steps, both only ever run by apply_retention() from the sync thread
# (never from a request handler), and only when the pass actually deleted
# rows: merge the FTS5 index with its own `optimize` command, then give the
# freed pages back to the operating system.
#
# Why PRAGMA incremental_vacuum and not VACUUM:
#   * VACUUM rewrites the whole database in one step. On a 600 MB history
#     that needs a second full copy on disk (peak usage 2x, a real
#     SQLITE_FULL risk on a small VM), it cannot be split into pieces and it
#     holds the write lock for the entire copy.
#   * incremental_vacuum returns free pages in short batches. It runs here in
#     a single write transaction with a page budget and a wall-clock budget,
#     so the sync thread never owns the write lock for more than a few
#     seconds; the sync lock already serializes every writer and WAL keeps
#     readers (the HTTP handlers) serving their snapshots meanwhile.
#     Measured on this machine: ~50k pages/s once the loop runs inside one
#     transaction (a 49 MB test history reclaimed 9000 pages in ~0.2 s).
#   * The FTS5 `optimize` merge itself is proportional to the index size and
#     runs before the pages above can be released; it is a plain write too,
#     so it costs the sync thread time but never blocks a reader.
# The trade-off: incremental_vacuum only works on a database created with
# PRAGMA auto_vacuum=INCREMENTAL (what schema.SCHEMA now does). A history
# created by an older version has auto_vacuum=NONE, so its free pages can
# never be returned without one file rewrite: that case is converted once
# with a guarded VACUUM (see _legacy_vacuum) and is incremental from then on.
INCREMENTAL_VACUUM_PAGES = 200    # pages per pragma call (SQLite frees >= 1)
RECLAIM_MAX_PAGES = 500_000       # hard cap per pass (~2 GB at 4 KiB pages)
RECLAIM_BUDGET_S = 5.0            # wall-clock cap per pass
LEGACY_VACUUM_MIN_BYTES = 8 << 20  # rewrite a legacy file only for >= 8 MB
LEGACY_VACUUM_DISK_FACTOR = 2.2    # VACUUM needs room for a second copy

MAX_CONFIG_BYTES = 2 * 1024 * 1024  # logo data-URLs are the only big payload
# S2: largest request body worth swallowing before answering early (403/409/
# 413/404). Anything bigger closes the connection instead of buffering it.
MAX_DRAIN_BYTES = 8 * 1024 * 1024


def _config_file():
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG["history_db"])),
                        "config.json")


def _clean_brand(brand):
    """Validate/normalize a brand object; None clears it. Raises ValueError."""
    if brand is None:
        return None
    if not isinstance(brand, dict):
        raise ValueError("brand must be an object")
    out = {}
    for key in ("title", "subtitle"):
        v = brand.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > 200:
            raise ValueError(f"brand.{key} must be a string of at most 200 chars")
        out[key] = v
    v = brand.get("logo")
    if v is not None:
        if (not isinstance(v, str) or not v.startswith("data:image/")
                or len(v) > MAX_CONFIG_BYTES):
            raise ValueError("brand.logo must be a data:image/ URL of at most 2 MB")
        out["logo"] = v
    return out or None


def _save_config_file():
    data = {"retentionDays": CONFIG["retention_days"], "brand": CONFIG.get("brand")}
    path = _config_file()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_config_file():
    """Startup: the config.json written by the settings UI overrides the
    environment defaults (it is the last explicit choice)."""
    try:
        with open(_config_file()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if (isinstance(data.get("retentionDays"), int)
            and not isinstance(data["retentionDays"], bool)
            and data["retentionDays"] >= 0):
        CONFIG["retention_days"] = data["retentionDays"]
    if data.get("brand") is not None:
        try:
            CONFIG["brand"] = _clean_brand(data["brand"])
        except ValueError:
            pass  # a corrupt file must not kill the service

STATUS_FILTER = {"success", "error", "crashed", "canceled", "waiting", "running"}


class State:
    """Single-flight guard for syncs (the lock IS the running flag)."""

    def __init__(self):
        self.sync_lock = threading.Lock()
        self.step = ""  # in-memory only: written while a sync holds the DB

    @property
    def running(self):
        return self.sync_lock.locked()


STATE = State()


def history(isolation_level=""):
    """Connection to the viewer history database.

    `isolation_level=None` switches sqlite3 to explicit transaction control
    (the caller issues BEGIN/COMMIT); the default keeps its implicit "BEGIN
    before a DML statement" convenience used by the short-lived helpers.
    """
    con = sqlite3.connect(CONFIG["history_db"], timeout=15,
                          isolation_level=isolation_level)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    return con


def set_state(key, value):
    con = history()
    try:
        con.execute(
            "INSERT INTO sync_state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        con.commit()
    finally:
        con.close()


def _sync_body():
    """One sync, end to end. Must run with the sync lock held.

    The progress callback is in-memory only: during run_sync the sync
    connection holds an open write transaction, and a second writer
    connection (set_state) would deadlock on the SQLite write lock.
    """

    def progress(step):
        STATE.step = step

    try:
        set_state("running", "1")
        STATE.step = "fetch"
        # S7: one cutoff for the whole pass, so the rows the import skips and
        # the rows retention deletes share the exact same boundary.
        cutoff = _retention_cutoff()
        result = syncmod.run_sync(
            CONFIG["n8n_db"], CONFIG["history_db"], progress,
            retention_cutoff=cutoff,
            max_blob_bytes=CONFIG.get("max_blob_mb", 50) * 1024 * 1024)
        result["retentionDeleted"] = apply_retention(cutoff)
        set_state("last_ok", "1")
        set_state("last_error", "")
        set_state("last_result", json.dumps(result))
        set_state("last_step", "import")
        set_state("last_finished", syncmod._now())
    except Exception as e:  # the history stays served; next sync retries
        set_state("last_ok", "0")
        set_state("last_error", f"{type(e).__name__}: {e}")
    finally:
        set_state("running", "0")
        STATE.step = ""


def _retention_cutoff():
    """The date/time boundary retention uses, or None when it is off.

    Stored the same way n8n stores startedAt, so the import skip (S7) and
    the delete compare against exactly the same string.
    """
    days = CONFIG.get("retention_days", 0)
    if days <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S.000")


def _incremental_vacuum(con):
    """Return the free pages to the file system in bounded batches.

    One write transaction, a page budget and a wall-clock budget: the sync
    thread never holds the write lock for long, and WAL keeps the readers
    working. Whatever is left over when a budget runs out is reclaimed by the
    following retention pass (the free pages stay reusable meanwhile).
    Returns the number of pages freed.
    """
    started = time.monotonic()
    freed = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        while freed < RECLAIM_MAX_PAGES:
            before = con.execute("PRAGMA freelist_count").fetchone()[0]
            if before <= 0:
                break
            con.execute(f"PRAGMA incremental_vacuum({INCREMENTAL_VACUUM_PAGES})")
            after = con.execute("PRAGMA freelist_count").fetchone()[0]
            freed += before - after
            if after >= before or time.monotonic() - started > RECLAIM_BUDGET_S:
                break
        con.execute("COMMIT")
    except sqlite3.Error:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return freed


def _legacy_vacuum(con):
    """Convert a pre-S6 history (auto_vacuum=NONE) so it can be compacted.

    Such a file can only lose its free pages through one full rewrite, so it
    happens once: `PRAGMA auto_vacuum=INCREMENTAL` is stored by the following
    VACUUM and every later pass uses _incremental_vacuum. Guarded because
    VACUUM needs room for a second copy of the live data: it only runs when
    there is something worth reclaiming and the disk can hold the copy.
    Returns a short description for the log.
    """
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    free_pages = con.execute("PRAGMA freelist_count").fetchone()[0]
    reclaimable = free_pages * page_size
    if reclaimable < LEGACY_VACUUM_MIN_BYTES:
        return f"legacy skip: {reclaimable} bytes to reclaim"
    db_path = CONFIG["history_db"]
    try:
        db_bytes = os.path.getsize(db_path)
        free_disk = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    except OSError as e:
        return f"legacy skip: {e}"
    if free_disk < db_bytes * LEGACY_VACUUM_DISK_FACTOR:
        return (f"legacy skip: VACUUM needs {int(db_bytes * LEGACY_VACUUM_DISK_FACTOR)} "
                f"free bytes, {free_disk} available")
    con.execute("PRAGMA auto_vacuum=INCREMENTAL")
    con.execute("VACUUM")
    return f"legacy converted: {reclaimable} bytes rewrote the file once"


def _reclaim_history_space(con):
    """S6: give back what a retention delete freed.

    Called only by apply_retention() (sync thread, sync lock held) and only
    when that pass deleted rows: a no-op pass never touches the file.
    """
    # 1) merge the FTS5 segments: deleting rows only marks their entries as
    #    gone, the text stays inside the on-disk segments until they merge.
    con.execute("INSERT INTO fts_executions(fts_executions) VALUES('optimize')")
    # 2) return the freed pages to the OS (see the trade-off note at the top)
    mode = con.execute("PRAGMA auto_vacuum").fetchone()[0]
    if mode == 2:
        detail = f"incremental: {_incremental_vacuum(con)} pages freed"
    else:
        detail = _legacy_vacuum(con)
    # 3) in WAL mode the main file only loses its pages on a checkpoint
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        detail += f" (checkpoint failed: {e})"
    LOG.info("retention reclaim: %s", detail)
    return detail


def apply_retention(cutoff=None):
    """S6: drop history older than N8N_VIEWER_RETENTION_DAYS (0 = keep all).

    Runs while the sync lock is held, never from a request handler. Deletes
    only from this viewer's own history, never from n8n's database.
    fts_executions and executions_data are cleaned explicitly (the fts5 table
    has no foreign keys and the per-connection foreign_keys pragma is not
    guaranteed), so the deletions are deterministic regardless of pragma
    state. When rows were deleted the freed disk space is reclaimed
    (_reclaim_history_space); a pass that deletes nothing does no work.

    `cutoff` is the boundary sync.run_sync used to skip expired rows (S7);
    when omitted it is derived from the configured retention days.
    """
    if cutoff is None:
        cutoff = _retention_cutoff()
    if cutoff is None:
        return 0
    # Explicit transaction control: the SELECT below must not sit inside
    # sqlite3's implicit transaction, and the deletion batch has to be one
    # atomic unit (BEGIN ... COMMIT) that a failure rolls back as a whole.
    con = history(isolation_level=None)
    try:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM executions WHERE started_at < ?", (cutoff,))]
        if not ids:
            return 0
        con.execute("BEGIN IMMEDIATE")
        try:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                # S4: this index stores execution_id as TEXT (fts5 columns have
                # no type affinity), so it must be matched with TEXT too;
                # integers would never match and the cleanup would be a no-op.
                con.execute(
                    f"DELETE FROM fts_executions WHERE execution_id IN ({marks})",
                    [str(i) for i in chunk])
                con.execute(
                    f"DELETE FROM executions_data WHERE execution_id IN ({marks})",
                    chunk)
                con.execute(
                    f"DELETE FROM executions WHERE id IN ({marks})", chunk)
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # sqlite3 may have rolled back on its own already
            raise
        # only past this point: the rows are really gone
        try:
            _reclaim_history_space(con)
        except sqlite3.Error as e:
            # the history is already clean; a failed compaction is not worth
            # failing the sync over (the next pass retries it)
            LOG.warning("retention reclaim failed: %s: %s", type(e).__name__, e)
        return len(ids)
    finally:
        con.close()


def run_sync_now():
    """Synchronous single-flight sync (timer thread). False when one is up."""
    if not STATE.sync_lock.acquire(blocking=False):
        return False
    try:
        _sync_body()
    finally:
        STATE.sync_lock.release()
    return True


def start_sync():
    """Manual trigger: claims the flight and runs in the background so the
    caller gets 200 immediately (the UI polls GET /api/sync). False = busy."""
    if not STATE.sync_lock.acquire(blocking=False):
        return False

    def _run():
        try:
            _sync_body()
        finally:
            STATE.sync_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def sync_loop():
    while True:
        run_sync_now()
        time.sleep(CONFIG["sync_interval"])


# ---------- execution data shaping (spec S3) ----------

def _error_box(message, node):
    if not message:
        return None
    return {"node": node, "message": message}


def _runs_view(run_data):
    """Per-run summary used by the list and the diagram (spec S3)."""
    runs = []
    for run in run_data or []:
        if not isinstance(run, dict):
            continue
        data = run.get("data") or {}
        main = data.get("main")
        items_out = [len(b) for b in main] if isinstance(main, list) else []
        err = run.get("error")
        if isinstance(err, dict):
            err_text = err.get("message")
            err_text = str(err_text) if err_text else "run failed"
        else:
            err_text = str(err) if err else None
        runs.append({
            "status": "error" if err else "success",
            "itemsOut": items_out,
            "executionTime": run.get("executionTime") or 0,
            "error": err_text,
        })
    return runs


def build_execution_view(parsed):
    """nodes[] + connections{} for the detail view and the diagram."""
    result = parsed.get("resultData") or {}
    run_data = result.get("runData") or {}
    wf_data = parsed.get("workflowData") or {}
    nodes = []
    for n in wf_data.get("nodes") or []:
        name = n.get("name")
        if not name:
            continue
        runs_src = run_data.get(name) or []
        entry = {
            "name": name,
            "type": n.get("type") or "",
            "executed": bool(runs_src),
            "runs": _runs_view(runs_src),
        }
        pos = n.get("position")
        if (isinstance(pos, list) and len(pos) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                        for x in pos)):
            entry["position"] = [pos[0], pos[1]]
        nodes.append(entry)
    return {"nodes": nodes, "connections": wf_data.get("connections") or {}}


def _node_params_and_type(parsed, name):
    for n in (parsed.get("workflowData") or {}).get("nodes") or []:
        if n.get("name") == name:
            return n.get("parameters"), (n.get("type") or "")
    return None, ""


def node_io_view(parsed, name, run_index):
    """Input/output view of one run (input is derived from the source run)."""
    result = parsed.get("resultData") or {}
    run_data = result.get("runData") or {}
    runs = run_data.get(name)
    if runs is None:
        raise LookupError(f"node {name} not found in execution")
    if not isinstance(runs, list) or run_index < 0 or run_index >= len(runs):
        raise LookupError(f"run {run_index} not found for node {name}")
    run = runs[run_index]
    if not isinstance(run, dict):
        raise LookupError(f"run {run_index} of node {name} is malformed")

    inputs = []
    # n8n >= 2.x keeps startData at the top level of the execution data;
    # older versions nest it inside resultData. Accept both.
    start_data = parsed.get("startData") or result.get("startData") or {}
    override = start_data.get("inputOverride")
    if override and name == start_data.get("sourceNodeName"):
        in_items = (((override.get("sourceData") or {}).get("input") or [{}])[0]
                    .get("items")) or []
        inputs.append({"from": "inputOverride", "inputIndex": 0, "items": in_items})
    # n8n >= 2.x: run["source"] is a list of source objects with
    # previousNode / previousNodeOutput / previousNodeRun (first entry is
    # the primary one); n8n <  2.x: inputSource / source (string) /
    # sourceOutput / sourceRun.
    src = None
    sources = run.get("source")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        s0 = sources[0]
        if s0.get("previousNode"):
            src = (str(s0["previousNode"]),
                   s0.get("previousNodeOutput") or 0,
                   s0.get("previousNodeRun") or 0)
    elif run.get("inputSource") == "node" and isinstance(run.get("source"), str):
        src = (str(run["source"]),
               run.get("sourceOutput") or 0,
               run.get("sourceRun") or 0)
    if src:
        src_node, src_out, src_run = src
        items = []
        src_runs = run_data.get(src_node)
        if isinstance(src_runs, list) and src_run < len(src_runs) \
                and isinstance(src_runs[src_run], dict):
            main = ((src_runs[src_run].get("data") or {}).get("main")) or []
            if isinstance(main, list) and src_out < len(main):
                items = main[src_out] or []
        inputs.append({
            "from": {"node": src_node, "run": src_run, "output": src_out},
            "inputIndex": src_out,
            "items": items,
        })

    err = run.get("error")
    if err is not None and not isinstance(err, dict):
        err = {"message": str(err)}
    parameters, node_type = _node_params_and_type(parsed, name)
    return {
        "node": name,
        "type": node_type,
        "status": "error" if err else "success",
        "executionTime": run.get("executionTime"),
        "startTime": run.get("start"),
        "run": run_index,
        "input": inputs,
        "output": run.get("data"),
        "parameters": parameters,
        "error": err,
    }


def _get_blob(con, execution_id):
    row = con.execute(
        "SELECT data FROM executions_data WHERE execution_id=?",
        (execution_id,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["data"])
    except (ValueError, TypeError):
        return None


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "n8n-viewer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout quiet; journal if needed
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            # say it out loud: the client must not reuse this connection
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def _content_length(self):
        """S3: Content-Length as a non-negative int, or None when the header
        is non-numeric or negative (the caller answers 400: reading a bogus
        length would block the thread or eat the next request)."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            length = int(raw)
        except (TypeError, ValueError):
            return None
        return length if length >= 0 else None

    def _drain_body(self, length):
        """S2: consume the request body before answering early.

        protocol_version is HTTP/1.1, so the connection is kept alive and any
        unread body byte would be parsed as the start of the next request
        (a POST with a body followed by another request on the same socket
        desynchronizes). Bodies above MAX_DRAIN_BYTES are not swallowed: the
        connection is closed instead. Common cases (403/409/413) are well
        inside that limit.
        """
        if length <= 0:
            return
        if length > MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    # -- GET routes --

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/stats":
                return self._stats()
            if path == "/api/workflows":
                return self._workflows()
            if path == "/api/executions":
                return self._executions(parse_qs(parsed.query))
            m = re.fullmatch(r"/api/executions/(\d+)/nodes/([^/]+)", path)
            if m:
                return self._node_detail(m.group(1), unquote(m.group(2)),
                                          parse_qs(parsed.query))
            m = re.fullmatch(r"/api/executions/(\d+)", path)
            if m:
                return self._exec_detail(m.group(1))
            if path == "/api/sync":
                return self._sync_status()
            if path == "/api/config":
                self._send(200, {"retentionDays": CONFIG["retention_days"],
                                 "brand": CONFIG.get("brand")})
                return
            self._error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        path = urlparse(self.path).path
        length = self._content_length()
        if length is None:
            # S3: the body boundary is unknown, so the stream cannot be
            # resynchronized: answer 400 and close.
            self.close_connection = True
            return self._error(400, "invalid Content-Length header")
        if path == "/api/sync":
            self._drain_body(length)  # no endpoint here reads a body
            if self.headers.get("x-viewer-action") != "sync":
                return self._error(403, "x-viewer-action: sync header required")
            if not start_sync():
                return self._error(409, "sync already running")
            return self._send(200, {"ok": True})
        if path == "/api/config":
            if self.headers.get("x-viewer-action") != "config":
                self._drain_body(length)
                return self._error(403, "x-viewer-action: config header required")
            return self._save_config(length)
        self._drain_body(length)
        return self._error(404, "not found")

    def _save_config(self, length):
        if length > MAX_CONFIG_BYTES:
            self._drain_body(length)
            return self._error(413, "payload too large")
        try:
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode() or "null")
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "invalid JSON")
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object")
        if "retentionDays" in payload:
            rd = payload["retentionDays"]
            if isinstance(rd, bool) or not isinstance(rd, int) or rd < 0:
                return self._error(400, "retentionDays must be an integer >= 0")
            CONFIG["retention_days"] = rd
        if "brand" in payload:
            try:
                CONFIG["brand"] = _clean_brand(payload["brand"])
            except ValueError as e:
                return self._error(400, str(e))
        _save_config_file()
        self._send(200, {"retentionDays": CONFIG["retention_days"],
                         "brand": CONFIG.get("brand")})

    # -- endpoint bodies --

    def _stats(self):
        con = history()
        try:
            total = con.execute("SELECT count(*) FROM executions").fetchone()[0]
            workflows = con.execute("SELECT count(*) FROM workflows").fetchone()[0]
            by_status = {r[0]: r[1] for r in con.execute(
                "SELECT status, count(*) FROM executions GROUP BY status")}
            deleted = con.execute(
                "SELECT count(*) FROM executions WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
            first, last, total_json = con.execute(
                "SELECT min(started_at), max(started_at), "
                "COALESCE(SUM(json_size),0) FROM executions").fetchone()
            issues = con.execute(
                "SELECT value FROM sync_state WHERE key='import_issues'").fetchone()
        finally:
            con.close()
        self._send(200, {
            "executions": total,
            "workflows": workflows,
            "byStatus": by_status,
            "deleted": deleted,
            "importIssues": int(issues[0]) if issues else 0,
            "firstStartedAt": first,
            "lastStartedAt": last,
            "retentionDays": CONFIG.get("retention_days", 0),
            "storage": self._storage_stats(first, total_json),
        })

    def _storage_stats(self, first_started, total_json):
        """S7: current disk use of the history DB plus an honest estimate of
        its growth (average imported payload per day since the first row).
        """
        db_path = CONFIG["history_db"]
        try:
            db_bytes = os.path.getsize(db_path)
        except OSError:
            db_bytes = 0
        try:
            du = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)))
            total_disk, free_disk = du.total, du.free
        except OSError:
            total_disk, free_disk = 0, 0
        est_bytes_per_day = 0
        if first_started:
            try:
                first_dt = datetime.strptime(
                    first_started, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                days = max((datetime.now(timezone.utc) - first_dt).total_seconds()
                           / 86400.0, 1.0)
                est_bytes_per_day = int(total_json / days)
            except ValueError:
                pass
        projected_30d = db_bytes + est_bytes_per_day * 30
        pct = (db_bytes / total_disk * 100) if total_disk else 0.0
        pct_30d = (projected_30d / total_disk * 100) if total_disk else 0.0
        return {
            "dbBytes": db_bytes,
            "freeDiskBytes": free_disk,
            "totalDiskBytes": total_disk,
            "percentOfDisk": round(pct, 3),
            "estBytesPerDay": est_bytes_per_day,
            "projectedBytes30d": projected_30d,
            "projectedPercentOfDisk30d": round(pct_30d, 3),
            "retentionDays": CONFIG.get("retention_days", 0),
        }

    def _workflows(self):
        con = history()
        try:
            rows = con.execute("""
                SELECT w.id, w.name, w.active, w.deleted_at,
                       (SELECT count(*) FROM executions e
                        WHERE e.workflow_id = w.id) AS total,
                       (SELECT count(*) FROM executions e
                        WHERE e.workflow_id = w.id
                          AND e.status IN ('error','crashed')) AS errors
                FROM workflows w
                ORDER BY w.name COLLATE NOCASE""").fetchall()
        finally:
            con.close()
        self._send(200, [{
            "id": r["id"],
            "name": r["name"],
            "active": bool(r["active"]),
            "deleted": r["deleted_at"] is not None,
            "errors": r["errors"],
            "total": r["total"],
        } for r in rows])

    def _executions(self, qs):
        clauses, params = [], []
        wf = (qs.get("workflow") or [""])[0]
        if wf:
            clauses.append("e.workflow_id = ?")
            params.append(wf)
        status = (qs.get("status") or [""])[0]
        if status:
            if status not in STATUS_FILTER:
                return self._error(400, f"invalid status filter: {status}")
            clauses.append("e.status = ?")
            params.append(status)
        q = (qs.get("q") or [""])[0]
        if q:
            if q.isdigit():
                clauses.append("e.id = ?")
                params.append(int(q))
            else:
                phrase = q.replace('"', '""')
                clauses.append(
                    "e.id IN (SELECT execution_id FROM fts_executions "
                    "WHERE fts_executions MATCH ?)")
                params.append(f'"{phrase}"')
        frm = (qs.get("from") or [""])[0]
        if frm:
            clauses.append("substr(e.started_at,1,10) >= ?")
            params.append(frm)
        to = (qs.get("to") or [""])[0]
        if to:
            clauses.append("substr(e.started_at,1,10) <= ?")
            params.append(to)
        before = (qs.get("before") or [""])[0]
        if before and ":" in before:
            b_started, b_id = before.rsplit(":", 1)
            if b_id.isdigit():
                clauses.append("(e.started_at < ? OR (e.started_at = ? AND e.id < ?))")
                params.extend([b_started, b_started, int(b_id)])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        con = history()
        try:
            rows = con.execute(
                "SELECT e.id, e.workflow_id, w.name AS workflow_name, e.status, "
                "e.started_at, e.duration_ms, e.error_message, e.error_node, "
                "e.deleted_at, e.json_size "
                "FROM executions e LEFT JOIN workflows w ON w.id = e.workflow_id "
                + where +
                " ORDER BY e.started_at DESC, e.id DESC LIMIT ?",
                params + [CONFIG["page_size"] + 1]).fetchall()
        finally:
            con.close()
        has_more = len(rows) > CONFIG["page_size"]
        rows = rows[:CONFIG["page_size"]]
        items = [{
            "id": str(r["id"]),
            "workflowId": r["workflow_id"],
            "workflowName": r["workflow_name"],
            "status": r["status"],
            "startedAt": r["started_at"],
            "durationMs": r["duration_ms"],
            "errorMessage": r["error_message"],
            "errorNode": r["error_node"],
            "deletedAt": r["deleted_at"],
            "dataSize": r["json_size"],
        } for r in rows]
        next_before = (f"{rows[-1]['started_at']}:{rows[-1]['id']}"
                       if has_more and rows else None)
        self._send(200, {"items": items, "nextBefore": next_before})

    def _exec_detail(self, eid):
        eid = int(eid)
        con = history()
        try:
            row = con.execute(
                "SELECT e.*, w.name AS workflow_name FROM executions e "
                "LEFT JOIN workflows w ON w.id = e.workflow_id WHERE e.id=?",
                (eid,)).fetchone()
            if row is None:
                return self._error(404, f"execution {eid} not found")
            parsed = None
            if row["parse_error"] is None:
                parsed = _get_blob(con, eid)
                if parsed is None and row["data_missing"]:
                    parsed = None
        finally:
            con.close()
        payload = {
            "id": str(row["id"]),
            "workflowId": row["workflow_id"],
            "workflowName": row["workflow_name"],
            "status": row["status"],
            "startedAt": row["started_at"],
            "stoppedAt": row["stopped_at"],
            "durationMs": row["duration_ms"],
            "mode": row["mode"],
            "dataSize": row["json_size"],
            "lastNodeExecuted": row["last_node"],
            "error": _error_box(row["error_message"], row["error_node"]),
            "deletedAt": row["deleted_at"],
            "dataMissing": bool(row["data_missing"]),
            "parseError": row["parse_error"],
            "nodes": [],
            "connections": {},
        }
        if parsed is not None:
            payload.update(build_execution_view(parsed))
        self._send(200, payload)

    def _node_detail(self, eid, name, qs):
        try:
            run_index = int((qs.get("run") or ["0"])[0])
        except ValueError:
            return self._error(400, "invalid run parameter")
        con = history()
        try:
            row = con.execute(
                "SELECT id FROM executions WHERE id=?", (int(eid),)).fetchone()
            if row is None:
                return self._error(404, f"execution {eid} not found")
            parsed = _get_blob(con, int(eid))
        finally:
            con.close()
        if parsed is None:
            return self._error(404,
                               "execution data is not available for this execution")
        try:
            payload = node_io_view(parsed, name, run_index)
        except LookupError as e:
            return self._error(404, str(e))
        self._send(200, payload)

    def _sync_status(self):
        con = history()
        try:
            st = dict(con.execute("SELECT key, value FROM sync_state"))
        finally:
            con.close()
        result = {}
        if st.get("last_result"):
            try:
                result = json.loads(st["last_result"])
            except ValueError:
                result = {}
        self._send(200, {
            "running": STATE.running,
            "step": STATE.step if STATE.running else st.get("last_step", ""),
            "fetchConfigured": os.path.exists(CONFIG["n8n_db"]),
            "last": {
                "ok": st.get("last_ok") == "1",
                "step": st.get("last_step", ""),
                "error": st.get("last_error") or None,
                "finishedAt": st.get("last_finished") or None,
                "result": result,
            },
        })


def main():
    db_dir = os.path.dirname(CONFIG["history_db"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    schema.init_history(CONFIG["history_db"])
    load_config_file()
    set_state("running", "0")
    server = ThreadingHTTPServer((CONFIG["bind"], CONFIG["port"]), Handler)
    threading.Thread(target=sync_loop, daemon=True).start()
    print(f"n8n-viewer backend on {CONFIG['bind']}:{CONFIG['port']} "
          f"(n8n db: {CONFIG['n8n_db']}, history: {CONFIG['history_db']})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
