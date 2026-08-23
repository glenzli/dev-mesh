"""Read-only resolution of source protocols supported by the Observer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dev_mesh_coord.constants import (
    DEV_MESH_DIRECTORY,
    EVENT_SCHEMA,
    LEGACY_DIRECTORY,
    PROTOCOL,
    PROTOCOL_VERSION,
    TOMBSTONE_NAME,
)
from dev_mesh_coord.errors import ProtocolError
from dev_mesh_coord.storage import ensure_regular_directory, read_json


SUPPORTED_SOURCE_PROTOCOLS = {
    "20260814.1": 2,
    PROTOCOL_VERSION: EVENT_SCHEMA,
}


class SourcePlaneError(ValueError):
    """An Observer-only source failure that never grants producer authority."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source_protocol_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.source_protocol_version = source_protocol_version


@dataclass(frozen=True)
class SourcePlane:
    workspace_root: Path
    state_root: Path
    version: str
    event_schema: int


def _manifest_record(namespace: Path) -> dict[str, object]:
    manifest = read_json(namespace / "manifest.json", base=namespace)
    expected = {
        "schema": 1,
        "kind": "dev-mesh.workspace",
        "created_at": manifest.get("created_at"),
        "coord_current": "coord/current.json",
    }
    if manifest != expected or not isinstance(manifest.get("created_at"), str):
        raise ProtocolError("marker_invalid", "unsupported workspace manifest")
    return manifest


def _current_record(namespace: Path) -> dict[str, object]:
    current = read_json(namespace / "coord/current.json", base=namespace)
    version = current.get("version")
    event_schema = current.get("event_schema")
    expected = {
        "schema": 1,
        "protocol": PROTOCOL,
        "version": version,
        "event_schema": event_schema,
        "state": version,
        "activated_at": current.get("activated_at"),
    }
    if (
        current != expected
        or not isinstance(version, str)
        or not version
        or not isinstance(event_schema, int)
        or not isinstance(current.get("activated_at"), str)
    ):
        raise ProtocolError("marker_invalid", "malformed current marker")
    supported_schema = SUPPORTED_SOURCE_PROTOCOLS.get(version)
    if supported_schema != event_schema:
        raise SourcePlaneError(
            "protocol_migration_required",
            f"Observer does not support source protocol {version} with event schema {event_schema}",
            source_protocol_version=version,
        )
    return current


def _protocol_record(state: Path, current: dict[str, object]) -> dict[str, object]:
    protocol = read_json(state / "protocol.json", base=state)
    expected = {
        "schema": 1,
        "protocol": PROTOCOL,
        "version": current["version"],
        "event_schema": current["event_schema"],
        "created_at": protocol.get("created_at"),
    }
    if "cutover_id" in protocol:
        expected["cutover_id"] = protocol.get("cutover_id")
    if (
        protocol != expected
        or not isinstance(protocol.get("created_at"), str)
        or (
            "cutover_id" in protocol
            and not isinstance(protocol.get("cutover_id"), str)
        )
    ):
        raise ProtocolError("split_brain", "unsupported active protocol marker")
    return protocol


def _validate_legacy_boundary(root: Path) -> None:
    legacy = root / LEGACY_DIRECTORY
    if not legacy.exists():
        return
    ensure_regular_directory(legacy, "legacy state")
    marker = legacy / TOMBSTONE_NAME
    if not marker.exists():
        raise ProtocolError("split_brain", "legacy and current control planes are both writable")
    unexpected = sorted(path.name for path in legacy.iterdir() if path.name != TOMBSTONE_NAME)
    if unexpected:
        raise ProtocolError(
            "split_brain",
            "legacy tombstone contains writable state: " + ", ".join(unexpected),
        )
    tombstone = read_json(marker, base=legacy)
    if (
        tombstone.get("kind") != "dev-mesh.coordination.legacy-tombstone"
        or tombstone.get("target_protocol") != PROTOCOL
        or tombstone.get("target_version")
        not in {"20260812.1", *SUPPORTED_SOURCE_PROTOCOLS}
    ):
        raise ProtocolError("split_brain", "legacy tombstone targets another protocol")


def resolve_source_plane(root: Path) -> SourcePlane:
    """Resolve a supported source for observation without creating write authority."""

    root = root.expanduser().resolve()
    namespace = root / DEV_MESH_DIRECTORY
    if not namespace.exists():
        if (root / LEGACY_DIRECTORY).exists():
            raise ProtocolError(
                "legacy_cutover_required", "legacy coordination requires retirement"
            )
        raise ProtocolError(
            "namespace_missing", "workspace has no Dev Mesh coordination state"
        )
    ensure_regular_directory(namespace, "Dev Mesh namespace")
    _manifest_record(namespace)
    coordination = namespace / "coord"
    ensure_regular_directory(coordination, "coordination namespace")
    current = _current_record(namespace)
    version = str(current["version"])
    state = coordination / version
    ensure_regular_directory(state, "active coordination state")
    protocol = _protocol_record(state, current)
    if current["activated_at"] != protocol["created_at"]:
        raise ProtocolError(
            "split_brain", "current and protocol activation timestamps disagree"
        )
    _validate_legacy_boundary(root)
    return SourcePlane(root, state, version, int(current["event_schema"]))
