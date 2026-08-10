---
name: observe-dev-mesh
description: Collect and analyze immutable dev-mesh coordination events across explicitly allowed local workspaces using read-only discovery, an external SQLite catalog, and a localhost web console. Use only when the user explicitly requests cross-workspace coordination observation, a friendly local dashboard, centralized activity reports, multi-repository agent lifecycle or handoff analysis, collection status, or coordination log aggregation. Do not use for ordinary editing, claims, handoffs, transaction arbitration, or single-workspace recovery.
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

## Run the local console

Start the bundled dashboard after registering at least one allowlisted root:

```bash
python3 <skill>/scripts/observe.py --data-dir <observer-data-dir> \
  serve --host 127.0.0.1 --port 8765
```

Open the printed local URL. Keep the server on `127.0.0.1` or `localhost`; the command rejects
remote interfaces. Use the dashboard to inspect summaries, workspaces, open runs, pending handoffs,
activity rankings, explicit conflict signals, conflict-related paths, transaction lifecycles,
protocol-use heuristics, filtered event timelines, raw event details, and collection issues. Treat
conflict-related paths as affected resources, not proof of a textual merge conflict. Treat likely
solo-protocol runs as a closed-run heuristic only; open runs remain unclassified, and the result
grants no authority. The console follows the operating-system light or dark appearance by default,
offers an explicit theme override, and ships Chinese and English locale catalogs whose choice
remains local to the browser.

Use **Add workspace** only after the user identifies the exact workspace or parent-directory path.
The dialog treats that explicit path as a new allowlisted scan root, bounds recursive discovery by
the selected depth, registers matching `.agent-coordination/events` sources, and performs one
idempotent collection. It accepts only an absolute path or `~`-prefixed path. **Collect now**
re-discovers only roots already stored in the Observer catalog.

Both actions require same-origin localhost requests plus the console-only request header. They may
update only the external Observer catalog and never mutate source workspaces or grant coordination
authority. Stop the server with `Ctrl-C`. Do not place it behind a remote proxy or treat it as a
multi-user service; remote access requires a separate authentication and disclosure design.

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
