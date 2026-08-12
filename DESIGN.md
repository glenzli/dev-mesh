# Dev Mesh Architecture

Dev Mesh coordinates short-lived Agent work inside one shared Git workspace. The activated
coordination contract is `dev-mesh.coordination@20260812.1`; protocol versions use `YYYYMMDD.x` and
are immutable after activation.

## Runtime boundaries

- `runtime/dev_mesh_coord/` owns workspace authority, immutable events, contention, direct commits,
  Git microtransactions, recovery, and legacy cutover.
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
    └── 20260812.1/
        ├── protocol.json
        ├── events/
        ├── runs/
        ├── claims/
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

Legacy `.agent-coordination/` state is never migrated into current authority. Cutover atomically
moves the complete legacy tree to an external archive, initializes empty current state, and leaves
only `.agent-coordination/TOMBSTONE.json` at the old location to fence legacy writers.

## Normal lifecycle

```text
join Run -> create bounded Claim -> edit -> validate -> managed direct commit -> release -> leave
```

No overlap means no contention, checkout, or transaction. An overlapping request is pending and has
no write authority until a bounded contention decision completes. Microtransactions are an advanced
response to clean, semantically independent overlap; they are not per-Agent workspaces.

All cooperative canonical Git mutations pass through the managed direct-commit or transaction
publication boundary. Both share a workspace-wide inherited-FD fence, while each transaction keeps
its own exact materialization and cleanup facts.

## Navigation

- Normative behavior: `contracts/dev-mesh-coordination-20260812.1.md`
- Current/event schemas: `schemas/`
- Retirement procedure: `docs/CUTOVER.md`
- Runtime and fault-injection checks: `runtime/tests/`

Git history preserves the retired v1 design and implementation. Do not retain live duplicate source
trees or compatibility writers in the working tree.
