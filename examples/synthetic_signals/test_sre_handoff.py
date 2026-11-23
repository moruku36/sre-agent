"""CLI acceptance for the offline synthetic pull handoff."""

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import sre_mvp


HERE = Path(__file__).resolve().parent
POLICY = HERE / "cost_policy.json"
BEFORE_RECOVERY = "2026-01-15T12:21:00Z"
AT_RECOVERY = "2026-01-15T12:22:00Z"
AWS = "synthetic-aws-01"


class SreHandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.db = self.directory / "signals ?# fixture.sqlite3"

    def cli(self, command, *args):
        return subprocess.run(
            [sys.executable, "-B", str(HERE / "sre_mvp.py"), "--db", str(self.db), command, *args],
            capture_output=True, timeout=15,
        )

    def successful_cli(self, command, *args):
        result = self.cli(command, *args)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        return result.stdout

    def pull(self, cutoff, policy=POLICY):
        return self.successful_cli("handoff", "--policy", str(policy), "--as-of", cutoff)

    def aws_sample(self, kind):
        return next(
            body for source, body in sre_mvp.synthetic_signals()
            if source == AWS and body["kind"] == kind
        )

    def test_both_paths_use_the_inclusive_receipt_cutoff(self):
        self.successful_cli("demo")
        recovery = self.aws_sample("recovery")
        missing_recovery = dict(
            recovery, event_id="synthetic-aws-missing-recovery",
            observed_at=None, received_at=BEFORE_RECOVERY,
            freshness="unknown", value=None, unit=None,
        )
        new_cost = dict(
            self.aws_sample("cost_actual"), event_id="synthetic-aws-cost-update",
            observed_at=recovery["observed_at"], received_at=AT_RECOVERY, value=151,
        )
        sre_mvp.ingest(self.db, [(AWS, missing_recovery), (AWS, new_cost)])

        early = json.loads(self.pull(BEFORE_RECOVERY))
        later = json.loads(self.pull(AT_RECOVERY))
        self.assertEqual({
            "kind", "version", "as_of", "incidents", "unapplied_health_evidence",
            "cost_classifications",
        }, set(early))
        self.assertEqual(("synthetic_sre_handoff", 1), (early["kind"], early["version"]))
        self.assertEqual((BEFORE_RECOVERY, AT_RECOVERY), (early["as_of"], later["as_of"]))

        self.assertEqual(["open"] * 5, [item["status"] for item in early["incidents"]])
        self.assertEqual(["resolved"] * 5, [item["status"] for item in later["incidents"]])
        before, after = early["incidents"][0], later["incidents"][0]
        self.assertEqual(before["incident_id"], after["incident_id"])
        self.assertIsNone(before["resolved_at"])
        self.assertEqual(recovery["observed_at"], after["resolved_at"])
        self.assertLess(after["resolved_at"], BEFORE_RECOVERY)
        self.assertEqual(["outage"], [item["kind"] for item in before["evidence"]])
        self.assertEqual(before["evidence"], after["evidence"][:-1])
        self.assertEqual(
            (AWS, recovery["scope"], recovery["event_id"], recovery["evidence"], AT_RECOVERY),
            tuple(after["evidence"][-1][field] for field in
                  ("source", "scope", "event_id", "evidence", "received_at")),
        )

        (unapplied,) = early["unapplied_health_evidence"]
        self.assertEqual(missing_recovery["event_id"], unapplied["event_id"])
        self.assertEqual(("missing_observation", "unknown", None),
                         (unapplied["reason"], unapplied["freshness"], unapplied["observed_at"]))
        self.assertEqual(early["unapplied_health_evidence"], later["unapplied_health_evidence"])

        for payload, status, event_id in (
            (early, "within_limit", "synthetic-aws-cost-0001"),
            (later, "anomaly", new_cost["event_id"]),
        ):
            cutoff = payload["as_of"]
            separate = self.successful_cli("cost", "--policy", str(POLICY), "--as-of", cutoff)
            self.assertEqual(json.loads(separate)["classifications"], payload["cost_classifications"])
            aws = payload["cost_classifications"][0]
            self.assertEqual((status, AWS, event_id),
                             (aws["status"], aws["evidence"][0]["source"], aws["evidence"][0]["event_id"]))

    def test_exact_numeric_output_freshness_repeatability_and_no_writes(self):
        self.successful_cli("demo")
        threshold = "123.45"
        amount = "123.4500000000000001"
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE signals SET body_json = replace(body_json, ?, ?) WHERE event_id = ?",
                ('"value":123.45', f'"value":{amount}', "synthetic-aws-cost-0001"),
            )

        path = self.directory / "policy.json"
        aws_rule = json.loads(POLICY.read_bytes())["rules"][0]
        aws_rule.update(max_age_seconds=300, threshold=None)
        policy = json.dumps({"version": 1, "rules": [aws_rule]})
        path.write_text(policy.replace('"threshold": null', f'"threshold": {threshold}'), encoding="utf-8")

        def fingerprint(file):
            status = file.stat()
            return file.read_bytes(), status.st_ino, status.st_mode, status.st_mtime_ns

        before = [fingerprint(file) for file in (self.db, path)]
        before_receipt = json.loads(self.pull("2026-01-15T12:04:59Z", path))
        (not_received,) = before_receipt["cost_classifications"]
        self.assertEqual(("unknown", "no_signal", []),
                         (not_received["status"], not_received["reason"], not_received["evidence"]))

        at_receipt = self.pull("2026-01-15T12:05:00Z", path)
        self.assertEqual(at_receipt, self.pull("2026-01-15T12:05:00Z", path))
        self.assertEqual(1, at_receipt.count(b"\n"))
        self.assertTrue(at_receipt.endswith(b"\n"))
        payload = json.loads(at_receipt, parse_float=Decimal)
        self.assertEqual([], payload["incidents"])
        self.assertEqual([], payload["unapplied_health_evidence"])
        (fresh,) = payload["cost_classifications"]
        self.assertEqual(("anomaly", "above_threshold"), (fresh["status"], fresh["reason"]))
        self.assertEqual(Decimal(threshold), fresh["rule"]["threshold"])
        self.assertEqual(Decimal(amount), fresh["evidence"][0]["value"])

        later = json.loads(self.pull("2026-01-15T12:05:01Z", path), parse_float=Decimal)
        (stale,) = later["cost_classifications"]
        self.assertEqual(("unknown", "stale_observation"), (stale["status"], stale["reason"]))
        self.assertEqual(fresh["evidence"], stale["evidence"])
        self.assertEqual(before, [fingerprint(file) for file in (self.db, path)])
        self.assertEqual({self.db, path}, set(self.directory.iterdir()))

    def test_both_readers_share_one_snapshot_even_if_a_writer_commits(self):
        outage = self.aws_sample("outage")
        cost = self.aws_sample("cost_actual")
        sre_mvp.ingest(self.db, [(AWS, outage), (AWS, cost)])

        recovery = self.aws_sample("recovery")
        cost_update = dict(cost, event_id="synthetic-aws-committed-cost",
                           observed_at=recovery["observed_at"], received_at=AT_RECOVERY, value=151)
        read_rows = sre_mvp._signal_rows

        def read_then_commit(db):
            snapshot = read_rows(db)
            # These receipts are eligible at the same cutoff but are not in this read.
            sre_mvp.ingest(db, [(AWS, recovery), (AWS, cost_update)])
            return snapshot

        args = ["--db", str(self.db), "handoff", "--policy", str(POLICY), "--as-of", AT_RECOVERY]
        with patch.object(sre_mvp, "_signal_rows", side_effect=read_then_commit) as reads:
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, sre_mvp.main(args))
        reads.assert_called_once_with(str(self.db))
        captured = json.loads(stdout.getvalue())
        self.assertEqual("open", captured["incidents"][0]["status"])
        self.assertEqual("within_limit", captured["cost_classifications"][0]["status"])
        next_snapshot = json.loads(self.pull(AT_RECOVERY))
        self.assertEqual("resolved", next_snapshot["incidents"][0]["status"])
        self.assertEqual("anomaly", next_snapshot["cost_classifications"][0]["status"])

    def test_requires_inputs_and_propagates_failures(self):
        flags = ("--policy", str(POLICY), "--as-of", AT_RECOVERY)
        for args in ((), flags[:2], flags[2:]):
            with self.subTest(args=args):
                result = self.cli("handoff", *args)
                self.assertEqual((2, b""), (result.returncode, result.stdout))
                self.assertIn(b"handoff requires --policy and --as-of", result.stderr)

        invalid_policy = self.directory / "invalid-policy.json"
        invalid_policy.write_text("{", encoding="utf-8")
        for args, error in (
            ((*flags[:-1], "2026-01-15T12:22:00+00:00"), b"invalid canonical UTC timestamp"),
            (("--policy", str(invalid_policy), *flags[2:]), b"invalid cost policy JSON"),
            (flags, b"unable to open database file"),
        ):
            with self.subTest(args=args):
                result = self.cli("handoff", *args)
                self.assertEqual((1, b""), (result.returncode, result.stdout))
                self.assertIn(b"error: " + error, result.stderr)
                self.assertFalse(self.db.exists())

    def test_post_cutoff_row_still_requires_matching_stored_key(self):
        self.successful_cli("demo")
        self.pull(BEFORE_RECOVERY)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE signals SET event_id = ? WHERE event_id = ?",
                ("synthetic-mismatched-key", "synthetic-sakura-recovery-0001"),
            )
        result = self.cli("handoff", "--policy", str(POLICY), "--as-of", BEFORE_RECOVERY)
        self.assertEqual((1, b""), (result.returncode, result.stdout))
        self.assertIn(b"error: stored signal key does not match body", result.stderr)
