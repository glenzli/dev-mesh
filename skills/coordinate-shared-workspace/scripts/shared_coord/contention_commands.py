"""Thin command facade for contention-local coordination."""

from __future__ import annotations

import json

from . import git_backend as git
from .contention import (
    contention_status,
    discover_contentions,
    drive_ready_requests,
    enact_decision,
    propose_decision,
    reconcile_contentions,
    respond_to_decision,
)
from .contention_lease import (
    acquire_coordination,
    handoff_coordination,
    renew_coordination,
)
from .state import coordination_guard, initialize


def command_contention_status(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    records = contention_status(location)
    if arguments.contention:
        records = [
            record
            for record in records
            if record.get("contention_id") == arguments.contention
        ]
    print(json.dumps(records, indent=2, ensure_ascii=False))
    return 0


def command_contention_reconcile(arguments) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "contention-reconcile"):
        discovered = discover_contentions(arguments.root, location)
        updates = reconcile_contentions(location)
        updates.extend(drive_ready_requests(arguments.root, location))
    print(
        json.dumps(
            {"discovered": discovered, "updates": updates},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def command_contention_renew(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "contention-renew"):
        record = renew_coordination(
            location,
            arguments.contention,
            arguments.owner,
            arguments.epoch,
            arguments.lease_seconds,
        )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def command_contention_acquire(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "contention-acquire"):
        record = acquire_coordination(
            location,
            arguments.contention,
            arguments.owner,
            arguments.expected_epoch,
            arguments.lease_seconds,
        )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def command_contention_handoff(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "contention-handoff"):
        record = handoff_coordination(
            location,
            arguments.contention,
            arguments.owner,
            arguments.epoch,
            arguments.next_owner,
            arguments.lease_seconds,
            arguments.reason,
        )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def command_contention_propose(arguments) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "contention-propose"):
        record = propose_decision(
            arguments.root,
            location,
            arguments.contention,
            arguments.owner,
            arguments.epoch,
            arguments.decision,
            arguments.target_scopes,
            arguments.reason,
        )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def command_contention_respond(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    if arguments.accept == arguments.reject:
        raise ValueError("choose exactly one of --accept or --reject")
    with coordination_guard(location, "contention-respond"):
        record = respond_to_decision(
            location,
            arguments.contention,
            arguments.owner,
            arguments.revision,
            arguments.accept,
            arguments.reason,
        )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def command_contention_enact(arguments) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "contention-enact"):
        result = enact_decision(
            arguments.root,
            location,
            arguments.contention,
            arguments.owner,
            arguments.epoch,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
