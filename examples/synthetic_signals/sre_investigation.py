"""Validate one static investigation mock against a local synthetic handoff."""

import argparse
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sys

from sre_mvp import _parse_utc, _reject_non_finite_constant, _unique_json_object, _write_json


def select_incident(snapshot, incident_id):
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    if set(snapshot) != {"kind", "version", "as_of", "incidents", "unapplied_health_evidence", "cost_classifications"}:
        raise ValueError("invalid snapshot keys")
    if snapshot["kind"] != "synthetic_sre_handoff":
        raise ValueError("invalid kind")
    version = snapshot["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("invalid version")
    as_of = _parse_utc(snapshot["as_of"])
    incidents = snapshot["incidents"]
    if not isinstance(incidents, list):
        raise ValueError("incidents must be a list")
    if not isinstance(snapshot["unapplied_health_evidence"], list):
        raise ValueError("unapplied_health_evidence must be a list")
    if not isinstance(snapshot["cost_classifications"], list):
        raise ValueError("cost_classifications must be a list")

    def nonblank(value):
        return isinstance(value, str) and value.strip() != ""

    incident_keys = {
        "provider", "source", "scope", "environment", "service", "target",
        "incident_id", "status", "opened_at", "last_outage_at", "resolved_at", "evidence"
    }
    record_keys = {
        "source", "scope", "event_id", "kind", "observed_at", "received_at",
        "freshness", "evidence"
    }

    seen_incident_ids = set()
    seen_event_ids = set()
    selected = None
    selected_records = None

    for incident in incidents:
        if not isinstance(incident, dict) or set(incident) != incident_keys:
            raise ValueError("invalid incident keys")

        for key in ("provider", "source", "scope", "environment", "service", "target", "incident_id"):
            if not nonblank(incident[key]):
                raise ValueError("invalid incident string field")

        incident_id_value = incident["incident_id"]
        if incident_id_value in seen_incident_ids:
            raise ValueError("duplicate incident_id")
        seen_incident_ids.add(incident_id_value)

        status = incident["status"]
        if status not in ("open", "resolved"):
            raise ValueError("invalid incident status")

        opened_at = _parse_utc(incident["opened_at"])
        last_outage_at = _parse_utc(incident["last_outage_at"])
        if not (opened_at <= last_outage_at <= as_of):
            raise ValueError("invalid incident outage times")

        resolved_at = incident["resolved_at"]
        if status == "open":
            if resolved_at is not None:
                raise ValueError("open incident must have null resolved_at")
        else:
            resolved_at = _parse_utc(resolved_at)
            if not (last_outage_at <= resolved_at <= as_of):
                raise ValueError("invalid incident resolved_at")

        evidence = incident["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("incident evidence must be a nonempty list")

        records = {}
        for record in evidence:
            if not isinstance(record, dict) or set(record) != record_keys:
                raise ValueError("invalid attached record keys")

            source = record["source"]
            scope = record["scope"]
            event_id = record["event_id"]
            ref = record["evidence"]
            if not (nonblank(source) and nonblank(scope) and nonblank(event_id) and nonblank(ref)):
                raise ValueError("invalid attached record identity or ref")

            if source != incident["source"] or scope != incident["scope"]:
                raise ValueError("attached record source/scope mismatch")

            kind = record["kind"]
            if kind not in ("outage", "recovery"):
                raise ValueError("invalid attached record kind")

            freshness = record["freshness"]
            if freshness not in ("fresh", "late"):
                raise ValueError("invalid attached record freshness")

            observed_at = _parse_utc(record["observed_at"])
            received_at = _parse_utc(record["received_at"])
            if not (observed_at <= received_at <= as_of):
                raise ValueError("invalid attached record times")

            event_key = (source, scope, event_id)
            if event_key in seen_event_ids:
                raise ValueError("duplicate attached event identity")
            seen_event_ids.add(event_key)

            if incident_id_value == incident_id:
                records[event_key] = record

        if incident_id_value == incident_id:
            selected = incident
            selected_records = records

    if selected is None:
        raise ValueError("incident not found")
    if not selected_records:
        raise ValueError("incident has no attached records")

    return selected, selected_records


def resolve_citations(citations, members, *, allow_empty=False):
    if not isinstance(citations, list):
        raise ValueError("citations must be a list")
    if not citations and allow_empty is not True:
        raise ValueError("citations must not be empty")

    required = {"source", "scope", "event_id", "evidence"}
    seen = set()
    resolved = []

    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != required:
            raise ValueError("invalid citation")

        source = citation["source"]
        scope = citation["scope"]
        event_id = citation["event_id"]
        evidence = citation["evidence"]

        if not all(isinstance(value, str) and value.strip() for value in (source, scope, event_id, evidence)):
            raise ValueError("invalid citation")

        key = (source, scope, event_id)
        if key in seen:
            raise ValueError("duplicate citation")
        seen.add(key)

        if key not in members:
            raise ValueError("unknown citation")

        member = members[key]
        if not isinstance(member, dict) or member.get("evidence") != evidence:
            raise ValueError("citation evidence mismatch")

        resolved.append(member)

    return resolved


def parse_json(raw):
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_non_finite_constant,
        parse_float=Decimal,
    )


def build_report(snapshot, response, incident_id, handoff_sha256):
    fields = {
        "kind", "version", "incident_id", "handoff_sha256", "status",
        "observed", "hypotheses", "unknowns", "next_checks",
    }
    if not isinstance(response, dict) or set(response) != fields:
        raise ValueError("invalid mock fields")
    if (response["kind"] != "synthetic_sre_investigation_mock"
            or type(response["version"]) is not int or response["version"] != 1):
        raise ValueError("invalid mock kind or version")
    if response["incident_id"] != incident_id:
        raise ValueError("mock incident_id does not match --incident-id")
    if response["handoff_sha256"] != handoff_sha256:
        raise ValueError("mock handoff_sha256 does not match exact handoff bytes")
    if response["status"] != "complete":
        raise ValueError("mock status must be complete")

    incident, members = select_incident(snapshot, incident_id)
    cited = resolve_citations(response["observed"], members)
    hypotheses = response["hypotheses"]
    if not isinstance(hypotheses, list):
        raise ValueError("hypotheses must be a list")
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict) or set(hypothesis) != {"text", "supports", "contradicts"}:
            raise ValueError("invalid hypothesis fields")
        if not isinstance(hypothesis["text"], str) or not hypothesis["text"].strip():
            raise ValueError("hypothesis text must be nonblank")
        cited.extend(resolve_citations(hypothesis["supports"], members))
        cited.extend(resolve_citations(hypothesis["contradicts"], members, allow_empty=True))

    for field in ("unknowns", "next_checks"):
        items = response[field]
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError(f"{field} must be a list of nonblank strings")

    cited_keys = {(item["source"], item["scope"], item["event_id"]) for item in cited}
    return {
        "kind": "synthetic_sre_investigation_report",
        "version": 1,
        "status": "references_validated",
        "handoff_sha256": handoff_sha256,
        "as_of": snapshot["as_of"],
        "incident": {field: value for field, value in incident.items() if field != "evidence"},
        "observed_evidence": [record for key, record in members.items() if key in cited_keys],
        "hypotheses": hypotheses,
        "unknowns": response["unknowns"],
        "next_checks": response["next_checks"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--incident-id", required=True)
    args = parser.parse_args(argv)
    try:
        handoff = args.handoff.read_bytes()
        snapshot = parse_json(handoff)
        response = parse_json(args.response.read_bytes())
        report = build_report(snapshot, response, args.incident_id, sha256(handoff).hexdigest())
    except (InvalidOperation, OSError, RecursionError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    _write_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
