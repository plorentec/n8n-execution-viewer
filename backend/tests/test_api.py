"""API tests: the /api/* contract the static viewer consumes (spec S3).

Boots the real HTTP server on an ephemeral port against a synthetic n8n
fixture + a throwaway history database.
"""
from http.client import HTTPConnection
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import fixture  # noqa: E402
import schema  # noqa: E402
import sync as syncmod  # noqa: E402


def http(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def http_post(url, headers=None, data=None):
    req = urllib.request.Request(url, method="POST", headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.n8n_db = os.path.join(cls.tmp.name, "n8n.sqlite")
        cls.hist_db = os.path.join(cls.tmp.name, "history.sqlite")
        fixture.create_n8n_db(cls.n8n_db)
        app.CONFIG.update({
            "n8n_db": cls.n8n_db,
            "history_db": cls.hist_db,
            "page_size": 50,
            "retention_days": 0,  # reset per class: retention tests mutate it
            "brand": None,  # reset per class: config tests mutate it
        })
        schema.init_history(cls.hist_db)
        syncmod.run_sync(cls.n8n_db, cls.hist_db)
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        return http(self.base + path)


class TestStats(ApiTestBase):
    def test_stats_shape_and_values(self):
        code, body = self.get("/api/stats")
        self.assertEqual(code, 200)
        self.assertEqual(body["executions"], 9)
        self.assertEqual(body["workflows"], 3)
        self.assertEqual(body["byStatus"].get("success"), 5)
        self.assertEqual(body["byStatus"].get("error"), 1)
        self.assertEqual(body["byStatus"].get("crashed"), 1)
        self.assertEqual(body["byStatus"].get("running"), 2)
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(body["importIssues"], 1)
        self.assertEqual(body["firstStartedAt"], "2026-10-20 08:00:00.000")
        self.assertEqual(body["lastStartedAt"], "2026-10-28 09:00:00.000")

    def test_workflows(self):
        code, body = self.get("/api/workflows")
        self.assertEqual(code, 200)
        by = {w["id"]: w for w in body}
        self.assertEqual(set(by), {"wfA", "wfB", "wfGone"})
        self.assertEqual(by["wfA"]["total"], 7)
        self.assertEqual(by["wfA"]["errors"], 2)  # error + crashed
        self.assertEqual(by["wfB"]["total"], 2)
        self.assertTrue(by["wfGone"]["deleted"])
        self.assertFalse(by["wfA"]["deleted"])
        self.assertTrue(by["wfA"]["active"])
        self.assertFalse(by["wfB"]["active"])


class TestExecutionsList(ApiTestBase):
    def test_order_and_fields(self):
        code, body = self.get("/api/executions")
        self.assertEqual(code, 200)
        self.assertEqual([e["id"] for e in body["items"]][0], "110")
        first = body["items"][0]
        self.assertEqual(first["workflowId"], "wfA")
        self.assertEqual(first["workflowName"], "Workflow A")
        self.assertEqual(first["startedAt"], "2026-10-28 09:00:00.000")
        self.assertIsNone(first["durationMs"])
        self.assertIsNone(body["nextBefore"])

    def test_status_filter(self):
        code, body = self.get("/api/executions?status=error")
        self.assertEqual(code, 200)
        self.assertEqual([e["id"] for e in body["items"]], ["102"])
        code, body = self.get("/api/executions?status=weird")
        self.assertEqual(code, 400)

    def test_workflow_filter(self):
        code, body = self.get("/api/executions?workflow=wfB")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(e["id"] for e in body["items"]), ["105", "106"])

    def test_q_exact_id(self):
        code, body = self.get("/api/executions?q=107")
        self.assertEqual(code, 200)
        self.assertEqual([e["id"] for e in body["items"]], ["107"])

    def test_q_fts_text(self):
        code, body = self.get("/api/executions?q=contrase%C3%B1a")
        self.assertEqual(code, 200)
        self.assertEqual([e["id"] for e in body["items"]], ["108"])

    def test_date_range(self):
        code, body = self.get("/api/executions?from=2026-10-21&to=2026-10-22")
        self.assertEqual(code, 200)
        self.assertEqual([e["id"] for e in body["items"]],
                         ["106", "105", "107", "104", "103"])

    def test_pagination_cursor(self):
        app.CONFIG["page_size"] = 3
        try:
            code, page1 = self.get("/api/executions")
            self.assertEqual(code, 200)
            self.assertEqual([e["id"] for e in page1["items"]], ["110", "108", "106"])
            self.assertEqual(page1["nextBefore"], "2026-10-22 11:00:00.000:106")
            code, page2 = self.get("/api/executions?before=" +
                                   urllib.parse.quote(page1["nextBefore"]))
            self.assertEqual([e["id"] for e in page2["items"]], ["105", "107", "104"])
            code, page3 = self.get("/api/executions?before=" +
                                   urllib.parse.quote(page2["nextBefore"]))
            self.assertEqual([e["id"] for e in page3["items"]], ["103", "102", "101"])
            self.assertIsNone(page3["nextBefore"])
        finally:
            app.CONFIG["page_size"] = 50

    def test_deleted_flag_in_list(self):
        code, body = self.get("/api/executions?q=105")
        self.assertEqual(body["items"][0]["deletedAt"], "2026-10-25 00:00:00.000")


class TestExecutionDetail(ApiTestBase):
    def test_detail_nodes_and_connections(self):
        code, body = self.get("/api/executions/107")
        self.assertEqual(code, 200)
        self.assertEqual(body["id"], "107")
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["lastNodeExecuted"], "Sink")
        self.assertEqual(body["dataSize"], 4096)
        self.assertIsNone(body["error"])
        self.assertFalse(body["dataMissing"])
        nodes = {n["name"]: n for n in body["nodes"]}
        self.assertEqual(set(nodes), {"Trigger", "Brancher", "Sink"})
        self.assertEqual(nodes["Trigger"]["position"], [0, 0])
        self.assertTrue(nodes["Brancher"]["executed"])
        self.assertEqual(nodes["Brancher"]["runs"][0]["itemsOut"], [2, 1])
        self.assertEqual(nodes["Sink"]["runs"][0]["status"], "success")
        conns = body["connections"]
        self.assertEqual(conns["Brancher"]["main"][1][0]["node"], "Brancher")  # loop-back

    def test_detail_error_box(self):
        code, body = self.get("/api/executions/102")
        self.assertEqual(code, 200)
        self.assertEqual(body["error"], {"node": "Node B", "message": "boom from node B"})
        node_b = [n for n in body["nodes"] if n["name"] == "Node B"][0]
        self.assertEqual(node_b["runs"][0]["status"], "error")
        self.assertEqual(node_b["runs"][0]["error"], "boom from node B")

    def test_detail_data_missing(self):
        code, body = self.get("/api/executions/106")
        self.assertEqual(code, 200)
        self.assertTrue(body["dataMissing"])
        self.assertEqual(body["nodes"], [])
        self.assertEqual(body["connections"], {})
        self.assertIsNone(body["dataSize"])

    def test_detail_not_found(self):
        code, body = self.get("/api/executions/99999")
        self.assertEqual(code, 404)
        self.assertIn("error", body)


