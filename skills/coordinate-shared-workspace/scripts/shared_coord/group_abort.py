"""Explicitly authorized rollback for transaction groups below the activation barrier."""

from __future__ import annotations

import copy
import shutil
import time
from pathlib import Path

from . import git_backend as git
from .arbitration import record_paths
from .maintenance import (
    attach_transaction_archive,
    cleanup_path,
    execute_cleanup,
    plan_cleanup,
    reauthorize_discard,
)
from .recovery import group_path
from .state import (
    archive_transaction,
    crash_if_testing,
    emit_event,
    now,
    read_json,
    replace_json,
    transaction_path,
    validate_slug,
    write_json_exclusive,
)


ABORTABLE_GROUP_STATES = {
    "abort-needs-attention",
    "aborting",
    "active",
    "materializing",
    "needs-attention",
    "planned",
}
ABORTABLE_TRANSACTION_STATES = {"aborted", "active", "materializing"}


class GroupAbortAttention(RuntimeError):
    """A group abort preserved resources because its exact authority was uncertain."""


def _members(group: dict[str, object]) -> list[dict[str, object]]:
    value = group.get("members")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("transaction group members are malformed")
    return value


def _planned_transaction(member: dict[str, object]) -> dict[str, object]:
    value = member.get("planned_transaction")
    if not isinstance(value, dict):
        raise ValueError("transaction group member plan is malformed")
    return value


def _transaction_archive(
    location: Path,
    transaction_id: str,
) -> tuple[Path, dict[str, object]] | None:
    for path in sorted(
        (location / "transactions" / "archive").glob(f"*-{transaction_id}.json")
    ):
        record = read_json(path)
        if record.get("transaction_id") == transaction_id:
            return path, record
    return None


def _completed_cleanup(location: Path, transaction_id: str) -> Path | None:
    for path in sorted(
        (location / "cleanups" / "archive").glob(f"*-{transaction_id}.json")
    ):
        if read_json(path).get("status") == "completed":
            return path
    return None


def authorize_group_abort(
    root: Path,
    location: Path,
    group_id: str,
    steward: str,
    owners: set[str],
    reason: str,
) -> tuple[Path, dict[str, object]]:
    path = group_path(location, group_id)
    if not path.exists():
        raise ValueError(f"active transaction group does not exist: {group_id}")
    group = read_json(path)
    if group.get("steward") != steward:
        raise ValueError("transaction group belongs to a different steward")
    if group.get("status") not in ABORTABLE_GROUP_STATES:
        raise ValueError(f"transaction group cannot be aborted from {group.get('status')!r}")
    if group.get("claims_promoted") is True:
        raise ValueError(
            "fully active groups require each transaction owner to use normal abort"
        )
    expected_owners = {str(member.get("owner", "")) for member in _members(group)}
    if owners != expected_owners:
        raise ValueError(
            "group abort requires exact owner acknowledgements: "
            + ", ".join(sorted(expected_owners))
        )
    for member in _members(group):
        transaction_id = str(member["transaction_id"])
        active = transaction_path(location, transaction_id)
        if active.exists():
            status = read_json(active).get("status")
            if status not in ABORTABLE_TRANSACTION_STATES:
                raise ValueError(
                    f"transaction {transaction_id!r} cannot join group abort from {status!r}"
                )
    prior = group.get("abort")
    if isinstance(prior, dict):
        if set(str(item) for item in prior.get("owners", [])) != owners:
            raise ValueError("group abort owner acknowledgement changed")
    group["status"] = "aborting"
    group["abort"] = {
        "steward": steward,
        "owners": sorted(owners),
        "reason": reason,
        "authorized_at": now(),
    }
    replace_json(path, group)
    emit_event(
        location,
        "group-abort-authorized",
        None,
        {"group_id": group_id, "owners": sorted(owners), "reason": reason},
    )
    for member in _members(group):
        planned = _planned_transaction(member)
        transaction_id = str(planned["transaction_id"])
        owner = str(planned["owner"])
        archived = _transaction_archive(location, transaction_id)
        active = transaction_path(location, transaction_id)
        if archived is not None:
            transaction_archive, transaction = archived
        elif active.exists():
            transaction_archive = None
            transaction = read_json(active)
        else:
            transaction_archive = None
            transaction = planned

        completed = _completed_cleanup(location, transaction_id)
        intent_path = cleanup_path(location, transaction_id)
        if completed is not None:
            member["cleanup_authorized"] = True
            member["cleanup_archive"] = str(completed)
            replace_json(path, group)
            continue
        if intent_path.exists():
            cleanup = read_json(intent_path)
            if cleanup.get("status") == "needs-attention":
                intent_path, cleanup = reauthorize_discard(
                    root,
                    location,
                    transaction_id,
                    owner,
                    reason,
                )
        else:
            intent_path, cleanup = plan_cleanup(
                root,
                location,
                transaction,
                "discard",
                owner,
                reason,
            )
        if transaction_archive is not None:
            attach_transaction_archive(intent_path, cleanup, transaction_archive)
        member["cleanup_authorized"] = True
        member["cleanup_authorized_at"] = now()
        replace_json(path, group)
        crash_if_testing(f"group-abort-cleanup-authorized:{planned['scope']}")
    crash_if_testing("group-abort-authorized")
    return path, group


