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
"""
import json
import os
import re
import sqlite3
import threading
import time
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
}

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


def history():
    con = sqlite3.connect(CONFIG["history_db"], timeout=15)
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
        result = syncmod.run_sync(CONFIG["n8n_db"], CONFIG["history_db"], progress)
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
    if not isinstance(runs, list) or run_index >= len(runs):
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
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

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
            self._error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        if urlparse(self.path).path != "/api/sync":
            return self._error(404, "not found")
        if self.headers.get("x-viewer-action") != "sync":
            return self._error(403, "x-viewer-action: sync header required")
        if not start_sync():
            return self._error(409, "sync already running")
        return self._send(200, {"ok": True})

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
            first, last = con.execute(
                "SELECT min(started_at), max(started_at) FROM executions").fetchone()
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
        })

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
    set_state("running", "0")
    server = ThreadingHTTPServer((CONFIG["bind"], CONFIG["port"]), Handler)
    threading.Thread(target=sync_loop, daemon=True).start()
    print(f"n8n-viewer backend on {CONFIG['bind']}:{CONFIG['port']} "
          f"(n8n db: {CONFIG['n8n_db']}, history: {CONFIG['history_db']})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
