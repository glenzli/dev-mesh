"""Restart-safe transaction-group materialization and recovery policy."""

from __future__ import annotations

import copy
import os
import shutil
import time
from pathlib import Path

from . import git_backend as git
from .arbitration import record_paths
from .state import (
    archive_claim,
    emit_event,
    now,
    read_json,
    replace_json,
    transaction_path,
    validate_slug,
    write_json_exclusive,
)


TEST_CRASH_ENV = "SHARED_COORD_TEST_CRASH_POINT"
TERMINAL_TRANSACTION_STATES = {"aborted", "committed"}


class RecoveryAttention(RuntimeError):
    """Automatic recovery stopped because ownership or content is uncertain."""


def crash_if_testing(point: str) -> None:
    """Terminate only when an integration test explicitly selects this boundary."""
    if os.environ.get(TEST_CRASH_ENV) == point:
        os._exit(86)


def make_group_id() -> str:
    suffix = f"{time.time_ns():x}"[-20:]
    return validate_slug(f"group-{suffix}", "group id")


def group_path(location: Path, group_id: str) -> Path:
    return location / "groups" / "active" / f"{validate_slug(group_id, 'group id')}.json"


def active_groups(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return [
        (path, read_json(path))
        for path in sorted((location / "groups" / "active").glob("*.json"))
    ]


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


def _replace_group(path: Path, group: dict[str, object]) -> None:
    group["updated_at"] = now()
    replace_json(path, group)


def create_group(
    location: Path,
    mode: str,
    reason: str,
    steward: str,
    canonical_branch: str,
    base_revision: str,
    transactions: list[dict[str, object]],
) -> tuple[Path, dict[str, object]]:
    group_id = make_group_id()
    members: list[dict[str, object]] = []
    for transaction in transactions:
        transaction["group_id"] = group_id
        members.append(
            {
                "transaction_id": transaction["transaction_id"],
                "scope": transaction["scope"],
                "owner": transaction["owner"],
                "materialization": "planned",
                "claim_promoted": False,
                "planned_transaction": copy.deepcopy(transaction),
            }
        )
    group: dict[str, object] = {
        "schema": 2,
        "group_id": group_id,
        "mode": mode,
        "reason": reason,
        "steward": steward,
        "canonical_branch": canonical_branch,
        "base_revision": base_revision,
        "status": "planned",
        "claims_promoted": False,
        "members": members,
        "created_at": now(),
    }
    path = group_path(location, group_id)
    write_json_exclusive(path, group)
    emit_event(
        location,
        "group-planned",
        None,
        {"group_id": group_id, "transactions": [item["transaction_id"] for item in members]},
    )
    crash_if_testing("group-planned")
    return path, group


def group_declared_paths(group: dict[str, object]) -> list[str]:
    return sorted(
        {
            path
            for member in _members(group)
            if member.get("transaction_status") not in TERMINAL_TRANSACTION_STATES
            for path in record_paths(_planned_transaction(member))
        }
    )


def _transaction_record_matches(
    existing: dict[str, object],
    planned: dict[str, object],
) -> bool:
    keys = (
        "group_id",
        "transaction_id",
        "scope",
        "owner",
        "base_revision",
        "branch",
        "checkout",
    )
    return all(existing.get(key) == planned.get(key) for key in keys)


def _ensure_transaction_record(
    location: Path,
    planned: dict[str, object],
) -> tuple[Path, dict[str, object]]:
    transaction_id = str(planned["transaction_id"])
    path = transaction_path(location, transaction_id)
    if not path.exists():
        write_json_exclusive(path, copy.deepcopy(planned))
        emit_event(
            location,
            "transaction-recorded",
            transaction_id,
            {"group_id": planned["group_id"]},
        )
        return path, copy.deepcopy(planned)
    existing = read_json(path)
    if not _transaction_record_matches(existing, planned):
        raise RecoveryAttention(
            f"transaction record {transaction_id!r} does not match its durable group plan"
        )
    return path, existing


def _materialization_issues(
    root: Path,
    planned: dict[str, object],
) -> list[str]:
    transaction_id = str(planned["transaction_id"])
    checkout = Path(str(planned["checkout"]))
    branch = str(planned["branch"])
    base = str(planned["base_revision"])
    issues: list[str] = []
    branch_revision = git.branch_head(root, branch)
    path_entry = git.worktree_for_path(root, checkout)
    branch_entry = git.worktree_for_branch(root, branch)

    if branch_revision is None:
        issues.append(f"{transaction_id}: branch {branch!r} is missing")
    elif branch_revision != base:
        issues.append(
            f"{transaction_id}: branch is at {branch_revision}, "
            f"expected materialization base {base}"
        )
    if not checkout.exists():
        issues.append(f"{transaction_id}: checkout {checkout} is missing")
    if path_entry is None:
        issues.append(f"{transaction_id}: checkout is not a registered Git worktree")
    elif path_entry.get("branch") != f"refs/heads/{branch}":
        issues.append(f"{transaction_id}: checkout is registered to another branch")
    if branch_entry is None:
        issues.append(f"{transaction_id}: branch has no registered worktree")
    elif Path(str(branch_entry.get("worktree", ""))).resolve() != checkout.resolve():
        issues.append(f"{transaction_id}: branch is registered at another checkout")

    if issues:
        return issues
    if git.current_head(checkout) != base:
        issues.append(f"{transaction_id}: checkout HEAD differs from its materialization base")
    operations = git.operation_in_progress(checkout)
    if operations:
        issues.append(
            f"{transaction_id}: checkout has active Git operations: "
            + ", ".join(operations)
        )
    dirty = git.status_paths(checkout)
    if dirty:
        issues.append(
            f"{transaction_id}: checkout contains uncommitted work: {', '.join(dirty)}"
        )
    return issues


def _ensure_materialized(root: Path, planned: dict[str, object]) -> None:
    checkout = Path(str(planned["checkout"]))
    branch = str(planned["branch"])
    base = str(planned["base_revision"])
    branch_revision = git.branch_head(root, branch)
    path_entry = git.worktree_for_path(root, checkout)
    branch_entry = git.worktree_for_branch(root, branch)

    if branch_revision is None and not checkout.exists() and branch_entry is None:
        git.materialize(root, checkout, branch, base)
    elif (
        branch_revision == base
        and not checkout.exists()
        and path_entry is None
        and branch_entry is None
    ):
        git.materialize_existing(root, checkout, branch)

    issues = _materialization_issues(root, planned)
    if issues:
        raise RecoveryAttention("; ".join(issues))


def _mark_attention(
    location: Path,
    path: Path,
    group: dict[str, object],
    issue: str,
) -> None:
    group["status"] = "needs-attention"
    group["recovery_issues"] = [issue]
    group["attention_at"] = now()
    _replace_group(path, group)
    emit_event(
        location,
        "group-needs-attention",
        None,
        {"group_id": group["group_id"], "issue": issue},
    )


def _archived_claim_exists(location: Path, scope: str, transaction_id: str) -> bool:
    for path in (location / "archive" / "claims").glob(f"*-{scope}.json"):
        if read_json(path).get("transaction_id") == transaction_id:
            return True
    return False


def _promote_claims(
    location: Path,
    path: Path,
    group: dict[str, object],
) -> None:
    for member in _members(group):
        if member.get("claim_promoted") is True:
            continue
        planned = _planned_transaction(member)
        scope = str(planned["scope"])
        transaction_id = str(planned["transaction_id"])
        claim = location / "claims" / f"{scope}.json"
        if claim.exists():
            record = read_json(claim)
            if (
                record.get("scope") != scope
                or record.get("owner") != planned.get("owner")
                or record_paths(record) != record_paths(planned)
            ):
                raise RecoveryAttention(
                    f"claim {scope!r} no longer matches transaction {transaction_id!r}"
                )
            record["status"] = "promoted"
            record["transaction_id"] = transaction_id
            record["group_id"] = group["group_id"]
            record["promoted_at"] = now()
            archive_claim(location, claim, record)
            crash_if_testing(f"claim-archived:{scope}")
        elif not _archived_claim_exists(location, scope, transaction_id):
            raise RecoveryAttention(
                f"claim {scope!r} is missing and has no matching promotion archive"
            )
        member["claim_promoted"] = True
        member["claim_promoted_at"] = now()
        _replace_group(path, group)
        crash_if_testing(f"claim-promoted:{scope}")
    group["claims_promoted"] = True
    group.pop("recovery_issues", None)
    group.pop("attention_at", None)
    _replace_group(path, group)
    emit_event(
        location,
        "group-claims-promoted",
        None,
        {"group_id": group["group_id"]},
    )


def materialize_group(
    root: Path,
    location: Path,
    path: Path,
    group: dict[str, object],
) -> list[dict[str, object]]:
    if group.get("status") == "active" and group.get("claims_promoted") is True:
        return [
            read_json(transaction_path(location, str(member["transaction_id"])))
            for member in _members(group)
        ]

    group["status"] = "materializing"
    group.pop("recovery_issues", None)
    group.pop("attention_at", None)
    _replace_group(path, group)
    live: list[tuple[Path, dict[str, object]]] = []
    try:
        for member in _members(group):
            planned = _planned_transaction(member)
            transaction_record_path, transaction = _ensure_transaction_record(location, planned)
            member["materialization"] = "recorded"
            _replace_group(path, group)
            crash_if_testing(f"member-recorded:{planned['scope']}")
            emit_event(
                location,
                "materialize-started",
                str(planned["transaction_id"]),
                {
                    "group_id": group["group_id"],
                    "base_revision": planned["base_revision"],
                    "branch": planned["branch"],
                },
            )
            _ensure_materialized(root, planned)
            member["materialization"] = "materialized"
            member["materialized_at"] = now()
            _replace_group(path, group)
            emit_event(
                location,
                "materialize-completed",
                str(planned["transaction_id"]),
                {"group_id": group["group_id"], "base_revision": planned["base_revision"]},
            )
            crash_if_testing(f"member-materialized:{planned['scope']}")
            live.append((transaction_record_path, transaction))

        for transaction_record_path, transaction in live:
            if transaction.get("status") == "materializing":
                transaction["status"] = "active"
                transaction["activated_at"] = now()
                replace_json(transaction_record_path, transaction)
                emit_event(
                    location,
                    "transaction-activated",
                    str(transaction["transaction_id"]),
                    {"group_id": group["group_id"]},
                )
            elif transaction.get("status") != "active":
                raise RecoveryAttention(
                    f"transaction {transaction['transaction_id']!r} advanced before "
                    "its group activated"
                )
        crash_if_testing("members-activated")
        group["status"] = "active"
        group["activated_at"] = now()
        _replace_group(path, group)
        emit_event(
            location,
            "group-activated",
            None,
            {"group_id": group["group_id"]},
        )
        crash_if_testing("group-activated")
        _promote_claims(location, path, group)
    except (OSError, ValueError, git.GitError, RecoveryAttention) as error:
        _mark_attention(location, path, group, str(error))
        raise RecoveryAttention(str(error)) from error
    return [read_json(transaction_record_path) for transaction_record_path, _ in live]


def require_transaction_group_active(
    location: Path,
    transaction: dict[str, object],
) -> None:
    group_id = transaction.get("group_id")
    if group_id is None:
        return
    if not isinstance(group_id, str):
        raise ValueError("transaction group id is malformed")
    path = group_path(location, group_id)
    if not path.exists():
        raise ValueError(f"active transaction group does not exist: {group_id}")
    group = read_json(path)
    if group.get("status") != "active" or group.get("claims_promoted") is not True:
        raise ValueError(
            f"transaction group {group_id!r} is not fully active; run tx reconcile"
        )


def reconcile_groups(
    root: Path,
    location: Path,
) -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for path, group in active_groups(location):
        group_id = str(group.get("group_id", path.stem))
        if group.get("status") == "closed":
            archive = _archive_closed_group(location, path, group)
            updates.append(
                {
                    "kind": "group",
                    "group_id": group_id,
                    "action": "closed",
                    "archive": str(archive),
                }
            )
            continue
        if group.get("status") == "active" and group.get("claims_promoted") is True:
            updates.append({"kind": "group", "group_id": group_id, "action": "unchanged"})
            continue
        try:
            materialize_group(root, location, path, group)
        except (OSError, ValueError, git.GitError, RecoveryAttention) as error:
            updates.append(
                {
                    "kind": "group",
                    "group_id": group_id,
                    "action": "needs-attention",
                    "issue": str(error),
                }
            )
            continue
        updates.append({"kind": "group", "group_id": group_id, "action": "activated"})
    return updates


def _archive_closed_group(
    location: Path,
    path: Path,
    group: dict[str, object],
) -> Path:
    archive = location / "groups" / "archive" / f"{time.time_ns()}-{path.name}"
    shutil.move(path, archive)
    emit_event(location, "group-closed", None, {"group_id": group["group_id"]})
    return archive


def mark_group_member_terminal(
    location: Path,
    transaction: dict[str, object],
    terminal_status: str,
) -> Path | None:
    if terminal_status not in TERMINAL_TRANSACTION_STATES:
        raise ValueError(f"unsupported terminal transaction status: {terminal_status}")
    group_id = transaction.get("group_id")
    if group_id is None:
        return None
    if not isinstance(group_id, str):
        raise ValueError("transaction group id is malformed")
    path = group_path(location, group_id)
    if not path.exists():
        return None
    group = read_json(path)
    transaction_id = str(transaction["transaction_id"])
    matched = False
    for member in _members(group):
        if member.get("transaction_id") == transaction_id:
            member["transaction_status"] = terminal_status
            member["terminal_at"] = now()
            matched = True
            break
    if not matched:
        raise ValueError(f"transaction {transaction_id!r} is absent from group {group_id!r}")
    emit_event(
        location,
        "group-member-terminal",
        transaction_id,
        {"group_id": group_id, "status": terminal_status},
    )
    if all(
        member.get("transaction_status") in TERMINAL_TRANSACTION_STATES
        for member in _members(group)
    ):
        group["status"] = "closed"
        group["closed_at"] = now()
        _replace_group(path, group)
        crash_if_testing("group-closed-recorded")
        return _archive_closed_group(location, path, group)
    _replace_group(path, group)
    return None
