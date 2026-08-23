# `dev-mesh.coordination@20260823.1`

Status: active

Event schema: `2`

## 1. Purpose

This protocol coordinates many cooperating Agents in one visible workspace. It deliberately exposes
shared context and overlap early instead of making a long-lived branch the default isolation unit.
Claims protect active editing; they do not exist to keep uncommitted work locked until somebody
chooses to create a Git commit.

The protocol owns workspace-local Runs, Claims, contention decisions, Work Results, bounded
interaction evidence, short Git transactions, and serialized canonical publication. It does not own
Agent conversation transport, arbitrary writes made outside the producer, automatic semantic merge,
or general source rollback.

Git content and the current workspace remain canonical. Observer output is diagnostic and never
grants authority.

## 2. Exact state identity

The one writable state is selected by `.dev-mesh/coord/current.json` and lives at:

```text
.dev-mesh/coord/20260823.1/
```

The marker, state protocol record, producer, and event schema must agree on:

```json
{
  "protocol": "dev-mesh.coordination",
  "version": "20260823.1",
  "event_schema": 2,
  "state": "20260823.1"
}
```

The state contains Runs, Claims, Work Results, interactions, contentions, suspended work,
transactions, direct commits, cleanup, checkouts, immutable events, and locks. Work Results live in
`work-results/`; they are evidence, not authority.

Only the selected version is writable. Retired `20260814.1` authority is discarded only through a
reviewed cutover after a bounded, authority-free analysis record is retained. Activated exact versions
are immutable; future incompatible semantics publish another
`YYYYMMDD.x` version.

## 3. Authority model

- A Run identifies one Agent task in one workspace.
- A writable Claim grants only its declared paths and semantic resources.
- `pending-arbitration` and `pending-baseline` Claims grant no write authority.
- `completing` is a durable terminal intent. It retains authority only until reconciliation finishes
  the already-authorized completion.
- A read Claim never blocks a writer.
- Timestamps and staleness are diagnostics, never takeover authority.
- Events are immutable evidence. Materialized state is authoritative between multi-file writes.
- Failed or abandoned Runs never release authority implicitly; same-owner recovery remains explicit.
- Cooperating Agents never mutate the canonical Git index or branch directly. They use managed
  publication or transaction publication.

All authority mutations validate markers, take the operation lock, revalidate markers, validate the
exact owner/Run/object state, persist a recoverable materialized intent when more than one write is
needed, and append one bounded event.

## 4. Routine lifecycle

The normal no-conflict path is:

```text
agent-join
  -> claim create or same-Run reuse
  -> edit
  -> validate
  -> claim-finish
       -> changed source: Work Result + Claim released
       -> unchanged source: Claim released
  -> agent-leave completed
```

Commit is deliberately absent from this mandatory lifecycle.

### Claim creation

A Claim declares at most 128 normalized workspace paths, one intent (`read`, `local-edit`,
`semantic-edit`, or `exclusive-refactor`), and one immutable projection mode. `git-tree` is the
default. `workspace-bytes` is an explicit non-Git mode for exact ignored regular-file paths.
Semantic write and sensitivity sets are bounded to 64.

`workspace-bytes` is deliberately narrow. Every declared path must be untracked and matched by a
Git ignore rule; directories, symlinks, devices, tracked paths, and more than 16 MiB of total file
content fail closed. Missing ignored files are valid so an Agent may claim a file before creating
it. The producer hashes exact bytes under the operation lock and stores only path identity, size,
digest, bounded counts, and validation evidence. It never copies file content into protocol state.
The mode is invalid for read Claims, Git publication, and `parallel-tx`; overlapping writers wait,
handoff, or retain exclusive authority instead.

If the same exact Owner/Run already owns one Claim that covers every requested path and semantic
resource with the same intent and projection mode, `claim` returns that existing Claim with
`claim_reused: true`. It creates no Claim, event, or contention. The caller continues with the
returned canonical scope. A narrower same-Run Claim must be extended explicitly with `claim-update`;
it never becomes self-contention.

