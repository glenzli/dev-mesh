"""Optional one-commit Git microtransactions with fast-forward publication."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid
from pathlib import Path

from . import canonical_git
from . import git_backend as git
from . import git_effects
from . import transaction_cleanup as cleanup
from . import transaction_materialization as materialization_recovery
from .contention import get_decision
from .constants import MAX_TRANSACTION_CHANGED_PATHS
from .control_plane import ControlPlane, operation
from .events import emit, materialized
from .storage import (
    advisory_lock,
    now,
    read_json,
    replace_json,
    require_identifier,
    require_slug,
    require_text,
    write_json_exclusive,
)


def _path(plane: ControlPlane, transaction_id: str, *, active: bool = True) -> Path:
    directory = "active" if active else "archive"
    return plane.state_root / "transactions" / directory / f"{transaction_id}.json"


def _read_active(plane: ControlPlane, transaction_id: str) -> tuple[Path, dict[str, object]]:
    transaction_id = require_identifier(transaction_id, "transaction id")
    path = _path(plane, transaction_id)
    return path, read_json(path, base=plane.state_root)


def _checkout(plane: ControlPlane, record: dict[str, object]) -> Path:
    return git.exact_managed_checkout(
        plane.state_root,
        str(record.get("transaction_id")),
        record.get("checkout"),
    )


def _assert_active_owner_run(
    plane: ControlPlane, record: dict[str, object], owner: str, run_id: str
) -> dict[str, object]:
    if record.get("owner") != owner:
        raise ValueError("transaction belongs to another owner")
    if record.get("run_id") != run_id:
        raise ValueError("transaction belongs to another exact owner Run")
    run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
    if run.get("owner") != owner or run.get("status") != "active":
        raise ValueError("transaction owner Run is not active; hand off or recover explicitly")
    return run


def _bounded_actual_paths(root: Path, base: str, candidate: str) -> list[str]:
    actual = git.changed_paths(root, base, candidate)
    if len(actual) > MAX_TRANSACTION_CHANGED_PATHS:
        raise ValueError(
            f"microtransaction changes more than {MAX_TRANSACTION_CHANGED_PATHS} paths; decompose it"
        )
    return actual


def _path_projection(paths: list[str]) -> dict[str, object]:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
    return {
        "actual_path_count": len(paths),
        "actual_paths_sha256": digest.hexdigest(),
        "actual_path_sample": paths[:16],
    }


def _transaction_event_exists(
    plane: ControlPlane, transaction_id: str, event_name: str
) -> bool:
    for event_path in sorted((plane.state_root / "events").glob("*.json")):
        event = read_json(event_path, base=plane.state_root)
        if event.get("event") == event_name and event.get("transaction_id") == transaction_id:
            return True
    return False


def _ensure_created_event(plane: ControlPlane, record: dict[str, object]) -> None:
    transaction_id = str(record["transaction_id"])
    if _transaction_event_exists(plane, transaction_id, "transaction-created"):
        return
    emit(
        plane,
        "transaction-created",
        transaction_id=transaction_id,
        payload={
            "transaction_id": transaction_id,
            "contention_id": record.get("contention_id"),
            "scope": record.get("scope"),
            "owner": record.get("owner"),
            "run_id": record.get("run_id"),
            "paths": record.get("paths", []),
            "semantic_resources": sorted(
                set(record.get("semantic_writes", [])) | set(record.get("sensitive_to", []))
            ),
            "base_revision": record.get("base_revision"),
            "branch": record.get("branch"),
            "recovered": record.get("begin_reconciled_at") is not None,
        },
    )


def _within(changed: str, declared: str) -> bool:
    changed_path = Path(changed)
    declared_path = Path(declared)
    return changed_path == declared_path or declared_path in changed_path.parents


def _run_git(
    root: Path,
    *arguments: str,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        pass_fds=pass_fds,
    )
    if check and completed.returncode != 0:
        raise git.GitCommandError(arguments, completed.returncode, completed.stderr)
    return completed


def begin(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    contention_id: str,
    reason: str,
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    contention_id = require_identifier(contention_id, "contention id")
    reason = require_text(reason, "transaction reason", 1000)
    root = git.repository_root(root)
    with operation(root, "transaction-begin") as plane:
        claim_path = plane.state_root / "claims" / f"{scope}.json"
        claim = read_json(claim_path, base=plane.state_root)
        if claim.get("owner") != owner or claim.get("run_id") != run_id:
            raise ValueError("transaction must promote the exact pending Claim owner and Run")
        if claim.get("status") != "pending-arbitration":
            raise ValueError("only a pending-arbitration Claim can become a microtransaction")
        if claim.get("projection_mode", "git-tree") != "git-tree":
            raise ValueError("workspace-bytes Claims must wait; they cannot use a Git microtransaction")
        run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
        if run.get("owner") != owner or run.get("status") != "active":
            raise ValueError("transaction requires the exact active Claim Run")
        decision = get_decision(plane, contention_id)
        if (
            decision.get("status") != "completed"
            or decision.get("decision") != "parallel-tx"
            or scope not in decision.get("scopes", [])
        ):
            raise ValueError("transaction requires an enacted parallel-tx contention decision")
        transaction_id = f"tx-{uuid.uuid4().hex}"
        branch = f"dev-mesh/tx/{transaction_id}"
        checkout = plane.state_root / "checkouts" / transaction_id
        base = git.head(root)
        canonical_branch = git.branch(root)
        record = materialized(
            {
                "schema": 1,
                "transaction_id": transaction_id,
                "contention_id": contention_id,
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "reason": reason,
                "paths": claim.get("paths", []),
                "semantic_writes": claim.get("semantic_writes", []),
                "sensitive_to": claim.get("sensitive_to", []),
                "status": "initializing",
                "base_revision": base,
                "canonical_branch": canonical_branch,
                "branch": branch,
                "checkout": str(checkout),
                "created_at": now(),
            }
        )
        write_json_exclusive(_path(plane, transaction_id), record, base=plane.state_root)
        try:
            git_effects.run(
                plane,
                transaction_id,
                _run_git,
                root,
                "worktree",
                "add",
                "-b",
                branch,
                str(checkout),
                base,
            )
            record["resources_materialized_at"] = now()
            replace_json(_path(plane, transaction_id), record, base=plane.state_root)
            claim.update(
                {
                    "status": "transaction",
                    "transaction_id": transaction_id,
                    "promoted_at": now(),
                }
            )
            replace_json(claim_path, claim, base=plane.state_root)
            record["status"] = "active"
            record["initialized_at"] = now()
            replace_json(_path(plane, transaction_id), record, base=plane.state_root)
            _ensure_created_event(plane, record)
            return record
        except BaseException:
            if claim_path.exists():
                current_claim = read_json(claim_path, base=plane.state_root)
                if current_claim.get("transaction_id") == transaction_id:
                    current_claim.update(
                        {
                            "status": "pending-arbitration",
                            "transaction_id": None,
                            "transaction_begin_rolled_back_at": now(),
                        }
                    )
                    replace_json(claim_path, current_claim, base=plane.state_root)
            transaction_path = _path(plane, transaction_id)
            if transaction_path.exists():
                current_record = read_json(transaction_path, base=plane.state_root)
                materialization_recovery.rollback_initializing(
                    root,
                    plane,
                    transaction_path,
                    current_record,
                )
            raise


def prepare(
    root: Path,
    *,
    transaction_id: str,
    owner: str,
    owner_run_id: str,
    summary: str,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    owner_run_id = require_identifier(owner_run_id, "owner run id")
    summary = require_text(summary, "commit summary", 300)
    root = git.repository_root(root)
    with operation(root, "transaction-prepare") as plane:
        path, record = _read_active(plane, transaction_id)
        git_effects.require_idle(plane, transaction_id)
        _assert_active_owner_run(plane, record, owner, owner_run_id)
        if record.get("status") not in {"active", "prepared", "conflicted"}:
            raise ValueError("transaction cannot be prepared in its current state")
        checkout = _checkout(plane, record)
        declared = [item for item in record.get("paths", []) if isinstance(item, str)]
        observed = git.dirty_paths(checkout)
        outside = [path for path in observed if not any(_within(path, allowed) for allowed in declared)]
        if outside:
            raise ValueError("transaction changed paths outside its declaration: " + ", ".join(outside))
        state = str(record.get("status"))
        if state == "prepared" and observed:
            raise ValueError("prepared microtransaction cannot add a second commit")
        if state == "conflicted" and observed:
            raise ValueError("finish or abort the Git rebase in the isolated checkout before prepare")
        if state == "active" and observed:
            _run_git(checkout, "add", "--", *declared)
            _run_git(checkout, "commit", "-m", summary)
        candidate = git.head(checkout)
        base = str(record.get("refresh_target") if state == "conflicted" else record["base_revision"])
        if candidate == base:
            raise ValueError("transaction has no candidate commit")
        commit_count = int(str(git.run(checkout, "rev-list", "--count", f"{base}..{candidate}")).strip())
        if commit_count != 1:
            raise ValueError("microtransaction must contain exactly one candidate commit")
        actual = _bounded_actual_paths(checkout, base, candidate)
        outside = [path for path in actual if not any(_within(path, allowed) for allowed in declared)]
        if outside:
            raise ValueError("candidate commit exceeds declared paths: " + ", ".join(outside))
        if state == "prepared" and candidate == record.get("candidate_revision"):
            return {**record, "event_emitted": False}
        record.update(
            {
                "status": "prepared",
                "base_revision": base,
                "candidate_revision": candidate,
                "actual_paths": actual,
                **_path_projection(actual),
                "prepared_at": now(),
                "summary": summary,
            }
        )
        record.pop("validation", None)
        record.pop("refresh_target", None)
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "transaction-prepared",
            transaction_id=transaction_id,
            payload={
                "transaction_id": transaction_id,
                "contention_id": record.get("contention_id"),
                "scope": record.get("scope"),
                "owner": owner,
                "run_id": record.get("run_id"),
                "base_revision": base,
                "candidate_revision": candidate,
                **_path_projection(actual),
            },
        )
        return record


def validate(
    root: Path,
    *,
    transaction_id: str,
    owner: str,
    owner_run_id: str,
    evidence: str,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    owner_run_id = require_identifier(owner_run_id, "owner run id")
    evidence = require_text(evidence, "validation evidence", 2000)
    root = git.repository_root(root)
    with operation(root, "transaction-validate") as plane:
        path, record = _read_active(plane, transaction_id)
        git_effects.require_idle(plane, transaction_id)
        _assert_active_owner_run(plane, record, owner, owner_run_id)
        if record.get("status") != "prepared":
            raise ValueError("only the owner of a prepared transaction may validate")
        checkout = _checkout(plane, record)
        candidate = git.head(checkout)
        if candidate != record.get("candidate_revision") or git.dirty_paths(checkout):
            raise ValueError("validation candidate or checkout changed after prepare")
        record["status"] = "ready"
        record["validation"] = {
            "candidate_revision": candidate,
            "base_revision": record.get("base_revision"),
            "evidence": evidence,
            "validated_at": now(),
        }
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "transaction-validated",
            transaction_id=transaction_id,
            payload={
                "transaction_id": transaction_id,
                "scope": record.get("scope"),
                "owner": owner,
                "run_id": record.get("run_id"),
                "candidate_revision": candidate,
                "evidence": evidence,
            },
        )
        return record


def _refresh_to_prepared(
    root: Path,
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
    *,
    recovered: bool,
) -> dict[str, object]:
    transaction_id = str(record["transaction_id"])
    checkout = _checkout(plane, record)
    branch = str(record["branch"])
    target = str(record.get("refresh_target"))
    previous_candidate = str(record.get("refresh_previous_candidate"))
    expected_ref = f"refs/heads/{branch}"
    worktrees = git.registered_worktrees(root)
    if not checkout.is_dir() or worktrees.get(checkout) != expected_ref:
        raise ValueError("refresh checkout is not the exact registered transaction worktree")
    if git.rebase_in_progress(checkout):
        record.update(
            {
                "status": "conflicted",
                "conflict_error": "rebase was interrupted with unresolved state",
                "conflicted_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        return record
    if git.dirty_paths(checkout):
        record.update(
            {
                "status": "refresh-needs-attention",
                "refresh_error": "refresh checkout changed outside a recoverable rebase",
                "updated_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        return record
    candidate = git.head(checkout)
    branch_head = str(git.run(root, "rev-parse", branch)).strip()
    if branch_head != candidate:
        raise ValueError("refresh branch and checkout HEAD disagree")
    if candidate == previous_candidate:
        record.update(
            {
                "status": "ready",
                "refresh_retryable_at": now(),
            }
        )
        record.pop("refresh_error", None)
        replace_json(path, record, base=plane.state_root)
        return record
    if not git.is_ancestor(root, target, candidate):
        raise ValueError("refreshed candidate is not based on the recorded target")
    commit_count = int(
        str(git.run(checkout, "rev-list", "--count", f"{target}..{candidate}")).strip()
    )
    if commit_count != 1:
        raise ValueError("refreshed microtransaction must contain exactly one candidate commit")
    actual = _bounded_actual_paths(checkout, target, candidate)
    declared = [item for item in record.get("paths", []) if isinstance(item, str)]
    outside = [
        changed
        for changed in actual
        if not any(_within(changed, allowed) for allowed in declared)
    ]
    if outside:
        raise ValueError("refreshed candidate exceeds declared paths: " + ", ".join(outside))
    record.update(
        {
            "status": "prepared",
            "base_revision": target,
            "candidate_revision": candidate,
            "actual_paths": actual,
            **_path_projection(actual),
            "refreshed_at": now(),
            "refresh_recovered": recovered,
        }
    )
    record.pop("validation", None)
    record.pop("refresh_error", None)
    replace_json(path, record, base=plane.state_root)
    emit(
        plane,
        "transaction-refreshed",
        transaction_id=transaction_id,
        payload={
            "transaction_id": transaction_id,
            "scope": record.get("scope"),
            "owner": record.get("owner"),
            "run_id": record.get("run_id"),
            "base_revision": target,
            "candidate_revision": candidate,
            **_path_projection(actual),
            "recovered": recovered,
        },
    )
    return record


def _recover_refresh(
    root: Path,
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
) -> dict[str, object]:
    transaction_id = str(record["transaction_id"])
    git_effects.require_idle(plane, transaction_id)
    try:
        return _refresh_to_prepared(
            root,
            plane,
            path,
            record,
            recovered=True,
        )
    except (RuntimeError, ValueError) as error:
        record.update(
            {
                "status": "refresh-needs-attention",
                "refresh_error": str(error)[:2000],
                "updated_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        return record


def _restore_interrupted_publish(
    root: Path,
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
) -> str:
    transaction_id = str(record["transaction_id"])
    git_effects.require_idle(plane, transaction_id)
    current = git.head(root)
    candidate = str(record.get("publishing_candidate") or record.get("candidate_revision"))
    expected = str(record.get("publish_expected_head") or record.get("base_revision"))
    if current == candidate:
        return "completed"
    record.update(
        {
            "status": "ready",
            "publish_retryable_at": now(),
            "publish_previous_result": "not-applied" if current == expected else "stale",
        }
    )
    replace_json(path, record, base=plane.state_root)
    return "retryable" if current == expected else "stale"


def _archive_claim(plane: ControlPlane, record: dict[str, object], status: str) -> None:
    scope = str(record["scope"])
    path = plane.state_root / "claims" / f"{scope}.json"
    if not path.exists():
        return
    claim = read_json(path, base=plane.state_root)
    if claim.get("transaction_id") != record.get("transaction_id"):
        raise ValueError("transaction Claim correlation changed")
    claim.update({"status": status, f"{status}_at": now()})
    replace_json(path, claim, base=plane.state_root)
    destination = plane.state_root / "archive" / "claims" / f"{time.time_ns()}-{scope}.json"
    os.replace(path, destination)


def _finalize_published(root: Path, plane: ControlPlane, path: Path, record: dict[str, object]) -> dict[str, object]:
    transaction_id = str(record["transaction_id"])
    if record.get("status") != "published":
        existing_terminal = None
        for event_path in sorted((plane.state_root / "events").glob("*.json")):
            event = read_json(event_path, base=plane.state_root)
            if event.get("event") == "transaction-published" and event.get("transaction_id") == transaction_id:
                existing_terminal = event
                break
        if existing_terminal is None:
            emit(
                plane,
                "transaction-published",
                transaction_id=transaction_id,
                payload={
                    "transaction_id": transaction_id,
                    "contention_id": record.get("contention_id"),
                    "scope": record.get("scope"),
                    "owner": record.get("owner"),
                    "run_id": record.get("run_id"),
                    "candidate_revision": record.get("candidate_revision"),
                    "status": "published",
                    "reason_code": "fast-forward-completed",
                    **_path_projection(
                        [item for item in record.get("actual_paths", []) if isinstance(item, str)]
                    ),
                    "publish_steward": record.get("publish_steward"),
                    "publish_steward_run_id": record.get("publish_steward_run_id"),
                },
            )
        record.update({"status": "published", "published_at": now()})
        replace_json(path, record, base=plane.state_root)
    _archive_claim(plane, record, "published")
    cleanup_record = cleanup.plan(
        root,
        plane,
        record,
        disposition="published",
        actor_owner=str(record.get("publish_steward")),
        actor_run_id=str(record.get("publish_steward_run_id")),
    )
    destination = _path(plane, transaction_id, active=False)
    if path.exists():
        os.replace(path, destination)
    cleanup_result = cleanup.reconcile_one(root, plane, str(cleanup_record["cleanup_id"]))
    return {**record, "archive": str(destination), "cleanup": cleanup_result}


def publish(
    root: Path, *, transaction_id: str, steward: str, steward_run_id: str
) -> dict[str, object]:
    steward = require_slug(steward, "steward")
    steward_run_id = require_identifier(steward_run_id, "steward run id")
    root = git.repository_root(root)
    with operation(root, "transaction-publish") as plane:
        with (
            advisory_lock(plane.state_root / "locks" / "git-publish.lock"),
            git_effects.canonical_fence(plane) as canonical_fd,
        ):
            canonical_git.require_publish_allowed(plane)
            path, record = _read_active(plane, transaction_id)
            git_effects.require_idle(plane, transaction_id)
            for other_path in sorted(
                (plane.state_root / "transactions" / "active").glob("*.json")
            ):
                if other_path == path:
                    continue
                other = read_json(other_path, base=plane.state_root)
                if other.get("status") == "publishing":
                    raise ValueError(
                        "another transaction has an unresolved publication effect"
                    )
            steward_run = read_json(
                plane.state_root / "runs" / f"{steward_run_id}.json", base=plane.state_root
            )
            if steward_run.get("owner") != steward or steward_run.get("status") != "active":
                raise ValueError("publication steward requires an exact active Run")
            if git.branch(root) != record.get("canonical_branch"):
                raise ValueError("canonical branch changed after the transaction began")
            record["publish_steward"] = steward
            record["publish_steward_run_id"] = steward_run_id
            if record.get("status") == "refreshing":
                record = _recover_refresh(root, plane, path, record)
            if record.get("status") == "publishing":
                outcome = _restore_interrupted_publish(root, plane, path, record)
                if outcome == "completed":
                    return _finalize_published(root, plane, path, record)
            if record.get("status") not in {"ready", "published"}:
                raise ValueError("transaction must be ready before publication")
            candidate = str(record.get("candidate_revision"))
            current = git.head(root)
            if current == candidate:
                return _finalize_published(root, plane, path, record)
            if record.get("status") == "published":
                raise ValueError("published transaction candidate is no longer canonical HEAD")
            validation = record.get("validation")
            if not isinstance(validation, dict) or validation.get("candidate_revision") != candidate:
                raise ValueError("transaction validation is not bound to its candidate")
            if not git.index_is_empty(root):
                raise ValueError("canonical Git index must be empty before publication")
            base = str(record["base_revision"])
            checkout = _checkout(plane, record)
            branch_head = str(git.run(root, "rev-parse", str(record["branch"]))).strip()
            if branch_head != candidate or git.head(checkout) != candidate or git.dirty_paths(checkout):
                raise ValueError("transaction branch or checkout changed after validation")
            if current != base:
                git.assert_canonical_git_writable(root)
                record.update(
                    {
                        "status": "refreshing",
                        "refresh_previous_base": base,
                        "refresh_previous_candidate": candidate,
                        "refresh_target": current,
                        "refresh_started_at": now(),
                    }
                )
                replace_json(path, record, base=plane.state_root)
                rebased = git_effects.run(
                    plane,
                    transaction_id,
                    _run_git,
                    checkout,
                    "rebase",
                    current,
                    check=False,
                    pass_fds=(canonical_fd,),
                )
                if rebased.returncode != 0:
                    record.update(
                        {
                            "status": "conflicted",
                            "conflict_error": rebased.stderr.strip()[:2000],
                            "refresh_target": current,
                            "conflicted_at": now(),
                        }
                    )
                    replace_json(path, record, base=plane.state_root)
                    emit(
                        plane,
                        "transaction-conflicted",
                        transaction_id=transaction_id,
                        payload={
                            "transaction_id": transaction_id,
                            "scope": record.get("scope"),
                            "owner": record.get("owner"),
                            "run_id": record.get("run_id"),
                            "previous_base": base,
                            "current_head": current,
                        },
                    )
                    return record
                return _refresh_to_prepared(
                    root,
                    plane,
                    path,
                    record,
                    recovered=False,
                )
            actual = [item for item in record.get("actual_paths", []) if isinstance(item, str)]
            blocking_claims: list[str] = []
            for claim_path in sorted((plane.state_root / "claims").glob("*.json")):
                claim = read_json(claim_path, base=plane.state_root)
                if claim.get("transaction_id") == transaction_id or claim.get("status") not in {"active", "paused"}:
                    continue
                declared = [item for item in claim.get("paths", []) if isinstance(item, str)]
                if any(_within(changed, owned) or _within(owned, changed) for changed in actual for owned in declared):
                    blocking_claims.append(str(claim.get("scope")))
            if blocking_claims:
                raise ValueError(
                    "publication still overlaps active direct Claims: " + ", ".join(blocking_claims)
                )
            overlap = [
                dirty
                for dirty in git.dirty_paths(root)
                if any(_within(dirty, changed) or _within(changed, dirty) for changed in actual)
            ]
            if overlap:
                raise ValueError("canonical workspace has overlapping dirty paths: " + ", ".join(overlap))
            if not git.is_ancestor(root, current, candidate):
                raise ValueError("candidate cannot fast-forward canonical HEAD")
            git.assert_canonical_git_writable(root)
            record.update(
                {
                    "status": "publishing",
                    "publish_expected_head": current,
                    "publishing_candidate": candidate,
                    "publish_started_at": now(),
                }
            )
            replace_json(path, record, base=plane.state_root)
            git_effects.run(
                plane,
                transaction_id,
                _run_git,
                root,
                "merge",
                "--ff-only",
                candidate,
                pass_fds=(canonical_fd,),
            )
            if git.head(root) != candidate:
                raise RuntimeError("Git publication completed without reaching the candidate")
            return _finalize_published(root, plane, path, record)


def handoff(
    root: Path,
    *,
    transaction_id: str,
    owner: str,
    owner_run_id: str,
    next_owner: str,
    next_run_id: str,
    checkpoint: str,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    owner_run_id = require_identifier(owner_run_id, "owner run id")
    next_owner = require_slug(next_owner, "next owner")
    next_run_id = require_identifier(next_run_id, "next run id")
    checkpoint = require_text(checkpoint, "checkpoint", 2000)
    root = git.repository_root(root)
    with operation(root, "transaction-handoff") as plane:
        path, record = _read_active(plane, transaction_id)
        git_effects.require_idle(plane, transaction_id)
        if record.get("status") in {
            "publishing",
            "refreshing",
        }:
            raise ValueError("reconcile the transaction Git effect before handoff")
        claim_path = plane.state_root / "claims" / f"{record['scope']}.json"
        claim = read_json(claim_path, base=plane.state_root)
        already_handed_off = (
            record.get("owner") == next_owner
            and record.get("run_id") == next_run_id
            and record.get("previous_owner") == owner
            and record.get("previous_run_id") == owner_run_id
            and claim.get("owner") == next_owner
            and claim.get("run_id") == next_run_id
        )
        if already_handed_off:
            existing_event = False
            for event_path in sorted((plane.state_root / "events").glob("*.json")):
                event = read_json(event_path, base=plane.state_root)
                if (
                    event.get("event") == "transaction-handed-off"
                    and event.get("transaction_id") == transaction_id
                    and event.get("run_id") == next_run_id
                ):
                    existing_event = True
                    break
            if not existing_event:
                emit(
                    plane,
                    "transaction-handed-off",
                    transaction_id=transaction_id,
                    payload={
                        "transaction_id": transaction_id,
                        "scope": record.get("scope"),
                        "owner": next_owner,
                        "actor_owner": owner,
                        "work_owner": next_owner,
                        "run_id": next_run_id,
                        "source_run_id": owner_run_id,
                        "checkpoint": checkpoint,
                        "recovered": True,
                    },
                )
            return record
        if record.get("owner") != owner or record.get("run_id") != owner_run_id:
            raise ValueError("transaction belongs to another exact owner Run")
        _assert_active_owner_run(plane, record, owner, owner_run_id)
        run = read_json(plane.state_root / "runs" / f"{next_run_id}.json", base=plane.state_root)
        if run.get("owner") != next_owner or run.get("status") != "active":
            raise ValueError("next transaction owner has no matching active run")
        if claim.get("transaction_id") != transaction_id:
            raise ValueError("transaction Claim correlation changed during handoff")
        claim.update({"owner": next_owner, "run_id": next_run_id})
        replace_json(claim_path, claim, base=plane.state_root)
        previous_run_id = record.get("run_id")
        record.update(
            {
                "previous_owner": owner,
                "previous_run_id": record.get("run_id"),
                "owner": next_owner,
                "run_id": next_run_id,
                "checkpoint": checkpoint,
                "handed_off_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "transaction-handed-off",
            transaction_id=transaction_id,
            payload={
                "transaction_id": transaction_id,
                "scope": record.get("scope"),
                "owner": next_owner,
                "actor_owner": owner,
                "work_owner": next_owner,
                "run_id": next_run_id,
                "source_run_id": previous_run_id,
                "checkpoint": checkpoint,
            },
        )
        return record


def abort(
    root: Path,
    *,
    transaction_id: str,
    owner: str,
    owner_run_id: str,
    reason_code: str,
    reason: str,
    discard: bool,
) -> dict[str, object]:
    if not discard:
        raise ValueError("transaction abort requires explicit discard authorization")
    owner = require_slug(owner, "owner")
    owner_run_id = require_identifier(owner_run_id, "owner run id")
    reason_code = require_identifier(reason_code, "reason code")
    reason = require_text(reason, "abort reason", 1000)
    root = git.repository_root(root)
    with operation(root, "transaction-abort") as plane:
        path, record = _read_active(plane, transaction_id)
        git_effects.require_idle(plane, transaction_id)
        if record.get("status") in {
            "publishing",
            "refreshing",
        }:
            raise ValueError("reconcile the transaction Git effect before abort")
        if record.get("owner") != owner or record.get("run_id") != owner_run_id:
            raise ValueError("transaction belongs to another exact owner Run")
        _assert_active_owner_run(plane, record, owner, owner_run_id)
        if record.get("status") in {"published", "aborted"}:
            raise ValueError("terminal transaction cannot be aborted")
        if record.get("status") == "aborting" and (
            record.get("reason_code") != reason_code or record.get("reason") != reason
        ):
            raise ValueError("transaction already has a different durable abort authorization")
        record.update(
            {
                "status": "aborting",
                "reason_code": reason_code,
                "reason": reason,
                "abort_authorized_at": record.get("abort_authorized_at") or now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        cleanup_record = cleanup.plan(
            root,
            plane,
            record,
            disposition="discard",
            actor_owner=owner,
            actor_run_id=owner_run_id,
        )
        return _finalize_aborted(root, plane, path, record, cleanup_record)


def _finalize_aborted(
    root: Path,
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
    cleanup_record: dict[str, object],
) -> dict[str, object]:
    transaction_id = str(record["transaction_id"])
    existing_terminal = None
    for event_path in sorted((plane.state_root / "events").glob("*.json")):
        event = read_json(event_path, base=plane.state_root)
        if event.get("event") == "transaction-aborted" and event.get("transaction_id") == transaction_id:
            existing_terminal = event
            break
    if existing_terminal is None:
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
                "reason_code": record.get("reason_code"),
                "reason": record.get("reason"),
            },
        )
    record.update({"status": "aborted", "aborted_at": now()})
    replace_json(path, record, base=plane.state_root)
    claim_path = plane.state_root / "claims" / f"{record['scope']}.json"
    if claim_path.exists():
        claim = read_json(claim_path, base=plane.state_root)
        if claim.get("transaction_id") not in {transaction_id, None}:
            raise ValueError("transaction Claim correlation changed during abort")
        claim.update(
            {
                "status": "pending-arbitration",
                "transaction_id": None,
                "restored_at": claim.get("restored_at") or now(),
            }
        )
        replace_json(claim_path, claim, base=plane.state_root)
    destination = _path(plane, transaction_id, active=False)
    if path.exists():
        os.replace(path, destination)
    cleanup_result = cleanup.reconcile_one(root, plane, str(cleanup_record["cleanup_id"]))
    return {**record, "archive": str(destination), "cleanup": cleanup_result}


def authorize_cleanup(
    root: Path,
    *,
    transaction_id: str,
    owner: str,
    owner_run_id: str,
    reason: str,
) -> dict[str, object]:
    root = git.repository_root(root)
    with operation(root, "cleanup-authorize") as plane:
        return cleanup.authorize_changed_discard(
            root,
            plane,
            cleanup_id=transaction_id,
            owner=owner,
            run_id=owner_run_id,
            reason=reason,
        )


def reconcile(
    root: Path, *, steward: str, steward_run_id: str
) -> dict[str, object]:
    """Complete only Git effects proven by durable transaction and cleanup facts."""

    steward = require_slug(steward, "steward")
    steward_run_id = require_identifier(steward_run_id, "steward run id")
    root = git.repository_root(root)
    with operation(root, "transaction-reconcile") as plane:
        steward_run = read_json(
            plane.state_root / "runs" / f"{steward_run_id}.json", base=plane.state_root
        )
        if steward_run.get("owner") != steward or steward_run.get("status") != "active":
            raise ValueError("transaction reconcile requires an exact active steward Run")
        publications: list[dict[str, object]] = []
        aborts: list[dict[str, object]] = []
        in_flight: list[dict[str, object]] = []
        for path in sorted((plane.state_root / "transactions" / "active").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            transaction_id = str(record.get("transaction_id"))
            if git_effects.is_in_flight(plane, transaction_id):
                in_flight.append(
                    {
                        "transaction_id": transaction_id,
                        "status": record.get("status"),
                    }
                )
                continue
            if record.get("status") in {"initializing", "initialization-needs-attention"}:
                claim_path = plane.state_root / "claims" / f"{record['scope']}.json"
                claim = read_json(claim_path, base=plane.state_root)
                if (
                    claim.get("transaction_id") == record.get("transaction_id")
                    and claim.get("status") == "transaction"
                    and claim.get("owner") == record.get("owner")
                    and claim.get("run_id") == record.get("run_id")
                ):
                    materialization_recovery.validate_promoted_resources(
                        root,
                        plane,
                        record,
                    )
                    record.update({"status": "active", "begin_reconciled_at": now()})
                    replace_json(path, record, base=plane.state_root)
                    _ensure_created_event(plane, record)
                    continue
                if claim.get("status") == "pending-arbitration" and not claim.get("transaction_id"):
                    result = materialization_recovery.rollback_initializing(
                        root,
                        plane,
                        path,
                        record,
                    )
                    aborts.append(
                        {
                            "transaction_id": record.get("transaction_id"),
                            "action": (
                                "initialization-rolled-back"
                                if result.get("status") == "aborted"
                                else "initialization-rollback-pending"
                            ),
                            "result": result,
                        }
                    )
                    continue
            if record.get("status") == "refreshing":
                refreshed = _recover_refresh(root, plane, path, record)
                publications.append(
                    {
                        "transaction_id": transaction_id,
                        "action": (
                            "refresh-recovered"
                            if refreshed.get("status") == "prepared"
                            else "refresh-retryable"
                            if refreshed.get("status") == "ready"
                            else "refresh-conflicted"
                            if refreshed.get("status") == "conflicted"
                            else "refresh-needs-attention"
                        ),
                        "result": refreshed,
                    }
                )
                continue
            if record.get("status") == "publishing":
                canonical_git.require_publish_allowed(plane)
                git_effects.require_canonical_idle(plane)
                outcome = _restore_interrupted_publish(root, plane, path, record)
                if outcome == "completed":
                    record["publish_steward"] = record.get("publish_steward") or steward
                    record["publish_steward_run_id"] = (
                        record.get("publish_steward_run_id") or steward_run_id
                    )
                    result = _finalize_published(root, plane, path, record)
                    publications.append(
                        {
                            "transaction_id": transaction_id,
                            "action": "publication-completed",
                            "result": result,
                        }
                    )
                else:
                    publications.append(
                        {
                            "transaction_id": transaction_id,
                            "action": f"publication-{outcome}",
                            "result": record,
                        }
                    )
                continue
            if record.get("status") == "active":
                claim_path = plane.state_root / "claims" / f"{record['scope']}.json"
                claim = read_json(claim_path, base=plane.state_root)
                if claim.get("transaction_id") == record.get("transaction_id"):
                    _ensure_created_event(plane, record)
            if record.get("status") in {"aborting", "aborted"}:
                cleanup_record = cleanup.plan(
                    root,
                    plane,
                    record,
                    disposition="discard",
                    actor_owner=str(record.get("owner")),
                    actor_run_id=str(record.get("run_id")),
                )
                result = _finalize_aborted(root, plane, path, record, cleanup_record)
                aborts.append(
                    {
                        "transaction_id": record.get("transaction_id"),
                        "action": "abort-completed",
                        "result": result,
                    }
                )
                continue
            candidate = record.get("candidate_revision")
            if (
                record.get("status") in {"ready", "published"}
                and isinstance(candidate, str)
                and git.head(root) == candidate
            ):
                canonical_git.require_publish_allowed(plane)
                git_effects.require_canonical_idle(plane)
                record["publish_steward"] = steward
                record["publish_steward_run_id"] = steward_run_id
                result = _finalize_published(root, plane, path, record)
                publications.append(
                    {"transaction_id": record.get("transaction_id"), "action": "publication-completed", "result": result}
                )
        cleanups = cleanup.reconcile_all(root, plane)
        return {
            "publications": publications,
            "aborts": aborts,
            "cleanups": cleanups,
            "in_flight": in_flight,
        }


def doctor(root: Path) -> dict[str, object]:
    return cleanup.doctor(root)
