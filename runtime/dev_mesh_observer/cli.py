"""Read-only collection and reporting CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dev_mesh_coord.errors import error_json

from .catalog import Catalog


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="dev-mesh-observer")
    result.add_argument("--db", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--root", action="append", required=True)
    collect.add_argument("--max-depth", type=int, default=5)
    report = commands.add_parser("report")
    report.add_argument("--workspace")
    report.add_argument("--stale-after-seconds", type=int, default=1800)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        with Catalog(Path(arguments.db)) as catalog:
            if arguments.command == "collect":
                value = catalog.collect_roots(
                    [Path(item) for item in arguments.root], max_depth=arguments.max_depth
                )
            else:
                value = catalog.report(
                    workspace=arguments.workspace,
                    stale_after_seconds=arguments.stale_after_seconds,
                )
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(error_json(error), file=sys.stderr)
        return 1