If authority owned by another exact Run overlaps, the Claim becomes `pending-arbitration` with no
write authority and opens one bounded contention. Callers do not need to predict overlap or opt into
pending state. One further contender is rejected while that contention is in flight, preventing an
unbounded queue on one hotspot.

If no authority overlaps but declared writable paths already contain uncommitted Git work or an
existing `workspace-bytes` file, the Claim becomes `pending-baseline`. Its record contains:

- the observed canonical revision and branch;
- dirty paths;
- an exact declared-content digest independent of unrelated paths;
- bounded changed-path count, digest, and sample;
- up to 16 related Work Result ids.

The Agent must inspect the inherited work and call `claim-baseline-accept` with the exact offered
digest. Acceptance reprojects the paths under the operation lock. For `git-tree`, the content
digest, observed canonical revision, and canonical branch form one baseline identity. For
`workspace-bytes`, only the exact ignored-file projection is authoritative; an unrelated Git commit
does not force another acknowledgement. If an authoritative field changed, the Claim stays
non-authoritative, records a refreshed baseline, and returns the current digest to inspect and
accept again; it does not require release and re-claim. Only the exact unchanged baseline identity
activates the Claim. The grant is sealed as a durable activation intent before
its immutable event and active snapshot are finalized, so recovery cannot create an unaudited
authority grant. Baseline refresh is snapshot-only because it grants no authority; heartbeats and
repeated refresh checks likewise create no immutable event. This extra step occurs only for
inherited dirty paths.

### Completion

`claim-finish` is the routine terminal operation. It requires an exact active writable Claim, a
caller-supplied stable finish id through `--result-id`, a bounded summary, and validation evidence.
It does not require a Git commit.

For `git-tree`, the producer projects declared paths using a temporary Git index without mutating
the canonical index. A declared future path that is absent and not yet present in the base tree is
an empty projection input rather than a Git pathspec failure. For `workspace-bytes`, it compares the
accepted starting file fingerprints with a fresh exact projection.

If the projection contains no changed path, or still equals the exact inherited baseline accepted by
this Claim, `claim-finish` emits one `claim-released` event and archives the Claim without creating a
Work Result. Exact retries return that same archived finish and cannot create a later result.

If declared source differs from the Claim's accepted start, `claim-finish` records:

- source Claim, owner, and Run;
- declared paths and semantic resources;
- Claim base revision and completion-time canonical revision;
- completion-time branch;
- dirty paths;
- projection mode and either the exact intended Git tree or the exact workspace-byte digest;
- declared-content digest;
- actual changed-path count, digest, and bounded sample;
- summary and validation evidence.

Before emitting the changed-source terminal event, the Claim becomes `completing` and embeds the exact Work Result
and terminal event intent. A retry with the same result id fills a missing result or event and
archives the Claim. A different result id fails closed. The completed Claim archive and Work Result
are deterministic, so repeated completion cannot create two results or two terminal events.

The resulting `claim-completed` event has authority effect `release`. Once the Claim is archived,
the Run may leave `completed` even while the workspace remains dirty.

A clean writable Claim may still use `claim-release` for cancellation. A dirty writable Claim uses
`claim-finish`; raw release cannot silently orphan dirty changes. `claim-complete` remains the exact
low-level changed-source completion primitive for compatible recovery callers. Read Claims release
directly.

### Pause

Pause means active work is genuinely unable to proceed. It records the blocker kind, checkpoint,
resume condition, and required environment/authorization evidence. Completion is never represented
as pause. A paused Claim continues to own its declared paths until resumed, recovered, or explicitly
terminated by a future compatible operation.

## 5. Work Results and publication

A Work Result is immutable, non-authoritative collaboration context. It does not lock paths, keep a
Run alive, promise a private rollback point, or claim that its bytes have been committed.

