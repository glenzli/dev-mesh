---
name: coordinate-shared-workspace
description: Coordinate concurrent Agents or tasks editing one local Git workspace with exact Runs and Claims, bounded status, managed direct commits, recorded handoffs, contention decisions, and short-lived Git microtransactions. Use when work may overlap by path or semantic contract, shared dirty files must be preserved, Git index or canonical branch updates must be serialized, another Agent must be contacted through real task controls and the action recorded, or a crashed workflow needs auditable recovery.
---

# Coordinate a Shared Workspace

Use the light direct path for ordinary work. Load the linked references only when their trigger
occurs; do not read the full protocol or immutable event directory during routine work.

## Preserve authority boundaries

- Never clean, revert, stage, commit, move, or discard another owner's work.
- A `pending-arbitration` Claim records intent but grants no write authority.
- Treat timestamps as diagnostics, never permission to take over authority.
- Treat sandbox, policy, approval, network, and tool failures as environment blockers, not Agent
  contention.
- Cooperating Agents must not run raw canonical `git add`, `git commit`, `git merge`, or ref
  updates. Use `direct-commit` or transaction publication. Read-only Git inspection is allowed.
- Events are immutable diagnostics. Materialized state under `.dev-mesh/coord/20260823.1/` is
  authoritative; Observer data never grants or reconstructs authority.
- Keep `.dev-mesh/` local unless the user explicitly authorizes committing it.

## Run the routine path

Use the repository-owned `python3 <skill>/scripts/coord.py` launcher. Replace uppercase placeholders
with stable, bounded identifiers; reuse one Run id only for the current Agent task in this workspace.

1. Find the exact Git root, read repository instructions, and inspect dirty state without changing
   it.
2. Join before claiming:

```bash
python3 <skill>/scripts/coord.py --root ROOT join \
  --owner OWNER --run-id RUN --task "bounded task"
```

3. Read only this Run's compact state:

```bash
python3 <skill>/scripts/coord.py --root ROOT status --owner OWNER --run-id RUN
```

Use unfiltered `status` only for a bounded workspace overview. Add root option `--verbose` before
the command only when an action reports `needs-attention`, recovery is required, or exact evidence
must be reviewed:

```bash
python3 <skill>/scripts/coord.py --root ROOT --verbose status --owner OWNER --run-id RUN
```

4. Claim exact likely write paths before editing:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim \
  --scope SCOPE --owner OWNER --run-id RUN \
  --task "bounded change" \
  --path src/example.py --path tests/test_example.py \
  --intent local-edit \
  --semantic-write api:example \
  --sensitive-to contract:example \
  --validation "focused tests" \
  --first-release "implementation and focused tests"
```

Use one bounded intent:

- `read` for read-only work;
- `local-edit` for a bounded existing unit;
- `semantic-edit` when same-file edits may be semantically independent;
- `exclusive-refactor` for moves, deletion, generation, or broad restructuring.

Declare semantic resources only when they affect overlap routing. Do not list incidental reads.

If the same Run already owns one Claim that fully covers the requested paths and semantic resources,
`claim` returns that existing Claim with `claim_reused: true`; use the returned `scope` and do not
create another lifecycle. If the existing Claim is narrower, extend that exact Claim with
`claim-update`. Same-Run overlap is scope hygiene, not Agent contention.

The default `--projection-mode git-tree` covers tracked and ordinary untracked source paths. For an
exact small regular file that is intentionally excluded by Git, opt in explicitly:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim \
  --scope SCOPE --owner OWNER --run-id RUN --task "update local data" \
  --path data/local.json --projection-mode workspace-bytes
```

`workspace-bytes` accepts only ignored, untracked regular files with at most 16 MiB total content.
It hashes bytes for Work Result and inherited-baseline evidence without storing the content. It
cannot use direct Git publication or `parallel-tx`; overlapping writers normally select wait and
release the small Claim quickly. Databases and external stores still require their own transaction.

5. Follow the returned `next_action`:

- `edit_and_validate_declared_scope`: edit only declared paths and run focused checks.
- `review_and_accept_inherited_baseline`: inspect the existing dirty work, then accept the exact
  offered digest before editing:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-baseline-accept \
  --scope SCOPE --owner OWNER --run-id RUN --baseline-sha256 DIGEST
```

- `review_changed_baseline_then_retry_accept`: the content, canonical revision, or branch changed
  during review. Inspect the newly returned baseline and invoke the same command once with its new
  `accept_baseline_sha256`; the first call deliberately grants no authority.

- `stop_overlap_writes_and_coordinate`: do not write the overlap; load
  [contention-and-transactions.md](references/contention-and-transactions.md).
- `wait_for_resume_condition`: preserve the Claim and follow its recorded condition.
- `preserve_state_and_inspect_verbose_recovery_facts`: stop mutation and load
  [recovery-and-cutover.md](references/recovery-and-cutover.md).

6. Finish editing independently of Git publication. Use the contribution-aware routine finish:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-finish \
  --result-id RESULT --scope SCOPE --owner OWNER --run-id RUN \
  --summary "what changed" \
  --validation-evidence "checks and results"

python3 <skill>/scripts/coord.py --root ROOT leave \
  --owner OWNER --run-id RUN --outcome completed --summary "completed result"
```

`claim-finish` creates an immutable, non-authoritative Work Result only when the Claim contributed
source bytes. If the paths are clean or still equal the accepted inherited baseline, it releases the
Claim without inventing a zero-change result. A later overlapping Claim must inspect and accept the
exact inherited dirty baseline before editing. Work Results are attribution and validation evidence,
not private branches, operation receipts, or rollback checkpoints. `claim-complete` remains the
exact low-level completion primitive for recovery and compatible callers.

