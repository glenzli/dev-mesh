# Dev Mesh 0.2.6

This patch release improves the Observer status path and completed direct-commit
archives, and clarifies a managed Git preflight error. The coordination protocol
remains `20260823.1`; no workspace cutover is required.

- Observer accepts bounded legacy completed direct-commit archives that exceed
  its ordinary 512 KiB snapshot limit. Newly completed direct commits retain
  path counts, digests, and samples in the archive instead of three full path
  lists. The active intent keeps full paths until completion, and the archive
  remains byte-stable after its atomic move.
- The Console computes the Infra Discovery facility summary during collection.
  Socket snapshot requests use that summary instead of reparsing the entire
  Observer catalog and rescanning event directories for each request.
- Managed Git distinguishes a nonempty index from a Git command failure and
  reports the latter with bounded stderr. The Claim and microtransaction path
  limits remain unchanged; ordinary directory-scoped commits can still include
  more than 128 changed files.

The runtime release gate covered 229 tests. Its only initial failure was a
package test that expected the previous version literally; after that test was
changed to read the source manifest, its focused rerun passed. Plugin structure
validation also passed. The restarted local Console completed three collection
cycles with no invalid records or discovery issues; five live status socket
requests returned healthy in approximately 0–1 ms each. These observations
verify the local service, not the Infra Sentinel window after its next refresh.
