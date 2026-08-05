"""Command-line routing for semantic Git microtransactions."""

from __future__ import annotations

import argparse
import sys

from . import git_backend as git
from .audit import command_log, command_workflow_report
from .contention import DECISIONS
from .contention_lease import DEFAULT_LEASE_SECONDS
from .contention_commands import (
    command_contention_acquire,
    command_contention_enact,
    command_contention_handoff,
    command_contention_propose,
    command_contention_reconcile,
    command_contention_renew,
    command_contention_respond,
    command_contention_status,
)
from .scheduler import REQUEST_MODES
from .state import TRANSACTION_MODES, root_path
from .transactions import (
    command_abort,
    command_abort_group,
    command_begin,
    command_cancel_request,
    command_cleanup_authorize,
    command_doctor,
    command_enqueue,
    command_handoff,
    command_hotspots,
    command_init,
    command_inspect,
    command_prepare,
    command_publish,
    command_reconcile,
    command_resume,
    command_schedule,
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

    contention_status_parser = subparsers.add_parser("contention-status")
    add_root(contention_status_parser)
    contention_status_parser.add_argument("--contention")
    contention_status_parser.set_defaults(handler=command_contention_status)

    contention_reconcile_parser = subparsers.add_parser("contention-reconcile")
    add_root(contention_reconcile_parser)
    contention_reconcile_parser.set_defaults(handler=command_contention_reconcile)

    contention_renew_parser = subparsers.add_parser("contention-renew")
    add_root(contention_renew_parser)
    contention_renew_parser.add_argument("--contention", required=True)
    contention_renew_parser.add_argument("--owner", required=True)
    contention_renew_parser.add_argument("--epoch", type=int, required=True)
    contention_renew_parser.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS
    )
    contention_renew_parser.set_defaults(handler=command_contention_renew)

    contention_acquire_parser = subparsers.add_parser("contention-acquire")
    add_root(contention_acquire_parser)
    contention_acquire_parser.add_argument("--contention", required=True)
    contention_acquire_parser.add_argument("--owner", required=True)
    contention_acquire_parser.add_argument("--expected-epoch", type=int, required=True)
    contention_acquire_parser.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS
    )
    contention_acquire_parser.set_defaults(handler=command_contention_acquire)

    contention_handoff_parser = subparsers.add_parser("contention-handoff")
    add_root(contention_handoff_parser)
    contention_handoff_parser.add_argument("--contention", required=True)
    contention_handoff_parser.add_argument("--owner", required=True)
    contention_handoff_parser.add_argument("--epoch", type=int, required=True)
    contention_handoff_parser.add_argument("--next-owner", required=True)
    contention_handoff_parser.add_argument("--reason", required=True)
    contention_handoff_parser.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS
    )
    contention_handoff_parser.set_defaults(handler=command_contention_handoff)

    contention_propose_parser = subparsers.add_parser("contention-propose")
    add_root(contention_propose_parser)
    contention_propose_parser.add_argument("--contention", required=True)
    contention_propose_parser.add_argument("--owner", required=True)
    contention_propose_parser.add_argument("--epoch", type=int, required=True)
    contention_propose_parser.add_argument("--decision", choices=sorted(DECISIONS))
    contention_propose_parser.add_argument("--target-scopes", nargs="*", default=[])
    contention_propose_parser.add_argument("--reason", required=True)
    contention_propose_parser.set_defaults(handler=command_contention_propose)

    contention_respond_parser = subparsers.add_parser("contention-respond")
    add_root(contention_respond_parser)
    contention_respond_parser.add_argument("--contention", required=True)
    contention_respond_parser.add_argument("--owner", required=True)
    contention_respond_parser.add_argument("--revision", type=int, required=True)
    response_group = contention_respond_parser.add_mutually_exclusive_group(required=True)
    response_group.add_argument("--accept", action="store_true")
    response_group.add_argument("--reject", action="store_true")
    contention_respond_parser.add_argument("--reason", default="")
    contention_respond_parser.set_defaults(handler=command_contention_respond)

    contention_enact_parser = subparsers.add_parser("contention-enact")
    add_root(contention_enact_parser)
    contention_enact_parser.add_argument("--contention", required=True)
    contention_enact_parser.add_argument("--owner", required=True)
    contention_enact_parser.add_argument("--epoch", type=int, required=True)
    contention_enact_parser.set_defaults(handler=command_contention_enact)

    enqueue_parser = subparsers.add_parser("enqueue")
    add_root(enqueue_parser)
    enqueue_parser.add_argument("--scopes", nargs="+", required=True)
    enqueue_parser.add_argument("--mode", choices=sorted(REQUEST_MODES), required=True)
    enqueue_parser.add_argument("--steward", required=True)
    enqueue_parser.add_argument("--reason", required=True)
    enqueue_parser.set_defaults(handler=command_enqueue)

    schedule_parser = subparsers.add_parser("schedule")
    add_root(schedule_parser)
    schedule_parser.add_argument("--steward", required=True)
    schedule_parser.add_argument("--limit", type=int, default=0)
    schedule_parser.set_defaults(handler=command_schedule)

    cancel_request_parser = subparsers.add_parser("cancel-request")
    add_root(cancel_request_parser)
    cancel_request_parser.add_argument("--request", required=True)
    cancel_request_parser.add_argument("--steward", required=True)
    cancel_request_parser.add_argument("--owners", nargs="+", required=True)
    cancel_request_parser.add_argument("--reason", required=True)
    cancel_request_parser.set_defaults(handler=command_cancel_request)

    doctor_parser = subparsers.add_parser("doctor")
    add_root(doctor_parser)
    doctor_parser.set_defaults(handler=command_doctor)

    cleanup_authorize_parser = subparsers.add_parser("cleanup-authorize")
    add_root(cleanup_authorize_parser)
    cleanup_authorize_parser.add_argument("--transaction", required=True)
    cleanup_authorize_parser.add_argument("--owner", required=True)
    cleanup_authorize_parser.add_argument("--reason", required=True)
    cleanup_authorize_parser.add_argument("--discard", action="store_true")
    cleanup_authorize_parser.set_defaults(handler=command_cleanup_authorize)

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

    abort_group_parser = subparsers.add_parser("abort-group")
    add_root(abort_group_parser)
    abort_group_parser.add_argument("--group", required=True)
    abort_group_parser.add_argument("--steward", required=True)
    abort_group_parser.add_argument("--owners", nargs="+", required=True)
    abort_group_parser.add_argument("--reason", required=True)
    abort_group_parser.add_argument("--discard", action="store_true")
    abort_group_parser.set_defaults(handler=command_abort_group)

    reconcile_parser = subparsers.add_parser("reconcile")
    add_root(reconcile_parser)
    reconcile_parser.add_argument("--steward", required=True)
    reconcile_parser.set_defaults(handler=command_reconcile)

    hotspot_parser = subparsers.add_parser("hotspots")
    add_root(hotspot_parser)
    hotspot_parser.set_defaults(handler=command_hotspots)

    log_parser = subparsers.add_parser("log")
    add_root(log_parser)
    log_parser.add_argument("--contention")
    log_parser.add_argument("--request")
    log_parser.add_argument("--transaction")
    log_parser.add_argument("--scope")
    log_parser.add_argument("--owner")
    log_parser.add_argument("--event")
    log_parser.add_argument("--limit", type=int, default=100)
    log_parser.set_defaults(handler=command_log)

    workflow_report_parser = subparsers.add_parser("workflow-report")
    add_root(workflow_report_parser)
    workflow_report_parser.set_defaults(handler=command_workflow_report)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except (OSError, ValueError, RuntimeError, git.GitError) as error:
        print(f"transaction coordination error: {error}", file=sys.stderr)
        return 1
