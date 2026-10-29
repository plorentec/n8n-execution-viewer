"""Sync engine tests: backfill, incremental delta, transitions, gaps, errors.

The n8n side is a synthetic fixture (tests/fixture.py); the history side is a
throwaway SQLite file. No network, no real n8n.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixture  # noqa: E402
import sync as syncmod  # noqa: E402


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n8n_db = os.path.join(self.tmp.name, "n8n.sqlite")
        self.hist_db = os.path.join(self.tmp.name, "history.sqlite")
        fixture.create_n8n_db(self.n8n_db)

    def hist(self):
        con = sqlite3.connect(self.hist_db)
        con.row_factory = sqlite3.Row
        return con

    def state(self, key):
        row = self.hist().execute(
            "SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


class TestBackfill(SyncTestBase):
    def test_counts_and_workflows(self):
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result, {"added": 9, "updated": 0, "skipped": 0})
        con = self.hist()
        self.assertEqual(con.execute("SELECT count(*) FROM executions").fetchone()[0], 9)
        wfs = {r["id"]: r for r in con.execute("SELECT * FROM workflows")}
        self.assertEqual(set(wfs), {"wfA", "wfB", "wfGone"})
        self.assertTrue(wfs["wfA"]["active"])
        self.assertTrue(wfs["wfGone"]["deleted_at"])

    def test_verbatim_dates(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        row = con.execute(
            "SELECT started_at, stopped_at, deleted_at FROM executions WHERE id=101"
        ).fetchone()
        self.assertEqual(row["started_at"], "2026-10-20 08:00:00.000")
        self.assertEqual(row["stopped_at"], "2026-10-20 08:00:05.000")
        row105 = con.execute("SELECT deleted_at FROM executions WHERE id=105").fetchone()
        self.assertEqual(row105["deleted_at"], "2026-10-25 00:00:00.000")

    def test_duration_computed(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        self.assertEqual(con.execute(
            "SELECT duration_ms FROM executions WHERE id=101").fetchone()[0], 5000)
        self.assertIsNone(con.execute(
            "SELECT duration_ms FROM executions WHERE id=103").fetchone()[0])

    def test_error_extracted(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        row = con.execute(
            "SELECT error_message, error_node FROM executions WHERE id=102").fetchone()
        self.assertEqual(row["error_message"], "boom from node B")
        self.assertEqual(row["error_node"], "Node B")
        self.assertIsNone(con.execute(
            "SELECT error_message FROM executions WHERE id=101").fetchone()[0])

    def test_data_missing_flag(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        self.assertEqual(con.execute(
            "SELECT data_missing FROM executions WHERE id=106").fetchone()[0], 1)
        self.assertIsNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=106").fetchone())
        self.assertEqual(con.execute(
            "SELECT data_missing FROM executions WHERE id=101").fetchone()[0], 0)

    def test_last_id_persisted(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(self.state("last_id"), "110")


class TestGapDetection(SyncTestBase):
    def test_first_run_counts_hole_in_current_window(self):
        # ids 101..110 with a hard hole at 109 -> 1 unrecoverable row
        syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(int(self.state("import_issues")), 1)

    def test_incremental_counts_new_holes(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        fixture.add_execution(self.n8n_db, 115, "wfA", "success",
                               "2026-10-28 10:00:00.000", "2026-10-28 10:00:01.000", 20)
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        # ids 111..115, only 115 exists -> 4 unrecoverable
        self.assertEqual(result["added"], 1)
        self.assertEqual(int(self.state("import_issues")), 1 + 4)


class TestIncremental(SyncTestBase):
    def test_new_and_transition(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        fixture.add_execution(self.n8n_db, 111, "wfA", "success",
                               "2026-10-28 09:30:00.000", "2026-10-28 09:30:01.000", 19)
        fixture.finish_execution(self.n8n_db, 104, "2026-10-21 10:05:00.000")
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 8)
        con = self.hist()
        row = con.execute(
            "SELECT status, stopped_at, duration_ms FROM executions WHERE id=104").fetchone()
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["duration_ms"], 300000)
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions WHERE id=111").fetchone()[0], 1)

    def test_idempotent_rerun(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result, {"added": 0, "updated": 0, "skipped": 9})

    def test_workflow_upsert_follows_renames(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = sqlite3.connect(self.n8n_db)
        con.execute("UPDATE workflow_entity SET name='Workflow A2' WHERE id='wfA'")
        con.commit()
        con.close()
        syncmod.run_sync(self.n8n_db, self.hist_db)
        name = self.hist().execute(
            "SELECT name FROM workflows WHERE id='wfA'").fetchone()[0]
        self.assertEqual(name, "Workflow A2")


class TestFts(SyncTestBase):
    def test_fts_indexed_and_searchable(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        hits = [r[0] for r in con.execute(
            'SELECT execution_id FROM fts_executions WHERE fts_executions MATCH ?',
            ('"contraseña"',))]
        self.assertIn("108", hits)
        hits = [r[0] for r in con.execute(
            'SELECT execution_id FROM fts_executions WHERE fts_executions MATCH ?',
            ('"ACME"',))]
        self.assertIn("107", hits)
        self.assertIn("108", hits)

    def test_fts_reindexed_on_update(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        # a real change (status flip) forces re-import + FTS rebuild
        fixture.finish_execution(self.n8n_db, 108, "2026-10-23 09:00:02.000",
                                  status="canceled")
        syncmod.run_sync(self.n8n_db, self.hist_db)
        rows = self.hist().execute(
            'SELECT node_name FROM fts_executions WHERE execution_id="108"').fetchall()
        self.assertEqual([r[0] for r in rows], ["Notes"])


class TestRobustness(SyncTestBase):
    def test_schema_change_detected(self):
        con = sqlite3.connect(self.n8n_db)
        con.executescript("""
            CREATE TABLE execution_entity_new (id INTEGER);
            DROP TABLE execution_entity;
            ALTER TABLE execution_entity_new RENAME TO execution_entity;
        """)
        con.close()
        with self.assertRaises(syncmod.SyncError) as ctx:
            syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertIn("not recognized", str(ctx.exception))

    def test_missing_db_is_error(self):
        with self.assertRaises(syncmod.SyncError):
            syncmod.run_sync(os.path.join(self.tmp.name, "nope.sqlite"), self.hist_db)


if __name__ == "__main__":
    unittest.main()
