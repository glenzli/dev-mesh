# Dev Mesh contracts

The files in this directory root are current contracts. Agents and integrations must not select a
contract by date alone: each protocol family versions independently.

## Current

- `dev-mesh-coordination-20260823.1.md` — active workspace authority and collaboration contract.
- `dev-mesh-cross-project-collaboration-20260823.1.md` — current optional cross-workspace evidence.
- `dev-mesh-observer-status-20260812.1.md` — current external Observer facility contract; its older
  date does not make it superseded.

## Archive

Selected released contracts that no longer grant writable state live in `archive/`. They are
immutable review evidence, not implementation choices. A deliberately discarded contract may live
only in Git history. Routine Agents, skills, producers, and Observers must not load either source.

Unreleased drafts are removed rather than archived; Git history is sufficient for them.
