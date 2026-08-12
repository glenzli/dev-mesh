# Dev Mesh

Dev Mesh provides auditable coordination for multiple Agents working in one shared Git workspace.
The current protocol is `dev-mesh.coordination@20260812.1`.

## Repository map

- `runtime/dev_mesh_coord/` — authority-bearing coordination and recoverable Git operations.
- `runtime/dev_mesh_observer/` — read-only catalog, diagnostics, and reports.
- `runtime/dev_mesh_console/` — loopback-only API, collection lifecycle, and browser dashboard.
- `runtime/tests/` — protocol, crash-window, concurrency, Observer, and cutover tests.
- `skills/coordinate-shared-workspace/` — globally linked Agent skill and runtime launcher.
- `skills/observe-dev-mesh/` — thin read-only launchers for the Observer and local Console.
- `contracts/` — normative coordination and facility contracts.
- `schemas/` — versioned JSON envelopes.
- `docs/CUTOVER.md` — fresh-start retirement of `.agent-coordination` into `.dev-mesh`.
- `DESIGN.md` — architecture boundaries and navigation.

## Validate

```bash
PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -v
PYTHONPATH=runtime python3 -m compileall -q runtime/dev_mesh_coord runtime/dev_mesh_observer runtime/dev_mesh_console runtime/tests
python3 skills/coordinate-shared-workspace/scripts/coord.py --help
python3 skills/observe-dev-mesh/scripts/console.py --help
```

## Keep the Console running on macOS

Install the repository-owned LaunchAgent after moving the checkout or changing its Python runtime:

```bash
python3 scripts/install_console_service.py install
python3 scripts/install_console_service.py status
```

The service keeps `http://127.0.0.1:8765/` available across Agent tasks and restarts after an
unexpected exit. By default it also publishes the redacted `dev-mesh.observer.status@20260812.1`
Unix-socket offer through `infra.discovery.registration@20260812.1`. A retained registration is
only a discovery candidate; consumers establish liveness by connecting to its current endpoint.

The coordination state and legacy tombstone are workspace-local and excluded from Git. Observer
catalogs live outside observed workspaces. Retired v1 source remains available through Git history;
the last uncommitted Console assets were preserved outside the repository during cutover.
