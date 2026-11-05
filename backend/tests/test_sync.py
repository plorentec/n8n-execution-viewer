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


class TestRetentionSkip(SyncTestBase):
    """S7: rows older than the retention cutoff are never imported.

    Fixture dates: 101/102/103/104/107 are before the cutoff below, while
    105/106/108/110 are kept.
    """

    CUTOFF = "2026-10-22 09:00:00.000"
    OLD = (101, 102, 103, 104, 107)

    def skipped_ids_sql(self):
        return " IN (%s)" % ",".join(str(i) for i in self.OLD)

    def test_old_rows_are_not_imported_at_all(self):
        result = syncmod.run_sync(self.n8n_db, self.hist_db,
                                  retention_cutoff=self.CUTOFF)
        self.assertEqual(result.get("retentionSkipped"), len(self.OLD))
        self.assertEqual(result["added"], 4)
        # they are not imported, so they count as skipped too: the viewer's
        # "sin copias nuevas" message keeps working
        self.assertEqual(result["skipped"], len(self.OLD))
        con = self.hist()
        ids = [r[0] for r in con.execute("SELECT id FROM executions ORDER BY id")]
        self.assertEqual(ids, [105, 106, 108, 110])
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions_data WHERE execution_id"
            + self.skipped_ids_sql()).fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT count(*) FROM fts_executions WHERE execution_id IN "
            "('101','102','103','104','107')").fetchone()[0], 0)
        # the skipped rows still advance the cursor: they are behind us now
        self.assertEqual(self.state("last_id"), "110")

    def test_skipped_rows_are_not_reported_as_gaps(self):
        syncmod.run_sync(self.n8n_db, self.hist_db,
                         retention_cutoff=self.CUTOFF)
        # the only unrecoverable id is the hard hole at 109, exactly as
        # without retention: skipping old rows is not an import gap
        self.assertEqual(int(self.state("import_issues")), 1)

    def test_no_churn_on_repeated_passes(self):
        first = syncmod.run_sync(self.n8n_db, self.hist_db,
                                 retention_cutoff=self.CUTOFF)
        con = self.hist()
        blobs = con.execute("SELECT count(*) FROM executions_data").fetchone()[0]
        con.close()
        second = syncmod.run_sync(self.n8n_db, self.hist_db,
                                  retention_cutoff=self.CUTOFF)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], 9)  # 4 kept unchanged + 5 expired
        self.assertEqual(second.get("retentionSkipped"), len(self.OLD))
        con = self.hist()
        # nothing was re-downloaded/re-serialized for the expired rows
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions_data").fetchone()[0], blobs)
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions WHERE id" + self.skipped_ids_sql()
        ).fetchone()[0], 0)
        con.close()
        self.assertEqual(first["added"], 4)

    def test_retention_off_imports_everything(self):
        # the sync result keeps its previous shape when nothing was skipped
        # or capped: /api/sync last.result stays byte-compatible
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result, {"added": 9, "updated": 0, "skipped": 0})
        self.assertNotIn("retentionSkipped", result)
        self.assertNotIn("oversizedBlobs", result)


