"""CLI tests using the synthetic handoff CLI, not a second correlator."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import sre_mvp


HERE = Path(__file__).resolve().parent
EARLY = "2026-01-15T12:04:59Z"
OPEN = "2026-01-15T12:21:00Z"
RESOLVED = "2026-01-15T12:22:00Z"
CITATION_FIELDS = ("source", "scope", "event_id", "evidence")


def citation(record):
    return {field: record[field] for field in CITATION_FIELDS}


def json_bytes(obj):
    return json.dumps(obj).encode("utf-8")


def cli(script, *args):
    return subprocess.run(
        [sys.executable, "-B", str(HERE / script), *args],
        capture_output=True, timeout=15,
    )


class InvestigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        db = Path(temp.name) / "signals.sqlite3"
        demo = cli("sre_mvp.py", "--db", str(db), "demo")
        if demo.returncode:
            raise RuntimeError(demo.stderr.decode("utf-8"))

        def pull(cutoff):
            result = cli(
                "sre_mvp.py", "--db", str(db), "handoff", "--policy",
                str(HERE / "cost_policy.json"), "--as-of", cutoff,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8"))
            return result.stdout

        cls.early = pull(EARLY)
        cls.open = pull(OPEN)
        cls.resolved = pull(RESOLVED)

        # This event is real demo-schema evidence, but correlation leaves it
        # outside the incident because its observation time is missing.
        source, recovery = next(
            (source, body) for source, body in sre_mvp.synthetic_signals()
            if source == "synthetic-aws-01" and body["kind"] == "recovery"
        )
        missing = dict(
            recovery, event_id="synthetic-aws-unobserved", observed_at=None,
            received_at=OPEN, freshness="unknown", value=None, unit=None,
        )
        late = dict(
            recovery, event_id="synthetic-aws-late-outage", kind="outage",
            received_at="2026-01-15T13:21:00Z", freshness="late", value=1,
            evidence="synthetic-aws-evidence-late-outage",
        )
        sre_mvp.ingest(db, [(source, missing), (source, late)])
        cls.with_unapplied = pull(OPEN)
        cls.with_late = pull(late["received_at"])
        cls.mock_file = (HERE / "investigation_mock.json").read_bytes()
        cls.incident_id = json.loads(cls.mock_file)["incident_id"]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.handoff_file = self.directory / "handoff.json"
        self.response_file = self.directory / "mock.json"

    def response(self, handoff):
        response = json.loads(self.mock_file)
        response["handoff_sha256"] = hashlib.sha256(handoff).hexdigest()
        return response

    def check(self, handoff, response, incident_id=None):
        if not isinstance(response, bytes):
            response = json_bytes(response)
        self.handoff_file.write_bytes(handoff)
        self.response_file.write_bytes(response)
        result = cli(
            "sre_investigation.py", "--handoff", str(self.handoff_file),
            "--response", str(self.response_file), "--incident-id",
            self.incident_id if incident_id is None else incident_id,
        )
        self.assertEqual(handoff, self.handoff_file.read_bytes())
        self.assertEqual(response, self.response_file.read_bytes())
        self.assertEqual({self.handoff_file, self.response_file}, set(self.directory.iterdir()))
        return result

    def reject(self, handoff, response, incident_id=None):
        result = self.check(handoff, response, incident_id)
        self.assertEqual((1, b""), (result.returncode, result.stdout))
        self.assertTrue(result.stderr.startswith(b"error: "), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def test_bundled_mock_reads_exact_open_snapshot(self):
        result = self.check(self.open, self.mock_file)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        self.assertEqual(result.stdout, self.check(self.open, self.mock_file).stdout)
        report = json.loads(result.stdout)
        self.assertEqual({
            "kind", "version", "status", "handoff_sha256", "as_of", "incident",
            "observed_evidence", "hypotheses", "unknowns", "next_checks",
        }, set(report))
        self.assertEqual(("synthetic_sre_investigation_report", 1, "references_validated"),
                         (report["kind"], report["version"], report["status"]))
        incident = json.loads(self.open)["incidents"][0]
        self.assertEqual(incident.pop("evidence"), report["observed_evidence"])
        self.assertEqual(incident, report["incident"])
        self.assertEqual(OPEN, report["as_of"])
        self.assertEqual(hashlib.sha256(self.open).hexdigest(), report["handoff_sha256"])
        response = json.loads(self.mock_file)
        for field in ("hypotheses", "unknowns", "next_checks"):
            self.assertEqual(response[field], report[field])

    def test_inclusive_receipt_cutoff_and_counterevidence(self):
        incident = json.loads(self.resolved)["incidents"][0]
        recovery = incident["evidence"][1]
        self.assertEqual(RESOLVED, recovery["received_at"])
        self.assertLess(recovery["observed_at"], OPEN)
        response = self.response(self.resolved)
        response["hypotheses"][0]["contradicts"] = [citation(recovery)]
        result = self.check(self.resolved, response)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        report = json.loads(result.stdout)
        self.assertEqual(("resolved", RESOLVED), (report["incident"]["status"], report["as_of"]))
        self.assertEqual(incident["evidence"], report["observed_evidence"])
        response["handoff_sha256"] = self.response(self.open)["handoff_sha256"]
        self.reject(self.open, response)

    def test_late_attached_outage_retains_original_freshness(self):
        evidence = json.loads(self.with_late)["incidents"][0]["evidence"]
        late = next(record for record in evidence if record["event_id"] == "synthetic-aws-late-outage")
        response = self.response(self.with_late)
        response["observed"].append(citation(late))
        result = self.check(self.with_late, response)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        report = json.loads(result.stdout)
        self.assertTrue(any(record == late and record["freshness"] == "late"
                            for record in report["observed_evidence"]))

    def test_rejects_different_bytes_and_later_snapshot_without_rebinding(self):
        for changed in (self.open + b"\n", self.open.rstrip(b"\n"), self.resolved):
            with self.subTest(digest=hashlib.sha256(changed).hexdigest()):
                self.reject(changed, self.mock_file)

    def test_exact_id_required_and_empty_snapshot_is_not_success(self):
        snapshot = json.loads(self.open)
        azure = snapshot["incidents"][1]
        other = self.response(self.open)
        other.update(incident_id=azure["incident_id"], observed=[citation(azure["evidence"][0])],
                     hypotheses=[])
        self.reject(self.open, other)  # Valid Azure response, caller requested AWS.
        for missing in (self.incident_id[:-1], self.incident_id.upper()):
            response = self.response(self.open)
            response["incident_id"] = missing
            self.reject(self.open, response, missing)
        self.reject(self.early, self.response(self.early))

    def test_selects_a_later_incident_by_exact_id(self):
        azure = json.loads(self.open)["incidents"][1]
        response = self.response(self.open)
        response.update(incident_id=azure["incident_id"], hypotheses=[],
                        observed=[citation(azure["evidence"][0])])
        result = self.check(self.open, response, azure["incident_id"])
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        report = json.loads(result.stdout)
        self.assertEqual(azure.pop("evidence"), report["observed_evidence"])
        self.assertEqual(azure, report["incident"])

    def test_reports_only_cited_records_in_handoff_order(self):
        outage, recovery = json.loads(self.resolved)["incidents"][0]["evidence"]
        response = self.response(self.resolved)
        response.update(hypotheses=[], observed=[citation(recovery)])
        result = self.check(self.resolved, response)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        self.assertEqual([recovery], json.loads(result.stdout)["observed_evidence"])
        response["observed"].append(citation(outage))
        result = self.check(self.resolved, response)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        self.assertEqual([outage, recovery], json.loads(result.stdout)["observed_evidence"])

    def test_rejects_citations_outside_the_selected_incident(self):
        snapshot = json.loads(self.with_unapplied)
        (unapplied,) = snapshot["unapplied_health_evidence"]
        cost = next(row["evidence"][0] for row in snapshot["cost_classifications"] if row["evidence"])
        aws = citation(snapshot["incidents"][0]["evidence"][0])
        azure = citation(snapshot["incidents"][1]["evidence"][0])
        for unsupported in (azure, citation(unapplied), citation(cost), aws["evidence"],
                            dict(aws, evidence=azure["evidence"]), dict(aws, event_id=azure["event_id"]),
                            dict(aws, scope=azure["scope"]), dict(aws, source=azure["source"])):
            with self.subTest(unsupported=unsupported):
                response = self.response(self.with_unapplied)
                response["observed"] = [unsupported]
                self.reject(self.with_unapplied, response)
        for field in ("supports", "contradicts"):
            response = self.response(self.with_unapplied)
            response["hypotheses"][0][field] = [azure]
            self.reject(self.with_unapplied, response)

    def test_rejects_duplicate_incident_or_evidence_keys(self):
        snapshot = json.loads(self.open)
        original = snapshot["incidents"][0]
        duplicates = []
        for second in (original, dict(original, service="conflicting-service")):
            modified = copy.deepcopy(snapshot)
            modified["incidents"].append(second)
            duplicates.append(modified)
        for evidence in (original["evidence"][0], dict(original["evidence"][0], evidence="different-ref")):
            modified = copy.deepcopy(snapshot)
            modified["incidents"][0]["evidence"].append(evidence)
            duplicates.append(modified)
        modified = copy.deepcopy(snapshot)
        sibling = copy.deepcopy(original)
        sibling["incident_id"] += "-other"
        modified["incidents"].append(sibling)
        duplicates.append(modified)
        # Duplicate IDs and attached identities wholly outside the selected incident.
        modified = copy.deepcopy(snapshot)
        modified["incidents"].append(copy.deepcopy(modified["incidents"][1]))
        duplicates.append(modified)
        modified = copy.deepcopy(modified)
        modified["incidents"][-1]["incident_id"] += "-other"
        modified["incidents"][-1]["evidence"][0]["evidence"] = "different-ref"
        duplicates.append(modified)
        for duplicate in duplicates:
            raw = json_bytes(duplicate)
            with self.subTest(digest=hashlib.sha256(raw).hexdigest()):
                self.reject(raw, self.response(raw))
        response = self.response(self.open)
        response["observed"] *= 2
        self.reject(self.open, response)

    def test_rejects_spliced_or_future_evidence_even_when_hash_matches(self):
        cutoff = json.loads(self.open)
        later = json.loads(self.resolved)["incidents"][0]["evidence"][-1]
        foreign = cutoff["incidents"].pop(1)["evidence"][0]
        for bad in (later, foreign, dict(later, received_at="not-a-timestamp")):
            edited = copy.deepcopy(cutoff)
            edited["incidents"][0]["evidence"].append(bad)
            handoff = json_bytes(edited)
            response = self.response(handoff)
            response["observed"].append(citation(bad))
            self.reject(handoff, response)

    def test_rejects_malformed_snapshot_and_other_incidents(self):
        snapshot = json.loads(self.open)
        for fields in (
            {"kind": "unknown"}, {"version": 1.0}, {"as_of": "2026-01-15T12:21:00+00:00"},
            {"incidents": {}}, {"cost_classifications": {}}, {"unapplied_health_evidence": None},
            {"extra": []},
        ):
            with self.subTest(envelope=fields):
                handoff = json_bytes(dict(snapshot, **fields))
                self.reject(handoff, self.response(handoff))

        for fields in (
            {"status": []}, {"opened_at": RESOLVED}, {"resolved_at": OPEN},
            {"status": "resolved"}, {"source": " "}, {"evidence": []}, {"extra": "unknown"},
        ):
            with self.subTest(incident=fields):
                edited = copy.deepcopy(snapshot)
                edited["incidents"][1].update(fields)
                handoff = json_bytes(edited)
                self.reject(handoff, self.response(handoff))

        for fields in (
            {"observed_at": OPEN, "received_at": EARLY}, {"received_at": RESOLVED},
            {"kind": ["outage"]}, {"freshness": "unknown"}, {"evidence": ""}, {"extra": ""},
        ):
            with self.subTest(attached=fields):
                edited = copy.deepcopy(snapshot)
                edited["incidents"][1]["evidence"][0].update(fields)
                handoff = json_bytes(edited)
                self.reject(handoff, self.response(handoff))

    def test_incomplete_or_invalid_shape_cannot_emit_report(self):
        valid = self.response(self.open)
        cases = [
            dict(valid, status="incomplete"),
            dict(valid, status="approved"),
            dict(valid, approval=True),
            dict(valid, version=True),
            dict(valid, version=1.0),
            dict(valid, observed=[]),
            dict(valid, next_checks=[" "]),
            dict(valid, hypotheses=[dict(valid["hypotheses"][0], action_id="restart")]),
            dict(valid, hypotheses=[dict(valid["hypotheses"][0], supports=[])]),
            {key: value for key, value in valid.items() if key != "next_checks"},
        ]
        for response in cases:
            with self.subTest(response=response):
                self.reject(self.open, response)

    def test_rejects_duplicate_json_fields_and_invalid_json(self):
        for response in (
            json_bytes(self.response(self.open)).replace(b'"version": 1', b'"version": 1, "version": 1'),
            self.mock_file.replace(b'"contradicts": []', b'"contradicts": [], "contradicts": []'),
            b'{"unknowns":[NaN]}',
            b'{"unknowns":[Infinity]}',
            b'{"unknowns":[-Infinity]}',
            self.mock_file[:-4],
            b"\xff",
        ):
            with self.subTest(response=response):
                self.reject(self.open, response)
        for handoff in (
            self.open.replace(b'"version":1', b'"version":true'),
            self.open.replace(b'"as_of":', b'"as_of":"2026-01-15T12:21:00Z","as_of":', 1),
            self.open.replace(b'"cost_classifications":[', b'"cost_classifications":[NaN,', 1),
        ):
            with self.subTest(digest=hashlib.sha256(handoff).hexdigest()):
                self.assertNotEqual(self.open, handoff)
                self.reject(handoff, self.response(handoff))


if __name__ == "__main__":
    unittest.main()
