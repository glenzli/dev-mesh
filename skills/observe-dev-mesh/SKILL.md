---
name: observe-dev-mesh
description: Collect and analyze immutable dev-mesh coordination events across explicitly allowed local workspaces using read-only discovery and an external SQLite catalog. Use only when the user explicitly requests cross-workspace coordination observation, centralized activity reports, multi-repository agent lifecycle or handoff analysis, collection status, or coordination log aggregation. Do not use for ordinary editing, claims, handoffs, transaction arbitration, or single-workspace recovery.
---

# Observe Dev Mesh Workspaces

Keep centralized observation outside the coordination correctness path. Read immutable event facts
from explicitly allowed workspaces and write only to the Observer-owned data directory.

## Preserve the boundary

- Require the user to identify the roots whose coordination data may be discovered. Do not broaden
  a root to a home directory or unrelated parent without explicit direction.
- Keep the Observer data directory outside every discovered workspace. The command rejects an
  in-workspace data directory before creating its database.
- Never write a manifest, cursor, acknowledgement, correction, repair, or lock into a source
  `.agent-coordination/` directory.
- Treat workspace ids, reports, lifecycle state, and integrity issues as diagnostics only. They
  grant no claim, lease, handoff, transaction, cleanup, publication, or Git authority.
- Do not invoke coordination mutations while collecting or reporting. Use
  `$coordinate-shared-workspace` separately when the user asks to act on a workspace.

## Discover and collect

Choose an external data directory and discover only the allowlisted roots:

```bash
python3 <skill>/scripts/observe.py --data-dir <observer-data-dir> \
  discover --roots <allowed-root> [<allowed-root> ...]
```

Mirror immutable event files after discovery. A later `collect` repeats discovery across the
registered roots, so it includes newly initialized workspaces:

```bash
python3 <skill>/scripts/observe.py --data-dir <observer-data-dir> collect
```

Ingestion is idempotent by Observer workspace id and event filename. Preserve the first mirrored
payload and SHA-256 digest. If the source digest later changes, report an immutable-integrity issue
without replacing the original mirror.

## Inspect and report

Inspect catalog health and source availability:

```bash
python3 <skill>/scripts/observe.py --data-dir <observer-data-dir> status
```

Derive a bounded cross-workspace activity report:

```bash
python3 <skill>/scripts/observe.py --data-dir <observer-data-dir> \
  report --since 48h --limit 10
```

Accept an ISO timestamp or a positive duration such as `48h` or `7d`. Explain that activity counts
use the selected time window while open runs and pending handoffs derive from the complete mirrored
history. Report collection issues separately from workflow findings.

## Handle incomplete or unsafe evidence

- Continue collecting other workspaces when one source is unavailable or malformed.
- Treat symlinks, non-regular event files, oversized payloads, malformed JSON, and changed immutable
  digests as collection issues.
- Preserve raw secrets and unrelated file contents by reading only discovered event JSON files and
  bounded Git identity metadata.
- Never repair a source from the central mirror. Direct recovery to the source workspace and the
  coordination skill when the user explicitly requests it.

## Load detailed design only when needed

Read [DESIGN.md](../../DESIGN.md) when changing workspace identity, collection idempotency, event
integrity, schema migration, or the one-way dependency between coordination and observation.
