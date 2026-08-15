# Recovery and Cutover

## Contents

- Fail closed first
- Recover a closed Run
- Reconcile durable intents
- Diagnose without deleting evidence
- Activate the protocol in a stop window

Load this reference only for `needs-attention`, a process crash or uncertain result, stranded
authority, cleanup recovery, or an explicitly authorized protocol cutover.

## Fail closed first

- Stop Git and authority mutations when facts are ambiguous.
- Preserve `.dev-mesh`, transaction checkouts, branches, atomic temporary residues, and event files.
- Do not use reset, clean, manual snapshot edits, branch deletion, or event rewriting as recovery.
- Use root `--verbose` only for the affected object; keep routine status compact and filtered.
- A failed command is not proof that its effect did not occur. Reconcile exact materialized and Git
  facts before retrying.

## Recover a closed Run

A newly joined Run of the same owner may recover recorded authority from a failed or abandoned Run:

```bash
python3 <skill>/scripts/coord.py --root ROOT run-recover-authority \
  --closed-run-id CLOSED_RUN --owner OWNER --recovery-run-id RECOVERY_RUN \
  --evidence "why the prior Run ended and how continuity was verified"
```

Recovery preflights every referenced object before changing one. It rebinds ordinary authority to
the same owner and records bounded lineage. A sealed direct-commit or contention terminal intent
keeps the original exact Run identity and must be completed by reconciliation, not rewritten.
The same rule applies to a Claim already in `completing`: recovery finishes its sealed Work Result
and terminal event instead of changing the recorded author Run.

When a Run cannot close normally, record the real terminal outcome and reason:

```bash
python3 <skill>/scripts/coord.py --root ROOT leave \
  --owner OWNER --run-id RUN --outcome failed --force-terminal \
  --reason-code PROCESS_LOST --summary "bounded failure summary"
```

Forced termination does not release Claims or other authority.

For a genuinely stopped task that cannot execute its own leave, the loopback Console offers an
operator-reviewed close. Review the materialized Run and bounded blocker list, enter the exact Run
id, and record a reviewer, outcome, reason, and evidence. The confirmation is bound to the preview
digest; if a heartbeat or authority object changes, preview again. A Run without authority may be
closed as completed. A Run with authority may only be failed or abandoned, and the authority is
preserved for same-owner `run-recover-authority` or its object-specific reconciler. The Console
action is not a stale-lock takeover or a bulk-delete mechanism.

## Reconcile durable intents

These operations are idempotent and fact-checked:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-reconcile

python3 <skill>/scripts/coord.py --root ROOT tx-reconcile \
  --steward STEWARD --steward-run-id STEWARD_RUN

python3 <skill>/scripts/coord.py --root ROOT direct-commit-reconcile \
  --steward STEWARD --steward-run-id STEWARD_RUN
```

Run the relevant command once, inspect its compact counts, and stop if attention is nonzero. Use
`--verbose` before the command only to inspect those exact attention records. Reconciliation may
finish an effect whose caller died because the Git child inherits its effect fence; it must not
guess from elapsed time.

## Diagnose without deleting evidence

Use the matching read-only doctor:

```bash
python3 <skill>/scripts/coord.py --root ROOT tx-doctor
python3 <skill>/scripts/coord.py --root ROOT direct-commit-doctor
```

Doctors report missing, orphaned, mismatched, symlinked, or residue facts. They do not delete them.
Observer may report missing/duplicate terminal evidence, invalid or missing sources, stale
collection, and cutover readiness; these remain diagnostics and never authorize repair.

## Activate or upgrade the protocol in a stop window

Cutover is fresh-start retirement, not object migration. Do not run it during ordinary Agent work. Read
`docs/CUTOVER.md` in the Dev Mesh repository before proceeding and require explicit user
authorization for every destructive or global step.

For a current `20260812.1` workspace, build the repository-local version plan:

```bash
python3 <skill>/scripts/coord.py --root ROOT --verbose version-cutover-plan \
  --cutover-id CUTOVER_ID
```

After every writer is stopped, apply and verify the exact digest. Retiring nonempty old authority
requires the explicit second confirmation:

```bash
python3 <skill>/scripts/coord.py --root ROOT version-cutover-apply \
  --cutover-id CUTOVER_ID --plan-digest PLAN_DIGEST \
  --confirm-agents-stopped --confirm-discard-old-authority

python3 <skill>/scripts/coord.py --root ROOT version-cutover-verify \
  --cutover-id CUTOVER_ID --plan-digest PLAN_DIGEST
```

For a pre-Dev-Mesh `.agent-coordination` workspace, build and review the legacy plan outside both
state roots:

```bash
python3 <skill>/scripts/coord.py --root ROOT --verbose cutover-plan \
  --archive-root EXTERNAL_ARCHIVE_ROOT --journal EXTERNAL_JOURNAL
```

After every affected Agent is stopped, no legacy writer can restart, and the exact digest is
reviewed, apply the same journal:

```bash
python3 <skill>/scripts/coord.py --root ROOT cutover-apply \
  --journal EXTERNAL_JOURNAL --plan-digest PLAN_DIGEST \
  --confirm-agents-stopped --confirm-no-legacy-writers
```

If the plan reports active legacy authority, also require an explicit decision to retire it and add
`--confirm-retire-active-authority`. Then verify:

```bash
python3 <skill>/scripts/coord.py --root ROOT cutover-verify \
  --journal EXTERNAL_JOURNAL --plan-digest PLAN_DIGEST
```

The exact Git root, protocol id, cutover id, archive destination, tombstone, and journal digest must
all match. A symlink or path-identity change fails closed. After verification, install/relink the new
skill and start only new Runs; do not import legacy Claims into the new authority plane.
