import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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


def health_sample(event_id, kind, observed_minute, received_minute, *,
                  source="synthetic-aws-01", **dimensions):
    def timestamp(minutes):
        return f"2024-01-01T{minutes // 60:02d}:{minutes % 60:02d}:00Z"

    missing = observed_minute is None
    if missing:
        freshness = "unknown"
    else:
        freshness = "late" if received_minute - observed_minute > 60 else "fresh"
    body = sample(
        sre_mvp.SOURCES[source], event_id, kind=kind,
        value=None if missing else int(kind == "outage"),
        unit=None if missing else "state",
        observed_at=None if missing else timestamp(observed_minute),
        received_at=timestamp(received_minute), freshness=freshness,
    )
    body.update(dimensions)
    return source, body


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


class SreIncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "signals.db"

    def test_groups_outages_by_observed_time_and_splits_recurrence(self):
        rows = [
            health_sample("evt-recurrence", "outage", 30, 31),
            health_sample("evt-recovery-b", "recovery", 20, 22),
            health_sample("evt-recovery-late", "recovery", 20, 85),
            health_sample("evt-repeat", "outage", 15, 30),
            health_sample("evt-before", "recovery", 5, 45),
            health_sample("evt-recovery-a", "recovery", 20, 21),
            health_sample("evt-first", "outage", 10, 38),
        ]
        ingest(self.db, rows)
        output = sre_mvp.list_incidents(self.db)
        first, second = output["incidents"]

        self.assertEqual("resolved", first["status"])
        self.assertEqual("2024-01-01T00:10:00Z", first["opened_at"])
        self.assertEqual("2024-01-01T00:15:00Z", first["last_outage_at"])
        self.assertEqual("2024-01-01T00:20:00Z", first["resolved_at"])
        self.assertEqual(
            ["evt-first", "evt-repeat", "evt-recovery-a", "evt-recovery-b"],
            [event["event_id"] for event in first["evidence"]],
        )
        opening = first["evidence"][0]
        self.assertEqual("2024-01-01T00:38:00Z", opening["received_at"])
        self.assertEqual(
            ("synthetic-aws-01", "synthetic-scope-a", "evt-first"),
            (opening["source"], opening["scope"], opening["event_id"]),
        )
        self.assertEqual("open", second["status"])
        self.assertIsNone(second["resolved_at"])
        self.assertEqual("evt-recurrence", second["evidence"][0]["event_id"])
        self.assertNotEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(
            [("evt-before", "no_open_incident"), ("evt-recovery-late", "late_recovery")],
            [(event["event_id"], event["reason"])
             for event in output["unapplied_health_evidence"]],
        )

        ingest(self.db, rows)
        self.assertEqual(output, sre_mvp.list_incidents(self.db))

    def test_same_observation_time_conflicts_leave_incident_open(self):
        rows = [
            health_sample("evt-z-first", "outage", 10, 15),
            health_sample("evt-a-first", "outage", 10, 25),
            health_sample("evt-0-conflict", "recovery", 10, 12),
            health_sample("evt-repeat", "outage", 20, 21),
            health_sample("evt-last-conflict", "recovery", 20, 28),
        ]
        ingest(self.db, rows)
        output = sre_mvp.list_incidents(self.db)
        (incident,) = output["incidents"]
        self.assertEqual("open", incident["status"])
        self.assertIsNone(incident["resolved_at"])
        self.assertEqual("health:synthetic-aws-01/synthetic-scope-a/evt-a-first",
                         incident["incident_id"])
        self.assertEqual(
            ["evt-0-conflict", "evt-last-conflict"],
            [event["event_id"] for event in output["unapplied_health_evidence"]],
        )
        self.assertTrue(all(event["reason"] == "ambiguous_recovery"
                            for event in output["unapplied_health_evidence"]))

        another = self.db.with_name("reversed.db")
        ingest(another, reversed(rows))
        self.assertEqual(output, sre_mvp.list_incidents(another))
        ingest(self.db, [health_sample("evt-recovery", "recovery", 21, 22)])
        (resolved,) = sre_mvp.list_incidents(self.db)["incidents"]
        self.assertEqual(incident["incident_id"], resolved["incident_id"])
        self.assertEqual("2024-01-01T00:21:00Z", resolved["resolved_at"])

    def test_missing_and_late_health_evidence_cannot_resolve(self):
        ingest(self.db, [
            health_sample("evt-late-outage", "outage", 10, 100),
            health_sample("evt-missing-recovery", "recovery", None, 22),
            health_sample("evt-late-recovery", "recovery", 20, 81),
            health_sample("evt-missing-outage", "outage", None, 23,
                          target="synthetic-other-target"),
        ])
        output = sre_mvp.list_incidents(self.db)
        (incident,) = output["incidents"]
        self.assertEqual("open", incident["status"])
        self.assertIsNone(incident["resolved_at"])
        (evidence,) = incident["evidence"]
        self.assertEqual("evt-late-outage", evidence["event_id"])
        self.assertEqual("late", evidence["freshness"])
        unapplied = {event["event_id"]: event for event in output["unapplied_health_evidence"]}
        self.assertEqual("late_recovery", unapplied["evt-late-recovery"]["reason"])
        for event_id in ("evt-missing-outage", "evt-missing-recovery"):
            self.assertEqual("missing_observation", unapplied[event_id]["reason"])
            self.assertIsNone(unapplied[event_id]["observed_at"])

    def test_matching_requires_all_health_dimensions(self):
        with patch.dict(sre_mvp.SOURCES, {"synthetic-aws-02": "aws"}):
            ingest(self.db, [
                health_sample("evt-outage", "outage", 10, 10),
                health_sample("evt-provider", "recovery", 20, 20, source="synthetic-azure-01"),
                health_sample("evt-source", "recovery", 20, 20, source="synthetic-aws-02"),
                health_sample("evt-scope", "recovery", 20, 20, scope="synthetic-scope-b"),
                health_sample("evt-environment", "recovery", 20, 20, environment="staging"),
                health_sample("evt-service", "recovery", 20, 20, service="other"),
                health_sample("evt-target", "recovery", 20, 20, target="synthetic-other-target"),
            ])
            output = sre_mvp.list_incidents(self.db)

        (incident,) = output["incidents"]
        self.assertEqual("open", incident["status"])
        self.assertEqual(6, len(output["unapplied_health_evidence"]))
        self.assertTrue(all(event["reason"] == "no_open_incident"
                            for event in output["unapplied_health_evidence"]))

    def test_incidents_cli_does_not_modify_demo_db(self):
        db = self.db.with_name("demo ?# evidence.sqlite3")
        ingest(db, synthetic_signals())
        db_bytes = db.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(0, sre_mvp.main(["--db", str(db), "incidents"]))
        output = json.loads(stdout.getvalue())
        self.assertEqual(1, output["version"])
        self.assertEqual(5, len(output["incidents"]))
        self.assertEqual({"resolved"}, {row["status"] for row in output["incidents"]})
        self.assertEqual([], output["unapplied_health_evidence"])
        self.assertEqual(db_bytes, db.read_bytes())
        self.assertEqual([db], list(db.parent.iterdir()))

    def test_cost_only_snapshot_keeps_raw_cost_without_health_classification(self):
        cost = synthetic_signals()[0]
        ingest(self.db, [cost])
        self.assertEqual(
            {"version": 1, "incidents": [], "unapplied_health_evidence": []},
            sre_mvp.list_incidents(self.db),
        )
        self.assertEqual(cost[1]["value"], list_signals(self.db)[0]["value"])

    def test_incidents_cli_missing_db_fails_without_creating_it(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(1, sre_mvp.main(["--db", str(self.db), "incidents"]))
        self.assertFalse(self.db.exists())
        self.assertEqual("", stdout.getvalue())
        self.assertTrue(stderr.getvalue().startswith("error: "))

    def test_incidents_cli_rejects_invalid_stored_evidence(self):
        source, recovery = health_sample("evt-recovery", "recovery", 20, 20)
        cases = {
            "malformed": "{",
            "non-object": "[]",
            "duplicate-kind": '{"kind":"outage",' + json.dumps(recovery)[1:],
            "wrong-key": json.dumps(dict(recovery, event_id="evt-different")),
            "bad-freshness": json.dumps(dict(recovery, freshness="late")),
        }
        for case, body_json in cases.items():
            with self.subTest(case=case):
                db = self.db.with_name(f"{case}.db")
                ingest(db, [health_sample("evt-outage", "outage", 10, 10), (source, recovery)])
                with sqlite3.connect(db) as conn:
                    conn.execute("UPDATE signals SET body_json = ? WHERE event_id = ?",
                                 (body_json, recovery["event_id"]))
                conn.close()

                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(1, sre_mvp.main(["--db", str(db), "incidents"]))
                self.assertEqual("", stdout.getvalue())
                self.assertTrue(stderr.getvalue().startswith("error: "))


if __name__ == "__main__":
    unittest.main()
