<p align="right"><a href="README.md">中文</a> · <strong>English</strong></p>

# Dev Mesh

Dev Mesh is the current-generation coordination layer for multiple Agents working in one shared
Git workspace. It gives short-lived work explicit authority, makes overlap resolvable, serializes
cooperative Git publication, and leaves bounded evidence that can be inspected after the work is
over.

The system is deliberately local and lightweight: an ordinary non-overlapping change follows one
Run and one reusable Claim. A Work Result exists only when source bytes were contributed; unchanged
inspection releases directly. Git commit is an optional independent publication step. Contention,
temporary branches, and microtransactions appear only when distinct Runs actually overlap.

## Current generation

The active authority contract is `dev-mesh.coordination@20260823.1`. This is the second-generation
implementation, but protocol versions use immutable `YYYYMMDD.x` identifiers rather than a mutable
`v2` label. The optional compatible cross-project evidence contract is
`dev-mesh.cross-project-collaboration@20260823.1`.

Workspace authority lives under `.dev-mesh/`. Cutover from `20260814.1` discards the old authority
state and earlier full archives, retaining only bounded, decontented, authority-free event evidence
under `.dev-mesh/coord/analysis/`. Retired `.agent-coordination/` state is not migrated or revived;
that retirement leaves only a tombstone that fences old writers. See
[`docs/CUTOVER.md`](docs/CUTOVER.md) for the retirement procedure.

## What it coordinates

- **Run** — one Agent task in one Git workspace.
- **Claim** — the exact paths and semantic resources that Run may change.
- **Work Result** — non-authority evidence of completed work; it releases the Claim without
  claiming a commit or private rollback point.
- **Workspace bytes** — a bounded content fingerprint for explicitly declared small Git-ignored
  files, allowing Work Result completion and baseline continuation without branching, publishing
  the data, or replacing a database transaction.
- **Contention** — when Claims overlap, the pending writer may simply wait. Only reassignment,
  handoff, exclusivity, or branch offload requires both parties to agree.
- **Managed Git publication** — direct commits and transaction publication share one canonical Git
  fence, so cooperating Agents do not race the workspace index or branch.
- **Events** — low-frequency immutable lifecycle evidence. Heartbeats update snapshots without
  producing event traffic.
- **Observer and Console** — read-only projections, diagnostics, collaboration flows, and
  cross-project evidence. They never grant or reconstruct authority.

Dev Mesh records communication; it does not deliver it. An Agent must first contact, create, or
resume the real target task using the host's task controls, then record that successful action with
Dev Mesh. `send`, `ack`, and handoff commands never start or wake another task by themselves.

## Install and routine Agent path