class TestNodeDetail(ApiTestBase):
    def test_url_encoded_node_name(self):
        # The frontend always sends encodeURIComponent(name); names with
        # spaces must resolve to the same run (regression: %20 used to 404).
        code, body = self.get("/api/executions/102/nodes/Node%20A")
        self.assertEqual(code, 200)
        self.assertEqual(body["run"], 0)
        self.assertEqual(body["output"]["main"][0], [{"json": {"x": 1}}])

    def test_input_override(self):
        code, body = self.get("/api/executions/107/nodes/Trigger")
        self.assertEqual(code, 200)
        self.assertEqual(body["run"], 0)
        self.assertEqual(body["input"][0]["from"], "inputOverride")
        self.assertEqual(body["input"][0]["items"], [{"json": {"manual": True}}])
        self.assertEqual(len(body["output"]["main"][0]), 2)
        self.assertEqual(body["parameters"], {"mode": "manual"})

    def test_input_derived_from_source(self):
        code, body = self.get("/api/executions/107/nodes/Brancher")
        self.assertEqual(code, 200)
        self.assertEqual(body["input"][0]["from"],
                         {"node": "Trigger", "run": 0, "output": 0})
        self.assertEqual(len(body["input"][0]["items"]), 2)
        self.assertEqual(len(body["output"]["main"]), 2)

    def test_binary_item_kept_as_reference(self):
        code, body = self.get("/api/executions/107/nodes/Sink")
        self.assertEqual(code, 200)
        self.assertEqual(body["input"][0]["from"],
                         {"node": "Brancher", "run": 0, "output": 1})
        self.assertEqual(body["input"][0]["items"], [{"json": {"c": 3}}])
        item = body["output"]["main"][0][0]
        self.assertEqual(item["binary"]["file"]["fileName"], "report.pdf")
        self.assertEqual(item["binary"]["file"]["mimeType"], "application/pdf")
        self.assertEqual(body["parameters"], {"url": "https://example.com"})

    def test_unknown_node_and_run_404(self):
        code, _ = self.get("/api/executions/107/nodes/Nope")
        self.assertEqual(code, 404)
        code, _ = self.get("/api/executions/107/nodes/Trigger?run=5")
        self.assertEqual(code, 404)

    def test_negative_run_index_404(self):
        # S5: runs[-1] would silently return the last run instead of a 404
        for run in (-1, -5):
            code, body = self.get(f"/api/executions/107/nodes/Trigger?run={run}")
            self.assertEqual(code, 404, f"run={run} must not resolve")
            self.assertIn("error", body)


