"""Recoverable direct commits serialized with canonical transaction publication."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid
from pathlib import Path

from . import git_backend as git
from . import git_effects
from .constants import MAX_CLAIM_PATHS, MAX_WORK_RESULTS_PER_COMMIT
from .control_plane import ControlPlane, operation
from .events import build_event, materialized, write_event
from .workspace_projection import declared_projection, path_projection, within
from .storage import (
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
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        pass_fds=pass_fds,
        env=environment,
    )
    if check and completed.returncode != 0:
        raise git.GitCommandError(arguments, completed.returncode, completed.stderr)
    return completed


def _path(plane: ControlPlane, direct_commit_id: str, *, active: bool = True) -> Path:
    direct_commit_id = require_identifier(direct_commit_id, "direct commit id")
    directory = "active" if active else "archive"
    return (
        plane.state_root
        / "direct-commits"
        / directory
        / f"{direct_commit_id}.json"
    )


def _staged_projection(base: str, tree: str, paths: list[str]) -> dict[str, object]:
    projected_paths = path_projection(paths)
    digest = hashlib.sha256()
    digest.update(base.encode("ascii"))
    digest.update(b"\0")
    digest.update(tree.encode("ascii"))
    return {
        "staged_tree": tree,
        "staged_diff_sha256": digest.hexdigest(),
        "staged_path_count": projected_paths["actual_path_count"],
        "staged_paths_sha256": projected_paths["actual_paths_sha256"],
        "staged_path_sample": projected_paths["actual_path_sample"],
    }


def _tree(root: Path, revision: str = "HEAD") -> str:
    return str(git.run(root, "rev-parse", f"{revision}^{{tree}}")).strip()


def _index_tree(root: Path) -> str:
    return str(git.run(root, "write-tree")).strip()


def _staged_paths(root: Path) -> list[str]:
    raw = git.run(
        root,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-renames",
        "HEAD",
        binary=True,
    )
    assert isinstance(raw, bytes)
    return sorted(
        path.decode("utf-8", errors="surrogateescape")
        for path in raw.split(b"\0")
        if path
    )


def _expected_projection(
    root: Path, plane: ControlPlane, declared: list[str], base_revision: str
) -> dict[str, object]:
    projection = declared_projection(
        root, plane, declared, base_revision, require_changes=True
    )
    return {
        **projection,
        "intended_worktree_sha256": projection["content_sha256"],
        "intended_paths": projection["actual_paths"],
    }


def _result_publication_blockers(
    plane: ControlPlane, declared: list[str]
) -> list[dict[str, object]]:
    """Return current writable Claims whose shared bytes still cover result paths."""

    blockers: list[dict[str, object]] = []
    for path in sorted((plane.state_root / "claims").glob("*.json")):
        claim = read_json(path, base=plane.state_root)
        if claim.get("intent") == "read":
            continue
        claim_paths = [item for item in claim.get("paths", []) if isinstance(item, str)]
        if not any(
            within(left, right) or within(right, left)
            for left in declared
            for right in claim_paths
        ):
            continue
        blockers.append(
            {
                "scope": claim.get("scope"),
                "owner": claim.get("owner"),
                "run_id": claim.get("run_id"),
                "status": claim.get("status"),
            }
        )
    return blockers


def _assert_projection_unchanged(
    root: Path, plane: ControlPlane, record: dict[str, object]
) -> None:
    projection = _expected_projection(
        root,
        plane,
        [path for path in record.get("paths", []) if isinstance(path, str)],
        str(record["base_revision"]),
    )
    for field in (
        "expected_index_tree",
        "intended_worktree_sha256",
        "actual_path_count",
        "actual_paths_sha256",
    ):
        if projection.get(field) != record.get(field):
            raise ValueError("declared-path content changed after direct commit intent")


def _active_records(plane: ControlPlane) -> list[tuple[Path, dict[str, object]]]:
    directory = plane.state_root / "direct-commits" / "active"
    return [
        (path, read_json(path, base=plane.state_root))
        for path in sorted(directory.glob("*.json"))
    ]


def _transaction_publication_pending(plane: ControlPlane) -> bool:
    for path in sorted((plane.state_root / "transactions" / "active").glob("*.json")):
        if read_json(path, base=plane.state_root).get("status") == "publishing":
            return True
    return False


def require_publish_allowed(plane: ControlPlane) -> None:
    if _active_records(plane):
        raise ValueError("an unresolved direct commit blocks transaction publication")


def _matching_event_paths(plane: ControlPlane, event: dict[str, object]) -> list[Path]:
    event_id = require_identifier(str(event.get("event_id")), "event id")
    event_name = require_identifier(str(event.get("event")), "event name")
    return sorted(
        (plane.state_root / "events").glob(f"*-{event_id}-{event_name}.json")
    )


def _ensure_exact_event(
    plane: ControlPlane, record: dict[str, object], field: str
) -> None:
    event = record.get(field)
    if not isinstance(event, dict):
        raise ValueError(f"direct commit lacks durable {field}")
    matches = _matching_event_paths(plane, event)
    if len(matches) > 1:
        raise ValueError(f"direct commit {field} has duplicate event ids")
    if matches:
        if read_json(matches[0], base=plane.state_root) != event:
            raise ValueError(f"direct commit {field} event facts changed")
        return
    write_event(plane, event)


def _ensure_started(plane: ControlPlane, record: dict[str, object]) -> None:
    _ensure_exact_event(plane, record, "started_event")


def _attention(
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
    error: BaseException | str,
) -> dict[str, object]:
    if isinstance(error, PermissionError):
        error_kind = "environment"
    elif isinstance(error, git.GitCommandError):
        error_kind = "git-effect"
    else:
        error_kind = "consistency"
    record.update(
        {
            "status": "needs-attention",
            "error_kind": error_kind,
            "error": str(error)[:2000],
            "updated_at": now(),
        }
    )
    replace_json(path, record, base=plane.state_root)
    return record


def _complete(
    plane: ControlPlane, path: Path, record: dict[str, object]
) -> dict[str, object]:
    direct_commit_id = str(record["direct_commit_id"])
    record.update(
        {
            "status": "completed",
            "completed_at": record.get("completed_at") or now(),
        }
    )
    replace_json(path, record, base=plane.state_root)
    _ensure_exact_event(plane, record, "terminal_event")
    destination = _path(plane, direct_commit_id, active=False)
    os.replace(path, destination)
    return {**record, "archive": str(destination)}


def _advance(
    root: Path,
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
    canonical_fd: int,
) -> dict[str, object]:
    branch = str(record["canonical_branch"])
    base = str(record["base_revision"])
    expected_tree = str(record["expected_index_tree"])
    current = git.head(root)
    index_tree = _index_tree(root)
    status = str(record.get("status"))
    if git.branch(root) != branch:
        raise ValueError("canonical branch changed during direct commit")
    candidate_value = record.get("candidate_revision")
    if isinstance(candidate_value, str) and current == candidate_value:
        recorded_paths = [
            item for item in record.get("staged_paths", []) if isinstance(item, str)
        ]
        for field, value in _staged_projection(
            base, expected_tree, recorded_paths
        ).items():
            if record.get(field) != value:
                raise ValueError("durable staged direct commit facts changed")
        if (
            _tree(root, candidate_value) != expected_tree
            or not git.index_is_empty(root)
        ):
            raise ValueError("completed direct commit Git facts disagree")
        return _complete(plane, path, record)
    if status in {"staging", "needs-attention"}:
        if current != base:
            raise ValueError("canonical HEAD changed before direct staging completed")
        base_tree = _tree(root, base)
        if index_tree == base_tree:
            _assert_projection_unchanged(root, plane, record)
            record.update({"status": "staging", "staging_started_at": now()})
            record.pop("error", None)
            replace_json(path, record, base=plane.state_root)
            _run_git(
                root,
                "add",
                "-A",
                "--",
                *[item for item in record.get("paths", []) if isinstance(item, str)],
                pass_fds=(canonical_fd,),
            )
            index_tree = _index_tree(root)
        if index_tree != expected_tree:
            raise ValueError("canonical index differs from the intended direct commit tree")
        staged_paths = _staged_paths(root)
        intended_paths = [
            item for item in record.get("intended_paths", []) if isinstance(item, str)
        ]
        if staged_paths != intended_paths:
            raise ValueError("canonical staged paths differ from the intended projection")
        record.update(
            {
                "status": "staging",
                "staged_paths": staged_paths,
                **_staged_projection(base, index_tree, staged_paths),
                "staged_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        candidate = _run_git(
            root,
            "commit-tree",
            expected_tree,
            "-p",
            base,
            "-m",
            str(record["summary"]),
            pass_fds=(canonical_fd,),
        ).stdout.strip()
        terminal_event = build_event(
            "direct-commit-completed",
            payload={
                "direct_commit_id": str(record["direct_commit_id"]),
                "scope": record.get("scope"),
                "owner": record.get("owner"),
                "run_id": record.get("run_id"),
                "status": "completed",
                "reason_code": "canonical-commit-completed",
                "base_revision": base,
                "candidate_revision": candidate,
                "work_result_ids": record.get("work_result_ids", []),
                **path_projection(staged_paths),
            },
        )
        record.update(
            {
                "status": "committing",
                "candidate_revision": candidate,
                "terminal_event": terminal_event,
                "committing_started_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        status = "committing"

    if status == "committing":
        candidate = str(record.get("candidate_revision"))
        current = git.head(root)
        staged_paths = _staged_paths(root) if current == base else [
            item for item in record.get("staged_paths", []) if isinstance(item, str)
        ]
        expected_staged = _staged_projection(base, expected_tree, staged_paths)
        for field, value in expected_staged.items():
            if record.get(field) != value:
                raise ValueError("durable staged direct commit facts changed")
        if current == base:
            if _index_tree(root) != expected_tree:
                raise ValueError("canonical index changed before direct commit publication")
            _run_git(
                root,
                "update-ref",
                f"refs/heads/{branch}",
                candidate,
                base,
                pass_fds=(canonical_fd,),
            )
            current = git.head(root)
        if current != candidate:
            raise ValueError("canonical HEAD is neither direct commit base nor candidate")
        if _tree(root, candidate) != expected_tree or not git.index_is_empty(root):
            raise ValueError("direct commit candidate or canonical index facts disagree")
        return _complete(plane, path, record)
    if status == "completed":
        return _complete(plane, path, record)
    raise ValueError("direct commit has an unsupported recovery status")


def _start_commit(
    root: Path,
    plane: ControlPlane,
    canonical_fd: int,
    *,
    scope: str,
    owner: str,
    run_id: str,
    declared: list[str],
    summary: str,
    validation_evidence: str,
    source_kind: str,
    work_result_ids: list[str],
) -> dict[str, object]:
    branch = git.branch(root)
    if not git.index_is_empty(root):
        raise ValueError("canonical Git index must be empty before direct commit")
    git.assert_canonical_git_writable(root)
    base = git.head(root)
    projection = _expected_projection(root, plane, declared, base)
    direct_commit_id = f"direct-commit-{uuid.uuid4().hex}"
    started_event = build_event(
        "direct-commit-started",
        payload={
            "direct_commit_id": direct_commit_id,
            "scope": scope,
            "owner": owner,
            "run_id": run_id,
            "status": "staging",
            "source_kind": source_kind,
            "work_result_ids": work_result_ids,
            "base_revision": base,
            "canonical_branch": branch,
            **path_projection(
                [
                    item
                    for item in projection.get("intended_paths", [])
                    if isinstance(item, str)
                ]
            ),
        },
    )
    record = materialized(
        {
            "schema": 1,
            "direct_commit_id": direct_commit_id,
            "scope": scope,
            "owner": owner,
            "run_id": run_id,
            "status": "staging",
            "source_kind": source_kind,
            "work_result_ids": work_result_ids,
            "canonical_branch": branch,
            "base_revision": base,
            "paths": declared,
            "summary": summary,
            "validation_evidence": validation_evidence,
            "started_event": started_event,
            **projection,
            "created_at": now(),
        }
    )
    path = _path(plane, direct_commit_id)
    write_json_exclusive(path, record, base=plane.state_root)
    _ensure_started(plane, record)
    try:
        return _advance(root, plane, path, record, canonical_fd)
    except (OSError, RuntimeError, ValueError) as error:
        return _attention(plane, path, record, error)


def commit(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    summary: str,
    validation_evidence: str,
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    summary = require_text(summary, "commit summary", 300)
    validation_evidence = require_text(
        validation_evidence, "validation evidence", 2000
    )
    root = git.repository_root(root)
    with operation(root, "direct-commit") as plane:
        with git_effects.canonical_fence(plane) as canonical_fd:
            if _active_records(plane):
                raise ValueError("another direct commit requires reconciliation")
            if _transaction_publication_pending(plane):
                raise ValueError("an unresolved transaction publication blocks direct commit")
            claim = read_json(
                plane.state_root / "claims" / f"{scope}.json", base=plane.state_root
            )
            run = read_json(
                plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root
            )
            if (
                claim.get("owner") != owner
                or claim.get("run_id") != run_id
                or claim.get("status") != "active"
                or claim.get("intent") == "read"
            ):
                raise ValueError("direct commit requires the exact active writable Claim")
            if claim.get("projection_mode", "git-tree") != "git-tree":
                raise ValueError("workspace-bytes Claims cannot publish through Git")
            if run.get("owner") != owner or run.get("status") != "active":
                raise ValueError("direct commit requires the exact active Claim Run")
            if git.branch(root) != claim.get("canonical_branch"):
                raise ValueError("canonical branch changed while the Claim was active")
            declared = [
                item for item in claim.get("paths", []) if isinstance(item, str)
            ]
            return _start_commit(
                root,
                plane,
                canonical_fd,
                scope=scope,
                owner=owner,
                run_id=run_id,
                declared=declared,
                summary=summary,
                validation_evidence=validation_evidence,
                source_kind="active-claim",
                work_result_ids=[],
            )


def commit_results(
    root: Path,
    *,
    result_ids: list[str],
    owner: str,
    run_id: str,
    summary: str,
    validation_evidence: str,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    summary = require_text(summary, "commit summary", 300)
    validation_evidence = require_text(
        validation_evidence, "validation evidence", 2000
    )
    result_ids = sorted({require_identifier(item, "result id") for item in result_ids})
    if not result_ids:
        raise ValueError("publish-results requires at least one Work Result")
    if len(result_ids) > MAX_WORK_RESULTS_PER_COMMIT:
        raise ValueError(
            f"publish-results accepts at most {MAX_WORK_RESULTS_PER_COMMIT} Work Results"
        )
    root = git.repository_root(root)
    with operation(root, "publish-results") as plane:
        with git_effects.canonical_fence(plane) as canonical_fd:
            if _active_records(plane):
                raise ValueError("another direct commit requires reconciliation")
            if _transaction_publication_pending(plane):
                raise ValueError("an unresolved transaction publication blocks result publication")
            run = read_json(
                plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root
            )
            if run.get("owner") != owner or run.get("status") != "active":
                raise ValueError("publish-results requires the exact active publisher Run")
            declared: list[str] = []
            for result_id in result_ids:
                result = read_json(
                    plane.state_root / "work-results" / f"{result_id}.json",
                    base=plane.state_root,
                )
                if result.get("result_id") != result_id or result.get("kind") != "dev-mesh.work-result":
                    raise ValueError("Work Result identity is malformed")
                if result.get("projection_mode", "git-tree") != "git-tree":
                    raise ValueError("workspace-bytes Work Results cannot publish through Git")
                for path in result.get("paths", []):
                    if isinstance(path, str) and path not in declared:
                        declared.append(path)
            if len(declared) > MAX_CLAIM_PATHS:
                raise ValueError(
                    f"published Work Results exceed the {MAX_CLAIM_PATHS}-path direct boundary"
                )
            blockers = _result_publication_blockers(plane, declared)
            if blockers:
                raise ValueError(
                    "published Work Result paths still have active editing authority: "
                    + repr(blockers[:16])
                )
            return _start_commit(
                root,
                plane,
                canonical_fd,
                scope="work-results",
                owner=owner,
                run_id=run_id,
                declared=declared,
                summary=summary,
                validation_evidence=validation_evidence,
                source_kind="work-results",
                work_result_ids=result_ids,
            )


def reconcile(
    root: Path, *, steward: str, steward_run_id: str
) -> dict[str, object]:
    steward = require_slug(steward, "steward")
    steward_run_id = require_identifier(steward_run_id, "steward run id")
    root = git.repository_root(root)
    with operation(root, "direct-commit-reconcile") as plane:
        run = read_json(
            plane.state_root / "runs" / f"{steward_run_id}.json",
            base=plane.state_root,
        )
        if run.get("owner") != steward or run.get("status") != "active":
            raise ValueError("direct commit reconcile requires an exact active steward Run")
        records = _active_records(plane)
        if not records:
            return {"completed": [], "attention": [], "in_flight": []}
        if len(records) != 1:
            raise ValueError("multiple unresolved direct commits require manual attention")
        path, record = records[0]
        direct_commit_id = str(record["direct_commit_id"])
        if git_effects.is_canonical_in_flight(plane):
            return {
                "completed": [],
                "attention": [],
                "in_flight": [direct_commit_id],
            }
        if _transaction_publication_pending(plane):
            raise ValueError("an unresolved transaction publication blocks direct reconcile")
        _ensure_started(plane, record)
        with git_effects.canonical_fence(plane) as canonical_fd:
            try:
                result = _advance(root, plane, path, record, canonical_fd)
            except (OSError, RuntimeError, ValueError) as error:
                result = _attention(plane, path, record, error)
        if result.get("status") == "completed":
            return {"completed": [result], "attention": [], "in_flight": []}
        return {"completed": [], "attention": [result], "in_flight": []}


def doctor(root: Path) -> dict[str, object]:
    root = git.repository_root(root)
    with operation(root, "direct-commit-doctor") as plane:
        return {
            "active_direct_commits": [record for _path, record in _active_records(plane)],
            "canonical_git_in_flight": git_effects.is_canonical_in_flight(plane),
        }
