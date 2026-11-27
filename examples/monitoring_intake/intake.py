#!/usr/bin/env python3
"""Read one local monitoring intake envelope; never query, write, or pool series."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
import re
import stat

INPUT_SCHEMA = "sre-monitoring-intake/v1"
OUTPUT_SCHEMA = "sre-monitoring-assessment/v1"
METRIC = "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count"
RESOURCE = "aiplatform.googleapis.com/PublisherModel"
PUBLISHER = "google"
SOURCE = "global"
MAX_BYTES = 1024 * 1024
MAX_SERIES = 128
MAX_POINTS = 4096      # in the whole bundle, never truncated
INT64_MAX = (1 << 63) - 1
DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,18})")
TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{3}|[0-9]{6}|[0-9]{9}))?Z"
)
RESOURCE_LABELS = {"resource_container", "location", "publisher", "model_user_id",
                   "model_version_id"}
REQUIRED_METRIC_LABELS = {"source", "response_code", "method"}
OPTIONAL_METRIC_LABELS = {"input_token_size", "output_token_size", "max_token_size",
                          "request_type", "shared_request_type", "error_category",
                          "explicit_caching", "accounting_resource"}
INCOMPLETE_FLAGS = {"page_token_present", "next_page_token_present",
                    "execution_errors_present", "unreachable_present"}
UNAVAILABLE_REASONS = {"console_display_only", "scope_unverified", "capture_unverified",
                       "incomplete_capture", "source_unavailable"}


class Reject(ValueError):
    """The message is a finite reason code, never input text."""


def require(condition, reason):
    if not condition:
        raise Reject(reason)


def keys(obj, required, reason, optional=()):
    require(isinstance(obj, dict) and required <= obj.keys() <= required | set(optional),
            reason)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate_json_key")
        obj[key] = value
    return obj


def reject_number(_token):
    # v1 contains no JSON numbers. Avoid even converting an untrusted large one.
    raise Reject("json_number")


def decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_int=reject_number, parse_float=reject_number,
                          parse_constant=reject_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise Reject("input_json") from None


def label(value, reason, allow_empty=False):
    require(isinstance(value, str) and len(value) <= 256
            and (value.isprintable() or (allow_empty and value == "")), reason)


def nanoseconds(value, reason):
    match = TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    require(match is not None, reason)
    try:
        dt = datetime(*map(int, match.groups()[:6]))
    except ValueError:
        raise Reject(reason) from None
    seconds = ((dt.toordinal() - 1) * 86400 + dt.hour * 3600 + dt.minute * 60 + dt.second)
    return seconds * 10**9 + int((match.group(7) or "").ljust(9, "0"))


def bounds(interval, reason):
    keys(interval, {"startTime", "endTime"}, reason)
    start = nanoseconds(interval["startTime"], reason)
    end = nanoseconds(interval["endTime"], reason)
    require(start < end, reason)
    return start, end


def expected_scope(project, model, start, end):
    label(project, "expected_scope")
    label(model, "expected_scope")
    scope = {"resource_container": project, "model_user_id": model,
             "interval": {"startTime": start, "endTime": end}}
    return scope, bounds(scope["interval"], "expected_window")


def check_scope(actual, expected, window):
    keys(actual, {"resource_container", "model_user_id", "interval"}, "scope_shape")
    require(actual["resource_container"] == expected["resource_container"]
            and actual["model_user_id"] == expected["model_user_id"]
            and bounds(actual["interval"], "scope_window") == window, "wrong_scope")


def check_capture(capture):
    keys(capture, {"view", "aggregation"} | INCOMPLETE_FLAGS, "capture_shape")
    require(capture["view"] == "FULL" and capture["aggregation"] == "none",
            "not_raw_full")
    require(all(type(capture[flag]) is bool for flag in INCOMPLETE_FLAGS), "capture_shape")
    require(not any(capture[flag] for flag in INCOMPLETE_FLAGS), "incomplete_capture")


def check_series(series, scope, response_unit):
    keys(series, {"metric", "resource", "metricKind", "valueType", "points"},
         "series_shape", {"unit"})
    require(series["metricKind"] == "DELTA" and series["valueType"] == "INT64",
            "metric_kind_or_value_type")
    require(("unit" in series or response_unit == "1") and series.get("unit", "1") == "1",
            "unit_not_count")
    metric, resource = series["metric"], series["resource"]
    keys(metric, {"type", "labels"}, "metric_shape")
    keys(resource, {"type", "labels"}, "resource_shape")
    require(metric["type"] == METRIC and resource["type"] == RESOURCE, "wrong_metric_or_resource")
    ml, rl = metric["labels"], resource["labels"]
    keys(ml, REQUIRED_METRIC_LABELS, "metric_labels", OPTIONAL_METRIC_LABELS)
    keys(rl, RESOURCE_LABELS, "resource_labels")
    for value in ml.values():
        label(value, "metric_labels", allow_empty=True)
    for value in rl.values():
        label(value, "resource_labels", allow_empty=True)
    require(rl["resource_container"] == scope["resource_container"]
            and rl["model_user_id"] == scope["model_user_id"]
            and rl["publisher"] == PUBLISHER and ml["source"] == SOURCE, "wrong_series_scope")
    # Types are already pinned. Include ALL label keys and values, not just display labels.
    return tuple(sorted(rl.items())), tuple(sorted(ml.items()))


def check_points(points, window):
    start, end = window
    seen = set()
    checked = []
    for point in points:
        keys(point, {"interval", "value"}, "point_shape")
        a, b = bounds(point["interval"], "point_interval")
        require(start <= a < b <= end, "point_outside_window")
        require((a, b) not in seen, "duplicate_point")
        seen.add((a, b))
        keys(point["value"], {"int64Value"}, "int64_shape")
        value = point["value"]["int64Value"]
        require(isinstance(value, str) and DECIMAL.fullmatch(value), "int64_decimal")
        count = int(value)
        require(count <= INT64_MAX, "int64_range")
        checked.append((a, b, count, point))

    cursor, total = start, 0
    checked.sort(key=lambda entry: entry[:2])
    for a, b, count, _point in checked:
        require(a >= cursor, "overlapping_points")
        require(a == cursor, "incomplete_window")
        cursor = b
        total += count
        require(total <= INT64_MAX, "window_total_out_of_int64")
    require(cursor == end, "incomplete_window")
    return total, [point for _, _, _, point in checked]


def response_rows(response, scope, window):
    keys(response, {"timeSeries"}, "response_shape", {"unit"})
    if "unit" in response:
        require(response["unit"] == "1", "unit_not_count")
    series_list = response["timeSeries"]
    require(isinstance(series_list, list), "series_list")
    require(len(series_list) <= MAX_SERIES, "series_limit")
    seen = set()
    rows = []
    point_count = 0
    for series in series_list:
        identity = check_series(series, scope, response.get("unit"))
        require(identity not in seen, "duplicate_series")
        seen.add(identity)
        points = series["points"]
        require(isinstance(points, list), "points_list")
        require(len(points) > 0, "incomplete_window")
        point_count += len(points)
        require(point_count <= MAX_POINTS, "point_limit")
        total, ordered = check_points(points, window)
        rows.append({"metric": series["metric"], "resource": series["resource"],
                     "metricKind": "DELTA", "valueType": "INT64", "unit": "1",
                     "points": ordered, "window_total_int64": str(total),
                     "observation": "positive_count" if total else "explicit_zero"})
    return rows


def assessment(status, reason, rows=(), **extra):
    return dict(schema=OUTPUT_SCHEMA, status=status, reason=reason, rows=list(rows),
                attribution_to_relay="not_established", availability="unknown", **extra)


def assess(raw, project, model, start, end):
    """Return the complete assessment in memory. Rejected bundles never expose rows."""
    try:
        require(len(raw) <= MAX_BYTES, "input_too_large")
        scope, window = expected_scope(project, model, start, end)
        snapshot = decode(raw)
        base = {"schema", "provenance", "scope", "read_state"}
        keys(snapshot, base, "snapshot_shape", {"capture", "response", "reason"})
        require(snapshot["schema"] == INPUT_SCHEMA, "schema_version")
        require(snapshot["provenance"] in ("synthetic", "operator_supplied"), "provenance")
        check_scope(snapshot["scope"], scope, window)
        info = dict(scope=scope, provenance=snapshot["provenance"], snapshot_sha256=sha256(raw).hexdigest())
        state = snapshot["read_state"]
        if state == "denied":
            keys(snapshot, base, "denied_shape")
            return assessment("unknown", "denied", **info)
        if state == "unavailable":
            keys(snapshot, base | {"reason"}, "unavailable_shape")
            reason = snapshot["reason"]
            require(isinstance(reason, str) and reason in UNAVAILABLE_REASONS, "unavailable_reason")
            return assessment("unknown", reason, **info)
        require(state == "ok", "read_state")
        keys(snapshot, base | {"capture", "response"}, "ok_shape")
        check_capture(snapshot["capture"])
        rows = response_rows(snapshot["response"], scope, window)
        if not rows:
            return assessment("unknown", "no_series", **info)
        return assessment("observed", "complete_series", rows, **info)
    except Reject as exc:
        return assessment("rejected", str(exc))


def read_regular_file(path):
    # O_NONBLOCK prevents a mistaken FIFO path from waiting for a writer.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Reject("input_not_regular")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(MAX_BYTES + 1)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="local sanitized v1 JSON file")
    parser.add_argument("--resource-container", required=True, help="independently checked exact label")
    parser.add_argument("--model", required=True, help="independently checked exact model_user_id")
    parser.add_argument("--start", required=True, help="requested exclusive lower bound (UTC Z)")
    parser.add_argument("--end", required=True, help="requested inclusive upper bound (UTC Z)")
    args = parser.parse_args()
    try:
        raw = read_regular_file(args.snapshot)
        result = assess(raw, args.resource_container, args.model, args.start, args.end)
    except OSError:
        result = assessment("rejected", "input_io")
    except Reject as exc:
        result = assessment("rejected", str(exc))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 2 if result["status"] == "rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
