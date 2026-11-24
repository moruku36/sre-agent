"""Read-only proposal preview for one synthetic staging fixture; never an approval."""

import argparse
from decimal import InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Final, Literal

from sre_investigation import build_report, parse_json
from sre_mvp import _is_ascii_identifier, _parse_utc


Preview = Literal["not_eligible", "no_action", "owner_review"]

_TARGET_FIELDS = ("provider", "source", "scope", "environment", "service", "target")
_REVIEWABLE_ACTION: Final[Literal["synthetic-review-evidence"]] = "synthetic-review-evidence"
_REVIEWABLE_TARGET: Final[tuple[str, str, str, str, str, str]] = (
    "azure", "synthetic-azure-01", "synthetic-azure-staging",
    "synthetic-staging", "synthetic-web", "synthetic-region-east",
)


def _decode_json(raw: bytes):
    if type(raw) is not bytes:
        raise ValueError("input must be JSON bytes")
    try:
        return parse_json(raw)
    except (InvalidOperation, RecursionError) as error:
        raise ValueError("invalid JSON input") from error


def _validate_digest(value: str):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("handoff_sha256 must be 64 lowercase hex characters")


def _read_proposal(raw: bytes) -> dict:
    proposal = _decode_json(raw)
    fields = {"kind", "version", "incident_id", "handoff_sha256", "as_of", "target", "action_id"}
    if not isinstance(proposal, dict) or set(proposal) != fields:
        raise ValueError("invalid proposal fields")
    if (proposal["kind"] != "synthetic_sre_proposal" or type(proposal["version"]) is not int
            or proposal["version"] != 1):
        raise ValueError("invalid proposal kind or version")
    incident_id = proposal["incident_id"]
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise ValueError("proposal incident_id must be nonblank")
    _validate_digest(proposal["handoff_sha256"])
    _parse_utc(proposal["as_of"])
    if not _is_ascii_identifier(proposal["action_id"]):
        raise ValueError("invalid proposal action_id")
    target = proposal["target"]
    if (not isinstance(target, dict) or set(target) != set(_TARGET_FIELDS)
            or not all(_is_ascii_identifier(value) for value in target.values())):
        raise ValueError("invalid proposal target")
    return proposal


PreviewReason = Literal[
    "stale_current_context",
    "proposal_context_mismatch",
    "outside_synthetic_scope",
    "resolved_incident",
    "open_incident",
]


def _classify_proposal_with_reason(
    request: dict,
    report: dict,
    *,
    current_handoff_sha256: str,
    current_as_of: str,
) -> tuple[Preview, PreviewReason]:
    if (
        current_handoff_sha256 != report["handoff_sha256"]
        or current_as_of != report["as_of"]
    ):
        return "not_eligible", "stale_current_context"

    incident = report["incident"]
    target = tuple(request["target"].get(field) for field in _TARGET_FIELDS)
    incident_target = tuple(incident.get(field) for field in _TARGET_FIELDS)

    if (
        request["incident_id"] != incident["incident_id"]
        or request["handoff_sha256"] != report["handoff_sha256"]
        or request["as_of"] != report["as_of"]
        or target != incident_target
    ):
        return "not_eligible", "proposal_context_mismatch"

    if target != _REVIEWABLE_TARGET or request["action_id"] != _REVIEWABLE_ACTION:
        return "not_eligible", "outside_synthetic_scope"

    status = incident["status"]

    if status == "resolved":
        return "no_action", "resolved_incident"

    if status == "open":
        return "owner_review", "open_incident"

    raise ValueError


def preview_proposal_with_reason(
    handoff: bytes,
    response: bytes,
    proposal: bytes,
    *,
    incident_id: str,
    current_handoff_sha256: str,
    current_as_of: str,
) -> tuple[Preview, PreviewReason]:
    """Return the advisory preview and its fixed reason; invalid inputs raise ValueError."""
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise ValueError("caller incident_id must be nonblank")

    _validate_digest(current_handoff_sha256)
    _parse_utc(current_as_of)

    request = _read_proposal(proposal)
    snapshot = _decode_json(handoff)
    report = build_report(
        snapshot,
        _decode_json(response),
        incident_id,
        sha256(handoff).hexdigest(),
    )

    return _classify_proposal_with_reason(
        request,
        report,
        current_handoff_sha256=current_handoff_sha256,
        current_as_of=current_as_of,
    )


