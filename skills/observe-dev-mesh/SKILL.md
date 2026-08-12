---
name: observe-dev-mesh
description: Collect and diagnose Dev Mesh coordination protocol 20260812.1 across local Git workspaces without writing source workspaces. Use when checking active authority, collaboration volume, contention, transactions, managed direct commits, audit gaps, source integrity, or cutover readiness.
---

# Observe Dev Mesh

Use the repository-owned read-only Observer. It discovers only the current
`.dev-mesh/coord/20260812.1` control plane and writes solely to the caller-selected SQLite catalog.

## Collect

Choose a catalog outside every observed workspace, then scan one or more roots:

```bash
python3 <skill>/scripts/observer.py --db /absolute/path/observer.sqlite3 collect \
  --root /absolute/workspace-or-parent \
  --max-depth 5
```

Repeat `--root` to combine independent trees. Recollection is idempotent: immutable events are
deduplicated by protocol identity and materialized snapshots are refreshed as a replaceable view.

## Report

```bash
python3 <skill>/scripts/observer.py --db /absolute/path/observer.sqlite3 report
```

Use `--workspace <workspace-id>` for one project and `--stale-after-seconds <seconds>` to adjust
stalled-work diagnostics. Treat these fields as the operational summary:

- `active`: current Runs, Claims, contentions, transactions, cleanup, work, and managed direct commits.
- `diagnostics`: bounded integrity, lifecycle, recovery, terminal-correlation, and ownership findings.
- `cutover_readiness`: fail-closed answer for whether the observed current control planes are empty
  and audit-complete enough to retire.
- `contention`, `transaction_outcomes`, `direct_commit`, and `interaction_counts`: low-frequency
  lifecycle aggregates rather than heartbeat volume.

Never infer authority from events or reconstruct deleted snapshots. Materialized state remains the
authorization source; the Observer only reports gaps. Do not point the catalog inside an observed
workspace.

## Boundaries

- This skill is read-only with respect to source workspaces.
- It does not read retired `.agent-coordination` archives.
- The v1 Web Console is retired and is not a current data source. A future Console must consume this
  report contract rather than revive the v1 catalog.
