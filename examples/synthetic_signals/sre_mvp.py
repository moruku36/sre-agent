import argparse
import datetime as dt
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from itertools import groupby

PROVIDERS = ("aws", "azure", "gcp", "oci", "sakura")

# Trusted synthetic source IDs. These are not production authentication IDs.
SOURCES = {
    "synthetic-aws-01": "aws",
    "synthetic-azure-01": "azure",
    "synthetic-gcp-01": "gcp",
    "synthetic-oci-01": "oci",
    "synthetic-sakura-01": "sakura",
}

FIELDS = {
    "version",
    "provider",
    "scope",
    "service",
    "environment",
    "target",
    "kind",
    "event_id",
    "observed_at",
    "received_at",
    "freshness",
    "value",
    "unit",
    "evidence",
}

KINDS = {"cost_actual", "cost_forecast", "cost_estimate", "outage", "recovery"}
COST_KINDS = {"cost_actual", "cost_forecast", "cost_estimate"}
COST_UNITS = {"USD", "JPY"}
STATE_UNIT = "state"
ONE_HOUR = dt.timedelta(hours=1)


def _is_ascii_identifier(value, max_len=64):
    if not isinstance(value, str) or not value or len(value) > max_len:
        return False
    if not value.isascii():
        return False
    if value[0].isdigit():
        return False
    return all(ch.isalnum() or ch in "._-" for ch in value)


def _is_synthetic_identifier(value, max_len=64):
    prefix = "synthetic-"
    return _is_ascii_identifier(value, max_len) and value.startswith(prefix) and len(value) > len(prefix)