class TestBlobCap(SyncTestBase):
    """S8: a blob above the cap is not loaded, serialized or indexed."""

    def add_big(self, eid, jsize, started="2026-10-29 09:00:00.000"):
        fixture.add_execution(self.n8n_db, eid, "wfA", "success", started,
                              started, jsize)

    def test_oversized_blob_is_marked_and_logged_once(self):
        self.add_big(111, 4_000_000)
        self.add_big(112, 3_000_000, started="2026-10-29 10:00:00.000")
        with self.assertLogs("n8n-viewer.sync", level="WARNING") as logs:
            result = syncmod.run_sync(self.n8n_db, self.hist_db,
                                      max_blob_bytes=1_000_000)
        self.assertEqual(result.get("oversizedBlobs"), 2)
        # one line per sync, with the count
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("2", messages[0])
        self.assertIn("1000000", messages[0])
        con = self.hist()
        for eid in (111, 112):
            row = con.execute(
                "SELECT data_missing, parse_error FROM executions WHERE id=?",
                (eid,)).fetchone()
            self.assertEqual(row["data_missing"], 1)
            self.assertIn("too large", row["parse_error"])
            self.assertIn(str(1_000_000), row["parse_error"])
            self.assertIsNone(con.execute(
                "SELECT data FROM executions_data WHERE execution_id=?",
                (eid,)).fetchone())
            self.assertEqual(con.execute(
                "SELECT count(*) FROM fts_executions WHERE execution_id=?",
                (str(eid),)).fetchone()[0], 0)
        # the small ones are untouched and still indexed
        self.assertEqual(con.execute(
            "SELECT data_missing FROM executions WHERE id=101").fetchone()[0], 0)
        self.assertGreater(con.execute(
            "SELECT count(*) FROM executions_data WHERE execution_id=101"
        ).fetchone()[0], 0)
        con.close()

    def test_blob_is_never_decoded_when_over_the_cap(self):
        # the payload is not valid JSON: if the sync loaded it, the reason
        # would be a decode error instead of the size cap
        self.add_big(111, 4_000_000)
        con = sqlite3.connect(self.n8n_db)
        con.execute("UPDATE execution_data SET data='{not json at all' "
                    "WHERE executionId=111")
        con.commit()
        con.close()
        with self.assertLogs("n8n-viewer.sync", level="WARNING"):
            syncmod.run_sync(self.n8n_db, self.hist_db, max_blob_bytes=1_000_000)
        con = self.hist()
        reason = con.execute(
            "SELECT parse_error FROM executions WHERE id=111").fetchone()[0]
        con.close()
        self.assertIn("too large", reason)
        self.assertNotIn("cannot decode", reason)

    def test_blob_below_the_cap_is_imported(self):
        self.add_big(111, 500)
        result = syncmod.run_sync(self.n8n_db, self.hist_db,
                                  max_blob_bytes=1_000_000)
        self.assertNotIn("oversizedBlobs", result)
        con = self.hist()
        self.assertEqual(con.execute(
            "SELECT data_missing FROM executions WHERE id=111").fetchone()[0], 0)
        self.assertIsNotNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=111").fetchone())
        con.close()


class TestBlobCapNullSize(SyncTestBase):
    """S15: the cap also applies when n8n's jsonSizeBytes is NULL, and the
    declared size is verified against the real payload in both directions.
    """

    CAP = 10_000

    def big_blob(self):
        return {"version": 1,
                "resultData": {"runData": {"Big": [
                    {"start": "2026-10-29T09:00:00.000Z", "source": [],
                     "data": {"main": [[{"json": {"text": "x" * 20_000}}]]}}]}}}

    def add_row(self, eid, jsize=None, raw_data=None):
        fixture.add_execution(self.n8n_db, eid, "wfA", "success",
                              "2026-10-29 09:00:00.000",
                              "2026-10-29 09:00:01.000", jsize,
                              blob=None if raw_data else self.big_blob(),
                              raw_data=raw_data)

    def assert_capped(self, eid):
        con = self.hist()
        row = con.execute(
            "SELECT data_missing, parse_error FROM executions WHERE id=?",
            (eid,)).fetchone()
        self.assertIsNotNone(row, "the execution was not stored at all")
        self.assertEqual(row["data_missing"], 1)
        self.assertIn("too large", row["parse_error"])
        self.assertIn(str(self.CAP), row["parse_error"])
        self.assertIsNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=?",
            (eid,)).fetchone())
        self.assertEqual(con.execute(
            "SELECT count(*) FROM fts_executions WHERE execution_id=?",
            (str(eid),)).fetchone()[0], 0)
        con.close()

    def test_null_declared_size_is_capped_by_the_real_length(self):
        self.add_row(111, jsize=None)
        with self.assertLogs("n8n-viewer.sync", level="WARNING"):
            result = syncmod.run_sync(self.n8n_db, self.hist_db,
                                      max_blob_bytes=self.CAP)
        self.assertEqual(result.get("oversizedBlobs"), 1)
        self.assertEqual(result["added"], 10)  # nothing was rolled back
        self.assert_capped(111)
        # the small executions of the fixture are untouched
        con = self.hist()
        self.assertEqual(con.execute(
            "SELECT data_missing FROM executions WHERE id=101").fetchone()[0], 0)
        self.assertIsNotNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=101").fetchone())
        con.close()

    def test_null_declared_size_is_not_decoded(self):
        # the payload is not even valid JSON: if the sync had decoded it, the
        # reason would be a decode error instead of the size cap
        self.add_row(111, jsize=None, raw_data="{" + "x" * 20_000)
        with self.assertLogs("n8n-viewer.sync", level="WARNING"):
            syncmod.run_sync(self.n8n_db, self.hist_db, max_blob_bytes=self.CAP)
        con = self.hist()
        reason = con.execute(
            "SELECT parse_error FROM executions WHERE id=111").fetchone()[0]
        con.close()
        self.assertIn("too large", reason)
        self.assertNotIn("cannot decode", reason)

    def test_undersized_declaration_cannot_smuggle_a_huge_blob(self):
        # n8n says 10 bytes, the real blob is ~20 KB: the serialized text is
        # re-checked against the cap before anything is written
        self.add_row(111, jsize=10)
        with self.assertLogs("n8n-viewer.sync", level="WARNING"):
            result = syncmod.run_sync(self.n8n_db, self.hist_db,
                                      max_blob_bytes=self.CAP)
        self.assertEqual(result.get("oversizedBlobs"), 1)
        self.assert_capped(111)

    def test_declared_and_real_size_below_the_cap_is_imported(self):
        fixture.add_execution(self.n8n_db, 111, "wfA", "success",
                              "2026-10-29 09:00:00.000",
                              "2026-10-29 09:00:01.000", 20)
        result = syncmod.run_sync(self.n8n_db, self.hist_db,
                                  max_blob_bytes=1_000_000)
        self.assertNotIn("oversizedBlobs", result)
        con = self.hist()
        self.assertEqual(con.execute(
            "SELECT data_missing FROM executions WHERE id=111").fetchone()[0], 0)
        self.assertIsNotNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=111").fetchone())
        con.close()


