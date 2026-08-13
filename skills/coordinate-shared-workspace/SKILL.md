---
name: coordinate-shared-workspace
description: Coordinate concurrent Agents or tasks editing one local Git workspace with exact Runs and Claims, bounded status, managed direct commits, handoffs, contention decisions, and short-lived Git microtransactions. Use when work may overlap by path or semantic contract, shared dirty files must be preserved, Git index or canonical branch updates must be serialized, another Agent must acknowledge or receive work, or a crashed workflow needs auditable recovery.
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
- Events are immutable diagnostics. Materialized state under `.dev-mesh/coord/20260812.1/` is
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

5. Follow the returned `next_action`:

- `edit_and_validate_declared_scope`: edit only declared paths and run focused checks.
- `stop_overlap_writes_and_coordinate`: do not write the overlap; load
  [contention-and-transactions.md](references/contention-and-transactions.md).
- `wait_for_resume_condition`: preserve the Claim and follow its recorded condition.
- `preserve_state_and_inspect_verbose_recovery_facts`: stop mutation and load
  [recovery-and-cutover.md](references/recovery-and-cutover.md).

6. If the user authorized committing, publish validated direct work through the managed boundary:

```bash
python3 <skill>/scripts/coord.py --root ROOT direct-commit \
  --scope SCOPE --owner OWNER --run-id RUN \
  --summary "what changed" \
  --validation-evidence "checks and results"
```

This stages only declared changed paths, binds the exact intended tree before advancing the
canonical branch, and serializes the shared index/branch with transaction publication. If commit
was not authorized and the Claim remains dirty, do not release it merely to make status look clean;
retain or pause it with an honest checkpoint.

7. After the work is clean and complete, release and leave:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-release \
  --scope SCOPE --owner OWNER --run-id RUN --summary "completed result"

python3 <skill>/scripts/coord.py --root ROOT leave \
  --owner OWNER --run-id RUN --outcome completed --summary "completed result"
```

Do not leave `completed` while this Run still owns active authority. A failed or abandoned Run
retains its authority until an explicit same-owner recovery.

## Communicate without transferring authority

Dev Mesh messages are passive workspace-local records. `send`, acknowledgement, and handoff never
create, deliver to, start, resume, or wake a Codex task, and an Owner is not a Codex task address.
Use Codex task controls to contact the actual target first; record Dev Mesh correlation only after
the target task id is known.

Use `send --kind notice` for information and `send --kind request --requires-ack` for a decision.
Use a caller-supplied stable `--handoff-id` for `--kind handoff`; retry with the same id after an
uncertain result. Acknowledging a handoff records acceptance but does not silently transfer a
Claim. Load the contention reference for the full handoff sequence.

When a Codex task in another Git workspace is created, messaged, awaited, or handed development
work, load [cross-project-collaboration.md](references/cross-project-collaboration.md). Record one
stable relation after the target task id is known, let the receiver bind its exact workspace and
Run, and close the relation once. This optional extension is diagnostic only and is not needed for
ordinary single-workspace work.

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
`contracts/dev-mesh-coordination-20260812.1.md` in the Dev Mesh repository only when changing
the core protocol itself. Cross-project correlation is the separate compatible extension
`contracts/dev-mesh-cross-project-collaboration-20260813.1.md`.