class TestSyncEndpoint(ApiTestBase):
    def test_status_shape(self):
        code, body = self.get("/api/sync")
        self.assertEqual(code, 200)
        self.assertFalse(body["running"])
        self.assertTrue(body["fetchConfigured"])
        self.assertIn("last", body)
        self.assertIn("result", body["last"])

    def test_post_requires_header(self):
        code, body = http_post(self.base + "/api/sync")
        self.assertEqual(code, 403)
        self.assertIn("error", body)

    def test_post_triggers_sync(self):
        code, body = http_post(self.base + "/api/sync",
                               headers={"x-viewer-action": "sync"})
        self.assertEqual(code, 200)
        deadline = time.time() + 15
        while time.time() < deadline:
            _, s = self.get("/api/sync")
            if not s["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(s["running"])
        self.assertTrue(s["last"]["ok"])
        self.assertEqual(s["last"]["result"]["added"], 0)

    def test_post_409_while_running(self):
        def slow(*a, **k):
            time.sleep(0.6)
            return {"added": 0, "updated": 0, "skipped": 0}
        original = app.syncmod.run_sync
        app.syncmod.run_sync = slow
        self.addCleanup(setattr, app.syncmod, "run_sync", original)
        code, _ = http_post(self.base + "/api/sync",
                            headers={"x-viewer-action": "sync"})
        self.assertEqual(code, 200)
        code, body = http_post(self.base + "/api/sync",
                               headers={"x-viewer-action": "sync"})
        self.assertEqual(code, 409)
        self.assertIn("error", body)
        deadline = time.time() + 10
        while time.time() < deadline:
            _, s = self.get("/api/sync")
            if not s["running"]:
                break
            time.sleep(0.05)


class TestHttpFraming(ApiTestBase):
    """S2/S3: HTTP/1.1 keep-alive framing of POST bodies.

    protocol_version is HTTP/1.1, so a response sent before the request body
    is read leaves those bytes in the stream and the next request on that
    connection is parsed from the body (the client sees a 400/501).
    """

    def conn(self):
        parsed = urllib.parse.urlparse(self.base)
        c = HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        self.addCleanup(c.close)
        return c

    def post(self, conn, path, body, headers=None):
        hdrs = {"Content-Length": str(len(body))}
        hdrs.update(headers or {})
        conn.request("POST", path, body=body, headers=hdrs)
        res = conn.getresponse()
        payload = json.loads(res.read().decode())
        return res.status, payload

    def get_on(self, conn, path):
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, json.loads(res.read().decode())

    def test_403_drains_body(self):
        conn = self.conn()
        code, body = self.post(conn, "/api/config", b'{"retentionDays": 5}', {})
        self.assertEqual(code, 403)
        self.assertIn("error", body)
        code, body = self.get_on(conn, "/api/config")
        self.assertEqual(code, 200, "the next request on that connection broke")

    def test_404_post_drains_body(self):
        conn = self.conn()
        code, _ = self.post(conn, "/api/nope", b'{"x": 1}', {})
        self.assertEqual(code, 404)
        code, body = self.get_on(conn, "/api/stats")
        self.assertEqual(code, 200)
        self.assertEqual(body["executions"], 9)

    def test_409_drains_body(self):
        def slow(*a, **k):
            time.sleep(0.6)
            return {"added": 0, "updated": 0, "skipped": 0}
        original = app.syncmod.run_sync
        app.syncmod.run_sync = slow
        try:
            conn = self.conn()
            code, _ = self.post(conn, "/api/sync", b'{"start": 1}',
                                {"x-viewer-action": "sync"})
            self.assertEqual(code, 200)
            code, body = self.post(conn, "/api/sync", b'{"again": 1}',
                                   {"x-viewer-action": "sync"})
            self.assertEqual(code, 409)
            self.assertIn("error", body)
            code, body = self.get_on(conn, "/api/sync")
            self.assertEqual(code, 200, "the next request on that connection broke")
            deadline = time.time() + 10
            while time.time() < deadline:
                _, s = self.get("/api/sync")
                if not s["running"]:
                    break
                time.sleep(0.05)
        finally:
            app.syncmod.run_sync = original

    def test_413_drains_body(self):
        limit = app.MAX_CONFIG_BYTES
        app.MAX_CONFIG_BYTES = 64
        try:
            conn = self.conn()
            code, body = self.post(conn, "/api/config", b"x" * 200,
                                   {"x-viewer-action": "config"})
            self.assertEqual(code, 413)
            self.assertIn("error", body)
            code, body = self.get_on(conn, "/api/config")
            self.assertEqual(code, 200, "the next request on that connection broke")
        finally:
            app.MAX_CONFIG_BYTES = limit

    def raw_post(self, conn, path, headers):
        conn.putrequest("POST", path)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders()
        res = conn.getresponse()
        payload = json.loads(res.read().decode())
        return res, payload

    def test_negative_content_length_rejected(self):
        # S3: rfile.read(-1) blocks until EOF instead of answering.
        conn = self.conn()
        res, body = self.raw_post(conn, "/api/config", {
            "x-viewer-action": "config", "Content-Length": "-5"})
        self.assertEqual(res.status, 400)
        self.assertIn("Content-Length", body["error"])
        self.assertEqual(res.getheader("Connection"), "close")

    def test_non_numeric_content_length_rejected(self):
        conn = self.conn()
        res, body = self.raw_post(conn, "/api/config", {
            "x-viewer-action": "config", "Content-Length": "abc"})
        self.assertEqual(res.status, 400)
        self.assertIn("Content-Length", body["error"])
        self.assertEqual(res.getheader("Connection"), "close")

    def test_zero_content_length_is_empty_body(self):
        conn = self.conn()
        code, body = self.post(conn, "/api/config", b"",
                               {"x-viewer-action": "config"})
        self.assertEqual(code, 400)
        self.assertIn("expected a JSON object", body["error"])
        code, _ = self.get_on(conn, "/api/config")
        self.assertEqual(code, 200)


class TestSocketTimeout(ApiTestBase):
    """S18: the Handler has a socket timeout, so a client that connects and
    sends nothing cannot pin a worker thread forever, and a timed-out
    keep-alive connection is closed by the stdlib without error noise."""

    def test_handler_has_a_socket_timeout(self):
        self.assertEqual(app.Handler.timeout, 30)

    def test_idle_connection_is_closed_without_an_error_response(self):
        import socket
        # the value the real handler uses, scaled down so the test is quick
        quiet = type("QuietHandler", (app.Handler,),
                     {"timeout": app.Handler.timeout / 50})
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sock = socket.create_connection(server.server_address, timeout=5)
        self.addCleanup(sock.close)
        sock.settimeout(5)
        # nothing is sent: the server must drop the connection (no bytes)
        self.assertEqual(sock.recv(1), b"",
                         "the idle connection was neither closed nor silent")
        # and the server is still healthy
        code, body = http(f"http://127.0.0.1:{server.server_address[1]}/api/stats")
        self.assertEqual(code, 200)
        self.assertEqual(body["executions"], 9)


class TestVerbHandling(ApiTestBase):
    """S19: GET drains a stray body (HTTP/1.1 keep-alive) and the methods with
    no handler answer 405 after draining instead of the stdlib 501."""

    def conn(self):
        parsed = urllib.parse.urlparse(self.base)
        c = HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        self.addCleanup(c.close)
        return c

    def get_on(self, conn, path):
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, json.loads(res.read().decode())

    def test_get_drains_a_stray_body(self):
        conn = self.conn()
        conn.putrequest("GET", "/api/stats")
        conn.putheader("Content-Length", "5")
        conn.endheaders()
        conn.send(b"hello")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(res.read().decode())["executions"], 9)
        # the unread body used to be parsed as the next request line
        code, body = self.get_on(conn, "/api/config")
        self.assertEqual(code, 200, "the GET body desynchronized the connection")
        self.assertIn("retentionDays", body)

    def test_get_with_a_bogus_content_length_is_rejected(self):
        # S3/S19: the body boundary is unknown, so a GET cannot be
        # resynchronized either: answer 400 and close, never read(-1)
        conn = self.conn()
        conn.putrequest("GET", "/api/stats")
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        res = conn.getresponse()
        self.assertEqual(res.status, 400)
        self.assertIn("Content-Length", json.loads(res.read().decode())["error"])
        self.assertEqual(res.getheader("Connection"), "close")

    def test_put_and_delete_answer_405_after_draining(self):
        for method in ("PUT", "DELETE"):
            conn = self.conn()
            conn.request(method, "/api/config", body=b'{"retentionDays": 5}')
            res = conn.getresponse()
            payload = json.loads(res.read().decode())
            self.assertEqual(res.status, 405, f"{method} should not be a 501")
            self.assertIn("error", payload)
            code, _ = self.get_on(conn, "/api/config")
            self.assertEqual(code, 200,
                             f"the {method} body desynchronized the connection")


