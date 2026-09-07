# Publish validated direct work

Load when the user has authorized a commit. Run declared validation on the working bytes first;
Git publication does not make unvalidated bytes authoritative.

While the Claim is active:

```bash
python3 <skill>/scripts/coord.py --root ROOT direct-commit \
  --scope SCOPE --owner OWNER --run-id RUN \
  --summary "what changed" --validation-evidence "checks and results"

python3 <skill>/scripts/coord.py --root ROOT claim-release \
  --scope SCOPE --owner OWNER --run-id RUN --summary "validated work committed"

python3 <skill>/scripts/coord.py --root ROOT leave \
  --owner OWNER --run-id RUN --outcome completed --summary "completed result"
```

`direct-commit` stages only materialized intended paths, binds the exact tree, and serializes the
shared index/branch with transaction publication. Verify the resulting Git revision, intended diff,
and remaining dirty state. A completed coordination record alone is not proof of source coverage.
Commit authorization does not authorize a push.

The 128-path Claim limit counts declared files or directories, not the changed files inside a
declared directory. Ordinary direct commits and Work Result publication can include larger file
sets without splitting the commit. Choose directory scopes that match ownership; microtransactions
retain their separate 128 changed-file limit.

## Git metadata permissions and recovery

The producer preflights Git-write capability before creating a durable direct-commit intent. If
permission is denied, do not repeat the command in the same restricted sandbox. Obtain approved
Git-write execution, or finish the validated dirty Work Result and leave with publication pending.
A failure without `direct_commit_id` created no publication authority; it is not an unrecoverable
transaction. Do not bypass the managed boundary with raw Git.

If a returned `direct_commit_id` reports `needs-attention`, preserve it and inspect
`direct-commit-doctor`. Load [recovery-and-cutover.md](recovery-and-cutover.md) and reconcile under an
exact active steward Run with Git-write capability. The durable record covers ambiguous crash
windows; it does not make ordinary completion depend on a commit.

## Plugin release identity

When publishing Dev Mesh itself, build from the validated clean source revision with
`scripts/plugin_dist.py`. Check release metadata `source_revision` and `tree_sha256`, then verify the
installed runtime and skill bytes against that package. A matching manifest version alone cannot
show that the installed producer includes a source fix. CLI-only compatible changes need a plugin
release, not a control-plane version change or a workspace cutover.