def _ensure_aborted_transaction_archive(
    root: Path,
    location: Path,
    group: dict[str, object],
    member: dict[str, object],
) -> tuple[Path, dict[str, object], dict[str, object]]:
    planned = _planned_transaction(member)
    transaction_id = str(planned["transaction_id"])
    owner = str(planned["owner"])
    abort = group.get("abort")
    if not isinstance(abort, dict):
        raise GroupAbortAttention("group abort authorization is missing")
    reason = str(abort.get("reason", "Authorized partial-group abort"))
    archived = _transaction_archive(location, transaction_id)
    active = transaction_path(location, transaction_id)
    intent_path = cleanup_path(location, transaction_id)
    completed = _completed_cleanup(location, transaction_id)

    if completed is not None and archived is not None:
        archive, transaction = archived
        return (
            archive,
            transaction,
            {
                "kind": "cleanup",
                "transaction": transaction_id,
                "action": "completed",
                "archive": str(completed),
            },
        )
    if not intent_path.exists():
        raise GroupAbortAttention(
            f"transaction {transaction_id!r} has no owner-authorized cleanup snapshot; "
            "run abort-group again with exact owner acknowledgements"
        )
    cleanup = read_json(intent_path)
    if cleanup.get("disposition") != "discard" or cleanup.get("authorized_by") != owner:
        raise GroupAbortAttention(
            f"transaction {transaction_id!r} cleanup authorization does not match its owner"
        )

    if archived is None:
        if active.exists():
            transaction = read_json(active)
            if transaction.get("status") not in ABORTABLE_TRANSACTION_STATES:
                raise GroupAbortAttention(
                    f"transaction {transaction_id!r} advanced to {transaction.get('status')!r}"
                )
        else:
            transaction = copy.deepcopy(planned)
            write_json_exclusive(active, transaction)
        transaction["status"] = "aborted"
        transaction["abort_reason"] = reason
        transaction["aborted_at"] = now()
        replace_json(active, transaction)
        archive = archive_transaction(location, active, transaction)
        attach_transaction_archive(intent_path, cleanup, archive)
        emit_event(
            location,
            "transaction-aborted",
            transaction_id,
            {"reason": reason, "group_id": group["group_id"]},
        )
        crash_if_testing(f"group-abort-member-archived:{planned['scope']}")
    else:
        archive, transaction = archived
        attach_transaction_archive(intent_path, cleanup, archive)

    result = execute_cleanup(root, location, intent_path, cleanup)
    if result.get("action") == "completed":
        transaction["cleanup_completed_at"] = now()
        transaction["cleanup_archive"] = result.get("archive")
        transaction.pop("cleanup_pending", None)
        transaction.pop("cleanup_error", None)
    else:
        transaction["cleanup_pending"] = True
        transaction["cleanup_error"] = result.get("issue")
    replace_json(archive, transaction)
    return archive, transaction, result


def _promoted_claim_archive(
    location: Path,
    scope: str,
    transaction_id: str,
) -> Path | None:
    for path in sorted((location / "archive" / "claims").glob(f"*-{scope}.json")):
        if read_json(path).get("transaction_id") == transaction_id:
            return path
    return None


