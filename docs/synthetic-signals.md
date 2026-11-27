# Synthetic signal demo reference

[README](../README.md) · [日本語](synthetic-signals.ja.md)

## Run the synthetic demo

Requires Python 3.10+ with SQLite support. No dependencies or credentials are needed. From the repository root, run:

```sh
cd examples/synthetic_signals
demo_dir="$(mktemp -d)"
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" demo
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" demo
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" list
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" incidents
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" cost --policy cost_policy.json --as-of 2026-01-15T13:05:00Z
python3 -B -m unittest -v
```

The first run stores 25 synthetic signals and skips five replays. The second stores nothing new and skips all 30 inputs. Each provider contributes cost, outage, recovery, late, and missing observations. Late observations retain their value; missing observations are unknown, not zero or healthy. Replays never overwrite the original event.

The read-only `incidents` command produces one versioned JSON object with `incidents` and `unapplied_health_evidence`. The demo yields five resolved incidents, one per provider. An incident is keyed by its opening outage's stored source, scope, and event ID. Its evidence includes those stored keys and the observed and received UTC times for each event.

Outages group only within the same provider, source, scope, environment, service, and target. Events are replayed by observation time, not receipt order. Only a strictly later fresh recovery with no simultaneous outage can resolve an incident; a later recurrence starts a new one. Missing observations, late recoveries, ambiguous recoveries, and recoveries with no open incident are reported separately with reasons. This is a historical snapshot: backfilling an earlier opening outage can change an incident ID. An empty incident list does not establish current health.

The command fails rather than treating a missing database or invalid stored evidence as an empty incident list. Raw cost signals are still available via `list`. No commands call OpenClaw, the OpenAI Agents API, or run remediation.

### Cost policy

The read-only `cost` command requires a policy file and a fixed UTC `--as-of` timestamp. It returns one decision per version 1 policy rule with the matching evidence. Each rule specifies the provider, source, scope, environment, service, target, cost kind, expected currency (`unit`), inclusive numeric `threshold`, and maximum observation age in seconds (`max_age_seconds`). A qualified observation above the threshold is an `anomaly`; one at or below it is `within_limit` for that rule only. Actual, forecast, and estimate are evaluated separately: a forecast anomaly is not a confirmed charge. The demo compares individual signal values; v1 has no billing-period field, aggregation, or currency conversion.

Only evidence received at or before the cutoff can affect a decision. Within a rule's stream, the latest receipt is evaluated without falling back to an earlier value. No visible signal or a missing, late, stale, ambiguous, out-of-order, or mismatched observation yields `unknown`, never zero or within limit. Late means delivery took more than one hour; stale means the observation is older than the rule allows at the cutoff. An unrelated cost kind cannot satisfy a rule. The entire store is still validated, including unrelated rows and receipts after the cutoff. Missing databases, malformed policies, and invalid stored evidence are errors, not empty results.

Threshold checks and same-observation conflict checks compare the exact decimal numbers in the policy JSON and the JSON actually stored in SQLite. The result emits those amounts as JSON numbers without rounding them to binary floats; use a decimal-aware reader to preserve that precision. Any precision lost before storage cannot be recovered. Existing databases do not need a migration.

SQLite is opened with `mode=ro`, which prevents data and schema writes. In WAL mode SQLite may still require or create `-wal` and `-shm` sidecar files.

The bundled synthetic policy produces five decisions: AWS is unknown (missing observation), Azure's JPY forecast is an anomaly, GCP is unknown (late estimate), OCI is unknown (the demo expects a forecast, but none was received), and Sakura's JPY forecast is within limit. Repeating the cost command at the same cutoff after replaying the demo gives identical JSON.

### Local synthetic handoff for OpenClaw

After running the demo, pull one historical incident and cost snapshot:

```sh
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" handoff \
  --policy cost_policy.json --as-of 2026-01-15T12:21:00Z
```

Read stdout as a single JSON object and check `kind: "synthetic_sre_handoff"` and `version: 1`. The object contains `as_of`, the `incidents` and `unapplied_health_evidence` from health correlation, and `cost_classifications` in the same form as the `cost` command's `classifications`. Use a decimal-aware reader for the numeric fields (for example, Python's `json.load(stream, parse_float=Decimal)`).

Both parts use one read-only SQLite snapshot and only evidence with `received_at <= as_of`, after validating the whole stored snapshot. The demo's five incidents are open at `12:21:00Z` and resolved at `12:22:00Z`, when the recoveries are received. The envelope retains synthetic evidence IDs, references, and reasons for unknown decisions. An empty incident list does not indicate current health. Invalid or missing input exits nonzero without an envelope.

This is a local pull contract for synthetic fixtures, not an authenticated OpenClaw integration. It sends no data, accepts no acknowledgment, and makes no changes to signals or the schema. The same store, policy, and cutoff produce the same JSON; SQLite's WAL sidecar behavior described above still applies.

### Look up one synthetic incident

Copy an `incident_id` from the handoff's `incidents` array and use the same cutoff to look up that historical result. For example, the demo's AWS incident is open at `12:21:00Z` and becomes resolved when the recovery is received at `12:22:00Z`.

```sh
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" get-incident \
  --as-of 2026-01-15T12:21:00Z \
  --incident-id 'health:synthetic-aws-01/synthetic-aws-prod/synthetic-aws-outage-0001'
```

The single JSON object has `kind: "synthetic_sre_incident_detail"`, `version: 1`, `as_of`, the requested `incident_id`, and a `status` of `found` or `not_found`. When found, `incident` is the matching handoff item, including its own `open` or `resolved` status and evidence. A well-formed ID without an exact match returns `not_found` and `incident: null` with exit status 0. This means only that the ID is absent from that historical snapshot, not that the service is healthy.

No cost policy is needed. The command validates the whole read-only database snapshot, including rows received after the cutoff, and correlates only evidence received at or before `as_of`. It returns no unrelated incidents, unapplied evidence, or cost results. Invalid timestamps, IDs, or database contents fail without a JSON response, as do missing required options or a missing database. This is a local synthetic lookup, not a live OpenClaw operation.

### Local synthetic investigation report

Using the demo store above, save the unmodified handoff bytes and check a bundled mock response for one caller-selected incident:

```sh
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" handoff \
  --policy cost_policy.json --as-of 2026-01-15T12:21:00Z > "$demo_dir/handoff.json"
python3 -B sre_investigation.py --handoff "$demo_dir/handoff.json" \
  --response investigation_mock.json \
  --incident-id 'health:synthetic-aws-01/synthetic-aws-prod/synthetic-aws-outage-0001'
```

The example [mock response](../examples/synthetic_signals/investigation_mock.json) is handwritten, not generated by an API. To try a different snapshot, record the SHA-256 of its exact file bytes (including the final newline) as `handoff_sha256` in your mock and use an exact incident ID present in that file. Reformatting the JSON changes the hash. The caller's `--incident-id` must match the mock's ID. Citations use all four fields: `source`, `scope`, `event_id`, and the opaque `evidence` reference. Only evidence attached to that incident and received by `as_of` can be cited; cost, unapplied health evidence, and evidence from other incidents are out of scope.

The report's `observed_evidence` contains the cited records in handoff order, including receipt time and freshness. Hypotheses (with supporting and opposing citations), unknowns, and next checks are separate and are not facts or instructions. `references_validated` checks membership and binding, not authorship, causal claims, or current health. An incomplete mock, a missing or duplicate incident ID, invalid citations, or unrecognized response fields fail without a report. This command reads only the two local JSON files. It does not follow evidence references, call a model, or approve or execute an action.
