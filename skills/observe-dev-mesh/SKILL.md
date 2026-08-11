---
name: observe-dev-mesh
description: Collect and analyze immutable dev-mesh coordination events and bounded active-contention diagnostics across explicitly allowed local workspaces using read-only discovery, an external SQLite catalog, and a localhost web console. Use only when the user explicitly requests cross-workspace coordination observation, a friendly local dashboard, centralized activity or stalled-contention reports, collaboration graphs, multi-repository agent lifecycle or handoff analysis, collection status, or coordination log aggregation. Do not use for ordinary editing, claims, handoffs, transaction arbitration, or single-workspace recovery.
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
- Read mutable state only from the fixed `contentions/active/*.json` boundary, with the same
  regular-file, symlink, and payload-size checks as event collection. Mirror it as replaceable
  diagnostic state, never as immutable history or recovery authority.
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

Event ingestion is idempotent by Observer workspace id and event filename. Preserve the first mirrored
payload and SHA-256 digest. If the source digest later changes, report an immutable-integrity issue
without replacing the original mirror. Each collection also refreshes the bounded active-contention
mirror so centralized reports can distinguish open, awaiting-response, lease-expired, and stalled
coordination. This mutable mirror may disappear or change between collections and grants no authority.

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
  serve --host 127.0.0.1 --port 8765 --collect-interval 5
```

Open the printed local URL. Keep the server on `127.0.0.1` or `localhost`; the command rejects
remote interfaces. Use the dashboard to inspect summaries, workspaces, open runs, pending handoffs,
activity rankings, explicit conflict signals, conflict-related paths, transaction lifecycles,
protocol-use heuristics, stalled active contentions, a bounded causal collaboration graph, filtered
event timelines, raw event details, and collection issues. The collaboration section defaults to a
per-workspace project overview. Select one workspace to load its chronological collaboration
storyline. The default canvas focuses one explicit collaboration episode instead of treating every
owner label in the selected window as a simultaneously live Agent. The header distinguishes owner
labels, joined runs, and peak observed run concurrency. A canonical Git rail stays at the top; only
owners attached to the focused contention, wait, handoff, transaction, or message receive lanes.
Each observed Agent run is a neutral execution spine with an outlined play node at its start and an
arrow showing reading direction. Agent identity comes from its numbered lane, never from color.
Colors encode workflow meaning: a temporary transaction becomes a distinct offset branch inside
the responsible Agent lane, publication turns its rejoin checkpoint green, waiting is amber,
contention is red, diversion is violet, and communication is cyan. The Agent's main execution spine
is cut while an explicitly correlated transaction branch is active, then resumes after rejoin.
Native action nodes sit on that spine. A complete
legacy claim episode may attach with a dotted stem only when it falls inside exactly one complete
run interval for the same owner; this is labeled `inferred`, never fills authoritative `run_id`, and
disappears when run intervals overlap or an episode boundary is unknown. Other unbound claims remain
independent dashed nodes. A claim created for the same explicit run at the same recorded moment as
the session start becomes a badge attached to the session node rather than a second floating point.
Actions and checkpoints carrying the same verified `transaction_id` sit directly on that temporary
branch. Give a local transaction one Git-graph-style fork node on the Agent spine, a clean offset
branch, and one green rejoin node before neutral progress resumes. Its short entry and rejoin bends
may be smooth curves, but ordinary cross-owner relations stay straight when aligned and use 90-degree
orthogonal paths when they change lanes. Keep every arrowhead the same semantic color as its line.
Use the canonical lane as an aggregate branch topology: keep the canonical trunk visible and stack
one derived branch arc per overlapping recorded transaction so concurrent branch count and explicit
merge times are visible without inventing a cross-project relationship.
Hide the neutral run spine while a waiting, diversion, or temporary branch interval owns that time;
let the semantic segment carry direction, then resume the neutral spine afterward.
Simultaneous actions can branch locally from one moment without inventing task causality. Only
the canonical Git context, a native short-lived transaction branch owned by its responsible Agent,
and explicit dependency or
communication relations otherwise remain lines. A transaction forks from its recorded base and
rejoins only after its recorded publish. There is no invented
coordination owner. Legacy events never gain guessed branch, dependency, or authority facts.
Keep relation lines free of persistent text. Distinguish handoff, message, waiting, diversion,
contention, run progress, and transaction branches through color, dash pattern, arrow shape, and the
bilingual legend; retain their exact label and status in hover cards and the inspector.
Number visible owner lanes by first appearance (`Actor 01`, `Actor 02`, and so on) because protocol
owner ids are coordination identities, not guaranteed Agent display names. Keep the raw owner id in
hover cards and the inspector. Make storyline labels, nodes, and the bilingual legend comfortably
readable at normal desktop scale. Hover or keyboard-focus an item for a bounded action, owner,
status, and time card; select it to keep the full protocol facts in the inspector. Keep moment
spacing dense enough that forks, rejoins, waits, diversion, notification, and cross-lane
intersections remain legible without expanding into a topology dashboard.
Do not describe the overview as a cross-project graph. The current event contract has no
cross-workspace correlation, so the console keeps same-name agents and all edges workspace-scoped
and explicitly reports that cross-project tracking is unavailable. Never infer such relationships
from names, timestamps, paths, or Git metadata. Treat
conflict-related paths as affected resources, not proof of a textual merge conflict. Treat likely
solo-protocol runs as a closed-run heuristic only; open runs remain unclassified, and the result
grants no authority. The console follows the operating-system light or dark appearance by default,
offers an explicit theme override, and ships Chinese and English locale catalogs whose choice
remains local to the browser.
Keep the console operator-first: place the observation window and collection actions in the top
bar, omit marketing-style hero copy, and keep metric, diagnostic, activity, and timeline cards dense
enough for scanning. Preserve extra vertical separation inside storyline lanes when cross-owner
relations or inferred attachments would otherwise crowd adjacent progress spines.
The console performs one collection immediately on startup and then repeats it at the configured
interval. It serializes automatic collection, **Collect now**, and **Add workspace** through one
operation slot. Set `--collect-interval 0` to disable the background loop. The header reports the
last successful cycle, recent ingestion deltas, collection errors, and source event files still
pending in the catalog so a fresh-looking dashboard cannot silently hide ingestion lag. A selected
project storyline also reports its latest recorded event time: global ingestion may be live while
that one project is quiet.

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
  digests, malformed active-contention snapshots, and unsafe mutable-state sources as collection
  issues.
- Preserve raw secrets and unrelated file contents by reading only discovered event JSON files and
  bounded Git identity metadata.
- Never repair a source from the central mirror. Direct recovery to the source workspace and the
  coordination skill when the user explicitly requests it.

## Load detailed design only when needed

Read [DESIGN.md](../../DESIGN.md) when changing workspace identity, collection idempotency, event
integrity, schema migration, or the one-way dependency between coordination and observation.
