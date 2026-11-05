"""Incremental sync engine: n8n's SQLite (read-only) -> the viewer history.

The n8n database is never written to and never copied as a file: every sync
issues ordinary SELECTs against the live database. n8n runs its SQLite in WAL
mode, so read-only connections never block the running instance.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import schema

LOG = logging.getLogger("n8n-viewer.sync")

# Rows re-read on every sync so late transitions (running -> finished,
# soft-deletes) are picked up even after their first import.
RESCAN_WINDOW = 500
# Per-node cap for the FTS index text (keeps the index lean on huge payloads).
NODE_TEXT_LIMIT = 200_000
# S8: per-execution payload cap (N8N_VIEWER_MAX_BLOB_MB in app.CONFIG). A
# blob above it is neither loaded nor re-serialized nor indexed: the execution
# is stored as data_missing with a parse_error reason instead, so one giant
# execution cannot bloat the history or stall the sync.
DEFAULT_MAX_BLOB_BYTES = 50 * 1024 * 1024

# Measured against n8n 2.29.10 (database.sqlite in the Docker volume).
# n8n keeps evolving this schema, so the set is checked on every sync and a
# mismatch fails the sync loudly instead of corrupting the history.
EXPECTED_COLUMNS = {
    "execution_entity": ["id", "workflowId", "status", "startedAt", "stoppedAt",
                          "mode", "deletedAt", "jsonSizeBytes"],
    "execution_data": ["executionId", "data"],
    # kept minimal on purpose: only the contract the viewer actually needs
    "workflow_entity": ["id", "name", "active"],
}

_DATE_FMT = "%Y-%m-%d %H:%M:%S.%f"


class SyncError(Exception):
    """Raised when the n8n database cannot be read or its schema changed."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def flatted_resolve(raw):
    """Decode a flatted array (n8n >= 2.x stores execution data this way).

    The stored JSON is a list of unique values: the root is entry 0 and every
    string that appears inside the structure is an index into the list; the
    fetched value is the real value (a shared object, or a primitive). This
    mirrors the parser shipped inside n8n (@n8n/backend-common
    flatted-async / flatted), including its shared-reference and cycle
    handling, ported to plain Python.

    S16: an empty array decodes to None instead of raising IndexError here
    (the caller then records the row as a parse_error).
    """
    input_arr = raw
    if not input_arr:
        return None
    parsed = set()

    def fetch(s):
        if isinstance(s, str) and s.isdigit():
            i = int(s)
            if i < len(input_arr):
                return input_arr[i]
        return None

    def revive(node):
        if isinstance(node, dict):
            for k in list(node.keys()):
                v = node[k]
                if isinstance(v, str):
                    tmp = fetch(v)
                    if isinstance(tmp, (dict, list)) and id(tmp) not in parsed:
                        parsed.add(id(tmp))
                        node[k] = revive(tmp)
                    else:
                        node[k] = tmp
        elif isinstance(node, list):
            for i in range(len(node)):
                v = node[i]
                if isinstance(v, str):
                    tmp = fetch(v)
                    if isinstance(tmp, (dict, list)) and id(tmp) not in parsed:
                        parsed.add(id(tmp))
                        node[i] = revive(tmp)
                    else:
                        node[i] = tmp
        return node

    root = input_arr[0]
    if isinstance(root, (dict, list)):
        return revive(root)
    return root

def decode_execution_data(text):
    """Decode the execution_data.data column into a plain Python object.

    Accepts both the current flatted list format and the legacy dict format
    (older n8n versions stored the IExecutionData object directly).
    """
    value = json.loads(text)
    if isinstance(value, list):
        return flatted_resolve(value)
    return value

