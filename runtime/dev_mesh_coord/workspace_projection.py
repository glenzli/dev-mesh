"""Content-addressed projections of declared workspace paths."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from . import git_backend as git
from .constants import MAX_TRANSACTION_CHANGED_PATHS, MAX_WORKSPACE_BYTES
from .control_plane import ControlPlane


def _run_git(
    root: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Git command failed ({' '.join(arguments)}): "
            + completed.stderr.decode("utf-8", errors="replace").strip()
        )
    return completed


def within(changed: str, declared: str) -> bool:
    changed_path = Path(changed)
    declared_path = Path(declared)
    return changed_path == declared_path or declared_path in changed_path.parents


def path_projection(paths: list[str]) -> dict[str, object]:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    return {
        "actual_path_count": len(paths),
        "actual_paths_sha256": digest.hexdigest(),
        "actual_path_sample": paths[:16],
    }


def _git_projection_pathspecs(
    root: Path,
    declared: list[str],
    environment: dict[str, str],
) -> list[str]:
    """Return exact tracked/untracked files, omitting future absent pathspecs."""

    tracked = _run_git(
        root, "ls-files", "-z", "--", *declared, environment=environment
    ).stdout
    untracked = _run_git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *declared,
        environment=environment,
    ).stdout
    return sorted(
        {
            item.decode("utf-8", errors="surrogateescape")
            for raw in (tracked, untracked)
            for item in raw.split(b"\0")
            if item
        }
    )


def _workspace_file(root: Path, relative: str) -> dict[str, object]:
    candidate = root / relative
    if candidate.resolve(strict=False) != candidate:
        raise ValueError(f"workspace-bytes path must not traverse a symlink: {relative}")
    if git.path_is_tracked(root, relative):
        raise ValueError(f"workspace-bytes path is tracked by Git: {relative}")
    if not git.path_is_ignored(root, relative):
        raise ValueError(f"workspace-bytes path must be explicitly ignored by Git: {relative}")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"path": relative, "state": "missing", "size": 0, "sha256": None}
    except OSError as error:
        raise ValueError(f"cannot open workspace-bytes path {relative}: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"workspace-bytes path must be a regular file: {relative}")
        if before.st_size > MAX_WORKSPACE_BYTES:
            raise ValueError(
                f"workspace-bytes path exceeds the {MAX_WORKSPACE_BYTES}-byte limit: {relative}"
            )
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"workspace-bytes path changed while hashing: {relative}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"workspace-bytes path changed while hashing: {relative}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"workspace-bytes path changed while hashing: {relative}")
        return {
            "path": relative,
            "state": "file",
            "size": before.st_size,
            "sha256": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def workspace_bytes_projection(root: Path, declared: list[str]) -> dict[str, object]:
    """Hash exact ignored regular files without copying their contents into state."""

    canonical = root.resolve()
    entries = [_workspace_file(canonical, path) for path in sorted(declared)]
    total_bytes = sum(int(entry["size"]) for entry in entries)
    if total_bytes > MAX_WORKSPACE_BYTES:
        raise ValueError(
            f"workspace-bytes Claim exceeds the {MAX_WORKSPACE_BYTES}-byte total limit"
        )
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(str(entry["path"]).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(entry["state"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry.get("sha256") or "-").encode("ascii"))
        digest.update(b"\0")
    actual = [str(entry["path"]) for entry in entries if entry["state"] == "file"]
    content_sha256 = digest.hexdigest()
    return {
        "projection_mode": "workspace-bytes",
        "declared_content_sha256": content_sha256,
        "content_sha256": content_sha256,
        "workspace_bytes_sha256": content_sha256,
        "workspace_file_count": len(actual),
        "workspace_missing_path_count": len(entries) - len(actual),
        "workspace_total_bytes": total_bytes,
        "workspace_entries": entries,
        "actual_paths": actual,
        **path_projection(actual),
    }


def workspace_bytes_changed_paths(
    before: dict[str, object], after: dict[str, object]
) -> list[str]:
    before_entries = {
        str(entry.get("path")): entry
        for entry in before.get("workspace_entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    after_entries = {
        str(entry.get("path")): entry
        for entry in after.get("workspace_entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    return sorted(
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    )


def declared_projection(
    root: Path,
    plane: ControlPlane,
    declared: list[str],
    base_revision: str,
    *,
    require_changes: bool,
) -> dict[str, object]:
    """Project declared paths into an exact tree without mutating the canonical index."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="projection-index-", suffix=".tmp", dir=plane.state_root / "locks"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    environment = {**os.environ, "GIT_INDEX_FILE": str(temporary)}
    try:
        _run_git(root, "read-tree", base_revision, environment=environment)
        projection_pathspecs = _git_projection_pathspecs(root, declared, environment)
        if projection_pathspecs:
            _run_git(
                root,
                "add",
                "-A",
                "--",
                *projection_pathspecs,
                environment=environment,
            )
        expected_tree = _run_git(
            root, "write-tree", environment=environment
        ).stdout.decode("ascii").strip()
        raw_paths = _run_git(
            root,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-renames",
            base_revision,
            environment=environment,
        ).stdout
        paths = sorted(
            path.decode("utf-8", errors="surrogateescape")
            for path in raw_paths.split(b"\0")
            if path
        )
        index_entries = _run_git(
            root,
            "ls-files",
            "-s",
            "-z",
            "--",
            *declared,
            environment=environment,
        ).stdout
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if require_changes and not paths:
        raise ValueError("projection has no declared-path changes")
    if len(paths) > MAX_TRANSACTION_CHANGED_PATHS:
        raise ValueError(
            f"declared paths change more than {MAX_TRANSACTION_CHANGED_PATHS} paths"
        )
    outside = [
        path for path in paths if not any(within(path, allowed) for allowed in declared)
    ]
    if outside:
        raise ValueError("projection exceeds declared paths: " + ", ".join(outside))
    content_digest = hashlib.sha256()
    content_digest.update(base_revision.encode("ascii"))
    content_digest.update(b"\0")
    content_digest.update(expected_tree.encode("ascii"))
    content_digest.update(b"\0")
    for path in paths:
        content_digest.update(path.encode("utf-8", errors="surrogateescape"))
        content_digest.update(b"\0")
    declared_digest = hashlib.sha256()
    for path in sorted(declared):
        declared_digest.update(path.encode("utf-8", errors="surrogateescape"))
        declared_digest.update(b"\0")
    declared_digest.update(index_entries)
    return {
        "projection_mode": "git-tree",
        "expected_index_tree": expected_tree,
        "content_sha256": content_digest.hexdigest(),
        "declared_content_sha256": declared_digest.hexdigest(),
        "actual_paths": paths,
        **path_projection(paths),
    }
