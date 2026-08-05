---
name: coordinate-shared-workspace
description: Coordinate multiple agents or threads that concurrently edit one local Git workspace using direct claims, messages, handoffs, semantic contention arbitration, and short-lived Git microtransactions. Use when work may overlap by path or contract, the workspace contains shared dirty files, agents need write ownership or cross-thread handoff, a same-file edit may be safely parallelized, or Git index/HEAD publication and recovery must be serialized.
---

# Coordinate a Shared Workspace

Keep one canonical workspace. Use direct claims for the normal case and create a short-lived
microtransaction only when a clean overlapping scope is worth developing concurrently.

## Preserve hard boundaries

- Never clean, revert, reformat, stage, commit, or discard another owner's work.
- Never take over a possibly active claim or transaction without owner or user authorization.
- Serialize mutations to shared Git index/HEAD and canonical build outputs through one release
  steward.
- Resolve rebase or merge conflicts only in a transaction shadow checkout, never in the canonical
  workspace.
- Publish only a candidate that can fast-forward the current canonical `HEAD`.
- Treat an expired timestamp as diagnostic evidence, not transfer authority.

Keep `.agent-coordination/` local unless the user explicitly wants its history committed.

## Start or hot-join

1. Find the Git worktree root and read repository instructions.
2. Inspect `git status`, active claims, active transactions, action-required messages, and handoffs.
3. Choose one semantic scope, likely write paths, and a concrete first release.
4. Use an owner id that identifies the task/thread and agent.
5. Declare intent before writing.
6. Treat unexplained dirty or staged files as another owner's work.

Initialize direct coordination when needed:

```bash
python3 <skill>/scripts/coord.py init --root <workspace>
```

Initialize the transaction steward only when microtransactions may be used:

```bash
python3 <skill>/scripts/tx.py init --root <workspace> --steward <owner-id>
```

The transaction initializer adds the local coordination directory to Git's local exclude file.

## Declare semantic intent

Create a normal direct claim:

```bash
python3 <skill>/scripts/coord.py claim --root <workspace> \
  --scope route-health --owner agent-a \
  --task "Add the health route" \
  --paths src/router.ts tests/router.test.ts \
  --intent additive \
  --semantic-writes route:/health \
  --sensitive-to contract:http-routing \
  --validation "focused router tests" \
  --first-release "Health route and focused test pass"
```

Use these intents:

- `read` for a read-only snapshot;
- `additive` for an independent new entry or case;
- `local-edit` for a bounded existing semantic unit;
- `contract`, `refactor`, `move`, `delete`, or `generated` for changes that normally require
  ordered or exclusive treatment.

Declare `semantic-writes` when same-path work may be independent. Declare `sensitive-to` only for
resources whose change would invalidate the implementation or validation; do not list every file
read during exploration.

## Record overlap without granting write authority

If a second request overlaps, record it as pending and stop new writes on the overlap:

```bash
python3 <skill>/scripts/coord.py claim --root <workspace> \
  --scope route-metrics --owner agent-b \
  --task "Add the metrics route" \
  --paths src/router.ts tests/router.test.ts \
  --intent additive \
  --semantic-writes route:/metrics \
  --sensitive-to contract:http-routing \
  --validation "focused router tests" \
  --first-release "Metrics route and focused test pass" \
  --allow-overlap --pending-on-conflict \
  --reason "Pending semantic arbitration; do not write the overlap"
```

`pending-arbitration` records intent but grants no direct write authority.

## Choose the cheapest safe shape

Inspect claims before materializing anything:

```bash
python3 <skill>/scripts/tx.py inspect --root <workspace> \
  --scopes route-health route-metrics
```

Choose among:

- `direct` when no relevant physical or semantic overlap exists;
- `wait` when the current owner is near its first release;
- `handoff` when both tasks belong to one semantic change;
- `parallel-tx` when mergeable intents have disjoint semantic writes;
- `ordered-tx` when development can overlap but publication must follow a dependency;
- `exclusive` for related contract, refactor, move, delete, or generated changes.

Create a transaction only when expected waiting cost exceeds checkout, refresh, validation, and
likely rework cost. If semantic independence or the cost tradeoff is unclear, let the coordinating
agent decide and record its reason.

Do not promote claims after overlapping dirty writes exist. Let the current owner finish a short
slice, checkpoint, or hand off instead.

## Activate a microtransaction group

After every affected owner has stopped writing and acknowledged the arbitration decision, activate
the clean scopes:

```bash
python3 <skill>/scripts/tx.py begin --root <workspace> \
  --scopes route-health route-metrics \
  --mode parallel-tx \
  --steward coordinator-a \
  --reason "Independent route entries; parallel work is cheaper than waiting"
```

For `ordered-tx`, scope order becomes publication order. The command returns each transaction id,
owner, group id, branch, base revision, and checkout path. The coordinator persists the entire
group plan before creating any branch. Edit declared transaction paths only after the command
returns successfully; a partial group grants no write authority. Continue unrelated direct work
in the canonical workspace.

A queued or merely requested task must not own a checkout. A transaction belongs to one contention
slice, not to an agent's whole task.

## Prepare, validate, and publish

Let the helper create the one semantic candidate commit; do not manually accumulate feature-branch
history:

