# `dev-mesh.coordination@20260812.1`

Status: archived; frozen and not writable

Event schema: `1`

## 1. Scope

This protocol owns cooperative workspace-local write declarations, contention decisions,
short-lived Git microtransactions, and immutable workflow evidence. Git content remains canonical.
Observer data is diagnostic only. The protocol does not own Agent conversations, arbitrary file
writes made outside its producer, cross-repository atomicity, or automatic semantic merge.

## 2. State identity and namespace

The exact protocol version is the identity of its one persistent state space:

```text
<workspace>/.dev-mesh/coord/20260812.1/
```

There is no generation UUID, random suffix, or restart counter. Restarts reuse this state. A change
that needs empty state publishes a new exact `YYYYMMDD.x` protocol version.

```text
.dev-mesh/
├── manifest.json
└── coord/
    ├── current.json
    ├── 20260812.1/
    │   ├── protocol.json
    │   ├── events/
    │   ├── runs/
    │   ├── claims/
    │   ├── messages/
    │   ├── acks/
    │   ├── handoffs/
    │   ├── contentions/
    │   ├── work/
    │   ├── direct-commits/
    │   ├── transactions/
    │   ├── cleanups/
    │   ├── checkouts/
    │   ├── archive/
    │   └── locks/
    └── cutovers/
```

The bootstrap lock is `<workspace>/.dev-mesh.bootstrap.lock`. Runtime state is added to local Git
exclude and is never a product commit.

Only the state selected by `current.json` is writable. Production commands expose no alternate
state-directory override. Marker files, the active state root, mutation parents, and mutation
targets must remain inside the workspace root and must not be symlinks.

## 3. Markers and writer fencing

`manifest.json` identifies a Dev Mesh workspace and points to `coord/current.json`.
`current.json` selects the exact supported protocol, version, event schema, and state name.
`protocol.json` self-identifies the selected state directory. All three must agree.

Every authority-bearing mutation executes:

1. validate the three markers and reject live legacy state or split brain;
2. acquire the operation lock inside the selected state;
3. revalidate the markers after lock acquisition;
4. validate owner, entity state, semantic scope, and Git predicates;
5. atomically replace each materialized authority record and append its immutable evidence event
   inside the same serialized operation; materialized state remains authoritative if a process
   stops between the two file writes;
6. revalidate the markers before releasing the lock.

Marker fencing does not replace Claim ownership, contention epochs, transaction ownership, or Git
publication predicates.

## 4. Default direct lifecycle

The normal path is:

```text
agent-join -> claim -> direct edit -> validate -> managed direct-commit -> release -> agent-leave
```

No contention, queue, checkout, or transaction is created without overlap.

- A Claim uses one bounded intent: `read`, `local-edit`, `semantic-edit`, or
  `exclusive-refactor`. A read Claim grants no write authority and never blocks a writer.
- A write Claim grants only its declared workspace paths and semantic resources.
- A pending-arbitration Claim records intent and grants no write authority.
- After its contention is terminal, a pending Claim becomes writable only through
  `claim-activate`, which rechecks every physical and semantic overlap. A third request cannot join
  an already in-flight overlap implicitly; it waits and retries after that slice closes.
- A dirty active or paused Claim cannot be released normally.
- A direct writer commits only through the producer's managed `direct-commit` operation. It binds
  the exact active writable Claim and Run, declared paths, canonical branch and base, validation
  evidence, intended tree, changed-path count/digest/sample, and content digest before staging the
  canonical index. The index must initially be empty. Undeclared dirty paths remain unstaged.
- Direct commit staging and publication share one workspace-wide canonical Git fence with
  transaction publication. The Git child inherits that fence, so a child that outlives its caller
  cannot race a second direct commit, transaction publication, or canonical Git recovery. The durable
  `staging`, `committing`, and `needs-attention` states reconcile only from exact branch, `HEAD`,
  index, tree, and digest facts; an ambiguous state fails closed.
