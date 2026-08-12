"""Fact-checked recovery for transaction initialization Git resources."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import git_backend as git
from . import git_effects
from .control_plane import ControlPlane
from .events import emit
from .storage import now, read_json, replace_json


def _run_git(
    root: Path,
    *arguments: str,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        pass_fds=pass_fds,
    )


def _branch_head(root: Path, branch: str) -> str | None:
    completed = _run_git(
        root,
        "show-ref",
        "--verify",
        "--hash",
        f"refs/heads/{branch}",
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _facts(
    root: Path,
    plane: ControlPlane,
    record: dict[str, object],
) -> tuple[Path, str, str, bool, str | None, str | None]:
    transaction_id = str(record.get("transaction_id"))
    checkout = git.exact_managed_checkout(
        plane.state_root, transaction_id, record.get("checkout")
    )
    branch = str(record.get("branch"))
    if branch != f"dev-mesh/tx/{transaction_id}":
        raise ValueError("initialization branch is outside the exact managed transaction namespace")
    base = str(record.get("base_revision"))
    if len(base) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in base
    ):
        raise ValueError("initialization base revision is malformed")
    worktrees = git.registered_worktrees(root)
    registered = checkout in worktrees
    return (
        checkout,
        branch,
        base,
        registered,
        worktrees.get(checkout),
        _branch_head(root, branch),
    )


def _checkout_matches_base(checkout: Path, base: str, *, head_available: bool) -> bool:
    if head_available:
        try:
            return git.head(checkout) == base and not git.dirty_paths(checkout)
        except (RuntimeError, ValueError):
            return False

    # A deleted branch makes the linked checkout's symbolic HEAD unresolvable.
    # Compare each durable Git layer with the recorded base instead: index to
    # base, worktree to index, and the complete untracked-file set.
    index = _run_git(
        checkout,
        "diff",
        "--cached",
        "--quiet",
        "--exit-code",
        base,
        "--",
    )
    worktree = _run_git(checkout, "diff", "--quiet", "--exit-code", "--")
    untracked = _run_git(checkout, "ls-files", "--others", "--exclude-standard")
    return (
        index.returncode == 0
        and worktree.returncode == 0
        and untracked.returncode == 0
        and not untracked.stdout.strip()
    )


def validate_promoted_resources(
    root: Path,
    plane: ControlPlane,
    record: dict[str, object],
) -> None:
    checkout, branch, base, registered, registered_branch, branch_head = _facts(
        root, plane, record
    )
    if (
        branch_head != base
        or not checkout.is_dir()
        or not registered
        or registered_branch != f"refs/heads/{branch}"
        or not _checkout_matches_base(checkout, base, head_available=True)
    ):
        raise ValueError(
            "transaction Claim advanced but exact initializing Git resources are invalid"
        )


def _terminal_event_exists(plane: ControlPlane, transaction_id: str) -> bool:
    for event_path in sorted((plane.state_root / "events").glob("*.json")):
        event = read_json(event_path, base=plane.state_root)
        if (
            event.get("event") == "transaction-aborted"
            and event.get("transaction_id") == transaction_id
        ):
            return True
    return False


def _persist_pending(
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
    message: str,
    *,
    attention: bool,
) -> dict[str, object]:
    record.update(
        {
            "status": "initialization-needs-attention" if attention else "initializing",
            "initialization_rollback_pending": True,
            "initialization_error": message[:2000],
            "updated_at": now(),
        }
    )
    replace_json(path, record, base=plane.state_root)
    return record


def _finalize(
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
) -> dict[str, object]:
    transaction_id = str(record["transaction_id"])
    if not _terminal_event_exists(plane, transaction_id):
        emit(
            plane,
            "transaction-aborted",
            transaction_id=transaction_id,
            payload={
                "transaction_id": transaction_id,
                "scope": record.get("scope"),
                "owner": record.get("owner"),
                "run_id": record.get("run_id"),
                "status": "aborted",
                "reason_code": "initialization-rolled-back",
                "reason": "Initialization stopped before Claim promotion; exact unchanged Git resources were removed.",
            },
        )
    record.update(
        {
            "status": "aborted",
            "reason_code": "initialization-rolled-back",
            "aborted_at": now(),
        }
    )
    record.pop("initialization_error", None)
    record.pop("initialization_rollback_pending", None)
    replace_json(path, record, base=plane.state_root)
    destination = plane.state_root / "transactions" / "archive" / path.name
    os.replace(path, destination)
    return {**record, "archive": str(destination)}


def rollback_initializing(
    root: Path,
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
) -> dict[str, object]:
    """Remove only exact, clean initialization resources, persisting each observed effect."""

    try:
        checkout, branch, base, registered, registered_branch, branch_head = _facts(
            root, plane, record
        )
        if registered:
            if (
                not checkout.is_dir()
                or registered_branch != f"refs/heads/{branch}"
                or not _checkout_matches_base(
                    checkout,
                    base,
                    head_available=branch_head is not None,
                )
                or branch_head not in {None, base}
            ):
                return _persist_pending(
                    plane,
                    path,
                    record,
                    "initialization checkout or branch changed before rollback",
                    attention=True,
                )
            record.update(
                {
                    "status": "initializing",
                    "initialization_worktree_removing_at": now(),
                    "initialization_rollback_pending": True,
                }
            )
            replace_json(path, record, base=plane.state_root)
            removed = git_effects.run(
                plane,
                str(record["transaction_id"]),
                _run_git,
                root,
                "worktree",
                "remove",
                "--force",
                str(checkout),
            )
            if removed.returncode != 0:
                return _persist_pending(
                    plane,
                    path,
                    record,
                    removed.stderr.strip() or "initialization worktree removal failed",
                    attention=False,
                )
            record.update(
                {
                    "status": "initializing",
                    "initialization_worktree_removed_at": now(),
                    "initialization_rollback_pending": True,
                }
            )
            replace_json(path, record, base=plane.state_root)
        elif checkout.exists():
            return _persist_pending(
                plane,
                path,
                record,
                "managed initialization checkout exists outside the Git worktree registry",
                attention=True,
            )

        branch_head = _branch_head(root, branch)
        if branch_head is not None:
            if branch_head != base:
                return _persist_pending(
                    plane,
                    path,
                    record,
                    "initialization branch changed before rollback",
                    attention=True,
                )
            record.update(
                {
                    "status": "initializing",
                    "initialization_branch_removing_at": now(),
                    "initialization_rollback_pending": True,
                }
            )
            replace_json(path, record, base=plane.state_root)
            removed = git_effects.run(
                plane,
                str(record["transaction_id"]),
                _run_git,
                root,
                "branch",
                "-D",
                branch,
            )
            if removed.returncode != 0:
                return _persist_pending(
                    plane,
                    path,
                    record,
                    removed.stderr.strip() or "initialization branch removal failed",
                    attention=False,
                )
            record.update(
                {
                    "status": "initializing",
                    "initialization_branch_removed_at": now(),
                    "initialization_rollback_pending": True,
                }
            )
            replace_json(path, record, base=plane.state_root)

        checkout, _branch, _base, registered, _registered_branch, branch_head = _facts(
            root, plane, record
        )
        if checkout.exists() or registered or branch_head is not None:
            return _persist_pending(
                plane,
                path,
                record,
                "initialization Git resources remain after rollback attempt",
                attention=False,
            )
        return _finalize(plane, path, record)
    except (OSError, RuntimeError, ValueError) as error:
        return _persist_pending(plane, path, record, str(error), attention=True)
