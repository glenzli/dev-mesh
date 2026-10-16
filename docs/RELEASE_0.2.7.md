# Dev Mesh 0.2.7

This release reduces routine coordination work while keeping the coordination protocol at
`20260823.1` and event schema at `2`. Ordinary work uses the same commands, required arguments,
and confirmation steps; the routine coordination skill remains 129 lines.

- `claim-finish` can release the caller's unneeded pending Claim after its exact completed wait
  decision. It creates no Work Result, grants no authority, and does not change another writer's
  files or Claim. Successful retries recover the archived finish.
- Inherited dirty baselines automatically include bounded validation context from related Work
  Results. Declared-input matches and owner-reported evidence remain distinct from fresh test
  execution, dependency/environment coverage, and integration readiness.
- Existing version, environment, lock, and marker failures gain actionable guidance. Version
  mismatch reports the producer's writable version and observed workspace marker without changing
  workspace authority or selecting a cutover.

The implementation and exact boundaries are documented in
[the ergonomics review](PROTOCOL_ERGONOMICS_2026-10-16.md). Its complete 236-test gate passed, with
nine sandbox socket skips subsequently run successfully with local socket access. Runtime and test
file identities are unchanged for this release; that evidence is reused. The version-dependent
packaging check is run separately. Release packages are built from the clean source commit and
bound to its exact revision, file count, and tree digest before installation and marketplace sync.