- A completed Run cannot leave while it owns an active/paused Claim, active transaction,
  unresolved handoff, active contention role, or suspended-work record.
- Failed or abandoned Runs never release authority implicitly.
- When a failed or abandoned Run strands recorded authority, a newly joined Run of the same owner
  may explicitly recover it with evidence. Recovery rebinds exact Claim, transaction, offered
  handoff, contention, and suspended-work Run correlations under the operation lock. It is
  idempotent across partial writes and cannot transfer authority to another owner. Every object and
  lineage bound is preflighted before the first record changes, so a later invalid object cannot
  leave a partially authorized recovery. Authority
  records retain a bounded lineage of at most 64 recovered Runs so a second recovery failure does
  not erase proof of an earlier partial recovery.
- Forced terminal Run attention uses an exact count, deterministic digest, and bounded sample rather
  than copying an unbounded blocker list into the Run snapshot.
- Staleness and lease expiry are diagnostic facts, never takeover authority.
- An unchanged Claim update mutates only `heartbeat_at`; it creates no immutable update event.
- Run joins/leaves and Claim grants/releases record the observed canonical branch and exact Git
  revision. These are correlation facts, not proof that one Agent authored every intervening commit.

A paused Claim records a bounded blocker kind, checkpoint, and resume condition. Authorization,
environment, and external-resource blockers additionally record the attempted operation, exact
resources, and a stable error kind; resume requires fresh evidence. This keeps a sandbox or
permission failure distinct from collaboration contention. If later inspection disproves an event
observation, the exact owner appends `audit-correction` referencing the superseded event; no event
file is rewritten.

### CLI presentation boundary

The command-line interface returns a bounded action-oriented projection by default. Routine status
should be filtered by exact owner, Run, or scope; an unfiltered overview contains counts and at most
eight action-required samples rather than every authority record. Mutating commands return stable
identity, outcome, bounded recovery collections, and one `next_action` when a conventional next
step exists. The root `--verbose` option exposes the complete materialized result only for exact
diagnosis, recovery, or protocol review.

This projection is presentation policy, not authority or persistence. It does not remove fields
from snapshots, suppress immutable events, change producer validation, or make a truncated sample
complete evidence. Programmatic integrations that need full facts use the producer API or explicit
verbose output. Agents do not routinely read immutable event files or Observer catalogs merely to
continue a successful lifecycle.

## 5. Interactions

Every message has one `interaction_kind`:

```text
notice | request | handoff
```

An optional topic classifies the content as `general`, `conflict`, `decision`, `takeover`, or
`validation`.

The sender and every acknowledging recipient must correlate the action to an exact active Run;
owner labels without a Run are not accepted as evidence.

- notice does not require acknowledgement;
- request may require acknowledgement and transfers no capability;
- handoff requires acknowledgement and enters the handoff lifecycle;
- message acknowledgement alone never transfers a Claim or transaction.

A handoff persists its inert message before the offered handoff snapshot, so an offered handoff
never points to a missing message. The caller must provide a stable handoff id for exact retry; a
retry must match the original source Run, owners, topic, subject, and body, and fills only missing
snapshot or event evidence. Notice and request messages do not require that id.

Handoff terminal states are `accepted`, `rejected`, and `withdrawn`. The target owner may accept or
reject; the source owner may withdraw. Acceptance transfers work responsibility only. Capability
transfer still requires a matching owner-authorized Claim or transaction transition.
Every handoff terminal transition persists the mutually exclusive snapshot before its event; an
event append gap is diagnosable, while a second terminal outcome is rejected.

## 6. Immutable events

Every event is strict UTF-8 JSON and contains:

```json
{
  "schema": 1,
  "event_id": "globally-unique-id",
  "event": "claim-created",
  "at": "2026-08-12T00:00:00Z",
  "protocol": "dev-mesh.coordination",
  "protocol_version": "20260812.1",
  "authority_effect": "grant",
  "transaction_id": null
}
```

