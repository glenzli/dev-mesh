# Dev Mesh 0.2.5

This release integrates the September 20 collaboration and Console changes with the earlier
Discovery maintenance update. The coordination protocol remains `20260823.1`; no workspace
cutover is required.

- Message records can bind an exact recipient Run. Pending Claims remain visible after a wait
  decision, with explicit guidance for the original participant to activate or release them.
- The Console provides folder browsing, grouped current work, searchable chronological
  collaboration history, clearer action feedback, consistent panel spacing and contained loading
  indicators.
- Discovery integrity checks restore missing publication under existing authority without
  rewriting healthy manifests. Conservative cleanup only removes eligible unreferenced,
  unbound Dev Mesh sockets. The service installer exposes the maintenance interval.

Detailed behavior and prior live UI evidence are recorded in
[the collaboration review](COLLABORATION_REVIEW_2026-09-20.md) and
[the Console review](CONSOLE_UX_REVIEW_2026-09-20.md). Their local-only delivery notes describe
the original implementation checkpoint; this release includes those changes for publication.

## Release verification

The full runtime test suite passed: **225 tests, no skips** with loopback and Unix sockets enabled.
Python compilation, plugin validation, both skill validators, syntax checks for all eight Console
JavaScript modules, CLI entry-point checks and `git diff --check` passed. The Console API, picker,
history and application code still match the recorded September 20 UI evidence; the later spacing
and loading-indicator changes have their own live validation in the Console review.

The package is built from the resulting clean source commit. Release metadata binds its source
revision, file count and tree digest. The marketplace snapshot and refreshed local plugin must
match every packaged file byte-for-byte. Release logs and identity evidence are retained under
`/private/tmp/dev-mesh-release-025-20260922/` (subject to OS temporary-file cleanup).