class TestPoisonRow(SyncTestBase):
    """S16: one undecodable row degrades to parse_error, never a rollback."""

    def assert_degraded_and_sync_survived(self, eid):
        con = self.hist()
        row = con.execute(
            "SELECT data_missing, parse_error FROM executions WHERE id=?",
            (eid,)).fetchone()
        self.assertIsNotNone(row, "the poison row was not stored at all")
        self.assertEqual(row["data_missing"], 1)
        self.assertTrue(row["parse_error"])
        # no run data reached the index for the poison row (only the workflow
        # snapshot may have been stored, which indexes nothing)
        self.assertEqual(con.execute(
            "SELECT count(*) FROM fts_executions WHERE execution_id=?",
            (str(eid),)).fetchone()[0], 0)
        # nothing was rolled back: the fixture rows before it are still there
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions").fetchone()[0], 10)
        self.assertIsNotNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=101").fetchone())
        con.close()
        self.assertEqual(self.state("last_id"), str(eid))

    def test_flatted_resolve_empty_list_returns_none(self):
        self.assertIsNone(syncmod.flatted_resolve([]))

    def test_empty_flatted_array_degrades_to_parse_error(self):
        # n8n's data column holding "[]": flatted_resolve([]) used to raise
        # IndexError at input_arr[0], outside the caller's except tuple
        fixture.add_execution(self.n8n_db, 111, "wfA", "success",
                              "2026-10-29 09:00:00.000",
                              "2026-10-29 09:00:01.000", 2,
                              raw_data="[]")
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result["added"], 10)
        self.assert_degraded_and_sync_survived(111)

    def test_non_object_root_degrades_to_parse_error(self):
        # root entry is the number 1, not an execution object: _error_of and
        # node_texts used to raise AttributeError on it
        fixture.add_execution(self.n8n_db, 111, "wfA", "success",
                              "2026-10-29 09:00:00.000",
                              "2026-10-29 09:00:01.000", 2,
                              raw_data="[1, 2]")
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result["added"], 10)
        self.assert_degraded_and_sync_survived(111)