class TestErrorAfterHeaders(ApiTestBase):
    """S20: a failure after the response headers went out must close the
    connection instead of writing a second status line on the same stream."""

    def raw_get(self, path):
        import socket
        parsed = urllib.parse.urlparse(self.base)
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def test_failure_after_the_headers_closes_the_connection(self):
        def half_send(handler):
            handler._send(200, [])  # status line + headers + body are out
            raise RuntimeError("late failure")

        original = app.Handler._workflows
        app.Handler._workflows = half_send
        try:
            raw = self.raw_get("/api/workflows")
        finally:
            app.Handler._workflows = original
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"), raw)
        self.assertEqual(raw.count(b"HTTP/1.1 "), 1,
                         f"a second status line was written: {raw!r}")
        self.assertNotIn(b"500", raw)

    def test_failure_before_the_headers_still_answers_500(self):
        def boom(handler):
            raise RuntimeError("early failure")

        original = app.Handler._stats
        app.Handler._stats = boom
        try:
            code, body = self.get("/api/stats")
        finally:
            app.Handler._stats = original
        self.assertEqual(code, 500)
        self.assertIn("early failure", body["error"])


class TestStatsStorage(ApiTestBase):
    """S7: /api/stats exposes the storage block (current + estimated disk use)."""

    def test_stats_storage_block(self):
        code, body = self.get("/api/stats")
        self.assertEqual(code, 200)
        st = body["storage"]
        self.assertGreater(st["dbBytes"], 0)
        self.assertGreaterEqual(st["percentOfDisk"], 0.0)
        self.assertLessEqual(st["percentOfDisk"], 100.0)
        self.assertGreater(st["estBytesPerDay"], 0)
        self.assertGreaterEqual(st["projectedBytes30d"], st["dbBytes"])
        self.assertGreaterEqual(st["projectedPercentOfDisk30d"], st["percentOfDisk"])
        self.assertEqual(st["retentionDays"], 0)
        self.assertIn("freeDiskBytes", st)
        self.assertIn("totalDiskBytes", st)


