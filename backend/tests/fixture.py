"""Build a synthetic n8n 2.x database with the exact tables/columns the sync
engine reads (execution_entity, execution_data, workflow_entity), measured
against n8n 2.29.10:

  * execution_entity: no lastNodeExecuted / executionDataId columns
  * execution_data:  executionId + data + workflowData (separate column)
  * the data column stores a FLATTED array: entry 0 is the root and every
    string inside the structure is the string form of the index of the real
    value in the array (n8n's dedup format, see sync.flatted_resolve)

Scenario covered by the default fixture (see create_n8n_db):
  * 3 workflows, one soft-deleted (deletedAt set)
  * 9 executions, ids 101..110 with a hard hole at 109 (gap detection)
  * statuses: success / error / crashed / running / soft-deleted
  * one execution with a missing execution_data row (dataMissing)
  * one execution with inputOverride + multi-branch output + loop-back
    connection + a binary item reference
  * one execution whose data contains searchable text ("ACME ...")
  * verbatim n8n-style dates ("YYYY-MM-DD HH:mm:ss.mmm")
  * node input provenance in the n8n >= 2.x shape (previousNode /
    previousNodeOutput / previousNodeRun)
"""
import json
import sqlite3

DATES = {
    101: "2026-10-20 08:00:00.000",
    102: "2026-10-20 09:00:00.000",
    103: "2026-10-21 09:00:00.000",
    104: "2026-10-21 10:00:00.000",
    105: "2026-10-22 09:00:00.000",
    106: "2026-10-22 11:00:00.000",
    107: "2026-10-21 12:00:00.000",
    108: "2026-10-23 09:00:00.000",
    110: "2026-10-28 09:00:00.000",
}


def flatted_strify(root):
    """Serialize a Python object into n8n's flatted array format.

    Mirrors the format n8n writes to execution_data.data: a JSON array of
    unique values (dicts, lists AND strings) in first-seen order, entry 0 is
    the root, and every occurrence of a previously seen value is replaced by
    the string form of its index. Numbers, booleans and null stay inline.
    Decoded back by sync.flatted_resolve.
    """
    arr = []
    idx = {}

    def assign(o):
        if isinstance(o, (dict, list, str)) and id(o) not in idx:
            idx[id(o)] = len(arr)
            arr.append(o)
            if isinstance(o, dict):
                for v in o.values():
                    assign(v)
            elif isinstance(o, list):
                for v in o:
                    assign(v)

    assign(root)

    def enc_value(v):
        if id(v) in idx:
            return str(idx[id(v)])
        if isinstance(v, dict):
            return {k: enc_value(x) for k, x in v.items()}
        if isinstance(v, list):
            return [enc_value(x) for x in v]
        return v

    # Array entries store dicts/lists with their nested references, but
    # string entries are kept raw (they are the referred-to values).
    def enc_entry(o):
        if isinstance(o, dict):
            return {k: enc_value(x) for k, x in o.items()}
        if isinstance(o, list):
            return [enc_value(x) for x in o]
        return o

    return [enc_entry(o) for o in arr]


def _run(start, exec_time=1, source=None, items=None, error=None):
    """One node run in the n8n >= 2.x shape.

    n8n stores the input provenance in run["source"] as a LIST of source
    objects (previousNode / previousNodeOutput / previousNodeRun); runs with
    no upstream node carry an empty list.
    """
    run = {
        "startTime": start, "executionIndex": 0, "executionTime": exec_time,
        "executionStatus": "error" if error else "success",
    }
    if source is not None:
        node, out, r = source
        run["source"] = [{"previousNode": node,
                           "previousNodeOutput": out,
                           "previousNodeRun": r}]
    else:
        run["source"] = []
    if items is not None or error is not None:
        data = {"main": items or []}
        run["data"] = data
    if error is not None:
        run["error"] = error
    return run


def _data_101():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Start"},
        "resultData": {
            "runData": {
                "Start": [_run("2026-10-20T08:00:00.000Z",
                                items=[[{"json": {"hello": "world"}}]])],
            },
            "lastNodeExecuted": "Start",
        },
    }


