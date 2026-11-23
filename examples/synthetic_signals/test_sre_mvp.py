import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import sre_mvp
from sre_mvp import ingest, list_signals, synthetic_signals, validate


def sample(provider, event_id, scope="synthetic-scope-a", kind="cost_actual",
           value=10, unit="USD", observed_at="2024-01-01T00:00:00Z",
           received_at="2024-01-01T00:30:00Z", freshness="fresh"):
    return {
        "version": 1,
        "provider": provider,
        "scope": scope,
        "target": "synthetic-target",
        "evidence": "synthetic-evidence",
        "service": "svc",
        "environment": "prod",
        "event_id": event_id,
        "kind": kind,
        "observed_at": observed_at,
        "received_at": received_at,
        "freshness": freshness,
        "value": value,
        "unit": unit,
    }


class SreMvpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db = Path(self.tmp.name) / "signals.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_provider_matrix_and_replay(self):
        counts = ingest(self.db, synthetic_signals())
        self.assertEqual(25, counts["inserted"])
        self.assertEqual(5, counts["duplicates"])

        rows = list_signals(self.db)
        self.assertEqual(25, len(rows))
        self.assertEqual(
            {"aws", "gcp", "azure", "oci", "sakura"},
            {row["provider"] for row in rows},
        )

        for provider in ("aws", "gcp", "azure", "oci", "sakura"):
            by_kind = {}
            for row in rows:
                if row["provider"] == provider:
                    kind = "cost" if row["kind"].startswith("cost_") else row["kind"]
                    by_kind[(kind, row["freshness"])] = row

            self.assertIn(("cost", "fresh"), by_kind)
            self.assertIn(("outage", "fresh"), by_kind)
            self.assertIn(("recovery", "fresh"), by_kind)
            self.assertIn(("cost", "late"), by_kind)
            self.assertIn(("cost", "unknown"), by_kind)

            late = by_kind[("cost", "late")]
            self.assertIsNotNone(late["value"])
            self.assertIsNotNone(late["unit"])

            unknown = by_kind[("cost", "unknown")]
            self.assertIsNone(unknown["observed_at"])
            self.assertIsNone(unknown["value"])
            self.assertIsNone(unknown["unit"])

        self.assertEqual(
            sorted(rows, key=lambda row: (row["source"], row["scope"], row["event_id"])),
            rows,
        )

    def test_dedupe_across_reconnects(self):
        rows = [(s, sample(p, "evt")) for s, p in sre_mvp.SOURCES.items()]
        sre_mvp.ingest(self.db, rows)
        res = sre_mvp.ingest(self.db, rows)
        self.assertEqual(res["inserted"], 0)
        self.assertEqual(res["duplicates"], 5)

    def test_scoped_key_and_source_isolation(self):
        s1, p1 = next(iter(sre_mvp.SOURCES.items()))
        s2, p2 = list(sre_mvp.SOURCES.items())[1]
        rows = [
            (s1, sample(p1, "evt", scope="synthetic-a")),
            (s1, sample(p1, "evt", scope="synthetic-b")),
            (s2, sample(p2, "evt", scope="synthetic-a")),
        ]
        res = sre_mvp.ingest(self.db, rows)
        self.assertEqual(res["inserted"], 3)
        self.assertEqual(len(sre_mvp.list_signals(self.db)), 3)

    def test_altered_payload_does_not_overwrite(self):
        s, p = next(iter(sre_mvp.SOURCES.items()))
        a = sample(p, "evt", value=1)
        b = sample(p, "evt", value=2)
        sre_mvp.ingest(self.db, [(s, a)])
        res = sre_mvp.ingest(self.db, [(s, b)])
        self.assertEqual(res["inserted"], 0)
        self.assertEqual(res["duplicates"], 1)
        self.assertEqual(sre_mvp.list_signals(self.db)[0]["value"], 1)

    def test_invalid_batch_atomic(self):
        source, body = synthetic_signals()[0]
        ingest(self.db, [(source, body)])
        self.assertEqual(1, len(list_signals(self.db)))

        valid = dict(body)
        valid["event_id"] = "evt-atomic-valid"
        invalid = dict(valid)
        invalid["version"] = 2

        with self.assertRaises(ValueError):
            ingest(self.db, [(source, valid), (source, invalid)])

        rows = list_signals(self.db)
        self.assertEqual(1, len(rows))
        self.assertEqual(body["event_id"], rows[0]["event_id"])

    def test_uri_punctuation_and_no_create_read(self):
        db = Path(self.tmp.name) / "weird ?# name.db"
        s, p = next(iter(sre_mvp.SOURCES.items()))
        sre_mvp.ingest(db, [(s, sample(p, "evt"))])
        self.assertEqual(len(sre_mvp.list_signals(db)), 1)
        missing = Path(self.tmp.name) / "missing ?# name.db"
        with self.assertRaises(sqlite3.OperationalError):
            sre_mvp.list_signals(missing)
        self.assertFalse(missing.exists())

    def test_strict_validation_table(self):
        source, base = synthetic_signals()[0]

        def mutate(**changes):
            signal = dict(base)
            signal.update(changes)
            return signal

        with self.assertRaises(ValueError):
            validate(source, mutate(version=1.0))
        with self.assertRaises(ValueError):
            validate(source, mutate(value=1.0, kind="outage", unit="state"))
        with self.assertRaises(ValueError):
            validate(source, mutate(received_at="2026-02-30T12:00:00Z"))
        with self.assertRaises(ValueError):
            validate(source, mutate(received_at="2026-01-15t12:05:00Z"))
        with self.assertRaises(ValueError):
            validate(source, mutate(received_at="2026-01-15T11:59:59Z"))
        with self.assertRaises(ValueError):
            validate(source, mutate(received_at="2026-01-15T13:00:01Z", freshness="fresh"))
        with self.assertRaises(ValueError):
            validate(source, mutate(kind=["cost_actual"]))
        with self.assertRaises(ValueError):
            validate(source, mutate(unit={"currency": "USD"}))
        with self.assertRaises(ValueError):
            validate(source, mutate(provider="wrong"))
        with self.assertRaises(ValueError):
            validate(source, mutate(scope="synthetic-"))
        with self.assertRaises(ValueError):
            validate(source, mutate(target="real-target"))
        with self.assertRaises(ValueError):
            validate(source, mutate(evidence="synthetic-"))
        with self.assertRaises(ValueError):
            validate(source, mutate(value=float("nan")))
        with self.assertRaises(ValueError):
            validate(source, mutate(value=float("inf")))
        with self.assertRaises(ValueError):
            validate(source, mutate(value=-1.0))
        with self.assertRaises(ValueError):
            validate(source, mutate(kind="outage", value=0, unit="state"))
        with self.assertRaises(ValueError):
            validate(source, mutate(kind="recovery", value=1, unit="state"))
        with self.assertRaises(ValueError):
            validate(source, mutate(received_at=None))
        with self.assertRaises(ValueError):
            validate(source, mutate(observed_at=None, freshness="unknown", value=1, unit="USD"))
        with self.assertRaises(ValueError):
            validate(source, mutate(observed_at=base["observed_at"], value=None, unit=None))

        with self.assertRaises(ValueError):
            validate([], base)

        boundary = validate(
            source,
            mutate(
                observed_at=base["observed_at"],
                received_at="2026-01-15T13:00:00Z",
                freshness="fresh",
            ),
        )
        self.assertEqual("fresh", boundary["freshness"])

        large = validate(source, mutate(value=10**30))
        self.assertEqual(10**30, large["value"])

    def test_freshness_late_and_unknown_preserved(self):
        signals = synthetic_signals()
        late = next(
            (source, body)
            for source, body in signals
            if body["freshness"] == "late"
        )
        missing = next(
            (source, body)
            for source, body in signals
            if body["freshness"] == "unknown"
        )

        ingest(self.db, [late, missing])
        rows = {row["event_id"]: row for row in list_signals(self.db)}

        late_row = rows[late[1]["event_id"]]
        self.assertEqual("late", late_row["freshness"])
        self.assertIsNotNone(late_row["observed_at"])
        self.assertIsNotNone(late_row["received_at"])

        missing_row = rows[missing[1]["event_id"]]
        self.assertEqual("unknown", missing_row["freshness"])
        self.assertIsNone(missing_row["observed_at"])
        self.assertIsNone(missing_row["value"])
        self.assertIsNone(missing_row["unit"])

    def test_cli_repeat_and_listing(self):
        db = str(self.db)
        first = io.StringIO()
        with redirect_stdout(first):
            self.assertEqual(0, sre_mvp.main(["--db", db, "demo"]))
        self.assertEqual({"inserted": 25, "duplicates": 5}, json.loads(first.getvalue()))

        second = io.StringIO()
        with redirect_stdout(second):
            self.assertEqual(0, sre_mvp.main(["--db", db, "demo"]))
        self.assertEqual({"inserted": 0, "duplicates": 30}, json.loads(second.getvalue()))

        listed = io.StringIO()
        with redirect_stdout(listed):
            self.assertEqual(0, sre_mvp.main(["--db", db, "list"]))
        payload = json.loads(listed.getvalue())
        self.assertEqual(25, len(payload))


if __name__ == "__main__":
    unittest.main()