class RetentionBase(ApiTestBase):
    """Base for S6 tests: make the history's old/new split calendar-proof
    by rewriting started_at relative to now (both DBs store UTC
    'YYYY-MM-DD HH:MM:SS.mmm')."""

    @classmethod
    def shift_history_dates(cls, old_ids, new_ids):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S.000"
        con = sqlite3.connect(cls.hist_db)
        for eid in old_ids:
            con.execute("UPDATE executions SET started_at=? WHERE id=?",
                        ((now - timedelta(days=10)).strftime(fmt), eid))
        for eid in new_ids:
            con.execute("UPDATE executions SET started_at=? WHERE id=?",
                        ((now - timedelta(hours=1)).strftime(fmt), eid))
        con.commit()
        con.close()


class TestConfig(ApiTestBase):
    """S9: GET/POST /api/config with persistence in config.json."""

    def config_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.hist_db)), "config.json")

    def post_config(self, payload, header=True):
        headers = {"x-viewer-action": "config"} if header else {}
        return http_post(self.base + "/api/config", headers=headers,
                         data=json.dumps(payload).encode())

    def test_get_config_defaults(self):
        code, body = self.get("/api/config")
        self.assertEqual(code, 200)
        self.assertEqual(body["retentionDays"], 0)
        self.assertIsNone(body["brand"])

    def test_post_retention_applies_and_persists(self):
        code, body = self.post_config({"retentionDays": 30})
        self.assertEqual(code, 200)
        code, body = self.get("/api/config")
        self.assertEqual(body["retentionDays"], 30)
        code, stats = self.get("/api/stats")
        self.assertEqual(stats["retentionDays"], 30)
        self.assertEqual(stats["storage"]["retentionDays"], 30)
        with open(self.config_path()) as f:
            self.assertEqual(json.load(f)["retentionDays"], 30)

    def test_post_brand_roundtrip(self):
        code, _ = self.post_config({"brand": {"title": "ACME Corp", "subtitle": "ops"}})
        self.assertEqual(code, 200)
        code, body = self.get("/api/config")
        self.assertEqual(body["brand"]["title"], "ACME Corp")
        self.assertEqual(body["brand"]["subtitle"], "ops")
        with open(self.config_path()) as f:
            self.assertEqual(json.load(f)["brand"]["title"], "ACME Corp")

    def test_post_invalid_logo_rejected(self):
        code, body = self.post_config({"brand": {"logo": "https://ejemplo.com/x.png"}})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_post_without_header_rejected(self):
        code, body = self.post_config({"retentionDays": 5}, header=False)
        self.assertEqual(code, 403)
        self.assertIn("error", body)

    def test_post_invalid_retention_rejected(self):
        for bad in (-1, "30", None):
            code, body = self.post_config({"retentionDays": bad})
            self.assertEqual(code, 400, f"retentionDays={bad!r} should be rejected")


