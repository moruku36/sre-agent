"""Local synthetic checks; no credentials, endpoints, or external packages."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import intake

ROOT = Path(__file__).resolve().parent
START, END = "2031-04-02T00:00:00Z", "2031-04-02T00:00:10Z"
MID = "2031-04-02T00:00:05.000000001Z"
PROJECT, MODEL = "projects/orchard-demo-invalid", "orchard-counter-v1"
ARGS = (PROJECT, MODEL, START, END)


def path(name):
    return ROOT / "fixtures" / ("synthetic-" + name + ".json")


def sample(name="positive"):
    return json.loads(path(name).read_bytes())


def series(sample_obj, index=0):
    return sample_obj["response"]["timeSeries"][index]


def point(a, b, value="0"):
    return {"interval": {"startTime": a, "endTime": b},
            "value": {"int64Value": value}}


class IntakeTests(unittest.TestCase):
    def check(self, snapshot, status, reason, args=ARGS):
        raw = snapshot if isinstance(snapshot, bytes) else json.dumps(snapshot).encode()
        result = intake.assess(raw, *args)
        self.assertEqual((result["status"], result["reason"]), (status, reason))
        self.assertEqual(result["availability"], "unknown")
        self.assertEqual(result["attribution_to_relay"], "not_established")
        if status != "observed":
            self.assertEqual(result["rows"], [])
        if status == "rejected":
            self.assertNotIn("scope", result)
            self.assertNotIn("snapshot_sha256", result)
        else:
            self.assertEqual(result["snapshot_sha256"], sha256(raw).hexdigest())
        return result

    def reject(self, snapshot, reason, args=ARGS):
        return self.check(snapshot, "rejected", reason, args)

    def test_positive_reverse_rest_order_exact_nanoseconds(self):
        source = sample()
        result = self.check(source, "observed", "complete_series")
        row, = result["rows"]
        self.assertEqual(result["scope"], source["scope"])
        self.assertEqual(result["provenance"], "synthetic")
        self.assertEqual((row["observation"], row["window_total_int64"]), ("positive_count", "7"))
        for key in ("metric", "resource", "metricKind", "valueType", "unit"):
            self.assertEqual(row[key], series(source)[key])
        self.assertEqual(row["points"], series(source)["points"][::-1])
        self.assertEqual(row["points"][-1]["interval"]["endTime"], END)
        self.assertEqual(row["points"][0]["interval"]["endTime"], MID)

    def test_explicit_zero_retains_opaque_code_empty_label_and_distinct_location(self):
        result = self.check(sample("zero"), "observed", "complete_series")
        row, = result["rows"]
        self.assertEqual((row["observation"], row["window_total_int64"]), ("explicit_zero", "0"))
        self.assertEqual(row["metric"]["labels"]["response_code"], "000")
        self.assertEqual(row["metric"]["labels"]["error_category"], "")
        self.assertEqual(row["metric"]["labels"]["source"], "global")
        self.assertEqual(row["resource"]["labels"]["location"], "orchard-land-12")

    def test_no_series(self):
        self.check(sample("no-series"), "unknown", "no_series")

    def test_explicit_denied(self):
        self.check(sample("denied"), "unknown", "denied")

    def test_unavailable_reasons_are_finite_and_not_no_series(self):
        item = sample("unavailable")
        for reason in ("console_display_only", "scope_unverified", "capture_unverified",
                       "incomplete_capture", "source_unavailable"):
            with self.subTest(reason=reason):
                item["reason"] = reason
                self.check(item, "unknown", reason)
        item["reason"] = "surprise"
        self.reject(item, "unavailable_reason")
        item["reason"] = {}
        self.reject(item, "unavailable_reason")

    def test_partial_rows_cannot_piggyback_on_denied_or_unavailable(self):
        for name in ("denied", "unavailable"):
            for key, value in (("response", sample()["response"]), ("capture", sample()["capture"])):
                with self.subTest(name=name, key=key):
                    obj = sample(name)
                    obj[key] = value
                    self.reject(obj, name + "_shape")

    def test_empty_ok_envelope_requires_explicit_verified_response(self):
        item = sample("no-series")
        del item["response"]
        self.reject(item, "ok_shape")
        item["response"] = {}
        self.reject(item, "response_shape")
        item["response"] = {"timeSeries": None}
        self.reject(item, "series_list")
        item["response"] = {"timeSeries": [None]}
        self.reject(item, "series_shape")

    def test_all_partial_transport_flags_reject_even_with_no_series(self):
        for case in ("positive", "no-series"):
            for flag in ("page_token_present", "next_page_token_present",
                         "execution_errors_present", "unreachable_present"):
                with self.subTest(case=case, flag=flag):
                    item = sample(case)
                    item["capture"][flag] = True
                    self.reject(item, "incomplete_capture")

    def test_only_full_raw_and_explicit_booleans(self):
        for key, value, reason in (("view", "HEADERS", "not_raw_full"),
                                   ("aggregation", "ALIGN_DELTA", "not_raw_full"),
                                   ("aggregation", "REDUCE_SUM", "not_raw_full"),
                                   ("unreachable_present", "false", "capture_shape")):
            with self.subTest(key=key, value=value):
                item = sample()
                item["capture"][key] = value
                self.reject(item, reason)
        item = sample()
        del item["capture"]["execution_errors_present"]
        self.reject(item, "capture_shape")

    def test_capture_fields_not_copied_as_raw_page_metadata(self):
        for key, value in (("nextPageToken", "opaque"), ("executionErrors", [{}]),
                           ("unreachable", ["fictional-region"])):
            with self.subTest(key=key):
                item = sample()
                item["response"][key] = value
                self.reject(item, "response_shape")

    def test_independent_root_window_model_project(self):
        for args in (("projects/other", MODEL, START, END),
                     (PROJECT, "other-model", START, END),
                     (PROJECT, MODEL, START, "2031-04-02T00:00:11Z")):
            with self.subTest(args=args):
                self.reject(sample(), "wrong_scope", args)
                self.reject(sample("denied"), "wrong_scope", args)

    def test_other_kind_type_metric_resource_fails_whole_bundle(self):
        for key, value, reason in (("metricKind", "GAUGE", "metric_kind_or_value_type"),
                                   ("valueType", "DISTRIBUTION", "metric_kind_or_value_type"),
                                   ("metric", "proxy/fictional_count", "wrong_metric_or_resource"),
                                   ("resource", "global", "wrong_metric_or_resource")):
            with self.subTest(key=key):
                item = sample()
                other = deepcopy(series(item))
                if key in ("metric", "resource"):
                    other[key]["type"] = value
                else:
                    other[key] = value
                item["response"]["timeSeries"].append(other)
                self.reject(item, reason)

    def test_foreign_label_fails_whole_bundle_without_rewriting(self):
        for group, key, value in (("resource", "resource_container", "another-project"),
                                  ("resource", "model_user_id", MODEL.upper()),
                                  ("resource", "publisher", "Google"),
                                  ("metric", "source", "eu"),
                                  ("metric", "source", "batch_global"),
                                  ("metric", "source", "global ")):
            with self.subTest(key=key, value=value):
                item = sample()
                other = deepcopy(series(item))
                other[group]["labels"][key] = value
                item["response"]["timeSeries"].append(other)
                self.reject(item, "wrong_series_scope")

    def test_resource_container_identifier_no_alias_or_coercion(self):
        item = sample()
        item["scope"]["resource_container"] = "orchard-demo-invalid"
        self.reject(item, "wrong_scope")
        item["scope"]["resource_container"] = PROJECT
        series(item)["resource"]["labels"]["resource_container"] = "orchard-demo-invalid"
        self.reject(item, "wrong_series_scope")

    def test_distinct_full_label_identities_stay_in_separate_rows(self):
        item = sample()
        sample_series = series(item)
        extra1 = deepcopy(sample_series)
        extra1["metric"]["labels"]["request_type"] = ""  # distinct from absent
        extra2 = deepcopy(sample_series)
        extra2["metric"]["labels"]["response_code"] = "orchard:R-28"
        extra3 = deepcopy(sample_series)
        extra3["resource"]["labels"]["model_version_id"] = "fictional-version-b"
        item["response"]["timeSeries"].extend([extra1, extra2, extra3])
        result = self.check(item, "observed", "complete_series")
        self.assertEqual([r["window_total_int64"] for r in result["rows"]], ["7"] * 4)
        self.assertNotIn("window_total_int64", result)
        for n, row in enumerate(result["rows"]):
            self.assertEqual(row["metric"], series(item, n)["metric"])
            self.assertEqual(row["resource"], series(item, n)["resource"])

    def test_required_and_unknown_identity_labels(self):
        for group, key, action in (("resource", "location", "delete"),
                                   ("resource", "model_version_id", "delete"),
                                   ("metric", "response_code", "delete"),
                                   ("metric", "method", "delete"),
                                   ("metric", "future_dimension", "add")):
            with self.subTest(group=group, key=key):
                item = sample()
                labels = series(item)[group]["labels"]
                if action == "delete":
                    del labels[key]
                else:
                    labels[key] = "unknown"
                self.reject(item, group + "_labels")

    def test_duplicate_full_identity_refuses_reordered_labels_and_different_value(self):
        item = sample()
        duplicated = deepcopy(series(item))
        duplicated["metric"]["labels"] = dict(reversed(list(duplicated["metric"]["labels"].items())))
        duplicated["points"] = [point(START, END, "9")]
        item["response"]["timeSeries"].append(duplicated)
        self.reject(item, "duplicate_series")

    def test_disjoint_partial_entries_are_not_stitched(self):
        item = sample()
        series(item)["points"] = [point(START, MID, "1")]
        other = deepcopy(series(item))
        other["points"] = [point(MID, END, "2")]
        item["response"]["timeSeries"].append(other)
        self.reject(item, "incomplete_window")

    def test_duplicate_point_at_equal_nanos_even_with_different_spelling_and_count(self):
        item = sample()
        series(item)["points"].append(point(START.replace("Z", ".000Z"), MID, "8"))
        self.reject(item, "duplicate_point")

    def test_partial_start_middle_end_no_points(self):
        for name, points in (("missing initial", [point(MID, END)]),
                             ("missing tail", [point(START, MID)]),
                             ("internal 1ns gap", series(sample("rejected-gap"))["points"]),
                             ("empty points", [])):
            with self.subTest(name=name):
                item = sample()
                extra = deepcopy(series(item))
                extra["metric"]["labels"]["response_code"] = "second"
                extra["points"] = points
                item["response"]["timeSeries"].append(extra)
                self.reject(item, "incomplete_window")

    def test_overlap_even_one_nanosecond(self):
        item = sample()
        series(item)["points"][0]["interval"]["startTime"] = "2031-04-02T00:00:05Z"
        self.reject(item, "overlapping_points")

    def test_exclusive_lower_inclusive_upper_and_never_clip(self):
        cases = [("end at exclusive start", "2031-04-01T23:59:59Z", START),
                 ("cross lower", "2031-04-01T23:59:59Z", MID),
                 ("cross upper", MID, "2031-04-02T00:00:10.000000001Z"),
                 ("start at end", END, "2031-04-02T00:00:11Z")]
        for name, a, b in cases:
            with self.subTest(name=name):
                item = sample()
                series(item)["points"].append(point(a, b))
                self.reject(item, "point_outside_window")

    def test_normalized_utc_profile_validates_calendar_and_nonzero_interval(self):
        for a, b in ((START, START), (END, START), ("2031-04-31T00:00:00Z", END),
                     ("2031-04-02T00:00:00+00:00", END), ("2031-04-02T00:00:00.0Z", END),
                     ("2031-04-02T00:00:00.0000000001Z", END), ("2031-04-01T23:59:60Z", END)):
            with self.subTest(a=a, b=b):
                item = sample()
                series(item)["points"].append(point(a, b))
                self.reject(item, "point_interval")
        self.reject(sample(), "expected_window", (PROJECT, MODEL, END, START))

    def test_real_instant_not_fractional_spelling_pins_window(self):
        item = sample()
        item["scope"]["interval"]["endTime"] = END.replace("Z", ".000000000Z")
        result = self.check(item, "observed", "complete_series")
        self.assertEqual(result["scope"]["interval"]["endTime"], END)

    def test_int64_strings_fixed_full_precision(self):
        for count in ("9007199254740993", "9223372036854775807"):
            with self.subTest(count=count):
                item = sample()
                series(item)["points"] = [point(START, END, count)]
                result = self.check(item, "observed", "complete_series")
                self.assertEqual(result["rows"][0]["window_total_int64"], count)

    def test_int64_rejects_noncanonical_negative_and_double(self):
        for wrong in ("", "-1", "-0", "02", "+1", "1e3", "1.0", "1 ", "١", None, True):
            with self.subTest(value=wrong):
                item = sample("zero")
                series(item)["points"][0]["value"]["int64Value"] = wrong
                self.reject(item, "int64_decimal")
        for wrong in (0, 1.0):
            item = sample("zero")
            series(item)["points"][0]["value"]["int64Value"] = wrong
            self.reject(item, "json_number")
        item = sample("zero")
        series(item)["points"][0]["value"]["stringValue"] = "0"
        self.reject(item, "int64_shape")

    def test_int64_point_and_total_overflow_are_separate(self):
        item = sample()
        series(item)["points"] = [point(START, END, "9223372036854775808")]
        self.reject(item, "int64_range")
        series(item)["points"] = [point(START, MID, "9223372036854775807"), point(MID, END, "1")]
        self.reject(item, "window_total_out_of_int64")

    def test_number_unit_required_consistent_not_RATE(self):
        item = sample()
        del series(item)["unit"]
        self.reject(item, "unit_not_count")
        item["response"]["unit"] = "1"
        result = self.check(item, "observed", "complete_series")
        self.assertEqual(result["rows"][0]["unit"], "1")
        series(item)["unit"] = "1/s"
        self.reject(item, "unit_not_count")
        series(item)["unit"] = "1"
        item["response"]["unit"] = "s-1"
        self.reject(item, "unit_not_count")

    def test_duplicate_json_keys_and_malformed_encoding(self):
        good = path("positive").read_bytes()
        self.reject(good.replace(b'"int64Value": "4"', b'"int64Value": "5", "int64Value": "4"'),
                    "duplicate_json_key")
        self.reject(b'{"schema":1' + b"0" * 10000 + b"}", "json_number")
        self.reject(b'{"other":NaN}', "json_number")
        for raw in (b"\xff", b"["):
            with self.subTest(size=len(raw)):
                self.reject(raw, "input_json")
        # Some runtimes parse 2000 valid nested arrays; others hit their recursion limit.
        deep = intake.assess(b"[" * 2000 + b"]" * 2000, *ARGS)
        self.assertEqual((deep["status"], deep["rows"]), ("rejected", []))
        self.assertIn(deep["reason"], ("input_json", "snapshot_shape"))

    def test_limits_reject_never_truncate(self):
        self.reject(b" " * (intake.MAX_BYTES + 1), "input_too_large")
        with patch.object(intake, "MAX_SERIES", 0):
            self.reject(sample(), "series_limit")
        with patch.object(intake, "MAX_POINTS", 1):
            self.reject(sample(), "point_limit")

    def test_cannot_supply_health_markers_or_logs_as_input(self):
        for key, value in (("availability", "up"), ("attribution_to_relay", "proven"),
                           ("logs", {"errors": []})):
            item = sample()
            item[key] = value
            self.reject(item, "snapshot_shape")

    def test_cli_exit_and_input_read_only(self):
        for name, status, code in (("positive", "observed", 0), ("zero", "observed", 0),
                                   ("no-series", "unknown", 0), ("denied", "unknown", 0),
                                   ("unavailable", "unknown", 0), ("rejected-gap", "rejected", 2)):
            with self.subTest(name=name):
                infile = path(name)
                before = infile.read_bytes()
                proc = self.cli(infile)
                self.assertEqual(proc.returncode, code)
                self.assertEqual(proc.stderr, b"")
                result = json.loads(proc.stdout)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["availability"], "unknown")
                self.assertEqual(infile.read_bytes(), before)

    def test_cli_does_not_open_to_read_a_directory_or_require_a_real_file_to_classify_io(self):
        for infile, reason in ((ROOT / "fixtures", "input_not_regular"),
                               (path("not-present"), "input_io")):
            with self.subTest(reason=reason):
                proc = self.cli(infile)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stderr, b"")
                self.assertEqual(json.loads(proc.stdout)["reason"], reason)

    @staticmethod
    def cli(infile):
        return subprocess.run([sys.executable, "-IB", str(ROOT / "intake.py"),
                               "--snapshot", str(infile), "--resource-container", PROJECT,
                               "--model", MODEL, "--start", START, "--end", END],
                              capture_output=True, check=False, timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