`authority_effect` is `none`, `grant`, `retain`, `transfer`, `release`, or `terminal`. Its mapping is
a producer-owned exhaustive table. Unknown event types cannot be emitted until classified.
The reviewable base envelope is `schemas/event.schema.json`; event-specific payload constraints are
enforced by the producer operation that owns that event.

Payloads include stable correlation identifiers when known. Producers do not infer missing identity
from timestamps or similar owner names. Terminal events include an event-specific `status` and a
bounded stable `reason_code` when a reason is required. A terminal event is written before its active
snapshot is archived. No universal preceding-state digest is required.

Events are immutable and diagnostic; they never reconstruct or override current authority.
Corrections append new evidence referencing the superseded event. Observer may diagnose a missing
event/snapshot correlation, but it cannot repair producer state.

Observer binds each collected event source path and event id to its exact-byte SHA-256 digest. A
later mismatch creates a persistent integrity finding while preserving the originally collected
record; it never silently replaces old evidence.

The first release bounds a Claim to 128 paths; each semantic, pause-resource, and correction set to
64 identifiers; a contention to 64 participant scopes; a transaction to 128 changed paths; and the
exact persisted bytes of an event file to 256 KiB. Large correlations use an exact count,
deterministic digest, and bounded sample. Physical overlap evidence is likewise a bounded path set
plus exact overlap count and digest, not an unbounded pairwise Cartesian product. Events carry
correlations, decisions, status, and stable reason codes; they do not copy message bodies, file
contents, diffs, prompts, tool output, tokens, or Agent reasoning. Heartbeats update snapshots
without appending events.

## 7. Contention and optional microtransactions

An overlap opens one contention slice. The deterministic initial coordinator is a participant, not
a permanent central Agent. Coordinator lease expiry permits a participant to acquire the next
coordination epoch only; it never transfers a Claim or work product.

Decision order is:

```text
decompose -> short wait -> handoff -> optional microtransaction -> exclusive
```

Decision acknowledgements are keyed by exact participant scope, owner, and active Run. One response
cannot silently stand in for several Claims that happen to reuse an owner label.
Coordinator renewal, proposal, enactment, cancellation, and acquisition all validate the exact
active Run as well as the owner and epoch. A participant may cancel an arbitration slice without
releasing either Claim. Producer reconciliation repairs only provable terminal event/snapshot
ordering gaps; it does not infer a decision for an orphaned live contention.

Opening contention first persists an exact bounded `contention-opened` event intent in the active
snapshot. A retry validates that intent, appends the one immutable event if absent, correlates the
pending Claim, and only then removes the opening intent. It never treats a same-scope but differently
identified contention as an idempotent retry.

Enactment and cancellation first persist a terminal intent containing the exact bounded event,
decision revision, decision, coordinator epoch, and coordinator Run. This immediately fences every
other contention mutation. Reconciliation writes or validates that exact event and archives only
the matching intent; stale or mutually exclusive terminal evidence fails closed.

A microtransaction is allowed only after explicit participant acceptance, before overlapping dirty
writes exist, when no participant is performing an exclusive refactor, and when declared semantic
writes are independent. It owns one short-lived branch and
checkout, produces one candidate commit, validates the exact candidate, and publishes only by
fast-forwarding the current canonical `HEAD`. Conflicts remain in the temporary checkout.

The first release is deliberately a one-isolated-Claim pilot: one accepted pending Claim may enter
one transaction checkout and produce one candidate commit. It does not split several Claims across
one branch or attempt a semantic merge.

Microtransactions are advanced and opt-in. Their observed, prepared, validated, published,
aborted, conflicted, and cleanup-attention outcomes are monitored before any automation expands.
Preparation and validation require the transaction owner's exact active Run. Publication is
serialized and requires an exact active steward Run; a slug alone cannot impersonate a publisher.
Handoff and abort also require the exact active owner Run.

