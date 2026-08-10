"""Discover coordination directories without mutating their workspaces."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


PRUNED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class WorkspaceSource:
    coordination_path: str
    workspace_root: str
    git_toplevel: str | None
    git_common_dir: str | None
    remote_fingerprint: str | None

    def as_record(self) -> dict[str, str | None]:
        return asdict(self)


def ensure_external_data_dir(
    data_dir: Path,
    sources: list[WorkspaceSource],
) -> None:
    candidate = data_dir.expanduser().resolve()
    for source in sources:
        workspace_root = Path(source.workspace_root)
        if candidate == workspace_root or candidate.is_relative_to(workspace_root):
            raise ValueError(
                "Observer data directory must be outside every discovered workspace: "
                f"{candidate} is inside {workspace_root}"
            )


def _git_value(root: Path, *arguments: str) -> str | None:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _workspace_source(coordination_path: Path) -> WorkspaceSource | None:
    if coordination_path.is_symlink() or not coordination_path.is_dir():
        return None
    events = coordination_path / "events"
    if events.is_symlink() or not events.is_dir():
        return None
    workspace_root = coordination_path.parent.resolve()
    top_value = _git_value(workspace_root, "rev-parse", "--show-toplevel")
    git_toplevel = str(Path(top_value).resolve()) if top_value is not None else None
    common_value = _git_value(workspace_root, "rev-parse", "--git-common-dir")
    if common_value is None:
        git_common_dir = None
    else:
        common_path = Path(common_value)
        if not common_path.is_absolute():
            common_path = workspace_root / common_path
        git_common_dir = str(common_path.resolve())
    remote = _git_value(workspace_root, "config", "--get", "remote.origin.url")
    remote_fingerprint = (
        hashlib.sha256(remote.encode("utf-8")).hexdigest() if remote else None
    )
    return WorkspaceSource(
        coordination_path=str(coordination_path.resolve()),
        workspace_root=str(workspace_root),
        git_toplevel=git_toplevel,
        git_common_dir=git_common_dir,
        remote_fingerprint=remote_fingerprint,
    )


def discover_workspaces(
    roots: list[Path],
    *,
    max_depth: int = 5,
) -> list[WorkspaceSource]:
    if max_depth < 0 or max_depth > 20:
        raise ValueError("max depth must be between 0 and 20")
    found: dict[str, WorkspaceSource] = {}
    for requested_root in roots:
        root = requested_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"discovery root is not a directory: {root}")
        if root.name == ".agent-coordination":
            source = _workspace_source(root)
            if source is not None:
                found[source.coordination_path] = source
            continue
        for current, directory_names, _ in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            directory_names[:] = [
                name
                for name in directory_names
                if name not in PRUNED_DIRECTORIES
                and not (current_path / name).is_symlink()
            ]
            if ".agent-coordination" in directory_names:
                candidate = current_path / ".agent-coordination"
                source = _workspace_source(candidate)
                if source is not None:
                    found[source.coordination_path] = source
                directory_names.remove(".agent-coordination")
            if depth >= max_depth:
                directory_names.clear()
    return [found[key] for key in sorted(found)]
