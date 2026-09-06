---
name: coordinate-shared-workspace
description: Coordinate concurrent Agents or tasks editing one local Git workspace with exact Runs and Claims, bounded status, managed direct commits, recorded handoffs, contention decisions, and short-lived Git microtransactions. Use when work may overlap by path or semantic contract, shared dirty files must be preserved, Git index or canonical branch updates must be serialized, another Agent must be contacted through real task controls and the action recorded, or a crashed workflow needs auditable recovery.
---

# Coordinate a Shared Workspace

Use the routine path below. Load references only at their trigger; ordinary work does not need the
full protocol, event directory, Observer catalog, or other Runs' complete state.

## Authority boundaries

- Never clean, revert, stage, commit, move, or discard another owner's work.
- `pending-arbitration` and `pending-baseline` grant no write authority. Timestamps never permit
  takeover; sandbox, approval, network, and tool failures are environment blockers, not contention.
- Use managed `direct-commit` or transaction publication for canonical Git writes. Do not run raw
  canonical `git add`, `git commit`, `git merge`, or ref updates. Read-only Git inspection is allowed.
- Materialized state under `.dev-mesh/coord/20260823.1/` is authoritative. Events and Observer are
  diagnostics. Keep `.dev-mesh/` local unless the user explicitly authorizes committing it.

## Routine work

Use `python3 <skill>/scripts/coord.py` from this plugin. Replace uppercase placeholders with stable,
bounded identifiers; reuse a Run id only for this Agent task in this workspace.

1. Find the exact Git root, read repository instructions, and inspect dirty state without changing it.
   Read-only inspection alone does not require a Run or Claim; join when recording collaboration or
   preparing to write.
2. Join:

```bash
python3 <skill>/scripts/coord.py --root ROOT join \
  --owner OWNER --run-id RUN --task "bounded task"
```

A fresh Run returns `claim_declared_scope`: go directly to Claim. If it returns
`inspect_scoped_status_then_claim`, inspect the existing Run before continuing:

```bash
python3 <skill>/scripts/coord.py --root ROOT status --owner OWNER --run-id RUN
```

Use that filtered status when resuming or checking progress. Unfiltered status is a bounded workspace
overview; root `--verbose` is for exact evidence or recovery, not routine polling.

3. Claim the likely write paths:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim \
  --scope SCOPE --owner OWNER --run-id RUN --task "bounded change" \
  --path src/example.py --path tests/test_example.py \
  --validation "focused checks"
```

The default intent is `local-edit`. If `claim_reused: true`, use the returned scope. Extend a narrower
Claim with `claim-update`; do not create competing lifecycles for the same Run. For refactors,
semantic dependencies, changing scope, or ignored local files, load
[claim-options.md](references/claim-options.md). Semantic tags are optional blocking constraints,
not ordinary dependency notes.

4. Follow `next_action`:

- `edit_and_validate_declared_scope`: edit only declared paths and run focused checks.
- `review_and_accept_inherited_baseline`: inspect the inherited dirty work, then accept the exact
  returned `accept_baseline_sha256` before writing:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-baseline-accept \
  --scope SCOPE --owner OWNER --run-id RUN --baseline-sha256 DIGEST
```

- `review_changed_baseline_then_retry_accept`: content, canonical revision, or branch changed during
  review. Inspect the refreshed baseline and retry with its new digest; the first call granted no
  authority. Never accept a stale or unreviewed digest.
- `stop_overlap_writes_and_coordinate`: inspect the bounded `conflicts` summary and load
  [contention-and-transactions.md](references/contention-and-transactions.md). A semantic dependency
  is not resolved just by reducing file paths.
- `wait_for_resume_condition`: preserve the Claim and follow its recorded condition.
- `preserve_state_and_inspect_verbose_recovery_facts`: stop mutation and load
  [recovery-and-cutover.md](references/recovery-and-cutover.md).

5. Finish validated work and leave:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-finish \
  --result-id RESULT --scope SCOPE --owner OWNER --run-id RUN \
  --summary "what changed" --validation-evidence "checks and results"

python3 <skill>/scripts/coord.py --root ROOT leave \
  --owner OWNER --run-id RUN --outcome completed --summary "completed result"
```

`claim-finish` records a Work Result only for a contribution, then releases the Claim. Clean paths or
bytes equal to the accepted baseline release without a zero-change result. Work Results are
attribution and validation evidence, not rollback checkpoints or private branches. A later writer
must review and accept inherited dirty work. `claim-complete` remains the low-level recovery primitive.

If an immediate commit is already authorized, use
[direct-publication.md](references/direct-publication.md) before finishing the active Claim. Building
and testing do not require a commit. Do not pause completed work to await an optional commit.

For clean or cancelled work, `claim-release --scope SCOPE --owner OWNER --run-id RUN --summary TEXT`
may precede `leave`. Never leave completed while owning active authority. Failed or abandoned Runs
retain authority until explicit same-owner recovery. Pause is for a real environment, authorization,
dependency, or external-resource blocker; use contention wait for overlap and finish for completed work.

## Communication and additional workflows

- Before contacting another task or recording a handoff, load
  [communication.md](references/communication.md). Use the actual task tool first, then
  `record-message` (compatible alias: `send`). Recording does not deliver or wake a task.
- For work between tasks in different Git workspaces, load
  [cross-project-collaboration.md](references/cross-project-collaboration.md). Record one relation,
  let the receiver bind it, and have the target close before leaving. Matching Owner/Run text across
  workspaces does not establish a relation.
- For crashes, ambiguous durable intents, stranded authority, or explicitly authorized cutover, load
  [recovery-and-cutover.md](references/recovery-and-cutover.md).
- Heartbeats only refresh snapshots; use them for genuinely long work, not per edit or tool call.

CLI aliases and compact projections do not change the `20260823.1` protocol. An older workspace
requires its own cutover assessment; never update its pointer as an ordinary task step. Read the
repository's `contracts/dev-mesh-coordination-20260823.1.md` only when changing core guarantees.