def preview_proposal(
    handoff: bytes,
    response: bytes,
    proposal: bytes,
    *,
    incident_id: str,
    current_handoff_sha256: str,
    current_as_of: str,
) -> Preview:
    """Revalidate the raw investigation inputs and return a preview label or raise ValueError.

    The caller must supply the digest and receipt cutoff from its current handoff,
    not from the proposal. They are comparison values, not identity or authority.
    "owner_review" does not request review, approve anything, or perform an action.
    """
    label, _reason = preview_proposal_with_reason(
        handoff,
        response,
        proposal,
        incident_id=incident_id,
        current_handoff_sha256=current_handoff_sha256,
        current_as_of=current_as_of,
    )
    return label


def _current_delivery_context(raw):
    envelope = _decode_json(raw)
    if not isinstance(envelope, dict):
        raise ValueError("invalid delivery envelope")
    if set(envelope) != {"delivery_id", "handoff_json", "acknowledged"}:
        raise ValueError("invalid delivery envelope")

    delivery_id = envelope["delivery_id"]
    handoff_json = envelope["handoff_json"]
    acknowledged = envelope["acknowledged"]

    if not isinstance(delivery_id, str):
        raise ValueError("invalid delivery envelope")
    if not isinstance(handoff_json, str):
        raise ValueError("invalid delivery envelope")
    if not isinstance(acknowledged, bool):
        raise ValueError("invalid delivery envelope")

    handoff_raw = handoff_json.encode("utf-8")
    digest = sha256(handoff_raw).hexdigest()
    if delivery_id != digest:
        raise ValueError("invalid delivery envelope")

    handoff = _decode_json(handoff_raw)
    if not isinstance(handoff, dict):
        raise ValueError("invalid delivery handoff")
    if handoff.get("kind") != "synthetic_sre_handoff":
        raise ValueError("invalid delivery handoff")

    version = handoff.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("invalid delivery handoff")

    as_of = handoff.get("as_of")
    if not isinstance(as_of, str):
        raise ValueError("invalid delivery handoff")
    _parse_utc(as_of)

    return digest, as_of


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--proposal", required=True, type=Path)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--current-handoff-sha256")
    parser.add_argument("--current-as-of")
    parser.add_argument(
        "--current-delivery", type=Path,
        help="saved local pull envelope used instead of the explicit current context pair",
    )
    parser.add_argument(
        "--include-reason",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    explicit = (
        args.current_handoff_sha256 is not None
        or args.current_as_of is not None
    )
    delivery = args.current_delivery is not None

    if explicit and delivery:
        parser.error(
            "--current-delivery cannot be combined with "
            "--current-handoff-sha256 or --current-as-of"
        )
    if not explicit and not delivery:
        parser.error(
            "provide either --current-delivery or both "
            "--current-handoff-sha256 and --current-as-of"
        )
    if explicit and (
        args.current_handoff_sha256 is None or args.current_as_of is None
    ):
        parser.error(
            "--current-handoff-sha256 and --current-as-of must be provided together"
        )

    try:
        handoff = args.handoff.read_bytes()
        response = args.response.read_bytes()
        proposal = args.proposal.read_bytes()

        if delivery:
            current_handoff_sha256, current_as_of = _current_delivery_context(
                args.current_delivery.read_bytes()
            )
        else:
            current_handoff_sha256 = args.current_handoff_sha256
            current_as_of = args.current_as_of

        label, reason = preview_proposal_with_reason(
            handoff,
            response,
            proposal,
            incident_id=args.incident_id,
            current_handoff_sha256=current_handoff_sha256,
            current_as_of=current_as_of,
        )
    except (OSError, ValueError):
        sys.stderr.write("error: cannot preview synthetic proposal\n")
        return 1

    payload = {
        "kind": "synthetic_sre_proposal_preview",
        "version": 1,
        "preview": label,
    }

    if args.include_reason:
        payload["reason"] = reason

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())