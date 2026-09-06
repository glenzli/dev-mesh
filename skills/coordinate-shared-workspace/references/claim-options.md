# Claim scope and optional constraints

Load when extending a Claim, refactoring, declaring a blocking semantic dependency, or editing an
ignored local file. Ordinary local edits need only exact paths and focused validation.

## Intent and scope

- `read`: an observed read-only participation; standalone inspection needs no Claim.
- `local-edit`: bounded edits to an existing unit; the default.
- `semantic-edit`: same-file edits whose semantic independence may matter to overlap routing.
- `exclusive-refactor`: moves, deletion, generation, or broad restructuring.

A same-Run Claim covering the entire request is reused. Extend a narrower Claim with `claim-update`.
The supplied path and semantic lists replace those fields, so include the complete intended scope:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-update \
  --scope SCOPE --owner OWNER --run-id RUN \
  --path src/example.py --path tests/test_example.py --path docs/example.md
```

Declare likely writes, not incidental reads. `--first-release` is optional planning text, useful
when another participant needs to know the first bounded deliverable; ordinary work can omit it.

## Semantic resources are blocking constraints

`--semantic-write api:example` declares a contract being changed.
`--sensitive-to api:example` declares that concurrent changes to that contract must block this work.
Either can create contention even with zero overlapping files. Use them only when needed for overlap
routing. A routine import, related feature, or dependency to revalidate later is not by itself a
reason to declare `sensitive-to`.

A compact conflict returns the other Owner/Run/Scope, `physical_overlap_count`, bounded
`semantic_resources`, and a routing hint. If only a semantic resource overlaps, removing a file path
will not help. Review whether the dependency really requires serialized work; if it does, wait or
coordinate its contract. Do not delete a real constraint merely to obtain authority. Decomposition
releases only this pending intent; follow the contention reference before reclaiming.

## Ignored local files

The default `--projection-mode git-tree` covers tracked and ordinary untracked source paths. For an
intentionally ignored, untracked regular file, opt in:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim \
  --scope SCOPE --owner OWNER --run-id RUN --task "update local data" \
  --path data/local.json --projection-mode workspace-bytes
```

`workspace-bytes` accepts at most 16 MiB total content. It hashes exact bytes without storing content,
and cannot use direct Git publication or `parallel-tx`. Overlapping writers normally wait and release
small Claims quickly. Databases and external stores require their own transaction.

For `git-tree`, baseline acceptance binds the observed canonical revision and branch as well as
content. Same-content Git drift still needs another review. `workspace-bytes` binds only its exact
ignored-file projection; unrelated Git drift does not force another acknowledgement.