def _data_102():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Node A"},
        "resultData": {
            "runData": {
                "Node A": [_run("2026-10-20T09:00:00.000Z",
                                 items=[[{"json": {"x": 1}}]])],
                "Node B": [_run("2026-10-20T09:00:01.000Z", exec_time=2,
                                 source=("Node A", 0, 0),
                                 error={"message": "boom from node B"})],
            },
            "error": {"message": "boom from node B", "node": {"name": "Node B"}},
            "lastNodeExecuted": "Node B",
        },
    }


def _data_103():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Crashy"},
        "resultData": {
            "runData": {
                "Crashy": [_run("2026-10-21T09:00:00.000Z", exec_time=5,
                                 items=[[{"json": {"n": 1}}]])],
            },
        },
    }


def _data_104():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Long"},
        "resultData": {
            "runData": {
                "Long": [_run("2026-10-21T10:00:00.000Z",
                               items=[[{"json": {"t": 0}}]])],
            },
        },
    }


def _data_105():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Old"},
        "resultData": {
            "runData": {
                "Old": [_run("2026-10-22T09:00:00.000Z",
                              items=[[{"json": {"old": True}}]])],
            },
            "lastNodeExecuted": "Old",
        },
    }


def _data_107():
    return {
        "version": 1,
        "startData": {
            "sourceNodeName": "Trigger",
            "inputOverride": {"sourceData": {"input": [
                {"items": [{"json": {"manual": True}}]}]}},
        },
        "resultData": {
            "runData": {
                "Trigger": [_run("2026-10-21T12:00:00.000Z",
                                  items=[[{"json": {"order": "ACME-42"}},
                                          {"json": {"order": "ACME-43"}}]])],
                "Brancher": [_run("2026-10-21T12:00:01.000Z", exec_time=2,
                                   source=("Trigger", 0, 0),
                                   items=[[{"json": {"a": 1}}, {"json": {"b": 2}}],
                                           [{"json": {"c": 3}}]])],
                "Sink": [_run("2026-10-21T12:00:02.000Z", exec_time=3,
                               source=("Brancher", 1, 0),
                               items=[[{"json": {"ok": True},
                                         "binary": {"file": {
                                             "fileName": "report.pdf",
                                             "mimeType": "application/pdf"}}}]]
                               )],
            },
            "lastNodeExecuted": "Sink",
        },
    }


def _data_108():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Notes"},
        "resultData": {
            "runData": {
                "Notes": [_run("2026-10-23T09:00:00.000Z",
                                items=[[{"json": {
                                    "summary": "contraseña secreta del cliente ACME registrada",
                                    "count": 3}}]])],
            },
            "lastNodeExecuted": "Notes",
        },
    }


def _data_110():
    return {
        "version": 1,
        "startData": {"sourceNodeName": "Live"},
        "resultData": {
            "runData": {
                "Live": [_run("2026-10-28T09:00:00.000Z",
                               items=[[{"json": {"live": True}}]])],
            },
        },
    }


