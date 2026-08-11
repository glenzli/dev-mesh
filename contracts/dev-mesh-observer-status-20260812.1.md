# `dev-mesh.observer.status` Protocol

Status: canonical local contract

Protocol: `dev-mesh.observer.status`

Protocol version: `20260812.1`

This contract owns the Dev Mesh Observer's bounded read-only facility snapshot. It is independent
of Infra Discovery. The publisher conforms to `infra.discovery.registration@20260812.1`; Discovery
only announces the exact protocol version, binding, endpoint, service identity, and generation.

## Discovery offer

The Observer Console process publishes one local service:

- service kind: `dev-mesh-observer`
- stable instance id: `local`
- protocol: `dev-mesh.observer.status`
- protocol version: `20260812.1`
- binding: `infra.local.unix-socket`

The process acquires publication authority, binds an owner-only socket, and atomically publishes
the stable manifest once the endpoint is ready. It changes `service.generation` and the opaque
socket endpoint on every start. Only one process may publish `dev-mesh-observer--local.json`;
publication authority is serialized outside Discovery. Shutdown stops accepting requests, releases
publication authority, leaves the stable manifest as a candidate entry, and removes only the
process-unique socket. A successor binds a new endpoint and atomically replaces the manifest with a
new generation; neither process periodically refreshes the manifest.

`INFRA_PROTOCOL_RUNTIME_DIR` may provide the final absolute runtime root. Otherwise the publisher
uses the platform root defined by Infra Discovery. The Console may be started explicitly with
`--no-infra-discovery` when discovery is unavailable or unwanted; it must not silently advertise a
partial or non-conformant offer.

## Unix stream contract

Before reading application bytes, the server obtains the connected peer's effective UID and
requires it to match its own. The socket parent is mode `0700`; the socket is mode `0600`.

One connection carries exactly one request and one response:

1. The client writes one strict UTF-8 JSON document followed by LF.
2. The request including LF is at most 512 bytes.
3. Duplicate keys, non-standard JSON numbers, unknown fields, trailing bytes, and unsupported
   operations are invalid.
4. The server writes one strict UTF-8 JSON document followed by LF.
5. The response including LF is at most 256 KiB.
6. The server closes its write side and connection after that frame; EOF is part of completion.

The request timeout is two seconds. A connection cannot issue a second operation.

## Request

Schema: `dev-mesh.observer.status.request`

```json
{"schema":"dev-mesh.observer.status.request","schema_version":"20260812.1","operation":"snapshot"}
```

All fields are required. `snapshot` is the only operation in this version.

## Snapshot

Schema: `dev-mesh.observer.status.snapshot`

```json
{
  "schema": "dev-mesh.observer.status.snapshot",
  "schema_version": "20260812.1",
  "service": {
    "kind": "dev-mesh-observer",
    "instance_id": "local",
    "generation": "gen_0123456789abcdef0123456789abcdef"
  },
  "sequence": 1,
  "captured_at": "2026-08-12T02:00:00Z",
  "status": {
    "state": "healthy",
    "reason_codes": []
  },
  "headline_metrics": [
    "dev_mesh.workspaces.available",
    "dev_mesh.collection.pending_events",
    "dev_mesh.contentions.stalled"
  ],
  "metrics": [],
  "issues": [],
  "extensions": {
    "dev-mesh-observer": {
      "collector_enabled": true
    }
  },
  "links": {
    "console_url": "http://127.0.0.1:8765/"
  },
  "redaction": {
    "excluded": [
      "branch_names",
      "claim_scopes",
      "coordination_owner_ids",
      "database_paths",
      "event_payloads",
      "git_revisions",
      "raw_errors",
      "workspace_paths"
    ]
  }
}
```

`service` must exactly match the selected live Discovery registration. `sequence` starts at one
and strictly advances within a generation. `captured_at` and issue observation times are UTC
timestamps.

`status.state` is `starting`, `healthy`, `degraded`, `unavailable`, or `stopping`. Stable reason
codes in this version are:

- `collection_failed`: the latest collection attempt failed;
- `collection_stale`: enabled periodic collection has not succeeded within its bounded freshness
  threshold;
- `workspace_unavailable`: at least one registered source is unavailable;
- `integrity_issue`: the latest completed collection observed malformed or changed evidence;
- `contention_stalled`: a current contention is explicitly projected as stalled; and
- `no_workspaces_registered`: the Observer catalog contains no monitored workspace.

Historical integrity issue totals remain a metric. They do not permanently degrade the service
after a later clean collection. Normal active Agents, claims, handoffs, transactions, conflicts,
fast branches, and non-stalled contentions are activity, not facility failures.

Each metric has `id`, `kind`, and a non-null scalar `value`; `unit` is optional. Consumers ignore
unknown metric IDs. This version publishes bounded aggregates from the following set:

| Metric | Meaning |
| --- | --- |
| `dev_mesh.workspaces.registered` | Registered workspace count |
| `dev_mesh.workspaces.available` | Currently readable workspace count |
| `dev_mesh.workspaces.unavailable` | Registered minus available |
| `dev_mesh.collection.pending_events` | Source event files not mirrored yet |
| `dev_mesh.collection.last_success_age` | Seconds since the last successful cycle |
| `dev_mesh.collection.running` | Whether one serialized collection is active |
| `dev_mesh.integrity.issues` | Historical immutable collection issue records |
| `dev_mesh.events.mirrored` | Events currently mirrored in the Observer catalog |
| `dev_mesh.contentions.active` | Current mirrored contention snapshots |
| `dev_mesh.contentions.stalled` | Current contentions projected as stalled |

Issues use stable codes, severity `info`, `warning`, or `critical`, observation time, and the
aggregate subject `observer`. They never include raw errors, paths, owners, scopes, branches,
transaction identifiers, or event payloads.

`links.console_url`, when present, must use a literal loopback HTTP(S) address. It is application
data and never appears in the Discovery manifest. Consumers may use it for a human deep-link.

Consumers must ignore unknown response fields, metric IDs, reason codes, issue codes, and extension
members. The `dev-mesh-observer` extension is provider-owned and must not be re-exported as a generic
Infra Protocol contract.

## Error response

Schema: `dev-mesh.observer.status.error`

```json
{
  "schema": "dev-mesh.observer.status.error",
  "schema_version": "20260812.1",
  "error": {"code": "invalid_request"}
}
```

Stable codes are:

- `invalid_request`: framing, size, encoding, JSON, schema, version, operation, unknown field, or
  trailing request bytes are invalid;
- `snapshot_unavailable`: the bounded snapshot could not be collected or serialized.

A peer-UID mismatch is rejected before application bytes and receives no application error.

## Privacy and authority boundary

The snapshot never exposes coordination owner ids, workspace or database paths, Git revisions,
branch names, claim scopes, event payloads, raw errors, messages, handoff identities, transaction
identities, or source filenames. It grants no claim, lease, handoff, transaction, recovery,
publication, or Git authority. Infra Sentinel remains a read-only consumer; operational details and
causal collaboration graphs stay in the native Dev Mesh Observer Console.
