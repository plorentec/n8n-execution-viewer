"""API tests: the /api/* contract the static viewer consumes (spec S3).

Boots the real HTTP server on an ephemeral port against a synthetic n8n
fixture + a throwaway history database.
"""
import json
import os
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


def http_post(url, headers=None):
    req = urllib.request.Request(url, method="POST", headers=headers or {})
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


if __name__ == "__main__":
    unittest.main()