def _restore_claim(
    location: Path,
    group: dict[str, object],
    member: dict[str, object],
) -> None:
    planned = _planned_transaction(member)
    scope = str(planned["scope"])
    transaction_id = str(planned["transaction_id"])
    claim = location / "claims" / f"{validate_slug(scope, 'scope')}.json"
    if claim.exists():
        record = read_json(claim)
        if (
            record.get("owner") != planned.get("owner")
            or record_paths(record) != record_paths(planned)
        ):
            raise GroupAbortAttention(
                f"claim {scope!r} is occupied by a different owner or scope"
            )
    else:
        archived = _promoted_claim_archive(location, scope, transaction_id)
        if archived is None:
            raise GroupAbortAttention(
                f"claim {scope!r} is missing and cannot be restored safely"
            )
        record = read_json(archived)
        record["status"] = record.pop(
            "status_before_promotion",
            planned.get("source_claim_status", "active"),
        )
        record.pop("transaction_id", None)
        record.pop("group_id", None)
        record.pop("promoted_at", None)
        record["restored_at"] = now()
        replace_json(archived, record)
        shutil.move(archived, claim)
    member["claim_restored"] = True
    member["claim_restored_at"] = now()


def _archive_aborted_group(
    location: Path,
    path: Path,
    group: dict[str, object],
) -> Path:
    archive = location / "groups" / "archive" / f"{time.time_ns()}-{path.name}"
    shutil.move(path, archive)
    emit_event(
        location,
        "group-aborted",
        None,
        {"group_id": group["group_id"], "archive": str(archive)},
    )
    return archive


def reconcile_group_abort(
    root: Path,
    location: Path,
    path: Path,
    group: dict[str, object],
) -> tuple[dict[str, object], set[str]]:
    group_id = str(group["group_id"])
    processed: set[str] = set()
    if group.get("status") == "aborted":
        archive = _archive_aborted_group(location, path, group)
        return (
            {"kind": "group-abort", "group_id": group_id, "action": "completed", "archive": str(archive)},
            processed,
        )

    issues: list[str] = []
    for member in _members(group):
        transaction_id = str(member["transaction_id"])
        processed.add(transaction_id)
        try:
            _, _, cleanup = _ensure_aborted_transaction_archive(
                root, location, group, member
            )
        except (OSError, ValueError, git.GitError, GroupAbortAttention) as error:
            issues.append(str(error))
            continue
        member["transaction_status"] = "aborted"
        member["cleanup_status"] = cleanup.get("action")
        if cleanup.get("action") != "completed":
            issues.append(str(cleanup.get("issue", "cleanup needs attention")))
        replace_json(path, group)

    if issues:
        group["status"] = "abort-needs-attention"
        group["abort_issues"] = sorted(set(issues))
        group["updated_at"] = now()
        replace_json(path, group)
        return (
            {
                "kind": "group-abort",
                "group_id": group_id,
                "action": "needs-attention",
                "issues": group["abort_issues"],
            },
            processed,
        )

    try:
        for member in _members(group):
            _restore_claim(location, group, member)
            replace_json(path, group)
            crash_if_testing(f"group-abort-claim-restored:{member['scope']}")
    except (OSError, ValueError, git.GitError, GroupAbortAttention) as error:
        group["status"] = "abort-needs-attention"
        group["abort_issues"] = [str(error)]
        replace_json(path, group)
        return (
            {
                "kind": "group-abort",
                "group_id": group_id,
                "action": "needs-attention",
                "issues": group["abort_issues"],
            },
            processed,
        )

    group["status"] = "aborted"
    group["aborted_at"] = now()
    group.pop("abort_issues", None)
    replace_json(path, group)
    crash_if_testing("group-abort-recorded")
    archive = _archive_aborted_group(location, path, group)
    return (
        {
            "kind": "group-abort",
            "group_id": group_id,
            "action": "completed",
            "archive": str(archive),
        },
        processed,
    )


def reconcile_group_aborts(
    root: Path,
    location: Path,
) -> tuple[list[dict[str, object]], set[str]]:
    updates: list[dict[str, object]] = []
    processed: set[str] = set()
    for path in sorted((location / "groups" / "active").glob("*.json")):
        group = read_json(path)
        if group.get("status") not in {"aborted", "aborting", "abort-needs-attention"}:
            continue
        update, transaction_ids = reconcile_group_abort(root, location, path, group)
        updates.append(update)
        processed.update(transaction_ids)
    return updates, processed