def open_n8n(path):
    if not os.path.exists(path):
        raise SyncError(f"n8n database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    return con


def check_schema(con):
    for table, cols in EXPECTED_COLUMNS.items():
        info = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if not info:
            raise SyncError(f"n8n schema not recognized: table {table} is missing")
        missing = [c for c in cols if c not in info]
        if missing:
            raise SyncError(
                f"n8n schema not recognized: {table} is missing columns {missing}")


def _duration_ms(started, stopped):
    if not started or not stopped:
        return None
    try:
        a = datetime.strptime(started, _DATE_FMT)
        b = datetime.strptime(stopped, _DATE_FMT)
        return int((b - a).total_seconds() * 1000)
    except (ValueError, TypeError):
        return None


def _error_of(result_data):
    err = result_data.get("error")
    if not isinstance(err, dict):
        return None, None
    msg = err.get("message")
    node = err.get("node")
    if isinstance(node, dict):
        node = node.get("name")
    return (str(msg) if msg else None), (str(node) if node else None)


def _iter_strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _iter_strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _iter_strings(v, out)


def node_texts(parsed):
    """{node_name: flattened text} for the FTS index."""
    run_data = (parsed.get("resultData") or {}).get("runData") or {}
    out = {}
    for node, runs in run_data.items():
        parts = []
        for run in runs or []:
            _iter_strings(run, parts)
        text = " ".join(parts)
        if text:
            out[str(node)] = text[:NODE_TEXT_LIMIT]
    return out


def _too_large(size, cap, what):
    """S8/S15: the parse_error recorded for a payload that does not fit."""
    return (f"execution data too large: {what} {size} bytes, over the "
            f"{cap} byte cap (N8N_VIEWER_MAX_BLOB_MB)")[:200]


def _state(con):
    return dict(con.execute("SELECT key, value FROM sync_state"))


def _set_state(con, key, value):
    con.execute(
        "INSERT INTO sync_state(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def run_sync(n8n_path, history_path, progress=None, retention_cutoff=None,
             max_blob_bytes=None):
    """Import the delta of n8n executions into the history.

    Returns {"added": n, "updated": n, "skipped": n}, plus counters that
    are only present when non-zero so an ordinary pass keeps the previous
    shape (and /api/sync last.result stays compatible):
      * retentionSkipped  rows older than `retention_cutoff`, a subset of
                          `skipped`: they were not imported at all because
                          retention would delete them at the end of this same
                          sync
      * oversizedBlobs    rows whose payload is over `max_blob_bytes`, so it
                          was never loaded, serialized or indexed
      * invalidRows       rows with a NULL in startedAt/status/workflowId,
                          a subset of `skipped`: they were skipped instead of
                          failing the history's NOT NULL columns (S17)
    Rows inside the rescan window whose indexed state did not change are not
    re-read, re-serialized nor re-written: they only count in `skipped`
    (S23).
    Raises SyncError (or sqlite3 errors) on failure; the history transaction
    is rolled back so a failed sync never leaves partial data behind.
    """
    if max_blob_bytes is None:
        max_blob_bytes = DEFAULT_MAX_BLOB_BYTES
    n8n = open_n8n(n8n_path)
    schema.init_history(history_path)  # idempotent; allows standalone first run
    hist = sqlite3.connect(history_path, timeout=15)
    hist.execute("PRAGMA busy_timeout=15000")
    added = updated = skipped = 0
    retention_skipped = oversized = invalid = 0
    try:
        check_schema(n8n)
        state = _state(hist)
        last_id = int(state.get("last_id", "0"))
        rescan_from = max(0, last_id - RESCAN_WINDOW)

        # ---- fetch: workflows (full set; n8n keeps them, they are small)
        # 2.x marks removal with isArchived; older versions used deletedAt.
        wf_cols = [r[1] for r in n8n.execute("PRAGMA table_info(workflow_entity)")]
        if "isArchived" in wf_cols:
            deleted_expr = "isArchived"
        elif "deletedAt" in wf_cols:
            deleted_expr = "deletedAt"
        else:
            deleted_expr = "NULL"
        for wf in n8n.execute(
                f"SELECT id, name, active, {deleted_expr} FROM workflow_entity"):
            hist.execute(
                "INSERT INTO workflows(id, name, active, deleted_at, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, active=excluded.active, "
                "deleted_at=excluded.deleted_at, updated_at=excluded.updated_at",
                (wf[0], wf[1], 1 if wf[2] else 0,
                 "archived" if wf[3] else None, _now()))

        # ---- fetch: execution delta
        rows = list(n8n.execute(
            "SELECT id, workflowId, status, mode, startedAt, stoppedAt, "
            "deletedAt, jsonSizeBytes "
            "FROM execution_entity WHERE id > ? ORDER BY id", (rescan_from,)))
        if progress:
            progress("fetch")
        all_ids = []
        new_ids = []
        max_id = last_id
        for (eid, wf, status, mode, started, stopped, deleted, jsize) in rows:
            # Gap accounting must see every id n8n still has, imported or not
            all_ids.append(eid)
            max_id = max(max_id, eid)
            if eid > last_id:
                new_ids.append(eid)
            if wf is None or status is None or started is None:
                # S17: n8n writes startedAt when the execution actually starts,
                # so a row can be read while one of these columns is still
                # NULL. started_at/status/workflow_id are NOT NULL in the
                # history, so importing such a row raised IntegrityError and
                # rolled back the whole sync: one bad row froze the history
                # forever. It is skipped instead (counted in `invalidRows` and
                # in `skipped`), and the cursor still moves past it; the row is
                # inside the rescan window, so a later pass imports it as soon
                # as n8n fills the column in. Alternative rejected: sentinel
                # values would index a row the list cannot order (`started_at`)
                # nor filter (`workflow_id`, `status`) meaningfully.
                invalid += 1
                skipped += 1
                continue
            if retention_cutoff is not None and started and started < retention_cutoff:
                # S7: retention deletes anything older than the cutoff at the
                # end of this very sync, so downloading, serializing and
                # indexing it would only be undone a moment later (and again
                # on every pass, because the rescan window keeps re-reading
                # these ids). Reported on its own as `retentionSkipped` and
                # also counted in `skipped` (that is what it is: a row seen
                # and not imported), so the sync result the viewer already
                # reads keeps its meaning. It never touches the gap math:
                # these ids do exist in n8n.
                retention_skipped += 1
                skipped += 1
                continue
            old = hist.execute(
                "SELECT status, stopped_at, deleted_at FROM executions WHERE id=?",
                (eid,)).fetchone()
            changed = (old is None or old[0] != status or old[1] != stopped
                       or old[2] != deleted)
            if old is None:
                added += 1
            elif changed:
                updated += 1
            else:
                # S23: the row is inside the rescan window and nothing the
                # history indexes changed (status, stoppedAt, deletedAt), so
                # the payload of n8n's 936 MB database is not read, decoded,
                # re-serialized nor re-written and the FTS rows are left as
                # they are. That is the pass's whole cost on a quiet cycle:
                # without this, every one of the ~RESCAN_WINDOW rows did all
                # of that work and refreshed its imported_at for nothing.
                skipped += 1
                continue

            data_text = None
            texts = {}
            parse_error = None
            data_missing = 0
            parsed = None
            cap_hit = False
            if jsize is not None and jsize > max_blob_bytes:
                # S8: the blob is skipped before it is read: n8n's own count
                # is enough to know it does not fit, so nothing is loaded,
                # re-serialized or indexed for it.
                cap_hit = True
                oversized += 1
                data_missing = 1
                parse_error = _too_large(jsize, max_blob_bytes, "n8n reports")
            else:
                row = n8n.execute(
                    "SELECT data, workflowData FROM execution_data "
                    "WHERE executionId=?", (eid,)).fetchone()
                data_raw = wf_raw = None
                if row is not None:
                    data_raw, wf_raw = row
                if data_raw is None:
                    data_missing = 1  # pruned by n8n or never written
                elif jsize is None and len(data_raw) > max_blob_bytes:
                    # S15: with no declared size the cap is decided on the real
                    # length, before decoding: otherwise a row whose
                    # jsonSizeBytes is NULL smuggled any size of payload into
                    # the history.
                    cap_hit = True
                    oversized += 1
                    data_missing = 1
                    parse_error = _too_large(len(data_raw), max_blob_bytes,
                                             "the stored blob is")
                else:
                    try:
                        parsed = decode_execution_data(data_raw)
                    except (ValueError, TypeError, RecursionError,
                            MemoryError, IndexError, AttributeError) as e:
                        parse_error = f"cannot decode execution data: {e}"[:200]
                        data_missing = 1
                    else:
                        if parsed is None:
                            # S16: an empty flatted array decodes to nothing
                            parse_error = ("cannot decode execution data: empty "
                                           "flatted array")[:200]
                            data_missing = 1
                        elif not isinstance(parsed, dict):
                            # S16/B3: a non-object root (a number, a string, a
                            # bare list) has no resultData/runData at all: it
                            # used to reach _error_of/node_texts and die there
                            # with AttributeError, outside the caller's except
                            parse_error = ("cannot decode execution data: root "
                                           "is not an object")[:200]
                            data_missing = 1
                            parsed = None
                # n8n >= 2.x keeps the workflow snapshot in its own column;
                # merge it into the stored blob so the detail view and the
                # diagram work even for rows whose run data was pruned. Not
                # for a capped row: the declared-size branch never touches the
                # data row at all, and the by-length branch must behave the
                # same (nothing read is stored, only the parse_error marker).
                if wf_raw and not cap_hit:
                    try:
                        if parsed is None:
                            parsed = {}
                        parsed["workflowData"] = json.loads(wf_raw)
                    except (ValueError, TypeError, MemoryError):
                        pass  # keep whatever the data column gave us
            # Serializing and indexing are part of the same step: flatted
            # revives shared references, so a cyclic payload reaches this
            # point as a real cycle and json.dumps (or the string walk)
            # rejects it. Such an execution is stored as parse_error with its
            # data marked missing instead of aborting the whole sync, and
            # nothing is serialized again for a blob being discarded.
            try:
                if parsed is not None:
                    data_text = json.dumps(parsed)
                    texts = node_texts(parsed)
            except (ValueError, TypeError, RecursionError, MemoryError,
                    AttributeError) as e:
                parse_error = f"cannot encode execution data: {e}"[:200]
                data_missing = 1
                data_text = None
                texts = {}
            if data_text is not None and len(data_text) > max_blob_bytes:
                # S15: the declared size is not trustworthy either way, so the
                # serialized length is re-checked before anything is written:
                # an undersized jsonSizeBytes cannot smuggle a huge blob in.
                # Treated exactly like the declared-size case above.
                oversized += 1
                data_missing = 1
                parse_error = _too_large(len(data_text), max_blob_bytes,
                                         "the serialized blob is")
                data_text = None
                texts = {}
            last_node = None
            if isinstance(parsed, dict):
                candidate = (parsed.get("resultData") or {}).get("lastNodeExecuted")
                if isinstance(candidate, str):
                    last_node = candidate
            error_message, error_node = _error_of(
                ((parsed or {}).get("resultData") or {}))
            hist.execute(
                "INSERT INTO executions(id, workflow_id, status, mode, started_at, "
                "stopped_at, duration_ms, last_node, error_message, error_node, "
                "json_size, deleted_at, data_missing, parse_error, imported_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                "workflow_id=excluded.workflow_id, stopped_at=excluded.stopped_at, "
                "duration_ms=excluded.duration_ms, error_message=excluded.error_message, "
                "error_node=excluded.error_node, json_size=excluded.json_size, "
                "deleted_at=excluded.deleted_at, data_missing=excluded.data_missing, "
                "parse_error=excluded.parse_error",
                (eid, wf, status, mode, started, stopped,
                 _duration_ms(started, stopped), last_node, error_message,
                 error_node, jsize, deleted, data_missing, parse_error, _now()))
            if data_text is not None:
                hist.execute(
                    "INSERT INTO executions_data(execution_id, data) VALUES(?,?) "
                    "ON CONFLICT(execution_id) DO UPDATE SET data=excluded.data",
                    (eid, data_text))
            hist.execute(
                "DELETE FROM fts_executions WHERE execution_id=?", (str(eid),))
            for node, text in texts.items():
                hist.execute(
                    "INSERT INTO fts_executions(execution_id, workflow_id, "
                    "node_name, text) VALUES(?,?,?,?)",
                    (str(eid), wf, node, text))

        # ---- import: gaps (ids n8n hard-deleted while we were running)
        missing = 0
        if all_ids:
            if last_id == 0:
                lo, hi = min(all_ids), max(all_ids)
                distinct = n8n.execute(
                    "SELECT count(DISTINCT id) FROM execution_entity "
                    "WHERE id BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
                missing = (hi - lo + 1) - distinct
            elif new_ids:
                hi = max(new_ids)
                missing = (hi - last_id) - len(new_ids)
        if missing > 0:
            _set_state(hist, "import_issues",
                       int(state.get("import_issues", "0")) + missing)
        if progress:
            progress("import")
        if all_ids:
            _set_state(hist, "last_id", max_id)
        hist.commit()
    except Exception:
        hist.rollback()
        raise
    finally:
        n8n.close()
        hist.close()
    if retention_skipped:
        LOG.debug("sync: %d execution(s) older than the retention cutoff were "
                  "not imported", retention_skipped)
    if oversized:
        # one line per sync, with the count: a single oversized execution
        # must not turn into one log line per row
        LOG.warning("sync: %d execution(s) stored without their payload: n8n "
                    "reports more than the %d byte cap "
                    "(N8N_VIEWER_MAX_BLOB_MB)", oversized, max_blob_bytes)
    if invalid:
        # one line per sync too: a n8n row still being written must not turn
        # into one log line per pass
        LOG.warning("sync: %d execution row(s) skipped: startedAt/status/"
                    "workflowId is NULL in n8n (retried while in the rescan "
                    "window)", invalid)
    result = {"added": added, "updated": updated, "skipped": skipped}
    if retention_skipped:
        result["retentionSkipped"] = retention_skipped
    if oversized:
        result["oversizedBlobs"] = oversized
    if invalid:
        result["invalidRows"] = invalid
    return result