WORKFLOW_DATA = {
    1: {"nodes": [{"name": "Start", "type": "n8n-nodes-base.manualTrigger",
                   "position": [0, 0], "parameters": {}}],
        "connections": {"Start": {"main": []}}},
    2: {"nodes": [
            {"name": "Node A", "type": "n8n-nodes-base.manualTrigger",
             "position": [0, 0], "parameters": {}},
            {"name": "Node B", "type": "n8n-nodes-base.httpRequest",
             "position": [220, 0], "parameters": {"url": "http://x"}},
        ],
        "connections": {
            "Node A": {"main": [[{"node": "Node B", "index": 0}]]},
            "Node B": {"main": []},
        }},
    3: {"nodes": [{"name": "Crashy", "type": "n8n-nodes-base.code",
                   "position": [0, 0], "parameters": {}}],
        "connections": {"Crashy": {"main": []}}},
    4: {"nodes": [{"name": "Long", "type": "n8n-nodes-base.wait",
                   "position": [0, 0], "parameters": {}}],
        "connections": {"Long": {"main": []}}},
    5: {"nodes": [{"name": "Old", "type": "n8n-nodes-base.manualTrigger",
                   "position": [0, 0], "parameters": {}}],
        "connections": {"Old": {"main": []}}},
    6: {"nodes": [
            {"name": "Trigger", "type": "n8n-nodes-base.manualTrigger",
             "position": [0, 0], "parameters": {"mode": "manual"}},
            {"name": "Brancher", "type": "n8n-nodes-base.if",
             "position": [200, 0], "parameters": {"rules": []}},
            {"name": "Sink", "type": "n8n-nodes-base.httpRequest",
             "position": [400, 200], "parameters": {"url": "https://example.com"}},
        ],
        "connections": {
            "Trigger": {"main": [[{"node": "Brancher", "index": 0}]]},
            "Brancher": {"main": [[{"node": "Sink", "index": 0}],
                                    [{"node": "Brancher", "index": 0}]]},
            "Sink": {"main": []},
        }},
    7: {"nodes": [{"name": "Notes", "type": "n8n-nodes-base.code",
                   "position": [0, 0], "parameters": {}}],
        "connections": {"Notes": {"main": []}}},
    8: {"nodes": [{"name": "Live", "type": "n8n-nodes-base.scheduleTrigger",
                   "position": [0, 0], "parameters": {}}],
        "connections": {"Live": {"main": []}}},
}

# execution id -> data row key (None = no execution_data row at all)
DATA_FOR = {101: 1, 102: 2, 103: 3, 104: 4, 105: 5, 106: None,
            107: 6, 108: 7, 110: 8}

DATA_BUILDERS = {1: _data_101, 2: _data_102, 3: _data_103, 4: _data_104,
                 5: _data_105, 6: _data_107, 7: _data_108, 8: _data_110}

# (id, workflowId, status, stoppedAt, mode, deletedAt, jsonSizeBytes)
EXECUTIONS = [
    (101, "wfA", "success", "2026-10-20 08:00:05.000", "manual", None, 1234),
    (102, "wfA", "error",   "2026-10-20 09:00:01.500", "manual", None, 2048),
    (103, "wfA", "crashed", None,                     "cron",   None, 512),
    (104, "wfA", "running", None,                     "manual", None, None),
    (105, "wfB", "success", "2026-10-22 09:00:01.000", "manual", "2026-10-25 00:00:00.000", 256),
    (106, "wfB", "success", "2026-10-22 11:00:01.000", "webhook", None, None),
    (107, "wfA", "success", "2026-10-21 12:00:03.000", "manual", None, 4096),
    (108, "wfA", "success", "2026-10-23 09:00:02.000", "cron",   None, 300),
    (110, "wfA", "running", None,                     "cron",   None, None),
]