class TestNullColumns(SyncTestBase):
    """S17: a row with NULL startedAt/status/workflowId is skipped, counted,
    and never freezes the history through an IntegrityError rollback."""

    def test_null_row_is_skipped_and_counted(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        fixture.add_null_row(self.n8n_db, 111, blob={
            "version": 1,
            "resultData": {"runData": {"New": [
                {"start": "2026-10-29T09:00:00.000Z", "source": [],
                 "data": {"main": [[{"json": {"n": 1}}]]}}]}}})
        with self.assertLogs("n8n-viewer.sync", level="WARNING") as logs:
            result = syncmod.run_sync(self.n8n_db, self.hist_db)
        # one line per sync, with the count (not one line per row)
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("1 execution row(s) skipped", messages[0])
        self.assertEqual(result.get("invalidRows"), 1)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 10)  # 9 unchanged + the skipped one
        con = self.hist()
        # the whole history is intact: no rollback of anything
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions").fetchone()[0], 9)
        self.assertIsNone(con.execute(
            "SELECT id FROM executions WHERE id=111").fetchone())
        self.assertIsNotNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=101").fetchone())
        con.close()
        # the row exists in n8n, so it is not an import gap either
        self.assertEqual(int(self.state("import_issues")), 1)
        # and the cursor moved past it
        self.assertEqual(self.state("last_id"), "111")

    def test_null_row_on_first_sync_does_not_abort_the_backfill(self):
        fixture.add_null_row(self.n8n_db, 111)
        with self.assertLogs("n8n-viewer.sync", level="WARNING"):
            result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result.get("invalidRows"), 1)
        self.assertEqual(result["added"], 9)
        con = self.hist()
        self.assertEqual(con.execute(
            "SELECT count(*) FROM executions").fetchone()[0], 9)
        con.close()
        self.assertEqual(self.state("last_id"), "111")

    def test_valid_rows_are_unaffected(self):
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertNotIn("invalidRows", result)
        self.assertEqual(result, {"added": 9, "updated": 0, "skipped": 0})


class TestMemoryError(SyncTestBase):
    """S22: MemoryError joins the caught set around decode/serialize/decode of
    the workflow snapshot: it degrades to parse_error instead of killing the
    sync process."""

    def test_memoryerror_while_serializing(self):
        fixture.add_execution(self.n8n_db, 111, "wfA", "success",
                              "2026-10-29 09:00:00.000",
                              "2026-10-29 09:00:01.000", 30)

        def boom(*a, **k):
            raise MemoryError("out of memory")

        original = syncmod.json.dumps
        syncmod.json.dumps = boom
        try:
            result = syncmod.run_sync(self.n8n_db, self.hist_db)
        finally:
            syncmod.json.dumps = original
        self.assertEqual(result["added"], 10)
        con = self.hist()
        row = con.execute(
            "SELECT data_missing, parse_error FROM executions WHERE id=111"
        ).fetchone()
        self.assertEqual(row["data_missing"], 1)
        self.assertIn("cannot encode", row["parse_error"])
        self.assertIsNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=111").fetchone())
        con.close()
        self.assertEqual(self.state("last_id"), "111")

    def test_memoryerror_while_decoding(self):
        fixture.add_execution(self.n8n_db, 111, "wfA", "success",
                              "2026-10-29 09:00:00.000",
                              "2026-10-29 09:00:01.000", 30)

        def boom(*a, **k):
            raise MemoryError("out of memory")

        original = syncmod.decode_execution_data
        syncmod.decode_execution_data = boom
        try:
            result = syncmod.run_sync(self.n8n_db, self.hist_db)
        finally:
            syncmod.decode_execution_data = original
        self.assertEqual(result["added"], 10)
        con = self.hist()
        row = con.execute(
            "SELECT data_missing, parse_error FROM executions WHERE id=111"
        ).fetchone()
        self.assertEqual(row["data_missing"], 1)
        self.assertIn("cannot decode", row["parse_error"])
        con.close()
        self.assertEqual(self.state("last_id"), "111")