Later Agents may edit on top of it after exact dirty-baseline acknowledgement. This can make an older
result no longer independently publishable; that is expected in fast collaboration. The result
remains attribution and validation evidence for the combined state.

`publish-results` is an optional independent operation for `git-tree` Work Results. A
`workspace-bytes` Work Result is intentionally non-publishable. A contention containing any
`workspace-bytes` Claim rejects `parallel-tx` before a decision is recorded; participants select
wait, handoff, or exclusive coordination instead. Completion requires:

- an exact active publisher Run;
- one to 64 Work Result ids;
- a bounded commit summary and validation evidence;
- an empty canonical Git index;
- no unresolved canonical publication effect.

It unions at most 128 declared result paths, projects the current shared bytes, stages only those
paths, creates a candidate commit with `commit-tree`, and advances the canonical branch through an
exact compare-and-swap. The publication records the consumed Work Result ids. It publishes the
current combined state of those paths; it does not falsely recreate an older private diff.

The existing `direct-commit` path remains available while an Agent still owns an active writable
Claim. Both publication paths share the same workspace-wide inherited-FD canonical Git fence with
transaction publication. A Git child that outlives its caller cannot race another canonical effect.

Within a Git workspace, Claim completion does not require creating a Git commit. Work Results and
dirty-baseline acknowledgement therefore remain usable when publication is intentionally deferred.

## 6. Contention, handoff, and short transactions

Contention routing and short transaction safety remain intentionally narrow:

- `wait` is selected unilaterally by the pending trigger Claim because it constrains only that
  non-authoritative participant. After the active Claim releases, `claim-activate` rechecks overlap
  and either activates or requires dirty-baseline acknowledgement;
- `handoff` transfers responsibility only after real task communication and explicit acceptance;
- `parallel-tx` is a branch offload: it creates one short-lived isolated checkout for one accepted
  pending Claim while the established owner remains on the canonical workspace. Every participant
  must declare semantic writes, and the declared resources must be independent;
- `exclusive` keeps the current owner authoritative until release.

The generic coordinator proposal, exact participant responses, and enact sequence is reserved for
the shared `handoff`, `parallel-tx`, and `exclusive` decisions. Local scope decomposition is not a
contention decision: the pending Agent cancels/releases its pending intent and declares a narrower
non-overlapping Claim.

Messages are records, not transport. The Agent must actually contact the target task first and only
then record the interaction. Message acknowledgement never grants Claim authority.

Clean work can be released and reclaimed. Completed dirty direct work is completed into a Work
Result; the receiving Agent then claims the current dirty baseline and explicitly accepts it. An
active transaction still uses `tx-handoff`, because its checkout and candidate identity are durable
transaction authority rather than shared direct work.

Transactions retain exact Run fencing, one-candidate validation, declared-path containment,
canonical publication serialization, inherited Git-effect fencing, crash-reconciled refresh,
two-phase destructive cleanup, and fail-closed checkout/branch identity checks from `20260812.1`.

## 7. Recovery boundary

This protocol separates three concepts:

- collaboration continuity: Work Results and accepted dirty baselines;
- publication recovery: durable direct-commit and transaction effects;
- source rollback: optional risk control outside the routine Claim lifecycle.

Work Results are not advertised as rollback snapshots. High-risk refactors may explicitly choose a
short transaction or a future bounded patch/tree checkpoint. The default path does not create a
long-lived branch merely to make every Agent modification privately reversible.

Recovery never derives authority from Observer data or immutable events. It uses exact materialized
intent, Git facts, owner/run lineage, and bounded identity evidence.

An operator may close an anomalous active Run only through a two-step reviewed operation. Preview
reads the current materialized Run and active authority under the operation lock and returns a
digest binding both. Confirmation must present that exact digest, a reviewer identity, outcome,
reason code, and bounded evidence; any intervening heartbeat or authority change invalidates the
review. The terminal record remains an `agent-left` event with `closure_kind=operator-reviewed` and
explicit operator evidence. A reviewed Run with no active authority may close as completed, failed,
or abandoned. A Run that still references authority may close only as failed or abandoned; its
Claims, transactions, Git intents, and other authority remain intact for same-owner recovery or
their specific reconciler. Reviewed closure never treats staleness as takeover permission and never
discards workspace bytes.