def _parse_utc(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be string")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise ValueError("invalid canonical UTC timestamp")
    try:
        naive = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("invalid canonical UTC timestamp") from exc
    return naive.replace(tzinfo=dt.timezone.utc)


def _derive_freshness(observed_at, received_at):
    received = _parse_utc(received_at)
    if observed_at is None:
        return "unknown"
    observed = _parse_utc(observed_at)
    if received < observed:
        raise ValueError("received_at before observed_at")
    lag = received - observed
    return "fresh" if lag <= ONE_HOUR else "late"


def validate(source, signal, *, exact_cost=False):
    """Validate an exact signal body against its separate synthetic source."""
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("unknown source")
    if not isinstance(signal, dict):
        raise ValueError("signal must be dict")
    if set(signal) != FIELDS:
        raise ValueError("invalid fields")

    version = signal["version"]
    kind = signal["kind"]
    observed_at = signal["observed_at"]
    received_at = signal["received_at"]
    freshness = signal["freshness"]
    value = signal["value"]
    unit = signal["unit"]

    if type(version) is not int or version != 1:
        raise ValueError("invalid version")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("invalid kind")
    if SOURCES[source] != signal.get("provider"):
        raise ValueError("source/provider mismatch")

    for field in ("scope", "target", "evidence"):
        if not _is_synthetic_identifier(signal[field]):
            raise ValueError("invalid synthetic identifier")

    for field in ("service", "environment", "event_id"):
        if not _is_ascii_identifier(signal[field]):
            raise ValueError("invalid ascii identifier")

    derived = _derive_freshness(observed_at, received_at)
    if derived != freshness:
        raise ValueError("invalid freshness")

    if observed_at is None:
        if value is not None or unit is not None:
            raise ValueError("unknown observation requires null value and unit")
        return signal

    if value is None or unit is None:
        raise ValueError("observed signal requires value and unit")

    if kind in COST_KINDS:
        if exact_cost and type(value) is Decimal:
            finite = value.is_finite()
        elif type(value) is bool or not isinstance(value, (int, float)):
            raise ValueError("invalid cost value")
        else:
            finite = not isinstance(value, float) or math.isfinite(value)
        if not finite or value < 0:
            raise ValueError("invalid cost value")
        if not isinstance(unit, str) or unit not in COST_UNITS:
            raise ValueError("invalid cost unit")
    elif kind == "outage":
        if type(value) is not int or value != 1:
            raise ValueError("invalid outage value")
        if unit != STATE_UNIT:
            raise ValueError("invalid outage unit")
    elif kind == "recovery":
        if type(value) is not int or value != 0:
            raise ValueError("invalid recovery value")
        if unit != STATE_UNIT:
            raise ValueError("invalid recovery unit")
    else:
        raise ValueError("invalid kind")

    return signal


def synthetic_signals():
    """Return deterministic synthetic samples: (source, signal).

    For each provider: cost, outage, recovery, late cost, missing unknown cost,
    plus one exact replay. No local clock is used.
    """
    base = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    def ts(offset_minutes):
        return (base + dt.timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    fixtures = {
        "aws": {
            "scope": "synthetic-aws-prod",
            "service": "synthetic-api",
            "environment": "synthetic-prod",
            "target": "synthetic-cluster-a",
            "cost_kind": "cost_actual",
            "currency": "USD",
        },
        "azure": {
            "scope": "synthetic-azure-staging",
            "service": "synthetic-web",
            "environment": "synthetic-staging",
            "target": "synthetic-region-east",
            "cost_kind": "cost_forecast",
            "currency": "JPY",
        },
        "gcp": {
            "scope": "synthetic-gcp-dev",
            "service": "synthetic-data",
            "environment": "synthetic-dev",
            "target": "synthetic-bucket-1",
            "cost_kind": "cost_estimate",
            "currency": "USD",
        },
        "oci": {
            "scope": "synthetic-oci-prod",
            "service": "synthetic-cache",
            "environment": "synthetic-prod",
            "target": "synthetic-vm-1",
            "cost_kind": "cost_actual",
            "currency": "JPY",
        },
        "sakura": {
            "scope": "synthetic-sakura-prod",
            "service": "synthetic-queue",
            "environment": "synthetic-prod",
            "target": "synthetic-zone-1",
            "cost_kind": "cost_forecast",
            "currency": "JPY",
        },
    }

    samples = []
    for provider in PROVIDERS:
        source = f"synthetic-{provider}-01"
        f = fixtures[provider]
        prefix = f"synthetic-{provider}"

        cost = {
            "version": 1,
            "provider": provider,
            "scope": f["scope"],
            "service": f["service"],
            "environment": f["environment"],
            "target": f["target"],
            "kind": f["cost_kind"],
            "event_id": f"{prefix}-cost-0001",
            "observed_at": ts(0),
            "received_at": ts(5),
            "freshness": "fresh",
            "value": 123.45,
            "unit": f["currency"],
            "evidence": f"{prefix}-evidence-cost",
        }

        outage = {
            "version": 1,
            "provider": provider,
            "scope": f["scope"],
            "service": f["service"],
            "environment": f["environment"],
            "target": f["target"],
            "kind": "outage",
            "event_id": f"{prefix}-outage-0001",
            "observed_at": ts(10),
            "received_at": ts(12),
            "freshness": "fresh",
            "value": 1,
            "unit": "state",
            "evidence": f"{prefix}-evidence-outage",
        }

        recovery = {
            "version": 1,
            "provider": provider,
            "scope": f["scope"],
            "service": f["service"],
            "environment": f["environment"],
            "target": f["target"],
            "kind": "recovery",
            "event_id": f"{prefix}-recovery-0001",
            "observed_at": ts(20),
            "received_at": ts(22),
            "freshness": "fresh",
            "value": 0,
            "unit": "state",
            "evidence": f"{prefix}-evidence-recovery",
        }

        late_cost = {
            "version": 1,
            "provider": provider,
            "scope": f["scope"],
            "service": f["service"],
            "environment": f["environment"],
            "target": f["target"],
            "kind": "cost_estimate",
            "event_id": f"{prefix}-late-0001",
            "observed_at": ts(0),
            "received_at": ts(61),
            "freshness": "late",
            "value": 67.89,
            "unit": f["currency"],
            "evidence": f"{prefix}-evidence-late",
        }

        missing = {
            "version": 1,
            "provider": provider,
            "scope": f["scope"],
            "service": f["service"],
            "environment": f["environment"],
            "target": f["target"],
            "kind": "cost_actual",
            "event_id": f"{prefix}-missing-0001",
            "observed_at": None,
            "received_at": ts(30),
            "freshness": "unknown",
            "value": None,
            "unit": None,
            "evidence": f"{prefix}-evidence-missing",
        }

        for signal in (cost, outage, recovery, late_cost, missing):
            samples.append((source, signal))
        samples.append((source, cost))

    return samples


def _canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@contextmanager
def _connect(db, *, writer=False):
    from pathlib import Path
    if writer:
        conn = sqlite3.connect(str(db))
    else:
        uri = Path(db).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            source TEXT NOT NULL,
            scope TEXT NOT NULL,
            event_id TEXT NOT NULL,
            body_json TEXT NOT NULL,
            PRIMARY KEY (source, scope, event_id)
        )
        """
    )


def ingest(db, samples):
    """Validate and atomically ingest (source, signal) samples.

    Returns {"inserted": int, "duplicates": int}.
    The primary key is (source, scope, event_id). If a key already exists, the
    original stored payload is kept even if the incoming payload is altered;
    that sample is counted as a duplicate.
    """
    validated = []
    for source, signal in samples:
        validate(source, signal)
        validated.append((source, signal["scope"], signal["event_id"], _canonical_json(signal)))

    inserted = 0
    duplicates = 0
    with _connect(db, writer=True) as conn:
        try:
            conn.execute("BEGIN")
            _ensure_schema(conn)
            for source, scope, event_id, body_json in validated:
                cur = conn.execute(
                    """
                    INSERT INTO signals (source, scope, event_id, body_json)
                    VALUES (?, ?, ?, ?) ON CONFLICT(source, scope, event_id) DO NOTHING
                    """,
                    (source, scope, event_id, body_json),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"inserted": inserted, "duplicates": duplicates}


def _signal_rows(db):
    with _connect(db) as conn:
        return conn.execute(
            """
            SELECT source, scope, event_id, body_json
            FROM signals
            ORDER BY source, scope, event_id
            """
        ).fetchall()


def list_signals(db):
    """Return stored signals as a list of dicts.

    Each item is the stored signal body plus an envelope key "source".
    Ordering is deterministic: source, scope, event_id.
    """
    out = []
    for source, _, _, body_json in _signal_rows(db):
        body = json.loads(body_json)
        body["source"] = source
        out.append(body)
    return out


def _unique_json_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate signal fields")
    return obj


def _reject_non_finite_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def _validated_signal_rows(db, *, exact_cost=False):
    parse_options = (
        {"parse_float": Decimal, "parse_constant": _reject_non_finite_constant}
        if exact_cost else {}
    )
    for source, scope, event_id, body_json in _signal_rows(db):
        if not isinstance(body_json, str):
            raise ValueError("invalid stored signal evidence")
        try:
            body = json.loads(body_json, object_pairs_hook=_unique_json_object, **parse_options)
            validate(source, body, exact_cost=exact_cost)
        except (ValueError, RecursionError, InvalidOperation):
            raise ValueError("invalid stored signal evidence") from None
        if (scope, event_id) != (body["scope"], body["event_id"]):
            raise ValueError("stored signal key does not match body")
        yield source, body


def correlate_health(rows):
    """Correlate validated (source, body) rows in observation order.

    Sort by health key, observation time (unknown last), then event ID.
    Receipt time affects freshness but does not order state transitions.
    """
    health_key_fields = (
        "provider",
        "source",
        "scope",
        "environment",
        "service",
        "target",
    )

    evidence_fields = (
        "source",
        "scope",
        "event_id",
        "kind",
        "observed_at",
        "received_at",
        "freshness",
        "evidence",
    )

    def evidence_item(body):
        return {field: body[field] for field in evidence_fields}

    def unapplied_item(body, reason):
        item = evidence_item(body)
        for field in health_key_fields:
            item[field] = body[field]
        item["reason"] = reason
        return item

    def observed_sort_key(body):
        observed_at = body["observed_at"]
        return observed_at is None, observed_at or "", body["event_id"]

    grouped = defaultdict(list)
    for source, body in rows:
        if body["kind"] in COST_KINDS:
            continue
        enriched_body = dict(body, source=source)
        key = tuple(enriched_body[field] for field in health_key_fields)
        grouped[key].append(enriched_body)

    incidents = []
    unapplied_health_evidence = []

    for key, bodies in sorted(grouped.items()):
        bodies.sort(key=observed_sort_key)
        identity = dict(zip(health_key_fields, key))
        open_incident = None

        # A timestamp bucket makes simultaneous outage/recovery evidence explicit.
        for observed_at, bucket in groupby(bodies, key=lambda body: body["observed_at"]):
            if observed_at is None:
                unapplied_health_evidence.extend(
                    unapplied_item(body, "missing_observation") for body in bucket
                )
                continue

            events = list(bucket)
            outages = [body for body in events if body["kind"] == "outage"]
            recoveries = [body for body in events if body["kind"] == "recovery"]

            if outages:
                if open_incident is None:
                    incident_id = "health:{source}/{scope}/{event_id}".format_map(outages[0])
                    open_incident = {
                        **identity,
                        "incident_id": incident_id,
                        "status": "open",
                        "opened_at": observed_at,
                        "last_outage_at": observed_at,
                        "resolved_at": None,
                        "evidence": [],
                    }
                    incidents.append(open_incident)

                open_incident["evidence"].extend(evidence_item(body) for body in outages)
                open_incident["last_outage_at"] = observed_at

            appended_recovery = False
            for body in recoveries:
                if body["freshness"] == "late":
                    reason = "late_recovery"
                elif outages:
                    reason = "ambiguous_recovery"
                elif open_incident is None:
                    reason = "no_open_incident"
                else:
                    open_incident["evidence"].append(evidence_item(body))
                    appended_recovery = True
                    continue
                unapplied_health_evidence.append(unapplied_item(body, reason))

            if appended_recovery:
                open_incident["status"] = "resolved"
                open_incident["resolved_at"] = observed_at
                open_incident = None

    return {
        "version": 1,
        "incidents": incidents,
        "unapplied_health_evidence": unapplied_health_evidence,
    }


def list_incidents(db):
    """Correlate a read-only snapshot of the existing synthetic signal store."""
    return correlate_health(_validated_signal_rows(db))


_COST_IDENTITY_FIELDS = ("provider", "scope", "environment", "service", "target")
_COST_RULE_FIELDS = {
    *_COST_IDENTITY_FIELDS, "source", "kind", "unit", "threshold", "max_age_seconds",
}
_COST_EVIDENCE_FIELDS = (
    "scope", "event_id", "kind", "observed_at", "received_at", "freshness",
    "value", "unit", "evidence",
)


def load_cost_policy(path):
    """Read exact policy numbers without duplicate fields or non-finite literals."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_unique_json_object,
                parse_float=Decimal,
                parse_constant=_reject_non_finite_constant,
            )
    except (ValueError, RecursionError, InvalidOperation) as exc:
        raise ValueError("invalid cost policy JSON") from exc


