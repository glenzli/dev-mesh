"""Command-line routing for semantic Git microtransactions."""

from __future__ import annotations

import argparse
import sys

from . import git_backend as git
from .state import TRANSACTION_MODES, root_path
from .transactions import (
    command_abort,
    command_begin,
    command_handoff,
    command_hotspots,
    command_init,
    command_inspect,
    command_prepare,
    command_publish,
    command_reconcile,
    command_resume,
    command_status,
    command_validate,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    def add_root(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--root", type=root_path, default=root_path("."))
        subparser.add_argument("--state-dir", default=".agent-coordination")

    init_parser = subparsers.add_parser("init")
    add_root(init_parser)
    init_parser.add_argument("--steward", required=True)
    init_parser.set_defaults(handler=command_init)

    inspect_parser = subparsers.add_parser("inspect")
    add_root(inspect_parser)
    inspect_parser.add_argument("--scopes", nargs="+", required=True)
    inspect_parser.set_defaults(handler=command_inspect)

    begin_parser = subparsers.add_parser("begin")
    add_root(begin_parser)
    begin_parser.add_argument("--scopes", nargs="+", required=True)
    begin_parser.add_argument("--mode", choices=sorted(TRANSACTION_MODES), required=True)
    begin_parser.add_argument("--steward", required=True)
    begin_parser.add_argument("--reason", required=True)
    begin_parser.set_defaults(handler=command_begin)

    status_parser = subparsers.add_parser("status")
    add_root(status_parser)
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(handler=command_status)

    prepare_parser = subparsers.add_parser("prepare")
    add_root(prepare_parser)
    prepare_parser.add_argument("--transaction", required=True)
    prepare_parser.add_argument("--owner", required=True)
    prepare_parser.add_argument("--summary", required=True)
    prepare_parser.set_defaults(handler=command_prepare)

    validate_parser = subparsers.add_parser("validate")
    add_root(validate_parser)
    validate_parser.add_argument("--transaction", required=True)
    validate_parser.add_argument("--owner", required=True)
    validate_parser.add_argument("--evidence", required=True)
    validate_parser.set_defaults(handler=command_validate)

    publish_parser = subparsers.add_parser("publish")
    add_root(publish_parser)
    publish_parser.add_argument("--transaction", required=True)
    publish_parser.add_argument("--steward", required=True)
    publish_parser.set_defaults(handler=command_publish)

    handoff_parser = subparsers.add_parser("handoff")
    add_root(handoff_parser)
    handoff_parser.add_argument("--transaction", required=True)
    handoff_parser.add_argument("--owner", required=True)
    handoff_parser.add_argument("--next-owner", required=True)
    handoff_parser.add_argument("--checkpoint", required=True)
    handoff_parser.set_defaults(handler=command_handoff)

    resume_parser = subparsers.add_parser("resume")
    add_root(resume_parser)
    resume_parser.add_argument("--transaction", required=True)
    resume_parser.add_argument("--owner", required=True)
    resume_parser.set_defaults(handler=command_resume)

    abort_parser = subparsers.add_parser("abort")
    add_root(abort_parser)
    abort_parser.add_argument("--transaction", required=True)
    abort_parser.add_argument("--owner", required=True)
    abort_parser.add_argument("--reason", required=True)
    abort_parser.add_argument("--discard", action="store_true")
    abort_parser.set_defaults(handler=command_abort)

    reconcile_parser = subparsers.add_parser("reconcile")
    add_root(reconcile_parser)
    reconcile_parser.add_argument("--steward", required=True)
    reconcile_parser.set_defaults(handler=command_reconcile)

    hotspot_parser = subparsers.add_parser("hotspots")
    add_root(hotspot_parser)
    hotspot_parser.set_defaults(handler=command_hotspots)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except (OSError, ValueError, RuntimeError, git.GitError) as error:
        print(f"transaction coordination error: {error}", file=sys.stderr)
        return 1
