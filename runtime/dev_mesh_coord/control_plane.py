"""Exact-version control-plane initialization, resolution, and writer fencing."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .constants import (
    DEV_MESH_DIRECTORY,
    EVENT_SCHEMA,
    LEGACY_DIRECTORY,
    PROTOCOL,
    PROTOCOL_VERSION,
    STATE_DIRECTORIES,
    TOMBSTONE_NAME,
)
from .errors import ProtocolError
from . import git_backend as git
from .storage import (
    ATOMIC_TEMP_NAME,
    _fsync_directory,
    advisory_lock,
    ensure_regular_directory,
    now,
    read_json,
    replace_json,
)


@dataclass(frozen=True)
class ControlPlane:
    workspace_root: Path
    state_root: Path
    version: str
    event_schema: int


def _manifest_record(path: Path) -> dict[str, object]:
    manifest = read_json(path)
    expected = {
        "schema": 1,
        "kind": "dev-mesh.workspace",
        "created_at": manifest.get("created_at"),
        "coord_current": "coord/current.json",
    }
    if manifest != expected or not isinstance(manifest.get("created_at"), str):
        raise ProtocolError("marker_invalid", "unsupported workspace manifest")
    return manifest


def _current_record(path: Path) -> dict[str, object]:
    current = read_json(path)
    expected = {
        "schema": 1,
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "event_schema": EVENT_SCHEMA,
        "state": PROTOCOL_VERSION,
        "activated_at": current.get("activated_at"),
    }
    if current != expected or not isinstance(current.get("activated_at"), str):
        raise ProtocolError("unsupported_protocol", "unsupported current marker")
    return current


def _protocol_record(path: Path) -> dict[str, object]:
    protocol = read_json(path)
    expected = {
        "schema": 1,
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "event_schema": EVENT_SCHEMA,
        "created_at": protocol.get("created_at"),
    }
    if "cutover_id" in protocol:
        if not isinstance(protocol.get("cutover_id"), str):
            raise ProtocolError("split_brain", "active protocol cutover id is malformed")
        expected["cutover_id"] = protocol.get("cutover_id")
    if protocol != expected or not isinstance(protocol.get("created_at"), str):
        raise ProtocolError("split_brain", "unsupported active protocol marker")
    return protocol


def _git_exclude(root: Path) -> None:
    raw = str(git.run(root, "rev-parse", "--git-path", "info/exclude")).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    additions = [
        item
        for item in (".dev-mesh/", ".dev-mesh.bootstrap.lock", ".agent-coordination/")
        if item not in lines
    ]
    if not additions:
        return
    with path.open("a", encoding="utf-8") as stream:
        if existing and not existing.endswith("\n"):
            stream.write("\n")
        stream.write("\n".join(additions) + "\n")


def _tombstone(root: Path) -> dict[str, object] | None:
    legacy = root / LEGACY_DIRECTORY
    if not legacy.exists():
        return None
    ensure_regular_directory(legacy, "legacy state")
    marker = legacy / TOMBSTONE_NAME
    if not marker.exists():
        return None
    unexpected = sorted(path.name for path in legacy.iterdir() if path.name != TOMBSTONE_NAME)
    if unexpected:
        raise ProtocolError(
            "split_brain",
            "legacy tombstone contains writable state: " + ", ".join(unexpected),
        )
    value = read_json(marker)
    if (
        value.get("kind") != "dev-mesh.coordination.legacy-tombstone"
        or value.get("target_protocol") != PROTOCOL
        or value.get("target_version")
        not in {"20260812.1", "20260814.1", PROTOCOL_VERSION}
    ):
        raise ProtocolError("split_brain", "legacy tombstone targets another protocol")
    return value


def _read_current(root: Path) -> ControlPlane:
    namespace = root / DEV_MESH_DIRECTORY
    ensure_regular_directory(namespace, "Dev Mesh namespace")
    manifest = _manifest_record(namespace / "manifest.json")
    coordination = namespace / "coord"
    ensure_regular_directory(coordination, "coordination namespace")
    current = _current_record(coordination / "current.json")
    state = coordination / PROTOCOL_VERSION
    ensure_regular_directory(state, "active coordination state")
    protocol = _protocol_record(state / "protocol.json")
    if current["activated_at"] != protocol["created_at"]:
        raise ProtocolError("split_brain", "current and protocol activation timestamps disagree")
    legacy = root / LEGACY_DIRECTORY
    tombstone = _tombstone(root)
    if legacy.exists() and tombstone is None:
        raise ProtocolError("split_brain", "legacy and current control planes are both writable")
    return ControlPlane(root, state, PROTOCOL_VERSION, EVENT_SCHEMA)


def _partial_created_at(namespace: Path) -> str:
    timestamps: list[str] = []
    for path, field in (
        (namespace / "manifest.json", "created_at"),
        (namespace / "coord" / "current.json", "activated_at"),
        (namespace / "coord" / PROTOCOL_VERSION / "protocol.json", "created_at"),
    ):
        if path.exists():
            if path.name == "manifest.json":
                record = _manifest_record(path)
            elif path.name == "current.json":
                record = _current_record(path)
            else:
                record = _protocol_record(path)
            value = record.get(field)
            if not isinstance(value, str):
                raise ProtocolError("marker_invalid", f"partial marker lacks {field}: {path}")
            timestamps.append(value)
    if len(set(timestamps)) > 1:
        raise ProtocolError("split_brain", "partial marker timestamps disagree")
    return timestamps[0] if timestamps else now()


def initialize(root: Path, *, cutover_id: str | None = None) -> ControlPlane:
    root = root.expanduser().resolve()
    if not root.is_dir() or git.repository_root(root) != root:
        raise ValueError("coordination root must be the exact root of an existing Git workspace")
    with advisory_lock(root / ".dev-mesh.bootstrap.lock"):
        namespace = root / DEV_MESH_DIRECTORY
        if namespace.exists():
            try:
                current = _read_current(root)
            except ProtocolError:
                if _tombstone(root) is None and (root / LEGACY_DIRECTORY).exists():
                    raise
                ensure_regular_directory(namespace, "partial Dev Mesh namespace")
            else:
                if cutover_id is not None:
                    protocol = _protocol_record(current.state_root / "protocol.json")
                    if protocol.get("cutover_id") != cutover_id:
                        raise ProtocolError(
                            "cutover_facts_changed",
                            "active protocol belongs to another cutover",
                        )
                return current
        legacy = root / LEGACY_DIRECTORY
        if legacy.exists() and _tombstone(root) is None:
            raise ProtocolError(
                "legacy_cutover_required",
                "legacy coordination must be explicitly retired before initialization",
            )
        created_at = _partial_created_at(namespace) if namespace.exists() else now()
        _git_exclude(root)
        state = namespace / "coord" / PROTOCOL_VERSION
        state.mkdir(parents=True, exist_ok=True)
        for relative in STATE_DIRECTORIES:
            (state / relative).mkdir(parents=True, exist_ok=True)
        protocol: dict[str, object] = {
            "schema": 1,
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "event_schema": EVENT_SCHEMA,
            "created_at": created_at,
        }
        if cutover_id:
            protocol["cutover_id"] = cutover_id
        protocol_path = state / "protocol.json"
        if not protocol_path.exists():
            replace_json(protocol_path, protocol, base=state)
        manifest_path = namespace / "manifest.json"
        if not manifest_path.exists():
            replace_json(
                manifest_path,
                {
                    "schema": 1,
                    "kind": "dev-mesh.workspace",
                    "created_at": created_at,
                    "coord_current": "coord/current.json",
                },
                base=namespace,
            )
        coordination = namespace / "coord"
        (coordination / "cutovers").mkdir(parents=True, exist_ok=True)
        current_path = coordination / "current.json"
        if not current_path.exists():
            replace_json(
                current_path,
                {
                    "schema": 1,
                    "protocol": PROTOCOL,
                    "version": PROTOCOL_VERSION,
                    "event_schema": EVENT_SCHEMA,
                    "state": PROTOCOL_VERSION,
                    "activated_at": created_at,
                },
                base=namespace,
            )
    return _read_current(root)


def resolve(root: Path, *, create: bool = False) -> ControlPlane:
    root = root.expanduser().resolve()
    if (root / DEV_MESH_DIRECTORY).exists():
        return _read_current(root)
    legacy = root / LEGACY_DIRECTORY
    if legacy.exists():
        if _tombstone(root) is not None:
            raise ProtocolError("state_missing", "legacy is retired but current state is missing")
        raise ProtocolError("legacy_cutover_required", "legacy coordination requires retirement")
    if create:
        return initialize(root)
    raise ProtocolError("namespace_missing", "workspace has no Dev Mesh coordination state")


@contextmanager
def operation(root: Path, name: str) -> Iterator[ControlPlane]:
    selected = resolve(root, create=True)
    lock = selected.state_root / "locks" / "coordination.lock"
    with advisory_lock(lock):
        after_lock = resolve(root)
        if after_lock.state_root.resolve() != selected.state_root.resolve():
            raise ProtocolError("stale_writer", f"state changed before {name}")
        yield after_lock
        final = resolve(root)
        if final.state_root.resolve() != after_lock.state_root.resolve():
            raise ProtocolError("stale_writer", f"state changed during {name}")


def install_tombstone(
    root: Path,
    *,
    cutover_id: str,
    archive_path: Path,
    archive_digest: str,
) -> Path:
    legacy = root / LEGACY_DIRECTORY
    if legacy.exists():
        existing = _tombstone(root)
        if existing is not None and (
            existing.get("cutover_id") == cutover_id
            and existing.get("archive_path") == str(archive_path.resolve())
            and existing.get("archive_digest") == archive_digest
        ):
            return legacy / TOMBSTONE_NAME
        raise ProtocolError("split_brain", "legacy path already exists during tombstone install")
    staging = (
        root
        / DEV_MESH_DIRECTORY
        / "coord"
        / "cutovers"
        / f".{cutover_id}.legacy-tombstone-staging"
    )
    if staging.exists():
        ensure_regular_directory(staging, "legacy tombstone staging directory")
    else:
        staging.mkdir()
        _fsync_directory(staging.parent)
    marker = staging / TOMBSTONE_NAME
    unexpected: list[str] = []
    for child in staging.iterdir():
        if child.name == TOMBSTONE_NAME:
            continue
        if (
            ATOMIC_TEMP_NAME.fullmatch(child.name)
            and child.is_file()
            and not child.is_symlink()
        ):
            child.unlink()
            continue
        unexpected.append(child.name)
    if unexpected:
        raise ProtocolError(
            "split_brain",
            "legacy tombstone staging contains unexpected state: "
            + ", ".join(sorted(unexpected)),
        )
    expected = {
        "schema": 1,
        "kind": "dev-mesh.coordination.legacy-tombstone",
        "cutover_id": cutover_id,
        "archive_path": str(archive_path.resolve()),
        "archive_digest": archive_digest,
        "target_protocol": PROTOCOL,
        "target_version": PROTOCOL_VERSION,
    }
    if marker.exists():
        existing = read_json(marker, base=staging)
        if (
            {key: existing.get(key) for key in expected} != expected
            or not isinstance(existing.get("retired_at"), str)
        ):
            raise ProtocolError(
                "split_brain", "legacy tombstone staging facts changed"
            )
    else:
        replace_json(marker, {**expected, "retired_at": now()}, base=staging)
    entries = sorted(path.name for path in staging.iterdir())
    if entries != [TOMBSTONE_NAME]:
        raise ProtocolError("split_brain", "legacy tombstone staging is incomplete")
    os.replace(staging, legacy)
    _fsync_directory(root)
    return legacy / TOMBSTONE_NAME