Transaction creation first persists an `initializing` materialization intent before creating its
branch or checkout, then promotes its exact Claim and makes the transaction active. Reconciliation
either completes that provable promotion, removes exact unchanged materialized resources one
fact-checked step at a time, or durably aborts an intent whose resources were never created. A
proven-missing worktree or branch is treated as an already-applied cleanup step; changed or
ambiguous Git facts require attention. Handoff is similarly retryable when the Claim transfer is
durable but the transaction record or evidence event lags. Before publication, the checkout must
be clean and its branch and checkout `HEAD` must still equal the exact validated candidate. Any
rebase creates a new candidate and repeats the one-commit and declared-path checks before
validation may succeed again.

Every protocol-managed Git mutation that can outlive its Python caller is preceded by a durable
exact effect intent and runs under an advisory-lock file descriptor inherited by the Git child. A
surviving child therefore keeps the effect visibly in flight after its parent stops. Abort, handoff,
other publication, and reconciliation cannot finalize that transaction until the inherited fence is
idle. Reconciliation then compares exact Git facts with the intent: `refreshing` either becomes a
new unvalidated prepared candidate or durable attention, while `publishing` becomes published only
when canonical `HEAD` is the exact intended candidate. Absence observed while the child holds the
fence is never proof that an effect did not happen.

Transaction publication also holds the same workspace-wide canonical fence as managed direct
commit. This serializes the shared index and canonical ref across both paths while retaining the
per-transaction fence needed to correlate a surviving child to its exact transaction. A raw Git
command that bypasses the producer is outside the cooperative protocol and cannot be made safe by
Claim records alone; the skill therefore must route every cooperative canonical stage/commit and
transaction publication through these managed operations.

Abort first persists its explicit authorization and a cleanup intent containing the exact managed
checkout, managed branch, expected branch head, disposition, and actor Run; only then does it emit
the terminal event. Publication records the same cleanup facts after the candidate is already
proven at canonical `HEAD`. Cleanup removes the worktree and branch in separately persisted steps.
A restart reconciles only effects proven by these facts. If a discard target changes after
authorization, cleanup stops with attention until the exact active owner Run inspects and
reauthorizes the new fingerprint. Cleanup authorization evidence is keyed by both cleanup id and
authorization revision; an older authorization event can never authorize a newer fingerprint.
Before any destructive cleanup, the shared Git registry parser must prove both directions of
identity: the recorded checkout path is registered to the exact transaction branch, and that branch
is not registered at another path. A matching content fingerprint cannot substitute for identity.

## 8. Legacy retirement cutover

There is no object migration and no schema conversion.

After all affected Agents stop, a plan first proves that its workspace is the exact Git root, then
binds workspace path, Git `HEAD`, staged/dirty/untracked path
sets, the complete legacy directory digest, active-object counts, unclassifiable legacy records,
archive destination, target protocol, exact cutover id, and event schema. Applying the plan requires
explicit confirmation that Agents stopped and no legacy writers remain. If the reviewed inventory
contains any active legacy object or unclassifiable record, applying also requires a separate
explicit confirmation that this potentially active authority may be retired.

The whole `.agent-coordination` directory moves byte-for-byte to an external archive. Git content,
index, worktree, and history are unchanged. Empty `20260812.1` state is initialized and the legacy
location receives a fail-closed tombstone.

```text
planned -> legacy-archived -> current-initialized -> tombstone-installed -> completed
```

Recovery reconciles the journal with actual filesystem facts at every step. A completed side effect
with a lagging journal advances only after exact digest or marker verification. The tombstone binds
the exact reviewed archive path as well as its digest and cutover id. Unknown legacy statuses are
unclassifiable authority, not assumed terminal. Contradictory facts fail closed. There is no
automatic rollback; an interrupted cutover is repaired and resumed. The legacy archive is retained
for audit, and any manual restoration is a separate user-authorized recovery operation.

