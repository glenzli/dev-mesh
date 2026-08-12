"""Durable, fact-checked cleanup for terminal Git microtransactions."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from . import git_backend as git
from . import git_effects
from .control_plane import ControlPlane, resolve
from .events import emit, materialized
from .storage import (
    atomic_temp_residues,
    now,
    read_json,
    replace_json,
    require_identifier,
    require_slug,
    require_text,
    write_json_exclusive,
)


def _run_git(
    root: Path,
    *arguments: str,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        pass_fds=pass_fds,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip())
    return completed


def _active_path(plane: ControlPlane, cleanup_id: str) -> Path:
    return plane.state_root / "cleanups" / "active" / f"{cleanup_id}.json"


def _archive_path(plane: ControlPlane, cleanup_id: str) -> Path:
    return plane.state_root / "cleanups" / "archive" / f"{cleanup_id}.json"


def _managed_checkout(plane: ControlPlane, transaction_id: str, value: object) -> Path:
    return git.exact_managed_checkout(plane.state_root, transaction_id, value)


def _branch_head(root: Path, branch: str) -> str | None:
    completed = _run_git(root, "show-ref", "--verify", "--hash", f"refs/heads/{branch}", check=False)
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("ascii", errors="strict").strip()


def _expected_branch_ref(branch: str) -> str:
    return f"refs/heads/{branch}"


def _validate_registry_identity(
    root: Path,
    checkout: Path,
    branch: str,
    *,
    checkout_required: bool,
) -> dict[Path, str | None]:
    registry = git.registered_worktrees(root)
    expected_ref = _expected_branch_ref(branch)
    registered_branch = registry.get(checkout)
    if checkout_required and registered_branch != expected_ref:
        raise ValueError(
            "managed checkout is not registered to the exact transaction branch"
        )
    wrong_locations = sorted(
        str(path)
        for path, branch_ref in registry.items()
        if branch_ref == expected_ref and path != checkout
    )
    if wrong_locations:
        raise ValueError(
            "managed transaction branch is registered at another worktree: "
            + ", ".join(wrong_locations)
        )
    return registry


def _checkout_fingerprint(checkout: Path) -> str:
    digest = hashlib.sha256()
    digest.update(git.head(checkout).encode("ascii"))
    status = _run_git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    digest.update(status)
    for relative in git.dirty_paths(checkout):
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        raw_path = checkout / relative
        path = raw_path.resolve(strict=False)
        try:
            path.relative_to(checkout.resolve())
        except ValueError as error:
            raise ValueError("dirty cleanup path escapes the managed checkout") from error
        if raw_path.is_symlink():
            digest.update(b"L\0" + os.readlink(raw_path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(b"F\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.exists():
            digest.update(b"O\0")
        else:
            digest.update(b"M\0")
    return digest.hexdigest()


def _exact_active_run(plane: ControlPlane, owner: str, run_id: str) -> None:
    run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
    if run.get("owner") != owner or run.get("status") != "active":
        raise ValueError("cleanup authorization requires the exact active owner Run")


def _has_event(
    plane: ControlPlane,
    event_name: str,
    cleanup_id: str,
    *,
    authorization_revision: int | None = None,
) -> bool:
    for event_path in sorted((plane.state_root / "events").glob("*.json")):
        event = read_json(event_path, base=plane.state_root)
        if (
            event.get("event") == event_name
            and event.get("cleanup_id") == cleanup_id
            and (
                authorization_revision is None
                or event.get("authorization_revision") == authorization_revision
            )
        ):
            return True
    return False


def _ensure_authorized_event(plane: ControlPlane, record: dict[str, object]) -> None:
    cleanup_id = str(record["cleanup_id"])
    revision = int(record.get("authorization_revision", 0))
    if _has_event(
        plane,
        "cleanup-authorized",
        cleanup_id,
        authorization_revision=revision,
    ):
        return
    emit(
        plane,
        "cleanup-authorized",
        transaction_id=str(record["transaction_id"]),
        payload={
            "cleanup_id": cleanup_id,
            "transaction_id": record.get("transaction_id"),
            "owner": record.get("actor_owner"),
            "run_id": record.get("actor_run_id"),
            "disposition": record.get("disposition"),
            "authorization_revision": revision,
            "reason_code": record.get("authorization_reason_code"),
            "reason": record.get("authorization_reason"),
            "recovered": True,
        },
    )


def plan(
    root: Path,
    plane: ControlPlane,
    transaction: dict[str, object],
    *,
    disposition: str,
    actor_owner: str,
    actor_run_id: str,
) -> dict[str, object]:
    if disposition not in {"published", "discard"}:
        raise ValueError("cleanup disposition must be published or discard")
    transaction_id = require_identifier(str(transaction["transaction_id"]), "transaction id")
    actor_owner = require_slug(actor_owner, "cleanup actor owner")
    actor_run_id = require_identifier(actor_run_id, "cleanup actor run id")
    path = _active_path(plane, transaction_id)
    if path.exists():
        existing = read_json(path, base=plane.state_root)
        if (
            existing.get("transaction_id") != transaction_id
            or existing.get("disposition") != disposition
            or existing.get("branch") != transaction.get("branch")
            or existing.get("checkout") != transaction.get("checkout")
        ):
            raise ValueError("existing cleanup intent conflicts with the terminal transaction")
        return existing
    checkout = _managed_checkout(plane, transaction_id, transaction.get("checkout"))
    branch = str(transaction.get("branch"))
    if not branch.startswith("dev-mesh/tx/"):
        raise ValueError("cleanup branch is outside the managed transaction namespace")
    if disposition == "discard":
        if transaction.get("owner") != actor_owner or transaction.get("run_id") != actor_run_id:
            raise ValueError("discard cleanup must be authorized by the exact transaction owner Run")
        _exact_active_run(plane, actor_owner, actor_run_id)
    expected_head = _branch_head(root, branch)
    if expected_head is None:
        raise ValueError("cleanup cannot be planned after the managed branch disappeared")
    if not checkout.is_dir():
        raise ValueError("cleanup cannot be planned without the exact registered checkout")
    _validate_registry_identity(
        root,
        checkout,
        branch,
        checkout_required=True,
    )
    candidate = transaction.get("candidate_revision")
    if disposition == "published":
        if not isinstance(candidate, str) or expected_head != candidate:
            raise ValueError("published cleanup branch is not the exact candidate")
        if not git.is_ancestor(root, candidate, git.head(root)):
            raise ValueError("published cleanup candidate is not contained in canonical HEAD")
        if git.dirty_paths(checkout):
            raise ValueError("published cleanup checkout is unexpectedly dirty")
    record = materialized(
        {
            "schema": 1,
            "cleanup_id": transaction_id,
            "transaction_id": transaction_id,
            "owner": transaction.get("owner"),
            "run_id": transaction.get("run_id"),
            "actor_owner": actor_owner,
            "actor_run_id": actor_run_id,
            "checkout": str(checkout),
            "branch": branch,
            "expected_branch_head": expected_head,
            "candidate_revision": candidate,
            "disposition": disposition,
            "authorization_revision": 1,
            "authorization_reason_code": transaction.get("reason_code"),
            "authorization_reason": transaction.get("reason"),
            "authorized_fingerprint": _checkout_fingerprint(checkout) if disposition == "discard" else None,
            "status": "planned",
            "created_at": now(),
        }
    )
    write_json_exclusive(path, record, base=plane.state_root)
    _ensure_authorized_event(plane, record)
    return record


def _attention(
    plane: ControlPlane, path: Path, record: dict[str, object], message: str
) -> dict[str, object]:
    message = message[:2000]
    duplicate = record.get("status") == "needs-attention" and record.get("error") == message
    record.update({"status": "needs-attention", "error": message, "updated_at": now()})
    replace_json(path, record, base=plane.state_root)
    if not duplicate:
        emit(
            plane,
            "cleanup-needs-attention",
            transaction_id=str(record["transaction_id"]),
            payload={
                "cleanup_id": record.get("cleanup_id"),
                "transaction_id": record.get("transaction_id"),
                "owner": record.get("owner"),
                "run_id": record.get("run_id"),
                "status": "needs-attention",
                "reason_code": "cleanup-facts-changed",
                "error": message,
            },
        )
    return record


def reconcile_one(root: Path, plane: ControlPlane, cleanup_id: str) -> dict[str, object]:
    cleanup_id = require_identifier(cleanup_id, "cleanup id")
    path = _active_path(plane, cleanup_id)
    if not path.exists():
        archived = _archive_path(plane, cleanup_id)
        return read_json(archived, base=plane.state_root)
    record = read_json(path, base=plane.state_root)
    if git_effects.is_in_flight(plane, cleanup_id):
        return {**record, "git_effect_in_flight": True}
    _ensure_authorized_event(plane, record)
    branch = str(record.get("branch"))
    disposition = record.get("disposition")
    try:
        checkout = _managed_checkout(plane, cleanup_id, record.get("checkout"))
        registry = git.registered_worktrees(root)
        if checkout.exists():
            _validate_registry_identity(
                root,
                checkout,
                branch,
                checkout_required=True,
            )
            if disposition == "discard":
                if _checkout_fingerprint(checkout) != record.get("authorized_fingerprint"):
                    raise ValueError("discard target changed after authorization")
            else:
                candidate = str(record.get("candidate_revision"))
                if git.dirty_paths(checkout):
                    raise ValueError("published cleanup checkout became dirty")
                if not git.is_ancestor(root, candidate, git.head(root)):
                    raise ValueError("published candidate is no longer contained in canonical HEAD")
            arguments = ["worktree", "remove"]
            if disposition == "discard":
                arguments.append("--force")
            arguments.append(str(checkout))
            record.update(
                {
                    "status": "removing-worktree",
                    "worktree_removal_started_at": now(),
                }
            )
            replace_json(path, record, base=plane.state_root)
            git_effects.run(
                plane,
                cleanup_id,
                _run_git,
                root,
                *arguments,
            )
            record.update({"status": "worktree-removed", "worktree_removed_at": now()})
            replace_json(path, record, base=plane.state_root)
        elif checkout in registry:
            raise ValueError("Git still registers a missing managed checkout")
        else:
            _validate_registry_identity(
                root,
                checkout,
                branch,
                checkout_required=False,
            )

        branch_head = _branch_head(root, branch)
        if branch_head is not None:
            if branch_head != record.get("expected_branch_head"):
                raise ValueError("managed branch changed after cleanup authorization")
            if disposition == "published" and not git.is_ancestor(root, branch_head, git.head(root)):
                raise ValueError("published branch is not contained in canonical HEAD")
            _validate_registry_identity(
                root,
                checkout,
                branch,
                checkout_required=False,
            )
            record.update(
                {
                    "status": "removing-branch",
                    "branch_removal_started_at": now(),
                }
            )
            replace_json(path, record, base=plane.state_root)
            git_effects.run(
                plane,
                cleanup_id,
                _run_git,
                root,
                "branch",
                "-D" if disposition == "discard" else "-d",
                branch,
            )
            record.update({"status": "branch-removed", "branch_removed_at": now()})
            replace_json(path, record, base=plane.state_root)

        record.update({"status": "completed", "completed_at": record.get("completed_at") or now()})
        if not _has_event(plane, "cleanup-completed", cleanup_id):
            emit(
                plane,
                "cleanup-completed",
                transaction_id=str(record["transaction_id"]),
                payload={
                    "cleanup_id": record.get("cleanup_id"),
                    "transaction_id": record.get("transaction_id"),
                    "owner": record.get("owner"),
                    "run_id": record.get("run_id"),
                    "status": "completed",
                    "reason_code": "resources-removed",
                    "disposition": disposition,
                },
            )
        replace_json(path, record, base=plane.state_root)
        destination = _archive_path(plane, cleanup_id)
        try:
            os.replace(path, destination)
        except OSError as error:
            record.update(
                {
                    "status": "archive-pending",
                    "archive_error": str(error)[:2000],
                    "updated_at": now(),
                }
            )
            replace_json(path, record, base=plane.state_root)
            return record
        return {**record, "archive": str(destination)}
    except (OSError, RuntimeError, ValueError) as error:
        return _attention(plane, path, record, str(error))


def authorize_changed_discard(
    root: Path,
    plane: ControlPlane,
    *,
    cleanup_id: str,
    owner: str,
    run_id: str,
    reason: str,
) -> dict[str, object]:
    cleanup_id = require_identifier(cleanup_id, "cleanup id")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    reason = require_text(reason, "cleanup authorization reason", 1000)
    path = _active_path(plane, cleanup_id)
    record = read_json(path, base=plane.state_root)
    if record.get("disposition") != "discard" or record.get("owner") != owner:
        raise ValueError("only the discard owner may refresh cleanup authorization")
    _exact_active_run(plane, owner, run_id)
    git_effects.require_idle(plane, cleanup_id)
    checkout = _managed_checkout(plane, cleanup_id, record.get("checkout"))
    if not checkout.is_dir():
        raise ValueError("changed discard checkout is not available for owner review")
    _validate_registry_identity(
        root,
        checkout,
        str(record.get("branch")),
        checkout_required=True,
    )
    branch_head = _branch_head(root, str(record.get("branch")))
    if branch_head is None:
        raise ValueError("changed discard branch is missing")
    revision = int(record.get("authorization_revision", 0)) + 1
    record.update(
        {
            "actor_owner": owner,
            "actor_run_id": run_id,
            "expected_branch_head": branch_head,
            "authorized_fingerprint": _checkout_fingerprint(checkout),
            "authorization_revision": revision,
            "authorization_reason": reason,
            "status": "planned",
            "updated_at": now(),
        }
    )
    record.pop("error", None)
    replace_json(path, record, base=plane.state_root)
    emit(
        plane,
        "cleanup-authorized",
        transaction_id=cleanup_id,
        payload={
            "cleanup_id": cleanup_id,
            "transaction_id": record.get("transaction_id"),
            "owner": owner,
            "run_id": run_id,
            "disposition": "discard",
            "authorization_revision": revision,
            "reason": reason,
        },
    )
    return record


def reconcile_all(root: Path, plane: ControlPlane) -> list[dict[str, object]]:
    return [
        reconcile_one(root, plane, path.stem)
        for path in sorted((plane.state_root / "cleanups" / "active").glob("*.json"))
    ]


def doctor(root: Path) -> dict[str, object]:
    root = git.repository_root(root)
    plane = resolve(root)
    active = [
        read_json(path, base=plane.state_root)
        for path in sorted((plane.state_root / "transactions" / "active").glob("*.json"))
    ]
    cleanups = [
        read_json(path, base=plane.state_root)
        for path in sorted((plane.state_root / "cleanups" / "active").glob("*.json"))
    ]
    records = [*active, *cleanups]
    expected: set[Path] = set()
    expected_registry: dict[Path, str] = {}
    invalid_checkout_records: list[dict[str, object]] = []
    for item in records:
        try:
            checkout = _managed_checkout(
                plane, str(item["transaction_id"]), item["checkout"]
            )
        except ValueError as error:
            invalid_checkout_records.append(
                {
                    "transaction_id": item.get("transaction_id"),
                    "checkout": item.get("checkout"),
                    "error": str(error),
                }
            )
            continue
        expected.add(checkout)
        expected_registry[checkout] = _expected_branch_ref(str(item["branch"]))
    registered = git.registered_worktrees(root)
    checkouts_root = (plane.state_root / "checkouts").resolve()
    actual = {
        path
        for path in registered
        if path != root.resolve() and (path == checkouts_root or checkouts_root in path.parents)
    }
    checkout_directories = {
        path.resolve() for path in (plane.state_root / "checkouts").iterdir() if path.is_dir()
    }
    expected_branches = {str(item["branch"]) for item in records}
    actual_branches = {
        line
        for line in str(
            git.run(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/dev-mesh/tx")
        ).splitlines()
        if line
    }
    return {
        "active_transactions": active,
        "active_cleanups": cleanups,
        "orphan_checkouts": sorted(str(path) for path in actual - expected),
        "missing_checkouts": sorted(str(path) for path in expected - actual),
        "unregistered_checkout_directories": sorted(
            str(path) for path in checkout_directories - actual
        ),
        "orphan_branches": sorted(actual_branches - expected_branches),
        "missing_branches": sorted(expected_branches - actual_branches),
        "mismatched_checkout_branches": sorted(
            (
                {
                    "checkout": str(path),
                    "expected_branch": expected_ref,
                    "registered_branch": registered.get(path),
                }
                for path, expected_ref in expected_registry.items()
                if path in registered and registered.get(path) != expected_ref
            ),
            key=lambda item: str(item["checkout"]),
        ),
        "invalid_checkout_records": invalid_checkout_records,
        "atomic_temp_residues": [
            str(path) for path in atomic_temp_residues(plane.state_root)
        ],
        "cleanup_attention": [item for item in cleanups if item.get("status") == "needs-attention"],
    }
