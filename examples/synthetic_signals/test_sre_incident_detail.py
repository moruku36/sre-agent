"""CLI regression tests for the offline synthetic incident lookup."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sre_mvp


HERE = Path(__file__).resolve().parent
POLICY = HERE / "cost_policy.json"
BEFORE_RECOVERY = "2026-01-15T12:21:00Z"
AT_RECOVERY = "2026-01-15T12:22:00Z"
AWS = "synthetic-aws-01"
AWS_INCIDENT = f"health:{AWS}/synthetic-aws-prod/synthetic-aws-outage-0001"


class SreIncidentDetailTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "signals ?# fixture.sqlite3"

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(HERE / "sre_mvp.py"), "--db", str(self.db), *args],
            capture_output=True, timeout=15,
        )

    def successful_cli(self, *args):
        result = self.cli(*args)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        return result.stdout

    def detail(self, incident_id, cutoff=BEFORE_RECOVERY):
        return self.successful_cli("get-incident", "--as-of", cutoff, "--incident-id", incident_id)

    def handoff(self, cutoff):
        return json.loads(self.successful_cli("handoff", "--as-of", cutoff, "--policy", str(POLICY)))

    def assert_cli_error(self, exit_code, diagnostic, *args):
        result = self.cli(*args)
        self.assertEqual((exit_code, b""), (result.returncode, result.stdout))
        self.assertIn(diagnostic, result.stderr)

    def test_exact_handoff_item_at_inclusive_recovery_receipt(self):
        self.successful_cli("demo")
        recovery = next(
            body for source, body in sre_mvp.synthetic_signals()
            if source == AWS and body["kind"] == "recovery"
        )
        unapplied = dict(
            recovery, event_id="synthetic-aws-missing-recovery", observed_at=None,
            received_at=BEFORE_RECOVERY, freshness="unknown", value=None, unit=None,
        )
        sre_mvp.ingest(self.db, [(AWS, unapplied)])
        before = self.handoff(BEFORE_RECOVERY)
        incident_id = next(item["incident_id"] for item in before["incidents"] if item["source"] == AWS)
        self.assertGreater(len(before["incidents"]), 1)
        self.assertTrue(before["cost_classifications"])
        self.assertEqual([unapplied["event_id"]],
                         [item["event_id"] for item in before["unapplied_health_evidence"]])

        for cutoff, status, kinds in (
            (BEFORE_RECOVERY, "open", ["outage"]),
            (AT_RECOVERY, "resolved", ["outage", "recovery"]),
        ):
            with self.subTest(cutoff=cutoff):
                handoff = before if cutoff == BEFORE_RECOVERY else self.handoff(cutoff)
                incident = next(item for item in handoff["incidents"] if item["incident_id"] == incident_id)
                self.assertEqual(status, incident["status"])
                self.assertEqual(kinds, [item["kind"] for item in incident["evidence"]])
                if status == "resolved":
                    self.assertEqual(cutoff, incident["evidence"][-1]["received_at"])
                    self.assertEqual(recovery["observed_at"], incident["resolved_at"])

                stdout = self.detail(incident_id, cutoff)
                self.assertEqual(self.detail(incident_id, cutoff), stdout)
                self.assertEqual(1, len(stdout.splitlines()))
                self.assertEqual({
                    "kind": "synthetic_sre_incident_detail", "version": 1,
                    "as_of": cutoff, "incident_id": incident_id, "status": "found",
                    "incident": incident,
                }, json.loads(stdout))

    def test_not_found_is_exact_and_historical(self):
        self.successful_cli("demo")
        for incident_id, cutoff in (
            (AWS_INCIDENT[:-1], BEFORE_RECOVERY),  # Prefix of an existing ID.
            (AWS_INCIDENT, "2026-01-15T12:11:59Z"),  # An observed but not received outage.
            ("health:synthetic-other-01/synthetic-other-scope/FutureOutage", BEFORE_RECOVERY),
        ):
            with self.subTest(incident_id=incident_id, cutoff=cutoff):
                self.assertEqual({
                    "kind": "synthetic_sre_incident_detail", "version": 1,
                    "as_of": cutoff, "incident_id": incident_id, "status": "not_found",
                    "incident": None,
                }, json.loads(self.detail(incident_id, cutoff)))

    def test_signal_event_ids_need_not_have_a_synthetic_prefix(self):
        outage = next(
            body for source, body in sre_mvp.synthetic_signals()
            if source == AWS and body["kind"] == "outage"
        )
        event_id = "Outage.v1_2"
        sre_mvp.ingest(self.db, [(AWS, dict(outage, event_id=event_id))])
        incident_id = f"health:{AWS}/{outage['scope']}/{event_id}"
        detail = json.loads(self.detail(incident_id))
        self.assertEqual("found", detail["status"])
        self.assertEqual(event_id, detail["incident"]["evidence"][0]["event_id"])

    def test_uses_one_store_snapshot(self):
        sre_mvp.ingest(self.db, sre_mvp.synthetic_signals())
        read_rows = sre_mvp._signal_rows
        with patch.object(sre_mvp, "_signal_rows", wraps=read_rows) as reads:
            envelope = sre_mvp.get_incident(self.db, AT_RECOVERY, AWS_INCIDENT)
        reads.assert_called_once_with(self.db)
        self.assertEqual("resolved", envelope["incident"]["status"])

    def test_invalid_and_missing_arguments_do_not_emit_json(self):
        self.successful_cli("demo")
        time_flag = ("--as-of", BEFORE_RECOVERY)
        id_flag = ("--incident-id", AWS_INCIDENT)
        for flags in ((), time_flag, id_flag):
            with self.subTest(flags=flags):
                self.assert_cli_error(2, b"requires --as-of and --incident-id", "get-incident", *flags)
        self.assert_cli_error(2, b"--policy is not used", "get-incident", *time_flag, *id_flag,
                              "--policy", str(POLICY))
        self.assert_cli_error(2, b"--incident-id is only used", "handoff", *time_flag, *id_flag,
                              "--policy", str(POLICY))
        self.assert_cli_error(1, b"invalid canonical UTC timestamp", "get-incident",
                              "--as-of", "2026-01-15T12:21:00+00:00", *id_flag)
        for invalid in ("", AWS_INCIDENT + "/extra", f"health:{AWS}/not-synthetic/Outage",
                        f"health:{AWS}/synthetic-aws-prod/1Outage"):
            with self.subTest(invalid=invalid):
                self.assert_cli_error(1, b"invalid incident ID", "get-incident", *time_flag,
                                      "--incident-id", invalid)

    def test_unavailable_or_invalid_stores_are_not_absence(self):
        args = ("get-incident", "--as-of", BEFORE_RECOVERY, "--incident-id", AWS_INCIDENT[:-1])
        self.assert_cli_error(1, b"unable to open database file", *args)
        self.assertFalse(self.db.exists())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (value INTEGER)")
        self.assert_cli_error(1, b"no such table: signals", *args)
        self.db.write_bytes(b"not a SQLite database" * 64)
        self.assert_cli_error(1, b"file is not a database", *args)

    def test_duplicate_stored_keys_are_rejected_even_after_cutoff(self):
        outage = next(
            body for source, body in sre_mvp.synthetic_signals()
            if source == AWS and body["kind"] == "outage"
        )
        duplicate = dict(outage, target="synthetic-cluster-copy", received_at=AT_RECOVERY)
        rows = (outage, duplicate)
        for body in rows:
            sre_mvp.validate(AWS, body, exact_cost=True)
        self.assertLessEqual(outage["received_at"], BEFORE_RECOVERY)
        self.assertLess(BEFORE_RECOVERY, duplicate["received_at"])

        # The malformed store has the producer's columns but lacks its primary key.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "CREATE TABLE signals (source TEXT NOT NULL, scope TEXT NOT NULL, "
                "event_id TEXT NOT NULL, body_json TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO signals VALUES (?, ?, ?, ?)",
                [(AWS, body["scope"], body["event_id"], json.dumps(body)) for body in rows],
            )

        for cutoff in (BEFORE_RECOVERY, AT_RECOVERY):
            with self.subTest(cutoff=cutoff):
                self.assert_cli_error(1, b"duplicate stored signal key", "get-incident",
                                      "--as-of", cutoff, "--incident-id", AWS_INCIDENT)

    def test_invalid_unrelated_rows_after_cutoff_prevent_partial_result(self):
        self.successful_cli("demo")
        args = ("get-incident", "--as-of", BEFORE_RECOVERY, "--incident-id", AWS_INCIDENT)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE signals SET body_json = replace(body_json, ?, ?) WHERE event_id = ?",
                ('"value":67.89', '"value":-1e-400', "synthetic-sakura-late-0001"),
            )
        self.assert_cli_error(1, b"invalid stored signal evidence", *args)

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE signals SET body_json = replace(body_json, ?, ?) WHERE event_id = ?",
                ('"value":-1e-400', '"value":67.89', "synthetic-sakura-late-0001"),
            )
            conn.execute("UPDATE signals SET event_id = ? WHERE event_id = ?",
                         ("wrong-sakura-recovery-key", "synthetic-sakura-recovery-0001"))
        self.assert_cli_error(1, b"stored signal key does not match body", *args)
