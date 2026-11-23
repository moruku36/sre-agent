import argparse
import datetime as dt
import json
import math
import re
import sqlite3
import sys
from contextlib import contextmanager

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


def validate(source, signal):
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
        if type(value) is bool or not isinstance(value, (int, float)):
            raise ValueError("invalid cost value")
        if isinstance(value, float):
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid cost value")
        elif value < 0:
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


def list_signals(db):
    """Return stored signals as a list of dicts.

    Each item is the stored signal body plus an envelope key "source".
    Ordering is deterministic: source, scope, event_id.
    """
    with _connect(db) as conn:
        rows = conn.execute(
            """
            SELECT source, body_json
            FROM signals
            ORDER BY source, scope, event_id
            """
        ).fetchall()
    out = []
    for source, body_json in rows:
        body = json.loads(body_json)
        body["source"] = source
        out.append(body)
    return out


def _write_json(obj):
    print(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sre_mvp.py")
    parser.add_argument("--db", required=True)
    parser.add_argument("command", choices=("demo", "list"))
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            _write_json(ingest(args.db, synthetic_signals()))
        else:
            _write_json(list_signals(args.db))
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
