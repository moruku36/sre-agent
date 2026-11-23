"""Offline acceptance for the synthetic cost policy command."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import sre_mvp


HERE = Path(__file__).resolve().parent
AS_OF = "2026-01-15T13:10:00Z"
SOURCE, SAMPLE = sre_mvp.synthetic_signals()[0]
RULE = {
    "source": SOURCE,
    **{field: SAMPLE[field] for field in (
        "provider", "scope", "service", "environment", "target", "kind", "unit",
    )},
    "threshold": 100,
    "max_age_seconds": 4200,
}


def row(event_id, **changes):
    return SOURCE, {**SAMPLE, "event_id": event_id, "value": 50, **changes}


class SreCostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = self.directory / "signals ?# evidence.db"

    def decisions(self, rules=(RULE,), *, as_of=AS_OF, db=None):
        return sre_mvp.list_cost_anomalies(
            self.db if db is None else db, {"version": 1, "rules": list(rules)}, as_of,
        )["classifications"]

    def run_cost(self, threshold):
        path = self.directory / "policy.json"
        policy = json.dumps({"version": 1, "rules": [RULE]})
        path.write_text(
            policy.replace('"threshold": 100', f'"threshold": {threshold}', 1),
            encoding="utf-8",
        )
        args = ["--db", str(self.db), "cost", "--policy", str(path), "--as-of", AS_OF]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = sre_mvp.main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    def store_amount_literal(self, event_id, literal):
        # Simulate preexisting JSON more precise than ordinary Python float input.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            (payload,) = conn.execute(
                "SELECT body_json FROM signals WHERE event_id = ?", (event_id,),
            ).fetchone()
            body = json.loads(payload)
            body["value"] = None
            payload = json.dumps(body, separators=(",", ":"))
            conn.execute(
                "UPDATE signals SET body_json = ? WHERE event_id = ?",
                (payload.replace('"value":null', f'"value":{literal}', 1), event_id),
            )

    def test_policy_literal_boundary_and_exact_numeric_output(self):
        sre_mvp.ingest(self.db, [row("evt-decimal", value=123.45)])
        for literal, expected in (
            ("123.449999999999999", "anomaly"),
            ("123.450", "within_limit"),
            ("1e400", "within_limit"),
        ):
            with self.subTest(literal=literal):
                status, output, error = self.run_cost(literal)
                self.assertEqual((0, ""), (status, error))
                (decision,) = json.loads(output, parse_float=Decimal)["classifications"]
                self.assertEqual(expected, decision["status"])
                self.assertEqual(Decimal(literal), decision["rule"]["threshold"])
                self.assertEqual(Decimal("123.45"), decision["evidence"][0]["value"])

        (decision,) = self.decisions((dict(RULE, threshold=123.45),))
        self.assertEqual("within_limit", decision["status"])

    def test_negative_underflow_is_invalid_in_policy(self):
        sre_mvp.ingest(self.db, [row("evt-visible")])
        status, output, error = self.run_cost("-1e-400")
        self.assertEqual((1, ""), (status, output))
        self.assertIn("finite nonnegative number", error)

    def test_invalid_stored_amount_is_rejected_after_cutoff(self):
        sre_mvp.ingest(self.db, [
            row("evt-visible"),
            row("evt-future", scope="synthetic-other-scope",
                observed_at="2026-01-15T13:00:00Z", received_at="2026-01-15T13:11:00Z"),
        ])
        self.assertEqual(0, self.run_cost("100")[0])
        for literal in ("-1e-400", "NaN", "Infinity", "true", '"50"', '50,"value":50'):
            with self.subTest(literal=literal):
                self.store_amount_literal("evt-future", literal)
                status, output, error = self.run_cost("100")
                self.assertEqual((1, ""), (status, output))
                self.assertIn("invalid stored signal evidence", error)

    def test_same_observation_compares_exact_stored_numbers(self):
        sre_mvp.ingest(self.db, [
            row("evt-first", value=123.45),
            row("evt-latest", value=123.45, received_at="2026-01-15T12:30:00Z"),
        ])
        self.store_amount_literal("evt-first", "123.449999999999999")
        status, output, error = self.run_cost("123.45")
        self.assertEqual((0, ""), (status, error))
        (conflict,) = json.loads(output, parse_float=Decimal)["classifications"]
        self.assertEqual(("unknown", "ambiguous_observation"),
                         (conflict["status"], conflict["reason"]))
        self.assertEqual(
            [("evt-first", Decimal("123.449999999999999")), ("evt-latest", Decimal("123.45"))],
            [(item["event_id"], item["value"]) for item in conflict["evidence"]],
        )

        self.store_amount_literal("evt-first", "1.23450e2")
        (equal,) = self.decisions((dict(RULE, threshold=123.45),))
        self.assertEqual("within_limit", equal["status"])
        self.assertEqual(["evt-latest"], [item["event_id"] for item in equal["evidence"]])

    def test_kind_lanes_and_inclusive_threshold_at_maximum_age(self):
        forecast = dict(RULE, kind="cost_forecast")
        estimate = dict(RULE, kind="cost_estimate")
        sre_mvp.ingest(self.db, [
            row("evt-forecast", kind=forecast["kind"], value=100),
            row("evt-estimate", kind=estimate["kind"], value=0),
            row("evt-actual", value=101),
        ])
        actual_decision, estimate_decision, forecast_decision = self.decisions(
            (forecast, RULE, estimate),
        )
        self.assertEqual(RULE, actual_decision["rule"])
        self.assertEqual("anomaly", actual_decision["status"])
        self.assertEqual("above_threshold", actual_decision["reason"])
        self.assertEqual("within_limit", forecast_decision["status"])
        self.assertEqual("at_or_below_threshold", forecast_decision["reason"])
        self.assertEqual("within_limit", estimate_decision["status"])
        for decision in (actual_decision, estimate_decision, forecast_decision):
            (evidence,) = decision["evidence"]
            self.assertEqual(decision["rule"]["kind"], evidence["kind"])
            self.assertEqual("USD", evidence["unit"])
            self.assertEqual((SOURCE, SAMPLE["scope"]), (evidence["source"], evidence["scope"]))
        self.assertEqual(0, estimate_decision["evidence"][0]["value"])

    def test_latest_unusable_evidence_never_falls_back_to_old_within_limit(self):
        cases = {
            "missing_observation": row(
                "evt-missing", observed_at=None, received_at="2026-01-15T12:30:00Z",
                freshness="unknown", value=None, unit=None,
            ),
            "late_observation": row(
                "evt-late", received_at="2026-01-15T13:01:00Z", freshness="late",
                unit="JPY", value=999,
            ),
            "currency_mismatch": row(
                "evt-currency", received_at="2026-01-15T12:30:00Z", unit="JPY",
            ),
            "stale_observation": row("evt-stale", received_at="2026-01-15T12:45:00Z"),
        }
        for reason, latest in cases.items():
            with self.subTest(reason=reason):
                db = self.directory / f"{reason}.db"
                rule = dict(RULE, max_age_seconds=4199) if reason == "stale_observation" else RULE
                sre_mvp.ingest(db, [row("evt-old"), latest])
                (decision,) = self.decisions((rule,), db=db)
                self.assertEqual(("unknown", reason), (decision["status"], decision["reason"]))
                (evidence,) = decision["evidence"]
                self.assertEqual(latest[1]["event_id"], evidence["event_id"])
                self.assertEqual(latest[1]["value"], evidence["value"])
                self.assertEqual(latest[1]["unit"], evidence["unit"])

    def test_other_kind_and_invisible_signals_cannot_satisfy_policy(self):
        sre_mvp.ingest(self.db, [
            row("evt-health", kind="outage", value=1, unit="state"),
            row("evt-forecast", kind="cost_forecast", value=0,
                received_at="2026-01-15T12:30:00Z"),
            row("evt-actual-not-yet-received", value=999,
                observed_at="2026-01-15T13:00:00Z", received_at="2026-01-15T13:11:00Z"),
        ])
        (empty,) = self.decisions(as_of="2026-01-15T12:10:00Z")
        self.assertEqual(("unknown", "no_signal", []),
                         (empty["status"], empty["reason"], empty["evidence"]))
        (wrong_kind,) = self.decisions()
        self.assertEqual(("unknown", "kind_mismatch"), (wrong_kind["status"], wrong_kind["reason"]))
        self.assertEqual(["evt-forecast"], [item["event_id"] for item in wrong_kind["evidence"]])
        (visible,) = self.decisions(as_of="2026-01-15T13:11:00Z")
        self.assertEqual(("anomaly", "evt-actual-not-yet-received"),
                         (visible["status"], visible["evidence"][0]["event_id"]))

    def test_no_scope_or_identity_cross_matching(self):
        other_source = "synthetic-aws-02"
        gcp_source = "synthetic-gcp-01"
        with patch.dict(sre_mvp.SOURCES, {other_source: "aws"}):
            sre_mvp.ingest(self.db, [
                (other_source, row("evt-other-source")[1]),
                (gcp_source, row("evt-other-provider", provider="gcp")[1]),
                row("evt-other-scope", scope="synthetic-other-scope"),
                row("evt-other-environment", environment="staging"),
                row("evt-other-service", service="other"),
                row("evt-other-target", target="synthetic-other-target"),
            ])
            (decision,) = self.decisions()
        self.assertEqual(("unknown", "no_signal", []),
                         (decision["status"], decision["reason"], decision["evidence"]))

    def test_latest_receipt_order_and_conflicts_fail_closed(self):
        cases = {
            "ambiguous_receipt": [
                row("evt-z", value=101, received_at="2026-01-15T12:30:00Z"),
                row("evt-a", value=50, received_at="2026-01-15T12:30:00Z"),
            ],
            "out_of_order_observation": [
                row("evt-newer", value=101, observed_at="2026-01-15T12:30:00Z",
                    received_at="2026-01-15T12:35:00Z"),
                row("evt-received-last", value=50, received_at="2026-01-15T12:55:00Z"),
            ],
            "ambiguous_observation": [
                row("evt-higher-earlier", value=101),
                row("evt-lower-later", value=50, received_at="2026-01-15T12:30:00Z"),
            ],
        }
        for reason, rows in cases.items():
            with self.subTest(reason=reason):
                db = self.directory / f"{reason}.db"
                sre_mvp.ingest(db, reversed(rows))
                (decision,) = self.decisions(db=db)
                self.assertEqual(("unknown", reason), (decision["status"], decision["reason"]))
                expected_ids = [body["event_id"] for _, body in sorted(
                    rows, key=lambda item: (item[1]["received_at"], item[1]["event_id"]),
                )]
                self.assertEqual(expected_ids, [item["event_id"] for item in decision["evidence"]])

    def test_invalid_policy_is_an_error(self):
        valid = {"version": 1, "rules": [RULE]}
        cases = [
            True, [], dict(valid, version=True), dict(valid, version=1.0),
            dict(valid, rules=[]), dict(valid, rules=[[]]), dict(valid, extra=1),
            dict(valid, rules=[RULE, dict(RULE, unit="JPY")]),
            dict(valid, rules=[{key: value for key, value in RULE.items() if key != "unit"}]),
        ]
        for field, invalid_value in (
            ("source", []), ("provider", "gcp"), ("scope", "not-synthetic"),
            ("kind", ["cost_actual"]), ("kind", "recovery"), ("unit", {}),
            ("unit", "EUR"), ("threshold", True), ("threshold", -1), ("threshold", float("inf")),
            ("max_age_seconds", True), ("max_age_seconds", -1), ("max_age_seconds", 4200.0),
        ):
            cases.append(dict(valid, rules=[dict(RULE, **{field: invalid_value})]))
        for policy in cases:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    sre_mvp.list_cost_anomalies(self.db, policy, AS_OF)
        for as_of in (None, "2026-01-15T13:10:00+00:00", "2026-02-30T13:10:00Z"):
            with self.subTest(as_of=as_of):
                with self.assertRaises(ValueError):
                    sre_mvp.list_cost_anomalies(self.db, valid, as_of)
        self.assertFalse(self.db.exists())

    def test_cost_numbers_do_not_coerce_large_ints_to_float(self):
        amount = 10**400
        sre_mvp.ingest(self.db, [row("evt-large", value=amount)])
        (decision,) = self.decisions((dict(RULE, threshold=amount - 1),))
        self.assertEqual("anomaly", decision["status"])
        self.assertEqual(amount, decision["evidence"][0]["value"])

    def test_policy_json_rejects_duplicate_fields_and_nonfinite_literals(self):
        path = self.directory / "policy.json"
        duplicate = json.dumps({"version": 1, "rules": [RULE]})
        for text in (
            duplicate.replace('{"version":', '{"version": 1, "version":', 1),
            duplicate.replace('"threshold":', '"threshold": 9999, "threshold":', 1),
            duplicate.replace('"threshold": 100', '"threshold": NaN', 1),
            duplicate.replace('"threshold": 100', '"threshold": Infinity', 1),
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    sre_mvp.load_cost_policy(path)

    def test_bundled_demo_repeats_without_cost_writes(self):
        def run(*command):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(0, sre_mvp.main(["--db", str(self.db), *command]))
            return stdout.getvalue()

        policy_path = HERE / "cost_policy.json"
        policy_bytes = policy_path.read_bytes()
        args = ("cost", "--policy", str(policy_path), "--as-of", "2026-01-15T13:05:00Z")
        self.assertEqual({"inserted": 25, "duplicates": 5}, json.loads(run("demo")))
        db_bytes = self.db.read_bytes()
        result = run(*args)
        self.assertEqual(db_bytes, self.db.read_bytes())
        self.assertEqual({"inserted": 0, "duplicates": 30}, json.loads(run("demo")))
        self.assertEqual(result, run(*args))
        self.assertEqual(policy_bytes, policy_path.read_bytes())
        self.assertEqual([self.db], list(self.directory.iterdir()))

        payload = json.loads(result)
        self.assertEqual(1, payload["version"])
        self.assertEqual(args[-1], payload["as_of"])
        self.assertEqual(
            [
                ("aws", "unknown", "missing_observation"),
                ("azure", "anomaly", "above_threshold"),
                ("gcp", "unknown", "late_observation"),
                ("oci", "unknown", "kind_mismatch"),
                ("sakura", "within_limit", "at_or_below_threshold"),
            ],
            [(item["rule"]["provider"], item["status"], item["reason"])
             for item in payload["classifications"]],
        )

    def test_cli_needs_explicit_inputs_and_fails_on_missing_or_invalid_evidence(self):
        policy_path = HERE / "cost_policy.json"
        valid_args = ["--db", str(self.db), "cost", "--policy", str(policy_path), "--as-of", AS_OF]
        for args in (valid_args[:3], valid_args[:-2], valid_args[:3] + valid_args[-2:]):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                sre_mvp.main(args)
            self.assertEqual(2, raised.exception.code)
            self.assertIn("cost requires --policy and --as-of", stderr.getvalue())

        def assert_failure():
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(1, sre_mvp.main(valid_args))
            self.assertEqual("", stdout.getvalue())
            self.assertTrue(stderr.getvalue().startswith("error: "))

        assert_failure()
        self.assertFalse(self.db.exists())
        sre_mvp.ingest(self.db, [row("evt-good")])
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE signals SET body_json = '[]'")
        conn.close()
        assert_failure()


if __name__ == "__main__":
    unittest.main()
