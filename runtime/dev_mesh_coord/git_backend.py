"""Narrow Git facts and microtransaction operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .constants import MAX_CLAIM_PATHS


MAX_GIT_ERROR_DETAIL = 1600


class GitCommandError(RuntimeError):
    """Bounded Git failure that keeps the actionable stderr instead of argv noise."""

    def __init__(
        self, arguments: tuple[str, ...], returncode: int, stderr: str
    ) -> None:
        self.operation = arguments[0] if arguments else "unknown"
        self.returncode = returncode
        detail = stderr.strip()
        self.stderr_tail = detail[-MAX_GIT_ERROR_DETAIL:]
        omitted = max(0, len(detail) - len(self.stderr_tail))
        suffix = self.stderr_tail or "no stderr"
        if omitted:
            suffix = f"{omitted} stderr characters omitted; tail: {suffix}"
        super().__init__(
            f"Git {self.operation} failed with exit {returncode}: {suffix}"
        )


def run(
    root: Path,
    *arguments: str,
    check: bool = True,
    binary: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> str | bytes:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        check=False,
        pass_fds=pass_fds,
    )
    if check and completed.returncode != 0:
        stderr = (
            completed.stderr.decode("utf-8", errors="replace")
            if isinstance(completed.stderr, bytes)
            else completed.stderr
        )
        raise GitCommandError(arguments, completed.returncode, stderr)
    return completed.stdout


def assert_canonical_git_writable(root: Path) -> None:
    """Fail before durable publication intent if Git metadata cannot be mutated."""

    raw_index = Path(str(run(root, "rev-parse", "--git-path", "index")).strip())
    index = raw_index if raw_index.is_absolute() else root / raw_index
    probe_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, probe = tempfile.mkstemp(
            prefix=".dev-mesh-index-write-probe-",
            dir=index.parent,
        )
        probe_path = Path(probe)
    except OSError as error:
        raise PermissionError(
            "canonical Git metadata is not writable; rerun the managed "
            "publication with Git write permission"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError as error:
                raise PermissionError(
                    "canonical Git metadata probe could not be removed; inspect "
                    "the repository before publication"
                ) from error


def repository_root(root: Path) -> Path:
    value = str(run(root, "rev-parse", "--show-toplevel")).strip()
    return Path(value).resolve()


def exact_managed_checkout(
    state_root: Path, transaction_id: str, value: object
) -> Path:
    """Bind a recorded checkout to its literal, symlink-free managed path."""

    checkout = Path(str(value))
    expected = state_root / "checkouts" / transaction_id
    if (
        not checkout.is_absolute()
        or checkout != expected
        or checkout.is_symlink()
        or checkout.resolve(strict=False) != checkout
    ):
        raise ValueError("checkout is not the exact managed transaction path")
    return checkout


def head(root: Path) -> str:
    return str(run(root, "rev-parse", "HEAD^{commit}")).strip()


def branch(root: Path) -> str:
    value = str(run(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)).strip()
    if not value:
        raise ValueError("canonical workspace must be on a named branch")
    return value


def status_entries(root: Path, paths: list[str] | None = None) -> list[tuple[str, str]]:
    arguments = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if paths:
        arguments.extend(("--", *paths))
    raw = run(root, *arguments, binary=True)
    assert isinstance(raw, bytes)
    values = [item for item in raw.split(b"\0") if item]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(values):
        item = values[index]
        status = item[:2].decode("ascii", errors="replace")
        path = item[3:].decode("utf-8", errors="surrogateescape")
        entries.append((status, path))
        if status[0] in {"R", "C"}:
            index += 1
        index += 1
    return entries


def dirty_paths(root: Path, paths: list[str] | None = None) -> list[str]:
    return sorted({path for _, path in status_entries(root, paths)})


def path_is_tracked(root: Path, path: str) -> bool:
    completed = subprocess.run(
        ("git", "-C", str(root), "ls-files", "--error-unmatch", "--", path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def path_is_ignored(root: Path, path: str) -> bool:
    completed = subprocess.run(
        ("git", "-C", str(root), "check-ignore", "--quiet", "--no-index", "--", path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def staged_paths(root: Path) -> list[str]:
    return sorted({path for status, path in status_entries(root) if status[0] not in {" ", "?"}})


def index_is_empty(root: Path) -> bool:
    completed = subprocess.run(
        ("git", "-C", str(root), "diff", "--cached", "--quiet", "--exit-code"),
        check=False,
    )
    return completed.returncode == 0


def normalize_paths(root: Path, values: list[str]) -> list[str]:
    normalized: list[str] = []
    canonical = root.resolve()
    for value in values:
        candidate = (canonical / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        try:
            relative = candidate.relative_to(canonical)
        except ValueError as error:
            raise ValueError(f"path is outside the workspace: {value}") from error
        if relative == Path("."):
            raise ValueError("declare bounded workspace paths, not the workspace root")
        text = relative.as_posix()
        if len(text) > 1024:
            raise ValueError("declared path exceeds 1024 characters")
        if text not in normalized:
            normalized.append(text)
    if not normalized:
        raise ValueError("at least one path is required")
    if len(normalized) > MAX_CLAIM_PATHS:
        raise ValueError(f"a Claim may declare at most {MAX_CLAIM_PATHS} paths")
    return normalized


def changed_paths(root: Path, base: str, candidate: str) -> list[str]:
    output = str(run(root, "diff", "--name-only", f"{base}..{candidate}"))
    return sorted(line for line in output.splitlines() if line)


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ("git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant),
        check=False,
    )
    return completed.returncode == 0


def registered_worktrees(root: Path) -> dict[Path, str | None]:
    """Return the exact linked-worktree path to branch-ref registry."""

    output = str(run(root, "worktree", "list", "--porcelain"))
    worktrees: dict[Path, str | None] = {}
    checkout: Path | None = None
    branch_ref: str | None = None
    for line in (*output.splitlines(), ""):
        if line.startswith("worktree "):
            if checkout is not None:
                worktrees[checkout] = branch_ref
            checkout = Path(line.removeprefix("worktree ")).resolve()
            branch_ref = None
        elif line.startswith("branch "):
            branch_ref = line.removeprefix("branch ")
        elif not line and checkout is not None:
            worktrees[checkout] = branch_ref
            checkout = None
            branch_ref = None
    return worktrees


def rebase_in_progress(root: Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        raw = str(run(root, "rev-parse", "--git-path", name)).strip()
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if path.exists():
            return True
    return False
