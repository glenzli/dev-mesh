# Dev Mesh Architecture

Dev Mesh coordinates short-lived Agent work inside one shared Git workspace. The activated
coordination contract is `dev-mesh.coordination@20260823.1`; protocol versions use `YYYYMMDD.x` and
are immutable after activation.

Cross-project task correlation is the separate optional
`dev-mesh.cross-project-collaboration@20260823.1` extension. It reuses the base event carrier and
does not change workspace authority or require a control-plane migration.

## Runtime boundaries

- `runtime/dev_mesh_coord/` owns workspace authority, immutable events, contention, direct commits,
  Git microtransactions, recovery, legacy cutover, and metadata-only cross-project relation
  production. `cross_project.py` owns that independent observational lifecycle.
- `runtime/dev_mesh_observer/` owns read-only discovery, bounded source validation, cataloging,
  diagnostics, and reports. It never reconstructs or mutates authority.
- `runtime/dev_mesh_console/` owns the loopback HTTP lifecycle, external scan-root configuration,
  bounded Dashboard API, and static presentation assets. It consumes Observer projections and does
  not query workspace authority directly.
- `skills/coordinate-shared-workspace/` owns Agent instructions and one thin launcher into the
  repository runtime. It does not duplicate protocol implementation.
- `skills/observe-dev-mesh/` owns thin launchers into the Observer and Console runtimes. It remains
  independent from the globally loaded coordination skill.
- `contracts/` and `schemas/` are the public protocol surface. Runtime code must agree with them.

## Workspace state

```text
.dev-mesh/
├── manifest.json
└── coord/
    ├── current.json
    ├── cutovers/
    ├── analysis/<cutover-id>/20260814.1.json
    └── 20260823.1/
        ├── protocol.json
        ├── events/
        ├── runs/
        ├── claims/
        ├── work-results/
        ├── handoffs/
        ├── contentions/
        ├── transactions/
        ├── direct-commits/
        ├── cleanups/
        ├── work/
        └── locks/
```

Materialized snapshots are authority. Immutable events are audit evidence. Observer projections are
diagnostic only. Timeouts and stale timestamps never transfer authority.

Version cutover retains only a bounded, decontented event envelope in `coord/analysis/`. It discards
the complete retired state and earlier full archives after target activation; the retained record is
never a producer input and cannot grant or reconstruct authority.

Legacy `.agent-coordination/` state is never migrated into current authority. Cutover atomically
moves the complete legacy tree to an external archive, initializes empty current state, and leaves
only `.agent-coordination/TOMBSTONE.json` at the old location to fence legacy writers.

## Normal lifecycle

```text
join Run -> create or reuse bounded Claim -> edit -> validate -> contribution-aware finish -> leave
```

No cross-Run overlap means no contention, checkout, or transaction. A same-Run request covered by an
existing Claim reuses it without another event. Finish records a Work Result only for contributed
source bytes; clean inspection releases directly. An overlapping request from another Run is pending
and has no write authority until a bounded contention decision completes. Microtransactions are an
advanced response to clean, semantically independent overlap; they are not per-Agent workspaces.

The Console consumes a collaboration projection, not raw event density. Its default flow contains
only cross-Run contention, interaction, dependency, transaction, and recovery facts. Routine Run and
Claim events remain available in the collapsed audit view and never justify producer-side ceremony.

All cooperative canonical Git mutations pass through the managed direct-commit or transaction
publication boundary. Both share a workspace-wide inherited-FD fence, while each transaction keeps
its own exact materialization and cleanup facts.

## Navigation

- Normative behavior: `contracts/dev-mesh-coordination-20260823.1.md`
- Cross-project correlation: `contracts/dev-mesh-cross-project-collaboration-20260823.1.md`
- Current/event schemas: `schemas/`
- Retirement procedure: `docs/CUTOVER.md`
- Runtime and fault-injection checks: `runtime/tests/`

Git history preserves the retired v1 design and implementation. Do not retain live duplicate source
trees or compatibility writers in the working tree.