class TestUnchangedRows(SyncTestBase):
    """S23: a row in the rescan window whose indexed state did not change is
    not re-read, re-serialized nor re-written (the `skipped` counter keeps
    counting it)."""

    def traced_sync(self):
        """Run one sync counting every statement against execution_data."""
        statements = []
        original = syncmod.open_n8n

        def traced(path):
            con = original(path)
            con.set_trace_callback(statements.append)
            return con

        syncmod.open_n8n = traced
        try:
            result = syncmod.run_sync(self.n8n_db, self.hist_db)
        finally:
            syncmod.open_n8n = original
        blob_reads = [s for s in statements if "FROM execution_data" in s]
        return result, blob_reads

    def test_unchanged_rows_do_not_read_the_blob(self):
        first, first_reads = self.traced_sync()
        self.assertEqual(first["added"], 9)
        self.assertEqual(len(first_reads), 9, first_reads)
        second, second_reads = self.traced_sync()
        self.assertEqual(second, {"added": 0, "updated": 0, "skipped": 9})
        self.assertEqual(second_reads, [],
                         "the sync re-read the payload of unchanged rows")

    def test_changed_rows_still_read_the_blob(self):
        self.traced_sync()
        fixture.finish_execution(self.n8n_db, 104, "2026-10-21 10:05:00.000")
        result, reads = self.traced_sync()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(reads), 1, reads)

    def test_unchanged_row_is_not_rewritten(self):
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        before = con.execute(
            "SELECT imported_at, data_missing, parse_error FROM executions "
            "WHERE id=101").fetchone()
        fts_before = [tuple(r) for r in con.execute(
            'SELECT node_name, text FROM fts_executions '
            'WHERE execution_id="101"')]
        con.close()
        # a sentinel: n8n's payload for an unchanged row becomes undecodable,
        # so only re-reading it could change the indexed state
        con = sqlite3.connect(self.n8n_db)
        con.execute("UPDATE execution_data SET data='{oops' WHERE executionId=101")
        con.commit()
        con.close()
        syncmod.run_sync(self.n8n_db, self.hist_db)
        con = self.hist()
        after = con.execute(
            "SELECT imported_at, data_missing, parse_error FROM executions "
            "WHERE id=101").fetchone()
        fts_after = [tuple(r) for r in con.execute(
            'SELECT node_name, text FROM fts_executions '
            'WHERE execution_id="101"')]
        data_after = con.execute(
            "SELECT data FROM executions_data WHERE execution_id=101").fetchone()
        con.close()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(fts_after, fts_before)
        self.assertIsNotNone(data_after, "the stored blob was dropped")


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


class TestCyclicPayload(SyncTestBase):
    """S1: flatted revives shared references, so a cyclic payload comes out
    of the decoder as a real cycle and json.dumps rejects it. That execution
    must be stored as parse_error/data_missing and the sync must keep going.
    """

    def add_cyclic(self, eid, blob):
        fixture.add_execution(
            self.n8n_db, eid, "wfA", "success",
            "2026-10-29 09:00:00.000", "2026-10-29 09:00:01.000", 32,
            blob=blob, wf={"nodes": [], "connections": {}})

    def assert_recorded_as_parse_error(self, eid):
        con = self.hist()
        row = con.execute(
            "SELECT data_missing, parse_error FROM executions WHERE id=?",
            (eid,)).fetchone()
        self.assertIsNotNone(row, "the broken execution was not stored at all")
        self.assertEqual(row["data_missing"], 1)
        self.assertTrue(row["parse_error"])
        self.assertIsNone(con.execute(
            "SELECT data FROM executions_data WHERE execution_id=?",
            (eid,)).fetchone())
        self.assertEqual(con.execute(
            "SELECT count(*) FROM fts_executions WHERE execution_id=?",
            (str(eid),)).fetchone()[0], 0)
        # and nothing was rolled back: last_id moved past the broken row
        self.assertEqual(self.state("last_id"), str(eid))

    def test_cycle_outside_rundata(self):
        run = {"start": "2026-10-29T09:00:00.000Z", "source": [],
               "data": {"main": [[{"json": {"ok": True}}]]}}
        blob = {"version": 1,
                "resultData": {"runData": {"Loop": [run]},
                               "lastNodeExecuted": "Loop"}}
        blob["resultData"]["loopback"] = blob["resultData"]  # cycle
        self.add_cyclic(111, blob)
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result, {"added": 10, "updated": 0, "skipped": 0})
        self.assert_recorded_as_parse_error(111)
        # the next execution is still imported on the following pass
        fixture.add_execution(self.n8n_db, 112, "wfA", "success",
                              "2026-10-29 10:00:00.000",
                              "2026-10-29 10:00:01.000", 33)
        self.assertEqual(syncmod.run_sync(self.n8n_db, self.hist_db)["added"], 1)
        self.assertEqual(self.state("last_id"), "112")

    def test_cycle_inside_rundata(self):
        items = [{"json": {"x": 1}}]
        items.append({"json": {"again": items}})  # cycle inside the output
        blob = {"version": 1,
                "resultData": {"runData": {"Spin": [
                    {"start": "2026-10-29T09:00:00.000Z", "source": [],
                     "data": {"main": [items]}}]}}}
        self.add_cyclic(111, blob)
        result = syncmod.run_sync(self.n8n_db, self.hist_db)
        self.assertEqual(result, {"added": 10, "updated": 0, "skipped": 0})
        self.assert_recorded_as_parse_error(111)


if __name__ == "__main__":
    unittest.main()
