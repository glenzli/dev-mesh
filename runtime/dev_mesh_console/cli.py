"""Command-line entry for the local Dev Mesh Console."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dev_mesh_coord.errors import error_json
from dev_mesh_observer.facility_status import build_facility_snapshot
from dev_mesh_observer.infra_discovery import ObserverFacilityService

from .registry import RootRegistry
from .server import ConsoleServer
from .state import ConsoleState


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="dev-mesh-console")
    result.add_argument("--db", required=True)
    result.add_argument("--registry")
    result.add_argument("--root", action="append", default=[])
    result.add_argument("--max-depth", type=int, default=5)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--collect-interval", type=float, default=15.0)
    result.add_argument(
        "--infra-runtime-dir",
        help="override the absolute Infra Discovery runtime root",
    )
    result.add_argument(
        "--no-infra-discovery",
        action="store_true",
        help="run without publishing dev-mesh.observer.status",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    server: ConsoleServer | None = None
    facility: ObserverFacilityService | None = None
    try:
        arguments = parser().parse_args(argv)
        database = Path(arguments.db).expanduser().resolve()
        registry_path = (
            Path(arguments.registry).expanduser().resolve()
            if arguments.registry
            else database.with_suffix(database.suffix + ".roots.json")
        )
        registry = RootRegistry(registry_path, [Path(item) for item in arguments.root])
        state = ConsoleState(
            database=database,
            registry=registry,
            max_depth=arguments.max_depth,
            collect_interval=arguments.collect_interval,
        )
        server = ConsoleServer(arguments.host, arguments.port, state)
        state.start()
        address, port = server.server_address[:2]
        console_url = f"http://{address}:{port}/"
        if not arguments.no_infra_discovery:
            runtime_root = (
                Path(arguments.infra_runtime_dir).expanduser()
                if arguments.infra_runtime_dir
                else None
            )
            facility = ObserverFacilityService(
                lambda service, sequence: build_facility_snapshot(
                    database=state.database,
                    collector=state.status(),
                    console_url=console_url,
                    service=service,
                    sequence=sequence,
                ),
                runtime_root=runtime_root,
            )
            facility.start()
            state.set_discovery_repair(facility.repair_publication)
        print(
            json.dumps(
                {
                    "kind": "dev-mesh.console.ready",
                    "url": console_url,
                    "database": str(database),
                    "roots": [str(item) for item in registry.roots()],
                    "infra_discovery": not arguments.no_infra_discovery,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(error_json(error), file=sys.stderr)
        return 1
    finally:
        if facility is not None:
            facility.stop()
        if server is not None:
            server.server_close()