## 8. Events

Event schema `2` adds:

- `claim-baseline-required` (`none`);
- `claim-baseline-accepted` (`grant`);
- `claim-completed` (`release`).

All `20260814.1` event names retain their authority classification. Event files are bounded to
256 KiB and use exact ids. Heartbeats mutate only snapshots and emit no event. Work Result ids in
events are bounded; full result metadata remains in the result file.

## 9. Observer semantics

Observer collects only the exact selected `20260823.1` state. It validates schema-2 events,
`pending-baseline` and `completing` Claims, and immutable Work Results. It reports separately:

- active edit authority;
- Claims awaiting baseline acknowledgement;
- completion intents awaiting reconciliation;
- completed Work Results;
- direct and transaction publication outcomes;
- contention, interaction, recovery, integrity, and audit gaps.

A Work Result never counts as active authority or blocks cutover readiness. A `pending-baseline` or
`completing` Claim does. Observer never reconstructs a missing Claim from a Work Result or event.

The external `dev-mesh.observer.status@20260812.1` facility contract remains compatible because its
bounded aggregate schema does not change. Its implementation queries coordination version
`20260823.1`; Infra Discovery registration remains `infra.discovery.registration@20260812.1`.

## 10. Version cutover

The supported cutover source is exact `20260814.1`. A reviewed plan binds:

- source and target versions and event schemas;
- exact source-state tree digest;
- active authority inventory;
- canonical branch, HEAD, index, dirty/untracked paths, and worktree/index diff digests;
- caller-supplied stable cutover id;
- the digest, counts, and path of one bounded analysis-retention record.

Application requires explicit confirmation that old writers stopped and that the entire old state
may be discarded. Before destruction, the producer writes one deterministic record under
`.dev-mesh/coord/analysis/<cutover-id>/20260814.1.json`. It retains at most 4096 recent immutable
event envelopes plus full event-kind counts. Message bodies, task and reason text, paths, diffs,
validation evidence, and every authority snapshot are omitted. The record has `authority: none` and
is never loaded by the producer or normal Observer collection.

Any earlier full tree under `.dev-mesh/coord/archive/` is separately digest-bound by the plan and
discarded at the same controlled point. It is not copied into the retained analysis record.

After the retained record is digest-verified, the source tree moves to a private discard staging
directory. No object is translated into new authority. The new empty state contains a
non-authoritative `cutover-baseline.json` binding the discarded source digest, retained analysis
record, and unchanged Git facts. The current selector switches only after target initialization;
the private staging tree is then destroyed. Every phase is journaled and retryable. Verification
requires source and staging absence, the exact retained record, target markers, baseline, plan
digest, and unchanged Git facts.

No user source file, index entry, branch, commit, or untracked product file is deleted or rewritten
by version cutover.

## 11. Activation criteria

Activation requires:

1. exact contract, marker, and schema review;
2. focused Work Result, inherited-baseline, independent publication, contention, transaction, and
   cutover tests;
3. crash-window tests for completion event gaps and version-cutover phases;
4. full runtime suite;
5. one real stopped-writer cutover preserving exact Git facts;
6. a canary using contribution-aware finish for both unchanged and changed source, leaving
   successfully, accepting a dirty baseline in another Run, and optionally publishing it;
7. Observer and Console verification against the live new state.

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
Git-fact and retired-state failures retain their protocol code. Other bounded validation and state
predicate failures use `operation_failed` with their human-readable message; none are rewritten as
collaboration contention. The producer has one exhaustive code registry, and an unregistered
`ProtocolError` code is itself rejected during development.
