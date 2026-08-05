"""Git-backed materialization, diff, refresh, publish, and cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .state import paths_overlap


class GitError(RuntimeError):
    """A Git command failed without a safe automatic continuation."""


def run_git(
    root: Path,
    *arguments: str,
    check: bool = True,
    binary: bool = False,
) -> str | bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=not binary,
        check=False,
    )
    if check and completed.returncode != 0:
        output = (
            completed.stdout.decode("utf-8", errors="replace")
            if isinstance(completed.stdout, bytes)
            else completed.stdout
        )
        command = " ".join(("git", *arguments))
        raise GitError(f"{command} failed in {root}:\n{output.rstrip()}")
    return completed.stdout


def git_returncode(root: Path, *arguments: str) -> int:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


def ensure_repository(root: Path) -> None:
    top = str(run_git(root, "rev-parse", "--show-toplevel")).strip()
    if Path(top).resolve() != root.resolve():
        raise ValueError(f"root must be the Git worktree root: expected {top}, got {root}")
    run_git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if git_returncode(root, "symbolic-ref", "--quiet", "HEAD") != 0:
        raise ValueError("canonical workspace must be on a named branch")


def current_head(root: Path) -> str:
    return str(run_git(root, "rev-parse", "HEAD^{commit}")).strip()


def current_branch(root: Path) -> str:
    return str(run_git(root, "symbolic-ref", "--short", "HEAD")).strip()


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def status_paths(root: Path) -> list[str]:
    output = run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        binary=True,
    )
    assert isinstance(output, bytes)
    tokens = output.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4:
            raise GitError("unexpected porcelain status record")
        status = token[:2].decode("ascii", errors="replace")
        path = _decode_path(token[3:])
        if path not in paths:
            paths.append(path)
        if "R" in status or "C" in status:
            if index >= len(tokens) or not tokens[index]:
                raise GitError("rename/copy status record is missing its source path")
            source = _decode_path(tokens[index])
            index += 1
            if source not in paths:
                paths.append(source)
    return paths


def diff_paths(root: Path, base: str, candidate: str) -> list[str]:
    output = run_git(
        root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        f"{base}..{candidate}",
        binary=True,
    )
    assert isinstance(output, bytes)
    tokens = output.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        raw_status = tokens[index]
        index += 1
        if not raw_status:
            continue
        status = raw_status.decode("ascii", errors="replace")
        path_count = 2 if status.startswith(("R", "C")) else 1
        for _ in range(path_count):
            if index >= len(tokens) or not tokens[index]:
                raise GitError("unexpected name-status diff record")
            path = _decode_path(tokens[index])
            index += 1
            if path not in paths:
                paths.append(path)
    return paths


def paths_within_scope(actual: list[str], declared: list[str]) -> list[str]:
    return [
        path
        for path in actual
        if not any(paths_overlap(path, allowed) for allowed in declared)
    ]


def operation_in_progress(root: Path) -> list[str]:
    active: list[str] = []
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-apply",
        "rebase-merge",
    ):
        location = Path(str(run_git(root, "rev-parse", "--git-path", name)).strip())
        if not location.is_absolute():
            location = root / location
        if location.exists():
            active.append(name)
    return active


def index_is_empty(root: Path) -> bool:
    return git_returncode(root, "diff", "--cached", "--quiet", "--exit-code") == 0


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return git_returncode(root, "merge-base", "--is-ancestor", ancestor, descendant) == 0


def commits_ahead(root: Path, base: str, candidate: str = "HEAD") -> int:
    return int(str(run_git(root, "rev-list", "--count", f"{base}..{candidate}")).strip())


def branch_exists(root: Path, branch: str) -> bool:
    return git_returncode(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}") == 0


def branch_head(root: Path, branch: str) -> str | None:
    if not branch_exists(root, branch):
        return None
    return str(run_git(root, "rev-parse", f"refs/heads/{branch}^{{commit}}")).strip()


def worktrees(root: Path) -> list[dict[str, str | bool]]:
    output = run_git(root, "worktree", "list", "--porcelain", "-z", binary=True)
    assert isinstance(output, bytes)
    entries: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for raw in output.split(b"\0"):
        if not raw:
            if current:
                entries.append(current)
                current = {}
            continue
        line = _decode_path(raw)
        key, separator, value = line.partition(" ")
        current[key] = value if separator else True
    if current:
        entries.append(current)
    return entries


def worktree_for_path(root: Path, checkout: Path) -> dict[str, str | bool] | None:
    expected = checkout.resolve()
    for entry in worktrees(root):
        value = entry.get("worktree")
        if isinstance(value, str) and Path(value).resolve() == expected:
            return entry
    return None


def worktree_for_branch(root: Path, branch: str) -> dict[str, str | bool] | None:
    expected = f"refs/heads/{branch}"
    for entry in worktrees(root):
        if entry.get("branch") == expected:
            return entry
    return None


def ensure_local_exclude(root: Path, state_directory: str) -> None:
    git_dir = Path(str(run_git(root, "rev-parse", "--git-dir")).strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    pattern = f"/{state_directory.strip('/')}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern in existing.splitlines():
        return
    with exclude.open("a", encoding="utf-8") as stream:
        if existing and not existing.endswith("\n"):
            stream.write("\n")
        stream.write(pattern + "\n")


def materialize(
    root: Path,
    checkout: Path,
    branch: str,
    base: str,
) -> None:
    if checkout.exists():
        raise ValueError(f"checkout path already exists: {checkout}")
    if branch_exists(root, branch):
        raise ValueError(f"transaction branch already exists: {branch}")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    run_git(root, "worktree", "add", "-b", branch, str(checkout), base)


def materialize_existing(root: Path, checkout: Path, branch: str) -> None:
    if checkout.exists():
        raise ValueError(f"checkout path already exists: {checkout}")
    if not branch_exists(root, branch):
        raise ValueError(f"transaction branch does not exist: {branch}")
    if worktree_for_branch(root, branch) is not None:
        raise ValueError(f"transaction branch is already checked out: {branch}")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    run_git(root, "worktree", "add", str(checkout), branch)


def stage_and_commit(
    checkout: Path,
    declared_paths: list[str],
    message: str,
    amend: bool,
) -> str:
    run_git(checkout, "add", "-A", "--", *declared_paths)
    if index_is_empty(checkout):
        raise ValueError("prepare found no staged transaction changes")
    arguments = ["commit"]
    if amend:
        arguments.append("--amend")
    arguments.extend(("-m", message))
    run_git(checkout, *arguments)
    return current_head(checkout)


def rebase_onto(checkout: Path, new_base: str) -> tuple[bool, str]:
    completed = subprocess.run(
        ["git", "-C", str(checkout), "rebase", new_base],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return completed.returncode == 0, completed.stdout.rstrip()


def conflicted_paths(checkout: Path) -> list[str]:
    output = run_git(
        checkout,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=U",
        binary=True,
    )
    assert isinstance(output, bytes)
    return [_decode_path(value) for value in output.split(b"\0") if value]


def fast_forward(root: Path, branch: str) -> None:
    run_git(root, "merge", "--ff-only", branch)


def cleanup_published(root: Path, checkout: Path, branch: str) -> None:
    run_git(root, "worktree", "remove", str(checkout))
    run_git(root, "branch", "-d", branch)


def discard_transaction(root: Path, checkout: Path, branch: str) -> None:
    """Discard only an explicitly authorized coordinator-owned transaction."""
    run_git(root, "worktree", "remove", "--force", str(checkout))
    run_git(root, "branch", "-D", branch)
