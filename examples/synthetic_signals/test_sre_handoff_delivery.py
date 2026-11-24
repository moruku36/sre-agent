"""Offline acceptance tests for one synthetic pull/ACK ledger."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from decimal import Decimal
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sre_handoff_delivery
import sre_mvp


HERE = Path(__file__).resolve().parent
POLICY = HERE / "cost_policy.json"
CUTOFF = "2026-01-15T12:21:00Z"
UNKNOWN = "0" * 64


def fingerprint(path):
    status = path.stat()
    return path.read_bytes(), status.st_ino, status.st_mode, status.st_mtime_ns


class HandoffDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.db = self.directory / "signals ?#.sqlite3"
        self.ledger = self.directory / "receipts ?#.sqlite3"
        sre_mvp.ingest(self.db, sre_mvp.synthetic_signals())

    def cli(self, *args, db=None, ledger=None):
        return subprocess.run(
            [sys.executable, "-B", str(HERE / "sre_handoff_delivery.py"),
             "--db", str(db or self.db), "--ledger", str(ledger or self.ledger), *args],
            capture_output=True, timeout=15,
        )

    def success(self, *args, **paths):
        result = self.cli(*args, **paths)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        return json.loads(result.stdout)

    def fails(self, *args, **paths):
        result = self.cli(*args, **paths)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"error:", result.stderr)
        return result.stderr

    def offer(self):
        return self.success("offer", "--policy", str(POLICY), "--as-of", CUTOFF)

    def test_offer_wraps_exact_legacy_json_and_does_not_write_signals(self):
        amount = "123.4500000000000001"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE signals SET body_json = replace(body_json, ?, ?) WHERE event_id = ?",
                ('"value":123.45', f'"value":{amount}', "synthetic-aws-cost-0001"),
            )
        before = {path: fingerprint(path) for path in (self.db, POLICY)}
        cutoff = "2026-01-15T12:05:00Z"
        flags = ("--policy", str(POLICY), "--as-of", cutoff)
        legacy = subprocess.run(
            [sys.executable, "-B", str(HERE / "sre_mvp.py"), "--db", str(self.db),
             "handoff", *flags], capture_output=True, check=True, timeout=15,
        )
        offered = self.success("offer", *flags)
        serialized = offered["handoff_json"].encode("utf-8")
        self.assertEqual(legacy.stdout, serialized + b"\n")
        self.assertEqual(sha256(serialized).hexdigest(), offered["delivery_id"])
        snapshot = json.loads(serialized, parse_float=Decimal)
        self.assertEqual("synthetic_sre_handoff", snapshot["kind"])
        aws = snapshot["cost_classifications"][0]
        self.assertEqual(Decimal(amount), aws["evidence"][0]["value"])
        self.assertEqual(offered, self.success("pull"))
        self.success("ack", offered["delivery_id"])
        self.assertEqual(before, {path: fingerprint(path) for path in before})

    def test_concurrent_duplicate_offers_and_restarted_ack_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.offer)
            second = workers.submit(self.offer)
            offered = first.result()
            self.assertEqual(offered, second.result())
        delivery_id = offered["delivery_id"]
        self.assertIs(offered["acknowledged"], False)
        self.assertEqual(offered, self.success("pull"))
        self.assertEqual(offered, self.success("pull"))

        receipt = {"delivery_id": delivery_id, "acknowledged": True}
        self.assertEqual(receipt, self.success("ack", delivery_id))
        ledger = fingerprint(self.ledger)
        self.assertEqual(receipt, self.success("ack", delivery_id))
        self.assertEqual(dict(offered, acknowledged=True), self.offer())
        self.assertIsNone(self.success("pull"))
        self.assertEqual(ledger, fingerprint(self.ledger))

    def test_new_snapshot_at_same_cutoff_stays_pending_after_old_ack(self):
        old = self.offer()
        cost = next(body for source, body in sre_mvp.synthetic_signals()
                    if source == "synthetic-aws-01" and body["kind"] == "cost_actual")
        updated = dict(cost, event_id="synthetic-aws-cost-new",
                       observed_at=CUTOFF, received_at=CUTOFF, value=172)
        sre_mvp.ingest(self.db, [("synthetic-aws-01", updated)])
        new = self.offer()

        self.assertNotEqual(old["delivery_id"], new["delivery_id"])
        self.assertNotEqual(old["handoff_json"], new["handoff_json"])
        for delivery in (old, new):
            self.assertEqual(CUTOFF, json.loads(delivery["handoff_json"])["as_of"])
        self.assertEqual(old, self.success("pull"))
        self.assertTrue(self.success("ack", old["delivery_id"])["acknowledged"])
        self.assertEqual(new, self.success("pull"))
        self.assertTrue(self.success("ack", old["delivery_id"])["acknowledged"])
        self.assertEqual(new, self.success("pull"))

    def test_replay_and_ack_do_not_require_source_to_reopen(self):
        pending = self.offer()
        self.db.rename(self.directory / "archived.sqlite3")
        self.assertEqual(pending, self.success("pull"))
        self.assertTrue(self.success("ack", pending["delivery_id"])["acknowledged"])
        self.assertIsNone(self.success("pull"))
        self.assertFalse(self.db.exists())

    def test_missing_unknown_and_malformed_ack_do_not_write(self):
        self.assertIsNone(self.success("pull"))
        self.assertIn(b"unknown", self.fails("ack", UNKNOWN))
        self.assertFalse(self.ledger.exists())
        # An interrupted first offer can leave a file with no committed schema.
        self.ledger.touch()
        self.assertIsNone(self.success("pull"))
        self.assertIn(b"unknown", self.fails("ack", UNKNOWN))
        self.assertEqual(b"", self.ledger.read_bytes())
        existing = self.offer()
        before = fingerprint(self.ledger)
        for delivery_id in (UNKNOWN, "G" * 64, "A" * 64, "0" * 63, "0" * 64 + "\n"):
            with self.subTest(delivery_id=delivery_id):
                self.fails("ack", delivery_id)
        self.assertEqual(before, fingerprint(self.ledger))
        self.assertEqual(existing, self.success("pull"))

    def test_rejects_signal_aliases_before_opening_a_ledger(self):
        symlink = self.directory / "symlink.sqlite3"
        symlink.symlink_to(self.db)
        hardlink = self.directory / "hardlink.sqlite3"
        os.link(self.db, hardlink)
        before = fingerprint(self.db)
        for alias in (self.db, symlink, hardlink):
            with self.subTest(alias=alias.name):
                self.fails("offer", "--policy", str(POLICY), "--as-of", CUTOFF, ledger=alias)
                self.fails("pull", ledger=alias)
                self.fails("ack", UNKNOWN, ledger=alias)
        self.assertEqual(before, fingerprint(self.db))

    def test_rejects_sidecar_path_and_inode_collisions(self):
        before = fingerprint(self.db)
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                name = Path(str(self.db) + suffix)
                self.fails("offer", "--policy", str(POLICY), "--as-of", CUTOFF, ledger=name)
                self.assertFalse(name.exists())
        ledger_journal = Path(str(self.ledger) + "-journal")
        os.link(self.db, ledger_journal)
        self.fails("offer", "--policy", str(POLICY), "--as-of", CUTOFF)
        self.assertEqual(before, fingerprint(self.db))
        self.assertFalse(self.ledger.exists())

    def assert_policy_rejected_before_connect(self, policy):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sqlite3, "connect", side_effect=AssertionError("unexpected SQLite connection")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = sre_handoff_delivery.main([
                    "--db", str(self.db), "--ledger", str(self.ledger),
                    "offer", "--policy", str(policy), "--as-of", CUTOFF,
                ])
        self.assertEqual(1, result)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("cost policy must be separate", stderr.getvalue())
        self.assertIn("SQLite sidecars", stderr.getvalue())

    def test_offer_rejects_reserved_policy_paths_without_creating_ledger(self):
        source_before = fingerprint(self.db)
        self.assert_policy_rejected_before_connect(self.db)
        self.assertIn(b"cost policy must be separate", self.fails(
            "offer", "--policy", str(self.ledger), "--as-of", CUTOFF,
        ))
        for database in (self.db, self.ledger):
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                with self.subTest(sidecar=sidecar.name):
                    sidecar.write_bytes(POLICY.read_bytes())
                    before = fingerprint(sidecar)
                    self.assert_policy_rejected_before_connect(sidecar)
                    self.assertEqual(before, fingerprint(sidecar))
        self.assertEqual(source_before, fingerprint(self.db))
        self.assertFalse(self.ledger.exists())

    def test_offer_rejects_policy_aliases_without_changing_existing_ledger(self):
        self.offer()
        sidecar = Path(f"{self.ledger}-journal")
        sidecar.write_bytes(POLICY.read_bytes())
        symlink = self.directory / "policy-symlink.json"
        symlink.symlink_to(sidecar)
        hardlink = self.directory / "policy-hardlink.json"
        os.link(sidecar, hardlink)
        before = {path: fingerprint(path) for path in (self.db, self.ledger, sidecar, symlink, hardlink)}
        for policy in (self.ledger, symlink, hardlink):
            with self.subTest(policy=policy.name):
                self.assert_policy_rejected_before_connect(policy)
        self.assertEqual(before, {path: fingerprint(path) for path in before})

    def test_offer_does_not_reserve_sidecar_paths_for_policy(self):
        policy = self.directory / "policy.json"
        policy.write_bytes(POLICY.read_bytes())
        ledger = Path(f"{policy}-wal")
        before = {path: fingerprint(path) for path in (self.db, policy)}
        offered = self.success("offer", "--policy", str(policy), "--as-of", CUTOFF, ledger=ledger)
        self.assertEqual(offered, self.success("pull", ledger=ledger))
        self.assertEqual(before, {path: fingerprint(path) for path in before})

    def test_invalid_snapshot_never_initializes_a_ledger(self):
        broken = self.directory / "invalid.sqlite3"
        broken.write_text("not a sqlite database", encoding="utf-8")
        invalid_policy = self.directory / "invalid.json"
        invalid_policy.write_text("{", encoding="utf-8")
        for paths, policy, cutoff in (
            ({"db": broken}, POLICY, CUTOFF),
            ({"db": self.directory / "absent.sqlite3"}, POLICY, CUTOFF),
            ({}, invalid_policy, CUTOFF),
            ({}, POLICY, "2026-01-15T12:21:00+00:00"),
        ):
            with self.subTest(paths=paths, policy=policy, cutoff=cutoff):
                self.fails("offer", "--policy", str(policy), "--as-of", cutoff, **paths)
                self.assertFalse(self.ledger.exists())

    def test_refuses_preexisting_foreign_and_malformed_ledgers(self):
        foreign = self.directory / "other-signals.sqlite3"
        foreign.write_bytes(self.db.read_bytes())
        broken = self.directory / "not-sqlite.txt"
        broken.write_text("not a sqlite database", encoding="utf-8")
        for path in (foreign, broken):
            with self.subTest(path=path.name):
                before = fingerprint(path)
                self.fails("offer", "--policy", str(POLICY), "--as-of", CUTOFF, ledger=path)
                self.fails("pull", ledger=path)
                self.fails("ack", UNKNOWN, ledger=path)
                self.assertEqual(before, fingerprint(path))

    def test_rejects_stored_payload_that_no_longer_matches_its_id(self):
        offered = self.offer()
        delivery_id = offered["delivery_id"]
        with closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                "UPDATE handoff_deliveries SET handoff_json = ? WHERE delivery_id = ?",
                ("{}", delivery_id),
            )
        before = fingerprint(self.ledger)
        self.assertIn(b"does not match", self.fails("pull"))
        self.assertIn(b"does not match", self.fails("ack", delivery_id))
        self.assertIn(b"does not match", self.fails("offer", "--policy", str(POLICY), "--as-of", CUTOFF))
        self.assertEqual(before, fingerprint(self.ledger))

    def test_failed_ack_commit_prints_no_receipt_and_can_be_retried(self):
        offered = self.offer()
        delivery_id = offered["delivery_id"]
        connect = sqlite3.connect

        def short_timeout(*args, **kwargs):
            return connect(*args, **dict(kwargs, timeout=0.03))

        with closing(connect(self.ledger)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT name FROM sqlite_master").fetchall()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(sqlite3, "connect", side_effect=short_timeout):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = sre_handoff_delivery.main([
                        "--db", str(self.db), "--ledger", str(self.ledger), "ack", delivery_id,
                    ])
            self.assertNotEqual(result, 0)
            self.assertEqual("", stdout.getvalue())
            self.assertIn("locked", stderr.getvalue())
        self.assertEqual(offered, self.success("pull"))
        self.assertTrue(self.success("ack", delivery_id)["acknowledged"])
        self.assertIsNone(self.success("pull"))


if __name__ == "__main__":
    unittest.main()