def create_n8n_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE workflow_entity (
            id TEXT PRIMARY KEY, name TEXT, active INTEGER,
            deletedAt TEXT, isArchived INTEGER);
        CREATE TABLE execution_entity (
            id INTEGER PRIMARY KEY,
            workflowId TEXT,
            status TEXT,
            stoppedAt TEXT,
            mode TEXT,
            deletedAt TEXT,
            startedAt TEXT,
            jsonSizeBytes INTEGER);
        CREATE TABLE execution_data (
            executionId INTEGER PRIMARY KEY,
            data TEXT,
            workflowData TEXT);
    """)
    con.executemany(
        "INSERT INTO workflow_entity(id, name, active, deletedAt, isArchived) "
        "VALUES(?,?,?,?,?)",
        [("wfA", "Workflow A", 1, None, 0),
         ("wfB", "Workflow B", 0, None, 0),
         ("wfGone", "Deleted WF", 0, "2026-10-20 10:00:00.000", 1)])
    con.executemany(
        "INSERT INTO execution_entity(id, workflowId, status, stoppedAt, mode, "
        "deletedAt, startedAt, jsonSizeBytes) VALUES(?,?,?,?,?,?,?,?)",
        [(eid, wf, status, stopped, mode, deleted, DATES[eid], jsize)
         for (eid, wf, status, stopped, mode, deleted, jsize) in EXECUTIONS])
    for eid, key in DATA_FOR.items():
        if key is None:
            continue
        con.execute(
            "INSERT INTO execution_data(executionId, data, workflowData) "
            "VALUES(?,?,?)",
            (eid,
             json.dumps(flatted_strify(DATA_BUILDERS[key]()), ensure_ascii=False),
             json.dumps(WORKFLOW_DATA[key], ensure_ascii=False)))
    con.commit()
    con.close()


def add_execution(path, eid, workflow, status, started, stopped=None, jsize=None,
                  blob=None, wf=None, raw_data=None):
    """Append a new execution (used to test incremental sync).

    blob/wf override the default payload, so a test can store a shape the
    builders above do not produce (e.g. a self-referencing structure, which
    flatted_strify encodes faithfully and flatted_resolve revives as a cycle).
    raw_data is written verbatim into execution_data.data, for the payloads a
    well-formed flatted encoder would never produce (an empty array, an
    undecodable blob).
    """
    if raw_data is not None and blob is not None:
        raise ValueError("pass either blob or raw_data, not both")
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO execution_entity(id, workflowId, status, stoppedAt, mode, "
        "deletedAt, startedAt, jsonSizeBytes) VALUES(?,?,?,?,?,?,?,?)",
        (eid, workflow, status, stopped, "manual", None, started, jsize))
    if blob is None:
        blob = {
            "version": 1,
            "startData": {"sourceNodeName": "New"},
            "resultData": {
                "runData": {
                    "New": [_run(started.replace(" ", "T") + "Z",
                                  items=[[{"json": {"fresh": True}}]])],
                },
                "lastNodeExecuted": "New",
            },
        }
    if wf is None:
        wf = {"nodes": [{"name": "New", "type": "n8n-nodes-base.manualTrigger",
                         "position": [0, 0], "parameters": {}}],
              "connections": {"New": {"main": []}}}
    data_text = (raw_data if raw_data is not None
                 else json.dumps(flatted_strify(blob), ensure_ascii=False))
    con.execute("INSERT INTO execution_data(executionId, data, workflowData) "
                "VALUES(?,?,?)",
                (eid, data_text, json.dumps(wf, ensure_ascii=False)))
    con.commit()
    con.close()


def add_null_row(path, eid, blob=None):
    """Append an execution_entity row with NULL startedAt/status/workflowId.

    n8n writes startedAt when the execution actually starts, so a row can be
    read while those columns are still NULL. Used to test the S17 guard: such
    a row must never hit the history's NOT NULL columns.
    """
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO execution_entity(id, workflowId, status, stoppedAt, mode, "
        "deletedAt, startedAt, jsonSizeBytes) VALUES(?,?,?,?,?,?,?,?)",
        (eid, None, None, None, "manual", None, None, None))
    con.commit()
    con.close()
    if blob is not None:
        add_execution_data(path, eid, blob)


def add_execution_data(path, eid, blob=None, wf=None, raw_data=None):
    """Attach/replace the execution_data row of an existing execution."""
    con = sqlite3.connect(path)
    data_text = (raw_data if raw_data is not None
                 else json.dumps(flatted_strify(blob), ensure_ascii=False))
    con.execute("INSERT INTO execution_data(executionId, data, workflowData) "
                "VALUES(?,?,?) ON CONFLICT(executionId) DO UPDATE SET "
                "data=excluded.data, workflowData=excluded.workflowData",
                (eid, data_text,
                 json.dumps(wf, ensure_ascii=False) if wf is not None else None))
    con.commit()
    con.close()


def finish_execution(path, eid, stopped, status="success"):
    """Flip an execution (used to test running->finished transitions)."""
    con = sqlite3.connect(path)
    con.execute("UPDATE execution_entity SET stoppedAt=?, status=? WHERE id=?",
                (stopped, status, eid))
    con.commit()
    con.close()
