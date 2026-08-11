"""Semantic arbitration and Git microtransaction lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import git_backend as git
from .activation import activate_transaction_group, load_claims
from .arbitration import recommend_decision, record_paths
from .contention import reconcile_contentions
from .contention_store import active_contentions
from .event_contract import transaction_event_details
from .group_abort import (
    authorize_group_abort,
    reconcile_group_abort,
    reconcile_group_aborts,
)
from .maintenance import (
    active_cleanups,
    attach_transaction_archive,
    doctor,
    execute_cleanup,
    plan_cleanup,
    reauthorize_discard,
    reconcile_cleanups,
)
from .observability import hotspot_report
from .recovery import (
    active_groups,
    mark_group_member_terminal,
    reconcile_groups,
    require_transaction_group_active,
)
from .scheduler import (
    active_requests,
    assert_manual_activation_allowed,
    cancel_request,
    create_request,
    reconcile_request_activations,
    refresh_queue_states,
    schedule_ready_requests,
)
from .state import (
    active_claims,
    active_transactions,
    archive_transaction,
    coordination_guard,
    crash_if_testing,
    emit_event,
    initialize,
    now,
    overlapping_pairs,
    paths_overlap,
    read_json,
    read_transaction,
    replace_json,
    require_text,
    string_list,
    state_root,
    transaction_is_committed,
    validate_slug,
    write_json_exclusive,
)


def coordinator_path(location: Path) -> Path:
    return location / "steward.json"


def require_steward(location: Path, steward: str) -> str:
    steward = validate_slug(steward, "steward")
    path = coordinator_path(location)
    if not path.exists():
        raise ValueError("transaction coordinator is not initialized; run tx init")
    record = read_json(path)
    if record.get("steward") != steward:
        raise ValueError(
            f"transaction steward is {record.get('steward')!r}, not {steward!r}"
        )
    return steward


def require_canonical_branch(location: Path, root: Path) -> str:
    record = read_json(coordinator_path(location))
    expected = record.get("canonical_branch")
    if not isinstance(expected, str) or not expected:
        raise ValueError("coordinator canonical branch is malformed")
    current = git.current_branch(root)
    if current != expected:
        raise ValueError(
            f"canonical workspace is on branch {current!r}, expected {expected!r}"
        )
    return expected


def transaction_checkout(record: dict[str, object]) -> Path:
    value = record.get("checkout")
    if not isinstance(value, str) or not value:
        raise ValueError("transaction checkout is malformed")
    return Path(value)


def assert_owner(record: dict[str, object], owner: str) -> str:
    owner = validate_slug(owner, "owner")
    if record.get("owner") != owner:
        raise ValueError(f"transaction belongs to {record.get('owner')!r}, not {owner!r}")
    return owner


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def command_init(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    git.ensure_local_exclude(arguments.root, arguments.state_dir)
    steward = validate_slug(arguments.steward, "steward")
    path = coordinator_path(location)
    with coordination_guard(location, "tx-init"):
        if path.exists():
            existing = read_json(path)
            if existing.get("steward") != steward:
                raise ValueError(
                    f"coordinator already belongs to {existing.get('steward')!r}"
                )
        else:
            write_json_exclusive(
                path,
                {
                    "schema": 1,
                    "steward": steward,
                    "canonical_branch": git.current_branch(arguments.root),
                    "created_at": now(),
                },
            )
            emit_event(
                location,
                "coordinator-initialized",
                None,
                {
                    "trace_schema": 1,
                    "actor_owner": steward,
                    "steward": steward,
                    "canonical_branch": git.current_branch(arguments.root),
                    "base_revision": git.current_head(arguments.root),
                },
            )
    print_json({"state": str(location), "steward": steward})
    return 0


def command_inspect(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    git.ensure_local_exclude(arguments.root, arguments.state_dir)
    claims = load_claims(location, arguments.scopes)
    result = recommend_decision(
        [record for _, record in claims],
        git.status_paths(arguments.root),
    )
    result["scopes"] = arguments.scopes
    print_json(result)
    return 0


def command_begin(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    git.ensure_local_exclude(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    canonical_branch = require_canonical_branch(location, arguments.root)
    reason = require_text(arguments.reason, "arbitration reason")

    with coordination_guard(location, "tx-begin"):
        assert_manual_activation_allowed(location, arguments.scopes)
        print_json(
            activate_transaction_group(
                arguments.root,
                location,
                arguments.scopes,
                arguments.mode,
                steward,
                canonical_branch,
                reason,
            )
        )
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    contentions = [record for _, record in active_contentions(location)]
    groups = [record for _, record in active_groups(location)]
    records = [record for _, record in active_transactions(location)]
    cleanups = [record for _, record in active_cleanups(location)]
    requests = [record for _, record in active_requests(location)]
    if arguments.json:
        print_json(
            {
                "contentions": contentions,
                "requests": requests,
                "groups": groups,
                "transactions": records,
                "cleanups": cleanups,
            }
        )
        return 0
    if not contentions and not requests and not groups and not records and not cleanups:
        print("no active contentions, requests, transaction groups, transactions, or cleanups")
        return 0
    for contention in contentions:
        coordinator = contention.get("coordinator", {})
        print(
            f"contention {contention.get('contention_id')}: "
            f"status={contention.get('status')} scopes={contention.get('scopes')} "
            f"coordinator={coordinator.get('owner') if isinstance(coordinator, dict) else '?'} "
            f"epoch={coordinator.get('epoch') if isinstance(coordinator, dict) else '?'}"
        )
    for request in requests:
        print(
            f"request {request.get('request_id')}: status={request.get('status')} "
            f"mode={request.get('mode')} scopes={request.get('scopes')} "
            f"blockers={request.get('blockers', [])}"
        )
    for group in groups:
        print(
            f"group {group.get('group_id')}: status={group.get('status')} "
            f"claims_promoted={group.get('claims_promoted')} "
            f"members={len(group.get('members', []))}"
        )
    for record in records:
        print(
            f"{record.get('transaction_id')}: status={record.get('status')} "
            f"group={record.get('group_id', 'legacy')} owner={record.get('owner')} "
            f"scope={record.get('scope')} "
            f"base={record.get('base_revision')} paths={record.get('paths')}"
        )
    for cleanup in cleanups:
        print(
            f"cleanup {cleanup.get('cleanup_id')}: status={cleanup.get('status')} "
            f"disposition={cleanup.get('disposition')} issue={cleanup.get('issue')}"
        )
    return 0


def command_enqueue(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    reason = require_text(arguments.reason, "scheduling reason")
    with coordination_guard(location, "tx-enqueue"):
        _, request = create_request(
            location,
            arguments.scopes,
            arguments.mode,
            steward,
            reason,
        )
        print_json(request)
    return 0


def command_schedule(arguments: argparse.Namespace) -> int:
    if arguments.limit < 0:
        raise ValueError("schedule limit cannot be negative")
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    canonical_branch = require_canonical_branch(location, arguments.root)
    with coordination_guard(location, "tx-schedule"):
        updates = schedule_ready_requests(
            arguments.root,
            location,
            steward,
            canonical_branch,
            arguments.limit,
        )
        updates.extend(reconcile_contentions(location))
        print_json(updates)
    return 0


def command_cancel_request(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    owners = {validate_slug(owner, "owner") for owner in arguments.owners}
    reason = require_text(arguments.reason, "request cancellation reason")
    with coordination_guard(location, "tx-cancel-request"):
        cancelled = cancel_request(
            location,
            arguments.request,
            steward,
            owners,
            reason,
        )
        queue_updates = refresh_queue_states(arguments.root, location)
        print_json({"cancelled": cancelled, "queue": queue_updates})
    return 0


def command_doctor(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = state_root(arguments.root, arguments.state_dir)
    if not location.exists():
        raise ValueError("coordination state does not exist; run tx init")
    print_json(doctor(arguments.root, location))
    return 0


def command_cleanup_authorize(arguments: argparse.Namespace) -> int:
    if not arguments.discard:
        raise ValueError("cleanup authorization requires --discard")
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    owner = validate_slug(arguments.owner, "owner")
    reason = require_text(arguments.reason, "cleanup authorization reason")
    with coordination_guard(location, "tx-cleanup-authorize"):
        path, cleanup = reauthorize_discard(
            arguments.root,
            location,
            arguments.transaction,
            owner,
            reason,
        )
        print_json(execute_cleanup(arguments.root, location, path, cleanup))
    return 0


def command_prepare(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    summary = require_text(arguments.summary, "summary")
    with coordination_guard(location, "tx-prepare"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") not in {"active", "conflicted", "prepared", "stale"}:
            raise ValueError(f"transaction cannot be prepared from {record.get('status')!r}")
        checkout = transaction_checkout(record)
        operations = git.operation_in_progress(checkout)
        if operations:
            raise ValueError(
                "finish or abort the transaction Git operation first: " + ", ".join(operations)
            )
        declared = record_paths(record)
        dirty = git.status_paths(checkout)
        out_of_scope = git.paths_within_scope(dirty, declared)
        if out_of_scope:
            raise ValueError("transaction diff exceeds its claim: " + ", ".join(out_of_scope))

        base = str(record["base_revision"])
        root_head = git.current_head(arguments.root)
        checkout_head = git.current_head(checkout)
        if record.get("status") in {"conflicted", "stale"} and git.is_ancestor(
            checkout, root_head, checkout_head
        ):
            base = root_head
        ahead = git.commits_ahead(checkout, base)
        if dirty:
            if ahead > 1:
                raise ValueError("transaction has multiple commits plus dirty changes; squash first")
            candidate = git.stage_and_commit(
                checkout,
                declared,
                f"[tx:{record['scope']}] {summary}",
                amend=ahead == 1,
            )
        else:
            if ahead != 1:
                raise ValueError(
                    f"prepared transaction must be exactly one commit ahead of its base; found {ahead}"
                )
            candidate = checkout_head
        actual = git.diff_paths(checkout, base, candidate)
        out_of_scope = git.paths_within_scope(actual, declared)
        if out_of_scope:
            raise ValueError("candidate exceeds its claim: " + ", ".join(out_of_scope))
        if not actual:
            raise ValueError("candidate has no transaction changes")
        record["base_revision"] = base
        record["candidate"] = candidate
        record["actual_paths"] = actual
        record["summary"] = summary
        record["status"] = "prepared"
        record["prepared_at"] = now()
        record.pop("validation_evidence", None)
        record.pop("conflicts", None)
        replace_json(path, record)
        emit_event(
            location,
            "transaction-prepared",
            str(record["transaction_id"]),
            transaction_event_details(
                record,
                actor_owner=arguments.owner,
                candidate=candidate,
                actual_paths=actual,
            ),
        )
        print_json(record)
    return 0


def command_validate(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    evidence = require_text(arguments.evidence, "validation evidence")
    with coordination_guard(location, "tx-validate"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") != "prepared":
            raise ValueError("only a prepared transaction can be validated")
        checkout = transaction_checkout(record)
        if git.status_paths(checkout):
            raise ValueError("transaction checkout changed after prepare")
        candidate = str(record["candidate"])
        if git.current_head(checkout) != candidate:
            raise ValueError("transaction HEAD changed after prepare")
        record["validation_evidence"] = {
            "candidate": candidate,
            "base_revision": record["base_revision"],
            "plan": record.get("validation_plan", []),
            "evidence": evidence,
            "validated_at": now(),
        }
        record["status"] = "ready"
        replace_json(path, record)
        emit_event(
            location,
            "transaction-validated",
            str(record["transaction_id"]),
            transaction_event_details(
                record,
                actor_owner=arguments.owner,
                candidate=candidate,
            ),
        )
        print_json(record)
    return 0


def check_publish_dependencies(location: Path, record: dict[str, object]) -> None:
    for dependency in string_list(record, "publish_after"):
        if not transaction_is_committed(location, dependency):
            raise ValueError(f"publish dependency is not committed: {dependency}")


def archive_committed_and_cleanup(
    root: Path,
    location: Path,
    path: Path,
    record: dict[str, object],
) -> tuple[Path, dict[str, object], dict[str, object]]:
    mark_group_member_terminal(location, record, "committed")
    cleanup_path, cleanup = plan_cleanup(
        root,
        location,
        record,
        "published",
        str(record.get("steward", "unknown")),
        "Published candidate is contained in canonical HEAD",
    )
    archive = archive_transaction(location, path, record)
    attach_transaction_archive(cleanup_path, cleanup, archive)
    crash_if_testing("transaction-archived-before-cleanup")
    result = execute_cleanup(root, location, cleanup_path, cleanup)
    if result["action"] != "completed":
        record["cleanup_pending"] = True
        record["cleanup_error"] = result.get("issue")
        record["cleanup_id"] = cleanup["cleanup_id"]
        replace_json(archive, record)
    else:
        record["cleanup_completed_at"] = now()
        record["cleanup_archive"] = result["archive"]
        replace_json(archive, record)
    return archive, record, result


def archive_aborted_and_cleanup(
    root: Path,
    location: Path,
    path: Path,
    record: dict[str, object],
    owner: str,
    reason: str,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    mark_group_member_terminal(location, record, "aborted")
    cleanup_path, cleanup = plan_cleanup(
        root,
        location,
        record,
        "discard",
        owner,
        reason,
    )
    archive = archive_transaction(location, path, record)
    attach_transaction_archive(cleanup_path, cleanup, archive)
    crash_if_testing("transaction-archived-before-cleanup")
    result = execute_cleanup(root, location, cleanup_path, cleanup)
    if result["action"] != "completed":
        record["cleanup_pending"] = True
        record["cleanup_error"] = result.get("issue")
        record["cleanup_id"] = cleanup["cleanup_id"]
    else:
        record["cleanup_completed_at"] = now()
        record["cleanup_archive"] = result["archive"]
    replace_json(archive, record)
    return archive, record, result


def command_publish(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    require_canonical_branch(location, arguments.root)
    with coordination_guard(location, "tx-publish"):
        path, record = read_transaction(location, arguments.transaction)
        if record.get("steward") != steward:
            raise ValueError("transaction belongs to a different release steward")
        require_transaction_group_active(location, record)
        if record.get("status") != "ready":
            raise ValueError("only a ready transaction can be published")
        check_publish_dependencies(location, record)
        checkout = transaction_checkout(record)
        candidate = str(record["candidate"])
        base = str(record["base_revision"])
        if git.status_paths(checkout):
            raise ValueError("ready transaction checkout is dirty")
        if git.operation_in_progress(checkout):
            raise ValueError("transaction checkout has an active Git operation")
        if git.current_head(checkout) != candidate:
            raise ValueError("transaction candidate no longer matches its branch HEAD")

        root_head = git.current_head(arguments.root)
        if root_head != base:
            record["status"] = "refreshing"
            replace_json(path, record)
            emit_event(
                location,
                "refresh-started",
                str(record["transaction_id"]),
                transaction_event_details(
                    record,
                    actor_owner=steward,
                    old_base=base,
                    new_base=root_head,
                ),
            )
            succeeded, output = git.rebase_onto(checkout, root_head)
            if not succeeded:
                record["status"] = "conflicted"
                record["conflicts"] = git.conflicted_paths(checkout)
                record["refresh_output"] = output
                replace_json(path, record)
                emit_event(
                    location,
                    "refresh-conflicted",
                    str(record["transaction_id"]),
                    transaction_event_details(
                        record,
                        actor_owner=steward,
                        conflicts=record["conflicts"],
                    ),
                )
                print_json(record)
                return 2
            refreshed_candidate = git.current_head(checkout)
            actual = git.diff_paths(checkout, root_head, refreshed_candidate)
            out_of_scope = git.paths_within_scope(actual, record_paths(record))
            if out_of_scope:
                raise ValueError(
                    "refreshed candidate exceeds its claim: " + ", ".join(out_of_scope)
                )
            record["base_revision"] = root_head
            record["candidate"] = refreshed_candidate
            record["actual_paths"] = actual
            record["status"] = "prepared"
            record["refreshed_at"] = now()
            record["refreshed_from"] = candidate
            record.pop("validation_evidence", None)
            replace_json(path, record)
            emit_event(
                location,
                "refresh-completed",
                str(record["transaction_id"]),
                transaction_event_details(
                    record,
                    actor_owner=steward,
                    candidate=refreshed_candidate,
                ),
            )
            print_json(record)
            return 2

        validation = record.get("validation_evidence")
        if not isinstance(validation, dict):
            raise ValueError("transaction has no validation evidence")
        if validation.get("candidate") != candidate or validation.get("base_revision") != base:
            raise ValueError("validation evidence is not bound to the current candidate and base")
        if git.commits_ahead(checkout, base, candidate) != 1:
            raise ValueError("candidate must be exactly one commit ahead of canonical HEAD")
        if not git.is_ancestor(checkout, base, candidate):
            raise ValueError("candidate cannot fast-forward canonical HEAD")
        root_operations = git.operation_in_progress(arguments.root)
        if root_operations:
            raise ValueError("canonical workspace has an active Git operation: " + ", ".join(root_operations))
        if not git.index_is_empty(arguments.root):
            raise ValueError("canonical Git index must be empty before transaction publish")

        actual = string_list(record, "actual_paths")
        dirty_overlap = [
            dirty
            for dirty in git.status_paths(arguments.root)
            if any(paths_overlap(dirty, changed) for changed in actual)
        ]
        if dirty_overlap:
            raise ValueError(
                "canonical workspace has overlapping dirty paths: " + ", ".join(dirty_overlap)
            )
        for _, claim in active_claims(location):
            if claim.get("status", "active") != "active":
                continue
            overlaps = overlapping_pairs(actual, record_paths(claim))
            if overlaps:
                raise ValueError(
                    f"active direct claim {claim.get('scope')!r} overlaps publish: "
                    + ", ".join(overlaps)
                )

        record["status"] = "publishing"
        record["expected_head"] = root_head
        record["publishing_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "publish-started",
            str(record["transaction_id"]),
            transaction_event_details(
                record,
                actor_owner=steward,
                expected_head=root_head,
                candidate=candidate,
            ),
        )
        crash_if_testing("publish-recorded")
        git.fast_forward(arguments.root, str(record["branch"]))
        crash_if_testing("publish-fast-forwarded")
        if git.current_head(arguments.root) != candidate:
            raise RuntimeError("Git reported success but canonical HEAD is not the candidate")
        record["status"] = "committed"
        record["committed_revision"] = candidate
        record["committed_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "publish-completed",
            str(record["transaction_id"]),
            transaction_event_details(
                record,
                actor_owner=steward,
                candidate=candidate,
            ),
        )
        crash_if_testing("publish-committed")
        archive, record, cleanup_result = archive_committed_and_cleanup(
            arguments.root, location, path, record
        )
        queue_updates = refresh_queue_states(arguments.root, location)
        print_json(
            {
                "archive": str(archive),
                "transaction": record,
                "cleanup": cleanup_result,
                "queue": queue_updates,
            }
        )
    return 0


def command_handoff(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    checkpoint = require_text(arguments.checkpoint, "handoff checkpoint")
    next_owner = validate_slug(arguments.next_owner, "next owner")
    with coordination_guard(location, "tx-handoff"):
        path, record = read_transaction(location, arguments.transaction)
        current_owner = assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") in {"publishing", "committed"}:
            raise ValueError("publishing or committed transactions cannot be handed off")
        record["handoff"] = {
            "from": current_owner,
            "to": next_owner,
            "checkpoint": checkpoint,
            "at": now(),
        }
        record["owner"] = next_owner
        record["status_before_pause"] = record.get("status")
        record["status"] = "paused"
        replace_json(path, record)
        emit_event(
            location,
            "transaction-handed-off",
            str(record["transaction_id"]),
            transaction_event_details(
                record,
                actor_owner=current_owner,
                work_owner=next_owner,
                source_owner=current_owner,
                target_owner=next_owner,
                **{"from": current_owner, "to": next_owner},
            ),
        )
        print_json(record)
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "tx-resume"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") != "paused":
            raise ValueError("only a paused transaction can be resumed")
        prior = record.pop("status_before_pause", "active")
        if prior not in {"active", "conflicted", "prepared", "ready", "stale"}:
            prior = "active"
        record["status"] = prior
        record["resumed_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "transaction-resumed",
            str(record["transaction_id"]),
            transaction_event_details(record, actor_owner=arguments.owner),
        )
        print_json(record)
    return 0


def command_abort(arguments: argparse.Namespace) -> int:
    if not arguments.discard:
        raise ValueError("abort requires --discard to explicitly authorize data deletion")
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "tx-abort"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") in {"publishing", "committed"}:
            raise ValueError("publishing or committed transactions cannot be aborted")
        reason = require_text(arguments.reason, "abort reason")
        record["status"] = "aborted"
        record["abort_reason"] = reason
        record["aborted_at"] = now()
        replace_json(path, record)
        archive, record, cleanup_result = archive_aborted_and_cleanup(
            arguments.root,
            location,
            path,
            record,
            str(record["owner"]),
            reason,
        )
        emit_event(
            location,
            "transaction-aborted",
            str(record["transaction_id"]),
            transaction_event_details(
                record,
                actor_owner=arguments.owner,
                reason=reason,
            ),
        )
        queue_updates = refresh_queue_states(arguments.root, location)
        print_json(
            {
                "archive": str(archive),
                "transaction": record,
                "cleanup": cleanup_result,
                "queue": queue_updates,
            }
        )
    return 0


def command_abort_group(arguments: argparse.Namespace) -> int:
    if not arguments.discard:
        raise ValueError(
            "group abort requires --discard to explicitly authorize data deletion"
        )
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    require_canonical_branch(location, arguments.root)
    owners = {validate_slug(owner, "owner") for owner in arguments.owners}
    reason = require_text(arguments.reason, "group abort reason")
    with coordination_guard(location, "tx-abort-group"):
        path, group = authorize_group_abort(
            arguments.root,
            location,
            arguments.group,
            steward,
            owners,
            reason,
        )
        update, _ = reconcile_group_abort(arguments.root, location, path, group)
        queue_updates = refresh_queue_states(arguments.root, location)
        update["queue"] = queue_updates
        print_json(update)
    return 0


def command_reconcile(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    require_steward(location, arguments.steward)
    require_canonical_branch(location, arguments.root)
    with coordination_guard(location, "tx-reconcile"):
        updates = reconcile_request_activations(location)
        group_abort_updates, processed_cleanups = reconcile_group_aborts(
            arguments.root, location
        )
        updates.extend(group_abort_updates)
        updates.extend(reconcile_groups(arguments.root, location))
        for path, record in active_transactions(location):
            if record.get("status") in {"aborted", "committed"}:
                transaction_id = str(record["transaction_id"])
                if record.get("status") == "committed":
                    archive, record, cleanup_result = archive_committed_and_cleanup(
                        arguments.root, location, path, record
                    )
                else:
                    archive, record, cleanup_result = archive_aborted_and_cleanup(
                        arguments.root,
                        location,
                        path,
                        record,
                        str(record.get("owner", "unknown")),
                        str(record.get("abort_reason", "Interrupted authorized abort")),
                    )
                processed_cleanups.add(transaction_id)
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "completed",
                        "archive": str(archive),
                    }
                )
                updates.append(cleanup_result)
                continue
            if record.get("status") != "publishing":
                continue
            head = git.current_head(arguments.root)
            candidate = str(record.get("candidate", ""))
            expected = str(record.get("expected_head", ""))
            if head == candidate:
                record["status"] = "committed"
                record["committed_revision"] = candidate
                record["committed_at"] = now()
                replace_json(path, record)
                archive, record, cleanup_result = archive_committed_and_cleanup(
                    arguments.root, location, path, record
                )
                processed_cleanups.add(str(record["transaction_id"]))
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "completed",
                        "archive": str(archive),
                    }
                )
                updates.append(cleanup_result)
            elif head == expected:
                record["status"] = "ready"
                record.pop("expected_head", None)
                replace_json(path, record)
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "retryable",
                        "transaction": record["transaction_id"],
                    }
                )
            else:
                record["status"] = "stale"
                record.pop("validation_evidence", None)
                replace_json(path, record)
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "stale",
                        "transaction": record["transaction_id"],
                    }
                )
        updates.extend(
            reconcile_cleanups(
                arguments.root,
                location,
                excluded_transactions=processed_cleanups,
            )
        )
        updates.extend(refresh_queue_states(arguments.root, location))
        updates.extend(reconcile_contentions(location))
    print_json(updates)
    return 0


def command_hotspots(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    print_json(hotspot_report(location))
    return 0