Dev Mesh is published through the [Glenzli Marketplace](https://github.com/glenzli/marketplace).
Register the marketplace once, then install Dev Mesh:

```bash
codex plugin marketplace add glenzli/marketplace --ref main
codex plugin add dev-mesh@glenzli-marketplace
```

The first command is required only once; later install or update plugins explicitly from the same
marketplace. Installation provides `coordinate-shared-workspace` and `observe-dev-mesh` together at
one version; do not keep a global skill symlink directly to this source checkout. Use a new task
after installation or an update.

Release builds use `MAJOR.MINOR.PATCH+codex.<source-short-sha>`: the leading version expresses
feature compatibility, while the build identity points to the source snapshot used for the installed
package rather than a timestamp. The
maintainer builds a minimal package from a clean source commit, then explicitly synchronizes it into
the plugin collection working tree; synchronization never commits or pushes either repository:

```bash
python3 scripts/plugin_dist.py build
python3 scripts/plugin_dist.py sync \
  --package dist/dev-mesh --marketplace-root ../marketplace --replace
```

The builder copies only the runtime manifest, assets, skills, runtime, current schemas, and current
contracts. Tests, archives, coordination state, and caches never enter `dist`. The
[`coordinate-shared-workspace`](skills/coordinate-shared-workspace/SKILL.md) skill owns the
operational instructions. Its normal path is:

```text
inspect -> join Run -> create or reuse Claim -> edit -> validate
        -> finish by actual contribution or release -> leave Run
                                  \
                                   -> optional managed publication
```

A request already covered by the same Run's Claim reuses that Claim without self-contention. With no
cross-Run overlap, no additional coordination flow appears. `claim-finish` records a Work Result only
for actual source contribution. The skill routes an Agent into contention, transaction, or recovery
only when the returned `next_action` requires it.

The common overlap path does not require a full negotiation: the later Claim becomes pending, it
selects wait, and it can activate after the original Claim completes. `parallel-tx` does not move
both Agents off the canonical line; it offloads only the pending writer to a short-lived branch,
and is available only when both Claims declare disjoint semantic writes. Dirty-baseline
acceptance binds the content digest, canonical revision, and branch. If any of them changes, the
command returns fresh evidence and requires one explicit retry.

## See the coordination model

These Console examples use virtual data rendered by the real project-relation and collaboration-
flow components. They illustrate the visual grammar; they are not captured production activity.

The project view separates explicit, receiver-bound cross-task collaboration from weaker same-Run
evidence. A solid arrow is a recorded collaboration relation. A dashed bracket means only that one
Owner/Run identity appeared in several workspaces; it is a clue, not proof that tasks communicated.

![Virtual Console project collaboration graph](docs/assets/console-project-collaboration-demo.en.png)

The collaboration flow projects only cross-Run contention, handoff, dependency, transaction, and
recovery. Routine Run and Claim lifecycle does not expand merely to fill a graph; it remains
available from the collapsed raw-event audit when needed.

## Observe locally

The independent [`observe-dev-mesh`](skills/observe-dev-mesh/SKILL.md) skill in the same plugin collects current
control planes into an external SQLite catalog and serves the loopback-only Web Console:

```bash
python3 skills/observe-dev-mesh/scripts/console.py \
  --db /absolute/path/observer.sqlite3 \
  --root /absolute/project-parent \
  --host 127.0.0.1 --port 8765
```

The Console defaults to current authority risk and cross-Run contention, handoff, dependency,
recovery, and cross-project relations. Routine lifecycle is collapsed and raw events are available
only in the on-demand audit section. Catalog data stays outside observed workspaces; the source
workspaces remain read-only to the Observer.

On macOS, install the repository-owned LaunchAgent to keep the Console available after moving the
checkout or changing its Python runtime:

```bash
python3 scripts/install_console_service.py install
python3 scripts/install_console_service.py status
```

By default the service also publishes the redacted
`dev-mesh.observer.status@20260812.1` Unix-socket offer through
`infra.discovery.registration@20260812.1`. Registration is discovery evidence, not proof of
liveness; consumers still connect to the current endpoint.

## Repository map

- [`runtime/dev_mesh_coord/`](runtime/dev_mesh_coord/) — authority, Work Results, dirty baselines,
  contention, recoverable Git effects, managed commits, microtransactions, and cross-project
  relation production.
- [`runtime/dev_mesh_observer/`](runtime/dev_mesh_observer/) — bounded source validation, catalog,
  diagnostics, and reports.
- [`runtime/dev_mesh_console/`](runtime/dev_mesh_console/) — loopback API, collection lifecycle, and
  browser dashboard.
- [`runtime/tests/`](runtime/tests/) — protocol, crash-window, concurrency, Observer, Console, and
  cutover coverage.
- [`skills/`](skills/) — thin Agent-facing launchers and instructions; protocol logic remains in the
  runtime.
- [`contracts/`](contracts/) and [`schemas/`](schemas/) — normative public surface.
- [`DESIGN.md`](DESIGN.md) — architecture boundaries, state layout, and deeper navigation.

## Validate

Run the complete gate for protocol or cross-boundary changes:

```bash
PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -v
PYTHONPATH=runtime python3 -m compileall -q \
  runtime/dev_mesh_coord runtime/dev_mesh_observer runtime/dev_mesh_console runtime/tests
python3 skills/coordinate-shared-workspace/scripts/coord.py --help
python3 skills/observe-dev-mesh/scripts/console.py --help
```

For bounded documentation or presentation changes, use the focused checks that prove the changed
links, commands, or rendered component before escalating to the full gate.