An overlap is materialized directly as a non-authoritative `pending-arbitration` Claim; callers do
not need to predict overlap or add a special flag. A changed inherited baseline is also recoverable:
retrying `claim-baseline-accept` with the previously offered digest refreshes the Claim and returns
the one current `accept_baseline_sha256` to inspect and accept. Acceptance also binds the observed
canonical revision and branch for `git-tree`, so same-content Git drift still requires a second
review. `workspace-bytes` instead binds only the exact ignored-file projection; unrelated Git drift
does not force another acknowledgement. Neither mode grants authority to stale content.

If the user already authorized an immediate commit while the Claim is active, publish through the
managed boundary, then release the now-clean Claim and leave:

```bash
python3 <skill>/scripts/coord.py --root ROOT direct-commit \
  --scope SCOPE --owner OWNER --run-id RUN \
  --summary "what changed" \
  --validation-evidence "checks and results"
```

This stages only declared changed paths, binds the exact intended tree before advancing the
canonical branch, and serializes the shared index/branch with transaction publication.

Run the declared validation on the working bytes before publication. A commit is not a prerequisite
for building or testing, and creating one does not make unvalidated bytes authoritative.

Managed publication requires permission to write the repository's Git metadata. The producer
preflights that capability before creating a durable direct-commit intent. If the preflight is
denied, do not repeat the same command in the same restricted sandbox: obtain approved Git-write
execution, or record the validated dirty Work Result and leave while reporting that publication is
still pending. A permission failure that returns no `direct_commit_id` created no publication
authority and must not be described as an unrecoverable transaction.

If a command does return a `direct_commit_id` with `needs-attention`, preserve it and inspect
`direct-commit-doctor`. Reconcile it under an exact active steward Run with Git-write capability;
do not bypass it with raw Git. The durable record exists for ambiguous crash windows, not to make
ordinary task completion depend on a commit.

Pause is only for work that genuinely cannot proceed because of authorization, environment,
dependency, or an external resource. Record the blocker, checkpoint, and resume condition; never
use pause to mean complete, awaiting optional commit, handed off, or waiting for a Claim overlap.
Use the contention wait path for overlaps and `claim-finish` for finished routine work.

7. Clean or cancelled work may release without a Work Result, then leave:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-release \
  --scope SCOPE --owner OWNER --run-id RUN --summary "completed result"

python3 <skill>/scripts/coord.py --root ROOT leave \
  --owner OWNER --run-id RUN --outcome completed --summary "completed result"
```

Do not leave `completed` while this Run still owns active authority. A failed or abandoned Run
retains its authority until an explicit same-owner recovery.

## Execute communication, then record it

Dev Mesh is the execution record, not the communication transport. Treat `send` as
`record-message` and a handoff as `record-handoff-offer`:

1. Identify the real target task. Use the current environment's task, team, thread, or subagent
   control to actually send the notice, request, or handoff. Create or resume the target task first
   when necessary.
2. If that real communication action fails or is unavailable, stop and report that the target was
   not contacted. Do not run Dev Mesh `send` and do not claim that notification or handoff occurred.
3. After the real action succeeds, run Dev Mesh `send` to persist bounded correlation evidence.
   Instruct the receiver to run `ack` with its exact active Run when acknowledgement is required.

A successful Dev Mesh `send`, `ack`, or handoff command means only that a workspace-local record was
persisted. It never creates, delivers to, starts, resumes, or wakes a Codex task. `--target-owner` is
an authority identity, not a task address, and `--requires-ack` does not contact or poll the target.

After executing the real communication, use `send --kind notice` to record information and
`send --kind request --requires-ack` to record a decision request. Use a caller-supplied stable
`--handoff-id` for `--kind handoff`; retry with the same id after an uncertain recording result.
Acknowledging a handoff records acceptance but does not silently transfer a Claim. For dirty direct
work, the current owner creates a Work Result and the target creates its own Claim, reviews the
inherited work, and accepts the exact baseline digest. An active transaction may instead use
`tx-handoff`. The target must not infer edit authority from pause,
message delivery, or acceptance. Load the contention reference for the full handoff sequence.

When a Codex task in another Git workspace is created, messaged, awaited, or handed development
work, load [cross-project-collaboration.md](references/cross-project-collaboration.md). Record one
stable relation after the target task id is known, let the receiver bind its exact workspace and
Run, and close the relation once **before the closing participant leaves its Run**. If that close was
missed, use the target-side same-owner reconciliation described in the reference; never rewrite the
original binding. This optional extension is diagnostic only and is not needed for ordinary
single-workspace work.

Owner and Run identities are workspace-scoped. Matching Owner/Run text in two workspaces may be a
single Codex task visiting both projects, but it is never proof that two tasks collaborated. Do not
replace `cross-project-open` and receiver `bind` evidence with matching names or a local handoff.

## Keep routine context bounded

- Prefer filtered compact status and the command's `next_action` over reading state files.
- Do not ingest event JSON, Observer catalogs, full diffs, or unrelated Claims into the prompt
  unless diagnosing a concrete correlation.
- Heartbeats update snapshots without creating events. Send one only for genuinely long work, not
  per file edit or tool call.
- Use Observer reports to understand system behavior; never use them to decide write permission.

For exact protocol guarantees, consult
`contracts/dev-mesh-coordination-20260823.1.md` in the Dev Mesh repository only when changing
the core protocol itself. Cross-project correlation is the separate compatible extension
`contracts/dev-mesh-cross-project-collaboration-20260823.1.md`.
