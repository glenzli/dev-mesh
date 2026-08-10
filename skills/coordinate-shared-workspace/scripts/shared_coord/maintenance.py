"""Durable terminal cleanup and read-only managed-resource diagnostics."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import git_backend as git
from .state import (
    active_transactions,
    crash_if_testing,
    emit_event,
    now,
    read_json,
    replace_json,
    validate_slug,
    write_json_exclusive,
)


CLEANUP_DISPOSITIONS = {"discard", "published"}


class MaintenanceAttention(RuntimeError):
    """Automatic maintenance stopped before an unsafe resource mutation."""


def cleanup_path(location: Path, transaction_id: str) -> Path:
    return (
        location
        / "cleanups"
        / "active"
        / f"{validate_slug(transaction_id, 'transaction id')}.json"
    )


def active_cleanups(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return [
        (path, read_json(path))
        for path in sorted((location / "cleanups" / "active").glob("*.json"))
    ]


def _managed_checkout_issue(location: Path, checkout: Path) -> str | None:
    managed_root = (location / "checkouts").resolve()
    try:
        relative = checkout.resolve().relative_to(managed_root)
    except ValueError:
        return f"checkout is outside the coordinator-managed directory: {checkout}"
    if len(relative.parts) != 1:
        return f"checkout is not a direct managed transaction directory: {checkout}"
    return None


def _capture_checkout_facts(
    root: Path,
    checkout: Path,
    branch: str,
) -> tuple[list[str], list[str], str | None, str | None]:
    path_entry = git.worktree_for_path(root, checkout)
    branch_entry = git.worktree_for_branch(root, branch)
    if checkout.exists() and path_entry is None:
        return [], [], None, "checkout exists but is not a registered Git worktree"
    if path_entry is not None and path_entry.get("branch") != f"refs/heads/{branch}":
        return [], [], None, "checkout is registered to another branch"
    if branch_entry is not None:
        registered = Path(str(branch_entry.get("worktree", ""))).resolve()
        if registered != checkout.resolve():
            return [], [], None, "transaction branch is registered at another checkout"
    if not checkout.exists():
        return [], [], None, None
    return (
        sorted(git.status_paths(checkout)),
        sorted(git.operation_in_progress(checkout)),
        git.checkout_fingerprint(checkout),
        None,
    )


def plan_cleanup(
    root: Path,
    location: Path,
    transaction: dict[str, object],
    disposition: str,
    authorized_by: str,
    reason: str,
) -> tuple[Path, dict[str, object]]:
    if disposition not in CLEANUP_DISPOSITIONS:
        raise ValueError(f"unsupported cleanup disposition: {disposition}")
    transaction_id = validate_slug(str(transaction["transaction_id"]), "transaction id")
    path = cleanup_path(location, transaction_id)
    if path.exists():
        existing = read_json(path)
        if (
            existing.get("transaction_id") != transaction_id
            or existing.get("disposition") != disposition
        ):
            raise ValueError(f"cleanup intent does not match transaction {transaction_id!r}")
        return path, existing

    checkout = Path(str(transaction.get("checkout", "")))
    branch = str(transaction.get("branch", ""))
    managed_issue = _managed_checkout_issue(location, checkout)
    dirty: list[str] = []
    operations: list[str] = []
    fingerprint: str | None = None
    identity_issue = managed_issue
    if identity_issue is None:
        dirty, operations, fingerprint, identity_issue = _capture_checkout_facts(
            root, checkout, branch
        )
    record: dict[str, object] = {
        "schema": 1,
        "cleanup_id": transaction_id,
        "transaction_id": transaction_id,
        "group_id": transaction.get("group_id"),
        "transaction_status": transaction.get("status"),
        "disposition": disposition,
        "authorized_by": authorized_by,
        "reason": reason,
        "checkout": str(checkout),
        "branch": branch,
        "candidate": transaction.get("committed_revision", transaction.get("candidate")),
        "expected_branch_head": git.branch_head(root, branch) if branch else None,
        "expected_dirty_paths": dirty,
        "expected_operations": operations,
        "expected_checkout_fingerprint": fingerprint,
        "status": "planned" if identity_issue is None else "needs-attention",
        "created_at": now(),
    }
    if identity_issue is not None:
        record["issue"] = identity_issue
    write_json_exclusive(path, record)
    emit_event(
        location,
        "cleanup-planned",
        transaction_id,
        {"disposition": disposition, "authorized_by": authorized_by},
    )
    crash_if_testing("cleanup-planned")
    return path, record


def attach_transaction_archive(
    path: Path,
    cleanup: dict[str, object],
    transaction_archive: Path,
) -> None:
    cleanup["transaction_archive"] = str(transaction_archive)
    cleanup["transaction_archived_at"] = now()
    replace_json(path, cleanup)


def reauthorize_discard(
    root: Path,
    location: Path,
    transaction_id: str,
    owner: str,
    reason: str,
) -> tuple[Path, dict[str, object]]:
    path = cleanup_path(location, transaction_id)
    if not path.exists():
        raise ValueError(f"active cleanup does not exist: {transaction_id}")
    cleanup = read_json(path)
    if cleanup.get("disposition") != "discard":
        raise ValueError("only an explicitly discarded transaction can be reauthorized")
    if cleanup.get("authorized_by") != owner:
        raise ValueError(
            f"discard belongs to {cleanup.get('authorized_by')!r}, not {owner!r}"
        )
    checkout = Path(str(cleanup.get("checkout", "")))
    branch = str(cleanup.get("branch", ""))
    managed_issue = _managed_checkout_issue(location, checkout)
    if managed_issue is not None:
        raise MaintenanceAttention(managed_issue)
    dirty, operations, fingerprint, identity_issue = _capture_checkout_facts(
        root, checkout, branch
    )
    if identity_issue is not None:
        raise MaintenanceAttention(identity_issue)
    cleanup["expected_branch_head"] = git.branch_head(root, branch)
    cleanup["expected_dirty_paths"] = dirty
    cleanup["expected_operations"] = operations
    cleanup["expected_checkout_fingerprint"] = fingerprint
    cleanup["status"] = "planned"
    cleanup["reason"] = reason
    cleanup["reauthorized_at"] = now()
    cleanup.pop("issue", None)
    replace_json(path, cleanup)
    emit_event(
        location,
        "cleanup-reauthorized",
        transaction_id,
        {"authorized_by": owner, "reason": reason},
    )
    return path, cleanup


def _mark_attention(
    location: Path,
    path: Path,
    cleanup: dict[str, object],
    issue: str,
) -> dict[str, object]:
    cleanup["status"] = "needs-attention"
    cleanup["issue"] = issue
    cleanup["attention_at"] = now()
    cleanup["attempts"] = int(cleanup.get("attempts", 0)) + 1
    replace_json(path, cleanup)
    emit_event(
        location,
        "cleanup-needs-attention",
        str(cleanup["transaction_id"]),
        {"issue": issue},
    )
    return {
        "kind": "cleanup",
        "transaction": cleanup["transaction_id"],
        "action": "needs-attention",
        "issue": issue,
    }


def _archive_completed_cleanup(
    location: Path,
    path: Path,
    cleanup: dict[str, object],
) -> dict[str, object]:
    cleanup["status"] = "completed"
    cleanup["completed_at"] = now()
    cleanup.pop("issue", None)
    replace_json(path, cleanup)
    crash_if_testing("cleanup-completed-recorded")
    archive = location / "cleanups" / "archive" / f"{time.time_ns()}-{path.name}"
    shutil.move(path, archive)
    emit_event(
        location,
        "cleanup-completed",
        str(cleanup["transaction_id"]),
        {"archive": str(archive)},
    )
    return {
        "kind": "cleanup",
        "transaction": cleanup["transaction_id"],
        "action": "completed",
        "archive": str(archive),
    }


def execute_cleanup(
    root: Path,
    location: Path,
    path: Path,
    cleanup: dict[str, object],
) -> dict[str, object]:
    checkout = Path(str(cleanup.get("checkout", "")))
    branch = str(cleanup.get("branch", ""))
    disposition = str(cleanup.get("disposition", ""))
    try:
        managed_issue = _managed_checkout_issue(location, checkout)
        if managed_issue is not None:
            raise MaintenanceAttention(managed_issue)
        if disposition not in CLEANUP_DISPOSITIONS:
            raise MaintenanceAttention(f"cleanup disposition is invalid: {disposition!r}")

        path_entry = git.worktree_for_path(root, checkout)
        branch_entry = git.worktree_for_branch(root, branch)
        current_branch_head = git.branch_head(root, branch)
        expected_branch_head = cleanup.get("expected_branch_head")
        if current_branch_head is not None and current_branch_head != expected_branch_head:
            raise MaintenanceAttention(
                f"transaction branch moved from {expected_branch_head} to {current_branch_head}"
            )
        if checkout.exists() and path_entry is None:
            raise MaintenanceAttention(
                "managed checkout path exists but is not a registered Git worktree"
            )
        if path_entry is not None and path_entry.get("branch") != f"refs/heads/{branch}":
            raise MaintenanceAttention("managed checkout is registered to another branch")
        if branch_entry is not None:
            registered = Path(str(branch_entry.get("worktree", ""))).resolve()
            if registered != checkout.resolve():
                raise MaintenanceAttention(
                    f"transaction branch is registered at another checkout: {registered}"
                )
        if not checkout.exists() and (path_entry is not None or branch_entry is not None):
            raise MaintenanceAttention("Git still registers a missing transaction checkout")

        if checkout.exists():
            dirty = sorted(git.status_paths(checkout))
            operations = sorted(git.operation_in_progress(checkout))
            if disposition == "published":
                if dirty or operations:
                    raise MaintenanceAttention(
                        "published checkout is no longer clean: "
                        + ", ".join(dirty + operations)
                    )
            else:
                expected_dirty = sorted(str(item) for item in cleanup["expected_dirty_paths"])
                expected_operations = sorted(
                    str(item) for item in cleanup["expected_operations"]
                )
                if dirty != expected_dirty or operations != expected_operations:
                    raise MaintenanceAttention(
                        "discard target changed after authorization; obtain fresh owner approval"
                    )
                if git.checkout_fingerprint(checkout) != cleanup.get(
                    "expected_checkout_fingerprint"
                ):
                    raise MaintenanceAttention(
                        "discard target content changed after authorization; "
                        "obtain fresh owner approval"
                    )
            git.remove_worktree(
                root,
                checkout,
                force=disposition == "discard",
            )
            cleanup["worktree_removed_at"] = now()
            replace_json(path, cleanup)
            crash_if_testing("cleanup-worktree-removed")

        current_branch_head = git.branch_head(root, branch)
        if current_branch_head is not None:
            if current_branch_head != expected_branch_head:
                raise MaintenanceAttention(
                    f"transaction branch moved from {expected_branch_head} to {current_branch_head}"
                )
            if disposition == "published":
                candidate = cleanup.get("candidate")
                if not isinstance(candidate, str) or not git.is_ancestor(
                    root, candidate, git.current_head(root)
                ):
                    raise MaintenanceAttention(
                        "published candidate is not contained in canonical HEAD"
                    )
            git.delete_branch(root, branch, force=disposition == "discard")
            cleanup["branch_removed_at"] = now()
            replace_json(path, cleanup)
            crash_if_testing("cleanup-branch-removed")
        return _archive_completed_cleanup(location, path, cleanup)
    except (OSError, ValueError, git.GitError, MaintenanceAttention) as error:
        return _mark_attention(location, path, cleanup, str(error))


def reconcile_cleanups(
    root: Path,
    location: Path,
    excluded_transactions: set[str] | None = None,
) -> list[dict[str, object]]:
    excluded = excluded_transactions or set()
    return [
        execute_cleanup(root, location, path, cleanup)
        for path, cleanup in active_cleanups(location)
        if cleanup.get("transaction_id") not in excluded
    ]


def _planned_group_transactions(location: Path) -> list[dict[str, object]]:
    transactions: list[dict[str, object]] = []
    for path in sorted((location / "groups" / "active").glob("*.json")):
        group = read_json(path)
        if group.get("status") in {"aborted", "aborting", "abort-needs-attention"}:
            continue
        members = group.get("members", [])
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            if member.get("transaction_status") in {"aborted", "committed"}:
                continue
            planned = member.get("planned_transaction")
            if isinstance(planned, dict):
                transactions.append(planned)
    return transactions


def doctor(root: Path, location: Path) -> dict[str, object]:
    expected: dict[str, dict[str, object]] = {}

    def include(record: dict[str, object], source: str) -> None:
        transaction_id = record.get("transaction_id")
        branch = record.get("branch")
        checkout = record.get("checkout")
        if not all(isinstance(value, str) and value for value in (transaction_id, branch, checkout)):
            return
        identity = expected.setdefault(
            str(transaction_id),
            {"branch": branch, "checkout": checkout, "sources": []},
        )
        sources = identity["sources"]
        assert isinstance(sources, list)
        if source not in sources:
            sources.append(source)

    for _, transaction in active_transactions(location):
        include(transaction, "active-transaction")
    for transaction in _planned_group_transactions(location):
        include(transaction, "active-group")
    for _, cleanup in active_cleanups(location):
        include(cleanup, "active-cleanup")

    findings: list[dict[str, object]] = []
    cleanup_ids = {str(record["transaction_id"]) for _, record in active_cleanups(location)}
    for path in sorted((location / "transactions" / "archive").glob("*.json")):
        transaction = read_json(path)
        transaction_id = str(transaction.get("transaction_id", ""))
        if transaction.get("cleanup_pending") and transaction_id not in cleanup_ids:
            include(transaction, "legacy-cleanup-pending")
            findings.append(
                {
                    "kind": "cleanup-journal-missing",
                    "transaction": transaction_id,
                    "path": str(path),
                }
            )

    expected_branches = {
        str(identity["branch"]): transaction_id
        for transaction_id, identity in expected.items()
    }
    expected_checkouts = {
        str(Path(str(identity["checkout"])).resolve()): transaction_id
        for transaction_id, identity in expected.items()
    }
    actual_branches = git.branches(root, "agent-tx/")
    for branch in actual_branches:
        if branch not in expected_branches:
            findings.append({"kind": "orphan-branch", "branch": branch})

    managed_root = (location / "checkouts").resolve()
    registered_managed: set[str] = set()
    for entry in git.worktrees(root):
        value = entry.get("worktree")
        if not isinstance(value, str):
            continue
        checkout = Path(value).resolve()
        try:
            checkout.relative_to(managed_root)
        except ValueError:
            continue
        checkout_text = str(checkout)
        registered_managed.add(checkout_text)
        if checkout_text not in expected_checkouts:
            findings.append(
                {
                    "kind": "orphan-worktree",
                    "checkout": checkout_text,
                    "branch": entry.get("branch"),
                }
            )

    if managed_root.exists():
        for child in sorted(managed_root.iterdir()):
            child_text = str(child.resolve())
            if child_text not in expected_checkouts:
                findings.append({"kind": "orphan-checkout-path", "checkout": child_text})
            elif child_text not in registered_managed:
                findings.append(
                    {"kind": "unregistered-managed-checkout", "checkout": child_text}
                )

    for transaction_id, identity in expected.items():
        sources = identity["sources"]
        assert isinstance(sources, list)
        if sources == ["active-cleanup"] or sources == ["legacy-cleanup-pending"]:
            continue
        branch = str(identity["branch"])
        checkout = str(Path(str(identity["checkout"])).resolve())
        if branch not in actual_branches:
            findings.append(
                {"kind": "missing-branch", "transaction": transaction_id, "branch": branch}
            )
        if checkout not in registered_managed:
            findings.append(
                {
                    "kind": "missing-worktree",
                    "transaction": transaction_id,
                    "checkout": checkout,
                }
            )

    for _, cleanup in active_cleanups(location):
        if cleanup.get("status") == "needs-attention":
            findings.append(
                {
                    "kind": "cleanup-needs-attention",
                    "transaction": cleanup.get("transaction_id"),
                    "issue": cleanup.get("issue"),
                }
            )
    return {
        "status": "attention" if findings else "ok",
        "findings": findings,
        "managed": {
            "expected_transactions": sorted(expected),
            "transaction_branches": sorted(actual_branches),
            "registered_checkouts": sorted(registered_managed),
        },
    }
