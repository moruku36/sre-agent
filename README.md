# SRE Agent

[English](README.md) · [日本語](README.ja.md)

A local prototype for exploring how OpenClaw could triage cloud outages and cost spikes. The design targets AWS, Azure, Google Cloud, Oracle Cloud Infrastructure (OCI), and Sakura Cloud.

**Current limits:** The demo uses synthetic data. The separate Cloud Monitoring example validates local JSON for one metric (model invocation count); it does not fetch metrics or prove service availability. There is no live alerting, OpenClaw or model API call, or automated remediation. Live OCI integration has not been verified. Missing data does not mean healthy or zero cost.

## Quick start

From the repository root, with Python 3.10+ and SQLite support. No extra packages or credentials are needed.

```sh
cd examples/synthetic_signals
demo_dir="$(mktemp -d)"
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" demo
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" incidents
```

The result shows five resolved synthetic incidents, not the current health of any service.

## More information

- [Synthetic demo reference](docs/synthetic-signals.md) — tests, cost rules, incident lookup, and mock investigation.
- [Local handoff and ACK](docs/synthetic-handoff-delivery.md) — try pulling a snapshot and recording receipt locally.
- [Offline metric intake](examples/monitoring_intake/README.md) — supported JSON format and validation limits (Japanese).
- [Architecture and rollout plan](docs/architecture.md) — proposed integrations and approval rules (Japanese).
