"""Command-line routing for the official dev-mesh Observer."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .catalog import discover_workspaces, ensure_external_data_dir
from .console import serve_console
from .operations import collect_registered, discover_and_collect, register_discovery
from .reports import build_report, parse_since
from .store import ObserverStore, default_data_dir


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def command_discover(arguments: argparse.Namespace) -> int:
    sources = discover_workspaces(arguments.roots, max_depth=arguments.max_depth)
    ensure_external_data_dir(arguments.data_dir, sources)
    with ObserverStore(arguments.data_dir) as store:
        _print(register_discovery(store, arguments.roots, sources))
    return 0


def command_collect(arguments: argparse.Namespace) -> int:
    with ObserverStore(arguments.data_dir) as store:
        if arguments.roots:
            result = discover_and_collect(
                store,
                roots=arguments.roots,
                max_depth=arguments.max_depth,
            )
        else:
            result = collect_registered(store, max_depth=arguments.max_depth)
        _print(result)
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    with ObserverStore(arguments.data_dir) as store:
        _print(store.status())
    return 0


def command_report(arguments: argparse.Namespace) -> int:
    with ObserverStore(arguments.data_dir) as store:
        _print(
            build_report(
                store.connection,
                since=parse_since(arguments.since),
                limit=arguments.limit,
            )
        )
    return 0


def command_serve(arguments: argparse.Namespace) -> int:
    serve_console(
        data_dir=arguments.data_dir,
        host=arguments.host,
        port=arguments.port,
        max_depth=arguments.max_depth,
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--data-dir", type=_path, default=default_data_dir())
    subparsers = result.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover")
    discover.add_argument("--roots", nargs="+", type=_path, required=True)
    discover.add_argument("--max-depth", type=int, default=5)
    discover.set_defaults(handler=command_discover)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--roots", nargs="*", type=_path, default=[])
    collect.add_argument("--max-depth", type=int, default=5)
    collect.set_defaults(handler=command_collect)

    status = subparsers.add_parser("status")
    status.set_defaults(handler=command_status)

    report = subparsers.add_parser("report")
    report.add_argument("--since", default="48h")
    report.add_argument("--limit", type=int, default=10)
    report.set_defaults(handler=command_report)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--max-depth", type=int, default=5)
    serve.set_defaults(handler=command_serve)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"observer error: {error}", file=sys.stderr)
        return 1
