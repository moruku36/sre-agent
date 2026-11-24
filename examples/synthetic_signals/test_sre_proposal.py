"""Offline tests of the recovered preview against actual synthetic CLI handoffs."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sre_proposal import preview_proposal, preview_proposal_with_reason
from test_sre_investigation import OPEN, RESOLVED, citation, cli, json_bytes

HERE = Path(__file__).resolve().parent
TARGET_FIELDS = ("provider", "source", "scope", "environment", "service", "target")


class ProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        db = str(Path(scratch.name) / "signals.sqlite3")
        demo = cli("sre_mvp.py", "--db", db, "demo")
        if demo.returncode:
            raise RuntimeError(demo.stderr.decode())
        cls.handoffs = {}
        for cutoff in (OPEN, RESOLVED):
            handoff = cli("sre_mvp.py", "--db", db, "handoff",
                          "--policy", str(HERE / "cost_policy.json"), "--as-of", cutoff)
            if handoff.returncode:
                raise RuntimeError(handoff.stderr.decode())
            cls.handoffs[cutoff] = handoff.stdout

    def case(self, cutoff=OPEN, provider="azure"):
        raw = self.handoffs[cutoff]
        snapshot = json.loads(raw)
        incident = next(item for item in snapshot["incidents"] if item["provider"] == provider)
        exact_digest = hashlib.sha256(raw).hexdigest()
        response = {
            "kind": "synthetic_sre_investigation_mock", "version": 1,
            "incident_id": incident["incident_id"], "handoff_sha256": exact_digest,
            "status": "complete", "observed": [citation(item) for item in incident["evidence"]],
            "hypotheses": [], "unknowns": [], "next_checks": [],
        }
        proposal = {
            "kind": "synthetic_sre_proposal", "version": 1,
            "incident_id": incident["incident_id"], "handoff_sha256": exact_digest,
            "as_of": cutoff, "target": {field: incident[field] for field in TARGET_FIELDS},
            "action_id": "synthetic-review-evidence",
        }
        caller = {
            "incident_id": incident["incident_id"],
            "current_handoff_sha256": exact_digest, "current_as_of": cutoff,
        }
        return raw, response, proposal, caller

    def check(self, case):
        handoff, response, proposal, caller = case
        return preview_proposal(handoff, json_bytes(response), json_bytes(proposal), **caller)

    def test_exact_open_azure_fixture_returns_only_advisory_label(self):
        self.assertEqual("owner_review", self.check(self.case()))

    def test_exact_resolved_azure_at_inclusive_cutoff_has_no_action(self):
        self.assertEqual("no_action", self.check(self.case(RESOLVED)))

    def test_recovery_does_not_honor_an_open_snapshot_proposal(self):
        _, _, previous_proposal, _ = self.case()
        handoff, response, _, caller = self.case(RESOLVED)
        self.assertEqual("not_eligible", self.check((handoff, response, previous_proposal, caller)))

    def test_current_digest_and_cutoff_each_bind(self):
        for field, stale in (("current_handoff_sha256", "0" * 64), ("current_as_of", RESOLVED)):
            with self.subTest(field=field):
                case = self.case()
                case[3][field] = stale
                self.assertEqual("not_eligible", self.check(case))

    def test_proposal_digest_and_cutoff_each_bind(self):
        for field, stale in (("handoff_sha256", "0" * 64), ("as_of", RESOLVED)):
            with self.subTest(field=field):
                case = self.case()
                case[2][field] = stale
                self.assertEqual("not_eligible", self.check(case))

    def test_proposal_incident_and_action_each_bind(self):
        aws_id = self.case(provider="aws")[2]["incident_id"]
        for field, different in (("incident_id", aws_id), ("action_id", "synthetic-other")):
            with self.subTest(field=field):
                case = self.case()
                case[2][field] = different
                self.assertEqual("not_eligible", self.check(case))

    def test_every_target_coordinate_binds(self):
        for field in TARGET_FIELDS:
            with self.subTest(field=field):
                case = self.case()
                case[2]["target"][field] += "-elsewhere"
                self.assertEqual("not_eligible", self.check(case))

    def test_self_consistent_aws_is_still_outside_allowlisted_fixture(self):
        self.assertEqual("not_eligible", self.check(self.case(provider="aws")))

    def test_semantically_identical_handoff_with_changed_bytes_is_invalid(self):
        handoff, response, proposal, caller = self.case()
        with self.assertRaises(ValueError):
            self.check((handoff + b"\n", response, proposal, caller))

    def test_citation_from_a_sibling_incident_is_invalid(self):
        case = self.case()
        case[1]["observed"] = self.case(provider="aws")[1]["observed"]
        with self.assertRaises(ValueError):
            self.check(case)

    def test_duplicate_keys_are_invalid_even_if_values_agree(self):
        handoff, response, proposal, caller = self.case()
        duplicate = json_bytes(proposal).replace(b'"version": 1', b'"version": 1, "version": 1')
        with self.assertRaises(ValueError):
            preview_proposal(handoff, json_bytes(response), duplicate, **caller)

    def run_cli(self, case, *, missing_proposal=False, abbreviated=False, include_reason=False):
        handoff, response, proposal, caller = case
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            files = {
                "handoff": handoff,
                "response": json_bytes(response),
                "proposal": json_bytes(proposal),
            }
            arguments = []
            for name, contents in files.items():
                path = directory / f"{name}.json"
                if name != "proposal" or not missing_proposal:
                    path.write_bytes(contents)
                arguments.extend((f"--{name}", str(path)))
            for name, value in caller.items():
                option = f"--{name.replace('_', '-')}"
                if abbreviated and name == "current_as_of":
                    option = "--current-as"
                arguments.extend((option, value))
            if include_reason:
                arguments.append("--include-reason")
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            result = cli("sre_proposal.py", *arguments)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
            return result

    def test_cli_emits_only_fixed_schema_for_all_three_labels(self):
        for case, label in ((self.case(), "owner_review"),
                            (self.case(RESOLVED), "no_action"),
                            (self.case(provider="aws"), "not_eligible")):
            with self.subTest(label=label):
                result = self.run_cli(case)
                self.assertEqual((0, b""), (result.returncode, result.stderr))
                self.assertEqual(1, result.stdout.count(b"\n"))
                expected = {"kind": "synthetic_sre_proposal_preview", "version": 1, "preview": label}
                self.assertEqual((json.dumps(expected, sort_keys=True) + "\n").encode(), result.stdout)
                self.assertEqual(result.stdout, self.run_cli(case).stdout)

    def check_reason(self, case, label, reason):
        handoff, response, proposal, caller = case
        self.assertEqual((label, reason), preview_proposal_with_reason(
            handoff, json_bytes(response), json_bytes(proposal), **caller))
        self.assertEqual(label, self.check(case))
        result = self.run_cli(case, include_reason=True)
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        expected = {
            "kind": "synthetic_sre_proposal_preview", "version": 1,
            "preview": label, "reason": reason,
        }
        self.assertEqual((json.dumps(expected, sort_keys=True) + "\n").encode(), result.stdout)

    def test_opt_in_fixed_reasons_for_aligned_open_resolved_and_outside_scope(self):
        unknown_action = self.case()
        unknown_action[2]["action_id"] = "synthetic-other"
        for case, label, reason in (
            (self.case(), "owner_review", "open_incident"),
            (self.case(RESOLVED), "no_action", "resolved_incident"),
            (self.case(provider="aws"), "not_eligible", "outside_synthetic_scope"),
            (unknown_action, "not_eligible", "outside_synthetic_scope"),
        ):
            with self.subTest(label=label, reason=reason, action=case[2]["action_id"]):
                self.check_reason(case, label, reason)

    def test_opt_in_distinguishes_stale_current_context_and_proposal_mismatch(self):
        wrong_target = self.case()
        wrong_target[2]["target"]["service"] = "synthetic-other"
        wrong_target[2]["action_id"] = "synthetic-other"
        self.check_reason(wrong_target, "not_eligible", "proposal_context_mismatch")
        stale_digest = self.case()
        stale_digest[3]["current_handoff_sha256"] = "0" * 64
        self.check_reason(stale_digest, "not_eligible", "stale_current_context")
        wrong_target[3]["current_as_of"] = RESOLVED
        self.check_reason(wrong_target, "not_eligible", "stale_current_context")

    def test_opt_in_does_not_emit_decision_for_invalid_input(self):
        case = self.case()
        case[2]["approved"] = True
        case[3]["current_as_of"] = RESOLVED
        default = self.run_cli(case)
        opted_in = self.run_cli(case, include_reason=True)
        self.assertEqual((1, b"", b"error: cannot preview synthetic proposal\n"),
                         (opted_in.returncode, opted_in.stdout, opted_in.stderr))
        self.assertEqual((default.returncode, default.stdout, default.stderr),
                         (opted_in.returncode, opted_in.stdout, opted_in.stderr))

    def test_cli_bad_inputs_and_missing_files_do_not_leak_input_or_emit_stdout(self):
        case = self.case()
        case[2]["approved"] = True
        for bad in (self.run_cli(case), self.run_cli(self.case(), missing_proposal=True)):
            self.assertEqual(1, bad.returncode)
            self.assertEqual(b"", bad.stdout)
            self.assertEqual(b"error: cannot preview synthetic proposal\n", bad.stderr)

    def test_cli_refuses_abbreviated_current_cutoff_option(self):
        bad = self.run_cli(self.case(), abbreviated=True)
        self.assertEqual((2, b""), (bad.returncode, bad.stdout))
        self.assertIn(b"usage: ", bad.stderr)

    def test_bool_version_and_approval_fields_are_invalid(self):
        for field, bad in (("version", True), ("approved", True)):
            with self.subTest(field=field):
                case = self.case()
                case[2][field] = bad
                with self.assertRaises(ValueError):
                    self.check(case)
