"""Explicit, fact-reconciled retirement of the legacy control plane."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import uuid
from pathlib import Path

from .constants import LEGACY_DIRECTORY, PROTOCOL, PROTOCOL_VERSION, TOMBSTONE_NAME
from .control_plane import initialize, install_tombstone, resolve
from .errors import ProtocolError
from .storage import now, read_json, replace_json


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ProtocolError(
            "git_fact_unavailable",
            completed.stderr.decode("utf-8", errors="replace").strip(),
        )
    return completed.stdout.decode("utf-8", errors="surrogateescape")


def _assert_exact_git_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ProtocolError("marker_invalid", "cutover workspace root is not a directory")
    discovered = Path(_run_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if discovered != root:
        raise ProtocolError("marker_invalid", "cutover root must be the exact Git workspace root")
    return root


def _coordination_status_line(line: str) -> bool:
    value = line[3:] if len(line) >= 3 else line
    return (
        value == LEGACY_DIRECTORY
        or value.startswith(f"{LEGACY_DIRECTORY}/")
        or value == ".dev-mesh"
        or value.startswith(".dev-mesh/")
        or value == ".dev-mesh.bootstrap.lock"
    )


def _coordination_path(path: str) -> bool:
    return (
        path == LEGACY_DIRECTORY
        or path.startswith(f"{LEGACY_DIRECTORY}/")
        or path == ".dev-mesh"
        or path.startswith(".dev-mesh/")
        or path == ".dev-mesh.bootstrap.lock"
    )


def _untracked_facts(root: Path) -> dict[str, object]:
    raw = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = sorted(
        path
        for path in raw.split("\0")
        if path and not _coordination_path(path)
    )
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        for relative in paths:
            path = root / relative
            facts = path.lstat()
            encoded_path = relative.encode("utf-8", errors="surrogateescape")
            digest.update(encoded_path)
            digest.update(b"\0")
            digest.update(f"{stat.S_IMODE(facts.st_mode):o}".encode("ascii"))
            digest.update(b"\0")
            if stat.S_ISREG(facts.st_mode):
                digest.update(b"F\0")
                digest.update(str(facts.st_size).encode("ascii"))
                digest.update(b"\0")
                total_bytes += facts.st_size
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif stat.S_ISLNK(facts.st_mode):
                target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                digest.update(b"L\0")
                digest.update(str(len(target)).encode("ascii"))
                digest.update(b"\0")
                digest.update(target)
                total_bytes += len(target)
            else:
                raise ProtocolError(
                    "git_fact_unavailable",
                    f"untracked path has unsupported file type: {relative}",
                )
            digest.update(b"\0")
    except OSError as error:
        raise ProtocolError(
            "git_fact_unavailable", f"cannot bind untracked workspace content: {error}"
        ) from error
    return {
        "untracked_paths": paths,
        "untracked_file_count": len(paths),
        "untracked_total_bytes": total_bytes,
        "untracked_content_sha256": digest.hexdigest(),
    }


def git_facts(root: Path) -> dict[str, object]:
    """Bind the plan to user Git state while excluding both coordination namespaces."""

    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    user_status = sorted(line for line in status if not _coordination_status_line(line))
    staged = sorted(line[3:] for line in user_status if line[:1] not in {" ", "?"})
    dirty = sorted(line[3:] for line in user_status if len(line) > 1 and line[1] not in {" ", "?"})
    untracked = _untracked_facts(root)
    return {
        "head": _run_git(root, "rev-parse", "HEAD").strip(),
        "branch": _run_git(root, "branch", "--show-current").strip(),
        "status": user_status,
        "staged_paths": staged,
        "dirty_paths": dirty,
        **untracked,
        "worktree_diff_sha256": hashlib.sha256(
            _run_git(root, "diff", "--binary", "--", ".", f":(exclude){LEGACY_DIRECTORY}", ":(exclude).dev-mesh").encode(
                "utf-8", errors="surrogateescape"
            )
        ).hexdigest(),
        "index_diff_sha256": hashlib.sha256(
            _run_git(root, "diff", "--cached", "--binary", "--", ".", f":(exclude){LEGACY_DIRECTORY}", ":(exclude).dev-mesh").encode(
                "utf-8", errors="surrogateescape"
            )
        ).hexdigest(),
    }


def legacy_inventory(root: Path) -> dict[str, object]:
    """Review aid only; the complete tree digest remains the cutover predicate."""

    active_directories: dict[str, tuple[Path, set[str] | None, set[str] | None]] = {
        "claims": (
            root / "claims",
            {"active", "paused", "pending-arbitration", "transaction"},
            {"active", "paused", "pending-arbitration", "transaction"},
        ),
        "runs": (root / "runs", {"active"}, {"active", "closed"}),
        "handoffs": (
            root / "handoffs",
            {"offered", "pending"},
            {"offered", "pending", "accepted", "rejected", "withdrawn"},
        ),
        "contentions": (root / "contentions" / "active", None, None),
        "transactions": (root / "transactions" / "active", None, None),
        "cleanups": (root / "cleanups" / "active", None, None),
        "groups": (root / "groups" / "active", None, None),
        "waiting": (root / "waiting" / "active", None, None),
        "work": (root / "work" / "active", None, None),
    }
    counts: dict[str, int] = {}
    invalid_records = 0
    for name, (path, active_statuses, known_statuses) in active_directories.items():
        count = 0
        for record_path in path.glob("*.json") if path.is_dir() and not path.is_symlink() else []:
            if active_statuses is None:
                count += 1
                continue
            try:
                value = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                invalid_records += 1
                continue
            if not isinstance(value, dict) or value.get("status") not in known_statuses:
                invalid_records += 1
            elif value.get("status") in active_statuses:
                count += 1
        counts[name] = count
    return {
        "total_files": sum(1 for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
        "active_object_files": counts,
        "invalid_json_records": invalid_records,
    }


def tree_digest(root: Path) -> str:
    """Hash names, types, modes, and file bytes; reject links and special files."""

    if root.is_symlink() or not root.is_dir():
        raise ProtocolError("legacy_invalid", f"legacy state must be a regular directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ProtocolError("legacy_invalid", f"legacy state contains a symlink: {relative}")
        mode = path.stat().st_mode & 0o777
        if path.is_dir():
            digest.update(f"D\0{relative}\0{mode:o}\0".encode())
        elif path.is_file():
            digest.update(f"F\0{relative}\0{mode:o}\0{path.stat().st_size}\0".encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise ProtocolError("legacy_invalid", f"legacy state contains a special file: {relative}")
    return digest.hexdigest()


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _plan_digest(plan: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(plan)).hexdigest()


def build_plan(root: Path, *, archive_root: Path) -> dict[str, object]:
    root = _assert_exact_git_root(root)
    archive_root = archive_root.expanduser().resolve()
    legacy = root / LEGACY_DIRECTORY
    if (root / ".dev-mesh").exists():
        raise ProtocolError("split_brain", "current Dev Mesh namespace already exists")
    if not legacy.exists() or (legacy / TOMBSTONE_NAME).exists():
        raise ProtocolError("legacy_cutover_required", "no writable legacy control plane is available")
    legacy_digest = tree_digest(legacy)
    cutover_id = f"cutover-{uuid.uuid4().hex}"
    archive_path = archive_root / cutover_id / "retired-legacy"
    if archive_path.exists():
        raise ProtocolError("cutover_facts_changed", f"archive target already exists: {archive_path}")
    plan: dict[str, object] = {
        "schema": 1,
        "kind": "dev-mesh.coordination.cutover-plan",
        "cutover_id": cutover_id,
        "protocol": PROTOCOL,
        "target_version": PROTOCOL_VERSION,
        "workspace_root": str(root),
        "legacy_path": str(legacy),
        "legacy_digest": legacy_digest,
        "legacy_inventory": legacy_inventory(legacy),
        "archive_path": str(archive_path),
        "git_facts": git_facts(root),
        "planned_at": now(),
    }
    return {**plan, "plan_digest": _plan_digest(plan)}


def write_plan(path: Path, plan: dict[str, object]) -> None:
    path = path.expanduser().resolve()
    workspace = Path(str(plan.get("workspace_root"))).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise ProtocolError("marker_invalid", "cutover journal must be outside the workspace")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ProtocolError("marker_invalid", f"cutover journal already exists: {path}")
    replace_json(path, {**plan, "state": "planned"}, base=path.parent)


def _load_journal(path: Path) -> tuple[Path, dict[str, object]]:
    path = path.expanduser().resolve()
    journal = read_json(path)
    digest = journal.pop("plan_digest", None)
    state = journal.pop("state", None)
    transient = {key: journal.pop(key) for key in list(journal) if key.endswith("_at") and key != "planned_at"}
    if digest != _plan_digest(journal):
        raise ProtocolError("cutover_facts_changed", "cutover plan changed after review")
    journal["plan_digest"] = digest
    journal["state"] = state
    journal.update(transient)
    return path, journal


def _write_journal(path: Path, journal: dict[str, object], state: str) -> dict[str, object]:
    updated = {**journal, "state": state, f"{state.replace('-', '_')}_at": now()}
    replace_json(path, updated, base=path.parent)
    return updated


def _exact_planned_path(value: object, label: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute() or path.resolve() != path:
        raise ProtocolError(
            "cutover_facts_changed",
            f"{label} no longer resolves to the exact reviewed path",
        )
    return path


def _assert_plan(journal: dict[str, object], expected_plan_digest: str) -> tuple[Path, Path, Path]:
    if journal.get("plan_digest") != expected_plan_digest:
        raise ProtocolError("cutover_facts_changed", "supplied digest does not match the reviewed plan")
    if journal.get("protocol") != PROTOCOL or journal.get("target_version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol", "cutover plan targets another protocol")
    planned_root = _exact_planned_path(journal["workspace_root"], "workspace root")
    root = _assert_exact_git_root(planned_root)
    legacy = _exact_planned_path(journal["legacy_path"], "legacy path")
    archive = _exact_planned_path(journal["archive_path"], "archive path")
    if legacy != root / LEGACY_DIRECTORY:
        raise ProtocolError("marker_invalid", "legacy path is not the workspace legacy namespace")
    try:
        archive.relative_to(root)
    except ValueError:
        pass
    else:
        raise ProtocolError("marker_invalid", "archive must be outside the workspace")
    return root, legacy, archive


def _archive_legacy(legacy: Path, archive: Path, expected_digest: str) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.resolve() != archive:
        raise ProtocolError(
            "cutover_facts_changed",
            "archive path changed through a symlink after review",
        )
    if archive.exists():
        if tree_digest(archive) != expected_digest:
            raise ProtocolError("cutover_facts_changed", "existing archive does not match the reviewed legacy state")
        if legacy.exists():
            raise ProtocolError("cutover_facts_changed", "both writable legacy state and its archive exist")
        return
    if not legacy.exists():
        raise ProtocolError("cutover_facts_changed", "legacy state and archive are both absent")
    if tree_digest(legacy) != expected_digest:
        raise ProtocolError("cutover_facts_changed", "legacy state changed after the plan was reviewed")
    if legacy.stat().st_dev != archive.parent.stat().st_dev:
        raise ProtocolError("cutover_facts_changed", "archive must be on the same filesystem for atomic retirement")
    os.replace(legacy, archive)
    if tree_digest(archive) != expected_digest:
        raise ProtocolError("cutover_facts_changed", "retired archive digest changed")


def apply(
    journal_path: Path,
    *,
    expected_plan_digest: str,
    confirm_agents_stopped: bool,
    confirm_no_legacy_writers: bool,
    confirm_retire_active_authority: bool = False,
) -> dict[str, object]:
    if not confirm_agents_stopped or not confirm_no_legacy_writers:
        raise ProtocolError("cutover_confirmation_required", "both stop-window confirmations are required")
    journal_path, journal = _load_journal(journal_path)
    root, legacy, archive = _assert_plan(journal, expected_plan_digest)
    inventory = journal.get("legacy_inventory")
    active_counts = inventory.get("active_object_files", {}) if isinstance(inventory, dict) else {}
    active_total = (
        sum(int(value) for value in active_counts.values()) if isinstance(active_counts, dict) else 0
    )
    invalid_records = int(inventory.get("invalid_json_records", 0)) if isinstance(inventory, dict) else 0
    if (active_total or invalid_records) and not confirm_retire_active_authority:
        raise ProtocolError(
            "cutover_confirmation_required",
            "reviewed legacy plan contains "
            f"{active_total} active authority objects and {invalid_records} unclassified records; "
            "explicit retirement confirmation is required",
        )
    try:
        journal_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise ProtocolError("marker_invalid", "cutover journal must be outside the workspace")
    if git_facts(root) != journal.get("git_facts"):
        raise ProtocolError("cutover_facts_changed", "workspace Git facts changed after cutover planning")
    expected_digest = str(journal["legacy_digest"])

    tombstone = legacy / TOMBSTONE_NAME
    if archive.exists() and tree_digest(archive) != expected_digest:
        raise ProtocolError("cutover_facts_changed", "archive does not match the reviewed legacy state")
    if legacy.exists() and not tombstone.exists() and (root / ".dev-mesh").exists():
        raise ProtocolError("split_brain", "writable legacy and current control planes coexist")

    if not archive.exists() or (legacy.exists() and not tombstone.exists()):
        _archive_legacy(legacy, archive, expected_digest)
    journal = _write_journal(journal_path, journal, "legacy-archived")

    plane = initialize(root, cutover_id=str(journal["cutover_id"]))
    journal = _write_journal(journal_path, journal, "current-initialized")

    install_tombstone(
        root,
        cutover_id=str(journal["cutover_id"]),
        archive_path=archive,
        archive_digest=expected_digest,
    )
    journal = _write_journal(journal_path, journal, "tombstone-installed")

    completed = _write_journal(journal_path, journal, "completed")
    internal = plane.workspace_root / ".dev-mesh" / "coord" / "cutovers" / f"{journal['cutover_id']}.json"
    replace_json(internal, completed, base=plane.workspace_root / ".dev-mesh")
    verify(journal_path, expected_plan_digest=expected_plan_digest)
    return completed


def verify(journal_path: Path, *, expected_plan_digest: str) -> dict[str, object]:
    journal_path, journal = _load_journal(journal_path)
    root, legacy, archive = _assert_plan(journal, expected_plan_digest)
    if tree_digest(archive) != journal.get("legacy_digest"):
        raise ProtocolError("cutover_facts_changed", "retired archive digest is invalid")
    plane = resolve(root)
    protocol = read_json(plane.state_root / "protocol.json", base=plane.state_root)
    tombstone = read_json(legacy / TOMBSTONE_NAME)
    if (
        tombstone.get("cutover_id") != journal.get("cutover_id")
        or tombstone.get("archive_digest") != journal.get("legacy_digest")
        or tombstone.get("archive_path") != str(archive)
        or protocol.get("cutover_id") != journal.get("cutover_id")
        or plane.version != PROTOCOL_VERSION
    ):
        raise ProtocolError("cutover_facts_changed", "cutover facts do not agree")
    return {
        "verified": True,
        "cutover_id": journal.get("cutover_id"),
        "plan_digest": journal.get("plan_digest"),
        "state": journal.get("state"),
        "workspace_root": str(root),
        "archive_path": str(archive),
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
    }