The archive destination remains the exact absolute, non-symlink path reviewed in the plan; a later
resolution change fails before retirement. Tombstone creation is staged wholly inside the current
namespace and atomically renamed into the legacy path, so a stop between directory creation and
marker persistence cannot leave an ambiguous empty legacy control plane.

## 9. Observer and evolution

Observer discovers the exact state selected by `current.json`, validates the supported protocol and
event schema, event-specific required identity and time fields, snapshot kind, object identity,
status, path binding, and bounded source-file size. It keys evidence by
`(workspace_id, protocol_version, event_id)`. It never merges
legacy evidence into current authority and never writes coordination state.

Its first projection reports event and interaction counts, direct-commit and transaction outcomes, conflict hot
paths, live-stalled versus orphaned contention, orphaned Claim/transaction ownership, offered
handoffs whose source Run ended, cleanup attention, active/archive terminal gaps, immutable-event
integrity findings, and bounded cutover readiness. It separately reports closed Runs that invoked
the protocol but show no inter-Agent collaboration event. Open Runs are not classified as
non-collaborative merely because their work has not finished. This category is an overhead signal,
not an error and not proof that the Run did no useful work.

Cutover readiness means both authority quiescence and audit closure: there is no active authority,
no current collection failure, no immutable-source integrity finding, and no unresolved terminal
event/snapshot correlation gap. A diagnostic warning cannot coexist with a positive readiness
verdict merely because authority snapshots appear empty.

Readiness also fails when a current grant/start event has neither a matching live snapshot nor a
terminal counterpart, when mutually exclusive or duplicate terminal events exist, or when a known
workspace is not observed in the current scan generation. Observer never reconstructs authority
from those events. Exact integrity and readiness totals come from the catalog, independently of the
bounded finding sample returned to a UI. A never-admitted invalid source is a current collection
blocker only while still present; removing it preserves historical evidence without permanently
claiming the current tree is unhealthy.

The old Observer database may remain as a read-only backup. The first release starts a separate
catalog for `20260812.1`; cross-version continuity is not inferred.

Future contract changes publish a new exact `YYYYMMDD.x` version. Activated versions are immutable.
Garbage collection, history compaction, and cross-version browsing require separate explicit
contracts and are not normal cleanup.

## Activation criteria

This isolated candidate is not published merely because its tests pass. Activation requires:

1. review of this exact `20260812.1` contract, schemas, and producer code;
2. all `runtime/tests/` passing, including real Git checkout/publication and cutover recovery;
3. a real stop window with no legacy writers;
4. integration of the reviewed producer/skill and an independently stored Observer catalog;
5. one canary workspace completing join, direct Claim, managed direct commit, contention, and
   terminal cleanup checks;
6. explicit user approval before broader workspace cutover.

Once activated, `20260812.1` is immutable. Fixes made before first activation remain part of this
candidate; an incompatible change after activation publishes a new exact `YYYYMMDD.x` version.

## Stable control-plane failures

Machine-readable stable codes are limited to:

- `namespace_missing`
- `legacy_cutover_required`
- `split_brain`
- `unsupported_protocol`
- `marker_invalid`
- `state_missing`
- `stale_writer`
- `cutover_facts_changed`
- `cutover_confirmation_required`
- `git_fact_unavailable`
- `legacy_invalid`
- `lock_busy`
- `permission_denied`
- `read_only_filesystem`
- `missing_path`
- `already_exists`
- `os_error`
- `operation_failed`

OS permission, missing-parent, and read-only-filesystem failures retain their specific stable code.
Git-fact and legacy-input failures retain their protocol code. Other bounded validation and state
predicate failures use `operation_failed` with their human-readable message; none are rewritten as
collaboration contention. The producer has one exhaustive code registry, and an unregistered
`ProtocolError` code is itself rejected during development.