```bash
python3 <skill>/scripts/tx.py prepare --root <workspace> \
  --transaction <tx-id> --owner <owner-id> \
  --summary "Add the health route"

python3 <skill>/scripts/tx.py validate --root <workspace> \
  --transaction <tx-id> --owner <owner-id> \
  --evidence "Focused router tests passed"

python3 <skill>/scripts/tx.py publish --root <workspace> \
  --transaction <tx-id> --steward <steward-id>
```

`prepare` rejects out-of-scope tracked, untracked, renamed, or deleted paths. `validate` binds
evidence to the exact candidate and canonical base.

`publish` may return exit code 2 with JSON state:

- `prepared` means canonical `HEAD` advanced and the candidate was refreshed successfully; rerun
  relevant validation, then publish again.
- `conflicted` means the rebase stopped in the shadow checkout; resolve or hand off there, finish
  the Git rebase, then prepare and validate again.

Publication requires an empty canonical index. Unrelated unstaged dirty files may remain, but any
dirty or direct-claimed path overlapping the actual transaction diff blocks publication.

After a successful fast-forward, the helper writes a durable cleanup intent before archiving the
transaction, then removes its clean merged checkout and branch step by step. Recovery can resume
between any of those boundaries without repeating publication.

## Hand off or abort safely

Transfer a transaction with a concrete checkpoint:

```bash
python3 <skill>/scripts/tx.py handoff --root <workspace> \
  --transaction <tx-id> --owner agent-a --next-owner agent-c \
  --checkpoint "Candidate is prepared; rerun focused validation"

python3 <skill>/scripts/tx.py resume --root <workspace> \
  --transaction <tx-id> --owner agent-c
```

Abort only with explicit authorization to discard the coordinator-owned transaction:

```bash
python3 <skill>/scripts/tx.py abort --root <workspace> \
  --transaction <tx-id> --owner <owner-id> \
  --reason "Superseded by the canonical implementation" --discard
```

Never use abort as stale-claim takeover.

If group activation was interrupted before every source claim was promoted, abort the incomplete
group only after every listed owner explicitly agrees:

```bash
python3 <skill>/scripts/tx.py abort-group --root <workspace> \
  --group <group-id> --steward <steward-id> \
  --owners <owner-a> <owner-b> \
  --reason "All owners approved rollback of the incomplete group" --discard
```

The owner list must exactly match the group. This command persists an exact discard snapshot for
every member before deleting anything, completes cleanup, restores any claim archived before the
activation barrier, and then archives the group. Do not use it for a fully active group; each
transaction owner must use normal `abort` there.

## Recover and observe

Inspect current work:

```bash
python3 <skill>/scripts/coord.py status --root <workspace>
python3 <skill>/scripts/tx.py status --root <workspace>
python3 <skill>/scripts/tx.py doctor --root <workspace>
```

Reconcile interrupted group materialization, claim promotion, or publication:

```bash
python3 <skill>/scripts/tx.py reconcile --root <workspace> --steward <steward-id>
```

Treat reconcile output as follows:

- `activated` means every planned member was verified, the group barrier completed, and source
  claims were promoted;
- `unchanged` means the active group was already consistent; repeated reconcile is safe;
- `completed` means canonical `HEAD` already reached a publishing candidate and bookkeeping was
  repaired;
- `closed` means every group member was already terminal and an interrupted group archive was
  completed without rematerializing resources;
- `retryable` or `stale` applies to an interrupted publication whose expected `HEAD` did or did not
  remain current;
- `needs-attention` means Git facts are ambiguous or a checkout contains unexpected work. Preserve
  it and obtain owner or user direction; never reset, clean, remove, or rematerialize over it.

`doctor` is read-only. It reports orphan transaction branches, registered worktrees, unmanaged
checkout paths, missing expected resources, and cleanup journals requiring attention. It never
deletes or repairs them.

If a discard target changed after authorization, inspect the preserved checkout and obtain fresh
approval from its owner. Refresh only that cleanup authorization:

```bash
python3 <skill>/scripts/tx.py cleanup-authorize --root <workspace> \
  --transaction <tx-id> --owner <owner-id> \
  --reason "Owner reviewed the current checkout and approved discard" --discard
```

Then run `reconcile` again. Automatic recovery may consume a persisted cleanup snapshot, but it
must never create missing discard authority or broaden an earlier snapshot to include later work.

If `begin` exits before returning transaction records, do not edit any created checkout. Run
`reconcile`; write authority exists only when the group is Active and all source claims are marked
promoted.

Inspect event activity:

```bash
python3 <skill>/scripts/tx.py hotspots --root <workspace>
```

Repeated same-path transactions, ordered refreshes, conflicts, or scope expansion are architecture
and task-decomposition signals. Prefer improving semantic ownership over making automatic merge
more aggressive.

## Use direct coordination for non-transaction work

Continue using `scripts/coord.py` for heartbeat, pause/resume, messages, acknowledgements, takeover
requests, and release. An active claim freezes only its declared boundary. Waiting records never
grant write authority.

Before a direct release:

1. Validate the claimed scope.
2. Acquire short Git index/HEAD authority.
3. Stage exact owned paths or explicit hunks.
4. Inspect the cached diff.
5. Create one coherent commit and publish old/new `HEAD` plus evidence.
6. Release or checkpoint without touching unrelated dirty state.

## Load detailed design only when needed

Read [DESIGN.md](DESIGN.md) when changing the transaction protocol, evaluating a non-obvious
arbitration, implementing another checkout backend, auditing publish predicates, or recovering an
ambiguous crash. Ordinary direct claims and routine clean microtransactions should not require
loading the full design.
