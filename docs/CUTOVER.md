# Legacy retirement into `20260812.1`

This is a fresh-start control-plane retirement, not an object migration.

## Before the stop window

1. Finish and validate the implementation under `runtime/`.
2. Run the complete runtime test suite in temporary Git repositories.
3. Validate the canonical skill launcher against the repository-owned runtime without touching
   unrelated dirty work.
4. Prepare an owner-private archive root outside every workspace.

## Stop window

1. Stop all affected Agents and prevent new tasks from loading the old skill.
2. Generate a cutover plan for each workspace.
3. Review Git facts, active legacy object counts, unclassifiable-record count, exact cutover id,
   archive destination, and plan digest.
4. Apply with both explicit stop-window confirmations. If the reviewed inventory contains active
   authority objects, separately confirm their retirement.
5. Verify archive digest, tombstone, exact markers, and empty current authority.
6. Start a new Observer catalog and prove it collects one schema-1 event.
7. Restart Agents; each creates a new Run and redeclares only current work.

The isolated review commands are:

```bash
PYTHONPATH=runtime python3 -m dev_mesh_coord --root WORKSPACE --verbose cutover-plan \
  --archive-root EXTERNAL_ARCHIVE_ROOT --journal EXTERNAL_JOURNAL

PYTHONPATH=runtime python3 -m dev_mesh_coord --root WORKSPACE cutover-apply \
  --journal EXTERNAL_JOURNAL --plan-digest REVIEWED_DIGEST \
  --confirm-agents-stopped --confirm-no-legacy-writers \
  --confirm-retire-active-authority

PYTHONPATH=runtime python3 -m dev_mesh_coord --root WORKSPACE cutover-verify \
  --journal EXTERNAL_JOURNAL --plan-digest REVIEWED_DIGEST
```

Planning does not stop Agents and does not mutate coordination state. Applying is the authorized
retirement action; do not run it until the stop window is real. Omit
`--confirm-retire-active-authority` only when the reviewed plan reports both zero active legacy
objects and zero unclassifiable legacy records.

## Failure handling

Re-run the same cutover journal and expected plan digest. Recovery determines the completed step from
the exact archive digest, current markers, and tombstone. It never creates a second state directory,
rearchives legacy state, rewinds Git, or revives legacy authority.

The reviewed archive path must still be the same absolute non-symlink path at apply and verify time.
The legacy tombstone is built as a complete staging directory under the current namespace and then
renamed atomically; an interrupted staging write is resumed from the same cutover id rather than
exposing a writable empty legacy directory.

There is no automatic rollback. Manual restoration, if ever required before current activity, is a
separate user-approved recovery procedure based on the retained archive and current Git facts.