def _cost_identity(source, item):
    return (source, *(item[field] for field in _COST_IDENTITY_FIELDS))


def _cost_threshold(value):
    if type(value) is float:
        # Direct Python callers have no JSON literal; use the float's decimal rendering.
        value = Decimal(str(value))
    if (type(value) is int or (type(value) is Decimal and value.is_finite())) and value >= 0:
        return value
    raise ValueError("cost policy threshold must be a finite nonnegative number")


def _validate_cost_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {"version", "rules"}:
        raise ValueError("cost policy must contain exactly version and rules")
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise ValueError("cost policy version must be integer 1")
    rules = policy["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("cost policy rules must be a nonempty list")

    validated = {}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != _COST_RULE_FIELDS:
            raise ValueError("invalid cost policy rule fields")

        source = rule["source"]
        if not isinstance(source, str) or source not in SOURCES or SOURCES[source] != rule["provider"]:
            raise ValueError("cost policy source/provider mismatch")
        for field in ("scope", "target"):
            if not _is_synthetic_identifier(rule[field]):
                raise ValueError(f"cost policy {field} must be a synthetic identifier")
        for field in ("environment", "service"):
            if not _is_ascii_identifier(rule[field]):
                raise ValueError(f"cost policy {field} must be an ASCII identifier")
        if not isinstance(rule["kind"], str) or rule["kind"] not in COST_KINDS:
            raise ValueError("invalid cost policy kind")
        if not isinstance(rule["unit"], str) or rule["unit"] not in COST_UNITS:
            raise ValueError("invalid cost policy currency")
        threshold = _cost_threshold(rule["threshold"])
        max_age = rule["max_age_seconds"]
        if type(max_age) is not int or max_age < 0:
            raise ValueError("cost policy max_age_seconds must be a nonnegative integer")

        key = _cost_identity(source, rule) + (rule["kind"],)
        if key in validated:
            raise ValueError("duplicate cost policy rule for the same stream and kind")
        validated[key] = dict(rule, threshold=threshold)

    return [validated[key] for key in sorted(validated)]


def _latest_cost_receipts(rows):
    latest = rows[-1]["received_at"]
    return [row for row in rows if row["received_at"] == latest]


def _classify_cost_rule(rule, rows, cutoff):
    """Evaluate one rule against its identity's visible, receipt-ordered rows."""
    if not rows:
        return "unknown", "no_signal", []
    matching = [row for row in rows if row["kind"] == rule["kind"]]
    if not matching:
        return "unknown", "kind_mismatch", _latest_cost_receipts(rows)
    latest = _latest_cost_receipts(matching)
    if len(latest) > 1:
        return "unknown", "ambiguous_receipt", latest

    row = latest[0]
    observed = row["observed_at"]
    if observed is None:
        return "unknown", "missing_observation", latest
    if row["freshness"] == "late":
        return "unknown", "late_observation", latest
    if row["unit"] != rule["unit"]:
        return "unknown", "currency_mismatch", latest
    if (cutoff - _parse_utc(observed)).total_seconds() > rule["max_age_seconds"]:
        return "unknown", "stale_observation", latest

    # A newer receipt need not describe a newer observation.
    earlier_known = [item for item in matching[:-1] if item["observed_at"] is not None]
    newer_observations = [item for item in earlier_known if item["observed_at"] > observed]
    if newer_observations:
        return "unknown", "out_of_order_observation", newer_observations + latest
    same_time_conflicts = [
        item for item in earlier_known
        if item["observed_at"] == observed
        and (item["value"], item["unit"]) != (row["value"], row["unit"])
    ]
    if same_time_conflicts:
        return "unknown", "ambiguous_observation", same_time_conflicts + latest

    if row["value"] > rule["threshold"]:
        return "anomaly", "above_threshold", latest
    return "within_limit", "at_or_below_threshold", latest


def _received_signal_rows(db, as_of):
    # Validate every stored row before considering its inclusive receipt cutoff.
    for source, body in _validated_signal_rows(db, exact_cost=True):
        if body["received_at"] <= as_of:
            yield source, body


def _classify_cost_rows(rules, visible_rows, cutoff):
    by_identity = defaultdict(list)
    for source, body in visible_rows:
        if body["kind"] in COST_KINDS:
            by_identity[_cost_identity(source, body)].append(body)
    for rows in by_identity.values():
        rows.sort(key=lambda body: (body["received_at"], body["event_id"]))

    classifications = []
    for rule in rules:
        source = rule["source"]
        rows = by_identity.get(_cost_identity(source, rule), [])
        status, reason, evidence = _classify_cost_rule(rule, rows, cutoff)
        classifications.append({
            "rule": rule,
            "status": status,
            "reason": reason,
            "evidence": [
                {"source": source, **{field: body[field] for field in _COST_EVIDENCE_FIELDS}}
                for body in evidence
            ],
        })
    return classifications


def list_cost_anomalies(db, policy, as_of):
    """Classify expected cost streams using one read-only historical snapshot."""
    rules = _validate_cost_policy(policy)
    cutoff = _parse_utc(as_of)
    classifications = _classify_cost_rows(rules, _received_signal_rows(db, as_of), cutoff)
    return {"version": 1, "as_of": as_of, "classifications": classifications}


def synthetic_handoff(db, policy, as_of):
    """Build a historical synthetic handoff from one read-only store snapshot."""
    rules = _validate_cost_policy(policy)
    cutoff = _parse_utc(as_of)
    rows = tuple(_received_signal_rows(db, as_of))
    health = correlate_health(rows)
    return {
        "kind": "synthetic_sre_handoff",
        "version": 1,
        "as_of": as_of,
        "incidents": health["incidents"],
        "unapplied_health_evidence": health["unapplied_health_evidence"],
        "cost_classifications": _classify_cost_rows(rules, rows, cutoff),
    }


def _encode_cost_json(value):
    """Serialize results without turning Decimal amounts into floats or strings."""
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("cannot encode a non-finite cost number")
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode_cost_json(item) for item in value) + "]"
    if isinstance(value, dict):
        fields = (
            _canonical_json(key) + ":" + _encode_cost_json(item)
            for key, item in sorted(value.items())
        )
        return "{" + ",".join(fields) + "}"
    return _canonical_json(value)


def _write_json(obj):
    print(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sre_mvp.py")
    parser.add_argument("--db", required=True)
    parser.add_argument("command", choices=("demo", "list", "incidents", "cost", "handoff"))
    parser.add_argument("--policy", help="JSON cost policy (required for cost and handoff)")
    parser.add_argument("--as-of", help="UTC snapshot time YYYY-MM-DDTHH:MM:SSZ (required for cost and handoff)")
    args = parser.parse_args(argv)
    if args.command in ("cost", "handoff"):
        if args.policy is None or args.as_of is None:
            parser.error(f"{args.command} requires --policy and --as-of")
    elif args.policy is not None or args.as_of is not None:
        parser.error("--policy and --as-of are only used by cost and handoff")

    try:
        if args.command == "demo":
            _write_json(ingest(args.db, synthetic_signals()))
        elif args.command == "list":
            _write_json(list_signals(args.db))
        elif args.command == "incidents":
            _write_json(list_incidents(args.db))
        else:
            read_snapshot = list_cost_anomalies if args.command == "cost" else synthetic_handoff
            result = read_snapshot(args.db, load_cost_policy(args.policy), args.as_of)
            print(_encode_cost_json(result))
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