class TestRetentionOn(RetentionBase):
    """S6: retention_days=2 deletes old history rows (exec + data + fts)."""

    OLD = (101, 102, 103, 104, 105, 106, 107, 108)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.shift_history_dates(cls.OLD, (110,))
        app.CONFIG["retention_days"] = 2

    def test_old_rows_deleted_everywhere(self):
        self.assertTrue(app.run_sync_now())
        con = sqlite3.connect(self.hist_db)
        try:
            gone = con.execute(
                "SELECT count(*) FROM executions WHERE id IN (101,102,103,104,105,106,107,108)"
            ).fetchone()[0]
            self.assertEqual(gone, 0)
            data_left = con.execute(
                "SELECT count(*) FROM executions_data WHERE execution_id IN (101,102,103,104,105,106,107,108)"
            ).fetchone()[0]
            self.assertEqual(data_left, 0)
            fts_left = con.execute(
                "SELECT count(*) FROM fts_executions WHERE execution_id IN (101,102,103,104,105,106,107,108)"
            ).fetchone()[0]
            self.assertEqual(fts_left, 0)
            kept = con.execute("SELECT count(*) FROM executions WHERE id=110").fetchone()[0]
            self.assertEqual(kept, 1)
        finally:
            con.close()

    def test_fts_rows_really_deleted(self):
        # S4: fts_executions.execution_id holds TEXT (FTS5 columns have no
        # type affinity), so the retention DELETE must bind str() or it
        # silently matches nothing. Compare the same way the data is stored.
        marks = ",".join("?" * len(self.OLD))
        params = tuple(str(i) for i in self.OLD)
        syncmod.run_sync(self.n8n_db, self.hist_db)  # rebuild the FTS index
        con = sqlite3.connect(self.hist_db)
        try:
            before = con.execute(
                f"SELECT count(*) FROM fts_executions WHERE execution_id IN ({marks})",
                params).fetchone()[0]
        finally:
            con.close()
        self.assertGreater(before, 0)
        self.assertEqual(app.apply_retention(), 8)
        con = sqlite3.connect(self.hist_db)
        try:
            after = con.execute(
                f"SELECT count(*) FROM fts_executions WHERE execution_id IN ({marks})",
                params).fetchone()[0]
            kept = con.execute(
                "SELECT count(*) FROM fts_executions WHERE execution_id = ?",
                ("110",)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(after, 0, "retention left stale full-text rows behind")
        self.assertGreater(kept, 0, "retention wiped an execution it should keep")

    def test_api_reflects_retention(self):
        self.assertTrue(app.run_sync_now())
        _, s = self.get("/api/stats")
        self.assertEqual(s["executions"], 1)
        self.assertEqual(s["retentionDays"], 2)
        self.assertEqual(s["storage"]["retentionDays"], 2)
        _, res = self.get("/api/sync")
        self.assertEqual(res["last"]["result"].get("retentionDeleted"), 8)

    def test_fts_search_no_longer_finds_deleted_text(self):
        self.assertTrue(app.run_sync_now())
        # "ACME" lived in execution 107, which is older than the cutoff
        _, body = self.get("/api/executions?q=ACME")
        self.assertEqual(body["items"], [])


class RetentionSpaceBase(unittest.TestCase):
    """S6: retention must give back the space it frees.

    These tests build their own history database (a few MB: hundreds of rows
    with a 100 KB blob each and two full-text rows) so page counts and file
    size are measurable, and they call app.apply_retention() directly: that is
    the maintenance entry point the sync thread uses (never a request
    handler).
    """

    OLD = 120
    NEW = 4
    BLOB = "z" * 100_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hist_db = os.path.join(self.tmp.name, "history.sqlite")
        saved = dict(app.CONFIG)

        def restore():
            app.CONFIG.clear()
            app.CONFIG.update(saved)

        self.addCleanup(restore)
        app.CONFIG.update({"history_db": self.hist_db, "retention_days": 2})

    def make_history(self, auto_vacuum="INCREMENTAL"):
        ddl = schema.SCHEMA.replace("PRAGMA auto_vacuum=INCREMENTAL;",
                                    f"PRAGMA auto_vacuum={auto_vacuum};")
        con = sqlite3.connect(self.hist_db)
        try:
            con.executescript(ddl)
            con.commit()
        finally:
            con.close()

    def seed(self, old=OLD, new=NEW, with_blob=True):
        """`old` rows well past a 2-day retention window, `new` rows from the
        last hour (kept), all of them searchable."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S.000"
        old_ts = (now - timedelta(days=30)).strftime(fmt)
        new_ts = (now - timedelta(hours=1)).strftime(fmt)
        con = sqlite3.connect(self.hist_db)
        try:
            con.execute("INSERT INTO workflows(id, name, active, updated_at) "
                        "VALUES('wf','WF',1,'2026-01-01 00:00:00.000')")
            for i in range(old + new):
                eid = 1000 + i
                started = old_ts if i < old else new_ts
                con.execute(
                    "INSERT INTO executions(id, workflow_id, status, started_at, "
                    "json_size, data_missing, imported_at) VALUES(?,?,?,?,?,0,?)",
                    (eid, "wf", "success", started, len(self.BLOB), new_ts))
                if with_blob:
                    con.execute(
                        "INSERT INTO executions_data(execution_id, data) "
                        "VALUES(?,?)", (eid, self.BLOB))
                for n in range(2):
                    con.execute(
                        "INSERT INTO fts_executions(execution_id, workflow_id, "
                        "node_name, text) VALUES(?,?,?,?)",
                        (str(eid), "wf", f"node{n}",
                         f"palabrasecreta{eid} relleno " * 400))
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()

    def metrics(self):
        con = sqlite3.connect(self.hist_db)
        try:
            return {
                "executions": con.execute(
                    "SELECT count(*) FROM executions").fetchone()[0],
                "page_count": con.execute("PRAGMA page_count").fetchone()[0],
                "freelist": con.execute("PRAGMA freelist_count").fetchone()[0],
                "fts_bytes": con.execute(
                    "SELECT COALESCE(SUM(length(block)),0) "
                    "FROM fts_executions_data").fetchone()[0],
                "size": os.path.getsize(self.hist_db),
            }
        finally:
            con.close()

    def fts_hits(self, phrase):
        con = sqlite3.connect(self.hist_db)
        try:
            return con.execute(
                "SELECT count(*) FROM fts_executions WHERE fts_executions MATCH ?",
                (f'"{phrase}"',)).fetchone()[0]
        finally:
            con.close()


class TestRetentionReclaimsSpace(RetentionSpaceBase):
    def test_deleting_pass_returns_space_to_the_os(self):
        self.make_history()
        self.seed()
        before = self.metrics()
        self.assertGreaterEqual(self.fts_hits("palabrasecreta1000"), 1)

        deleted = app.apply_retention()

        self.assertEqual(deleted, self.OLD)
        after = self.metrics()
        # the rows are gone (S4 already covers the deletion itself)
        self.assertEqual(after["executions"], self.NEW)
        # S6: the deleted text no longer lives in the FTS index
        self.assertEqual(self.fts_hits("palabrasecreta1000"), 0)
        self.assertGreaterEqual(self.fts_hits("palabrasecreta1123"), 1)  # kept
        self.assertLess(after["fts_bytes"], before["fts_bytes"] // 2,
                        "the FTS index kept the bytes of the deleted rows")
        # S6: the pages the delete freed went back to the OS
        self.assertEqual(after["freelist"], 0,
                         "retention left free pages trapped inside the file")
        self.assertLess(after["page_count"], before["page_count"],
                        "the history database did not get smaller")
        self.assertLess(after["size"], before["size"] * 0.8,
                        "the history file on disk did not shrink")

    def test_noop_pass_does_not_touch_the_file(self):
        self.make_history()
        self.seed(old=0, new=40)
        # free pages from something other than retention: they must survive a
        # pass that deletes nothing
        con = sqlite3.connect(self.hist_db)
        con.execute("DELETE FROM executions_data WHERE execution_id < 1030")
        con.execute("DELETE FROM executions WHERE id < 1030")
        con.commit()
        con.close()
        before = self.metrics()
        self.assertGreater(before["freelist"], 0)
        # below the S21 threshold a quiet pass stays completely free
        self.assertLess(before["freelist"], app.RECLAIM_NOOP_FREELIST_PAGES)

        self.assertEqual(app.apply_retention(), 0)

        after = self.metrics()
        self.assertEqual(after["freelist"], before["freelist"],
                         "a no-op retention pass ran a compaction")
        self.assertEqual(after["page_count"], before["page_count"])
        self.assertEqual(after["fts_bytes"], before["fts_bytes"])

    def test_failed_reclaim_is_retried_by_a_noop_pass(self):
        # S21: a reclaim whose incremental_vacuum/checkpoint failed is only
        # logged, so its free pages stay trapped inside the file. The next
        # pass that deletes nothing has to retry it once the freelist is over
        # the documented threshold.
        self.make_history()
        self.seed(old=0, new=120)
        con = sqlite3.connect(self.hist_db)
        con.execute("DELETE FROM executions_data WHERE execution_id < 1080")
        con.execute("DELETE FROM executions WHERE id < 1080")
        con.commit()
        con.close()
        before = self.metrics()
        self.assertGreater(before["freelist"], app.RECLAIM_NOOP_FREELIST_PAGES)
        self.assertEqual(app.apply_retention(), 0)  # nothing is expired
        after = self.metrics()
        self.assertEqual(after["executions"], before["executions"])
        self.assertEqual(after["freelist"], 0,
                         "the trapped free pages of a failed reclaim were "
                         "not retried")
        self.assertLess(after["page_count"], before["page_count"])
        self.assertLess(after["size"], before["size"])

    def test_legacy_database_is_converted_and_compacted(self):
        # a history created by an older version has auto_vacuum=NONE: its free
        # pages can never go back to the OS without rewriting the file once
        self.make_history("NONE")
        self.seed()
        con = sqlite3.connect(self.hist_db)
        self.assertEqual(con.execute("PRAGMA auto_vacuum").fetchone()[0], 0)
        con.close()
        before = self.metrics()
        saved = app.LEGACY_VACUUM_MIN_BYTES
        app.LEGACY_VACUUM_MIN_BYTES = 0   # the test history is a few MB
        try:
            self.assertEqual(app.apply_retention(), self.OLD)
        finally:
            app.LEGACY_VACUUM_MIN_BYTES = saved
        after = self.metrics()
        con = sqlite3.connect(self.hist_db)
        try:
            self.assertEqual(con.execute("PRAGMA auto_vacuum").fetchone()[0], 2,
                             "the legacy history was not switched to incremental")
        finally:
            con.close()
        self.assertEqual(after["freelist"], 0)
        self.assertLess(after["page_count"], before["page_count"])
        self.assertLess(after["size"], before["size"] * 0.8)


class TestRetentionOff(RetentionBase):
    """S6: retention_days=0 (default) never deletes, even very old rows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.shift_history_dates((101, 102), (110,))
        # retention_days stays 0 (set by ApiTestBase)

    def test_nothing_deleted(self):
        self.assertTrue(app.run_sync_now())
        _, res = self.get("/api/sync")
        self.assertEqual(res["last"]["result"].get("retentionDeleted"), 0)
        con = sqlite3.connect(self.hist_db)
        kept = con.execute("SELECT count(*) FROM executions WHERE id IN (101,102)").fetchone()[0]
        con.close()
        self.assertEqual(kept, 2)


class TestOversizedBlobExposure(ApiTestBase):
    """S8: the blob cap is reported through /api/sync and hides the data."""

    def test_sync_reports_oversized_blobs(self):
        fixture.add_execution(self.n8n_db, 120, "wfA", "success",
                              "2026-10-29 12:00:00.000",
                              "2026-10-29 12:00:01.000", 600_000_000)
        saved = app.CONFIG["max_blob_mb"]
        app.CONFIG["max_blob_mb"] = 1  # 1 MB: execution 120 is 600 MB by n8n
        try:
            self.assertTrue(app.run_sync_now())
        finally:
            app.CONFIG["max_blob_mb"] = saved
        _, s = self.get("/api/sync")
        self.assertTrue(s["last"]["ok"])
        self.assertEqual(s["last"]["result"]["oversizedBlobs"], 1)
        code, body = self.get("/api/executions/120")
        self.assertEqual(code, 200)
        self.assertTrue(body["dataMissing"])
        self.assertIn("too large", body["parseError"])
        self.assertEqual(body["nodes"], [])

    def test_default_cap_documented(self):
        self.assertEqual(
            app.CONFIG["max_blob_mb"],
            int(os.environ.get("N8N_VIEWER_MAX_BLOB_MB", "50")))


if __name__ == "__main__":
    unittest.main()
