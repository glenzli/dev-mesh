---
name: observe-dev-mesh
description: Collect, diagnose, and display Dev Mesh coordination protocol 20260814.1 across local Git workspaces without writing source workspaces. Use when opening the local Web Console, checking active authority and inherited baselines, viewing project-linked collaboration flows and Work Results, measuring contention or transactions, investigating audit gaps and source integrity, or assessing cutover readiness.
---

# Observe Dev Mesh

Use the repository-owned read-only Observer. It discovers only the current
`.dev-mesh/coord/20260814.1` control plane and writes solely to the caller-selected SQLite catalog.

## Open the Web Console

Start the local Console with an external catalog and one or more project roots:

```bash
python3 <skill>/scripts/console.py \
  --db /absolute/path/observer.sqlite3 \
  --root /absolute/project-parent \
  --host 127.0.0.1 --port 8765
```

The Console prints its URL after the first collection. It provides compact overview cards,
project-linked metrics, a semantic collaboration flow, current authority, recent events, and
diagnostics. It follows the browser's light/dark preference, supports Chinese and English, and lets
the user add durable scan roots from the UI. Root configuration is stored next to the external
catalog unless `--registry` selects another external path.

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
- `work_results`: completed non-authoritative work, pending dirty-baseline acknowledgement, and
  completion intents awaiting reconciliation.
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
- The Web Console consumes current Observer projections; it never grants or reconstructs authority.
