"""Metadata-only cross-project collaboration correlation extension."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .control_plane import ControlPlane, operation
from .events import build_event, materialized, write_event
from .storage import read_json, require_identifier, require_slug, write_json_exclusive


EXTENSION_PROTOCOL = "dev-mesh.cross-project-collaboration"
EXTENSION_VERSION = "20260814.1"
COLLABORATION_KINDS = {
    "notice",
    "request",
    "dependency",
    "handoff",
    "review",
    "integration",
}
COLLABORATION_OUTCOMES = {"completed", "cancelled", "failed"}
WORKSPACE_ID = re.compile(r"^[0-9a-f]{24}$")


def workspace_id(root: Path) -> str:
    """Return the Observer-compatible privacy-preserving workspace identity."""

    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:24]


def _workspace_id(value: str, label: str) -> str:
    if not WORKSPACE_ID.fullmatch(value):
        raise ValueError(f"{label} must be exactly 24 lowercase hexadecimal characters")
    return value


def _kind(value: str) -> str:
    if value not in COLLABORATION_KINDS:
        raise ValueError(f"unsupported cross-project collaboration kind: {value}")
    return value


def _active_run(plane: ControlPlane, owner: str, run_id: str) -> None:
    run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
    if run.get("owner") != owner or run.get("status") != "active":
        raise ValueError("actor Run is not active for the exact owner")


def _message_id(collaboration_id: str, phase: str, local_workspace_id: str) -> str:
    digest = hashlib.sha256(
        f"{EXTENSION_PROTOCOL}\0{collaboration_id}\0{phase}\0{local_workspace_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"msg-cross-{digest}"


def _event_path(plane: ControlPlane, event_id: str) -> Path | None:
    matches = list((plane.state_root / "events").glob(f"*-{event_id}-message-sent.json"))
    if len(matches) > 1:
        raise ValueError("cross-project phase has duplicate exact events")
    return matches[0] if matches else None


def _record_phase(
    root: Path,
    *,
    collaboration_id: str,
    phase: str,
    kind: str,
    actor_role: str,
    owner: str,
    run_id: str,
    source: dict[str, object],
    target: dict[str, object],
    outcome: str | None = None,
    reconciliation: dict[str, object] | None = None,
) -> dict[str, object]:
    collaboration_id = require_identifier(collaboration_id, "collaboration id")
    kind = _kind(kind)
    if phase not in {"opened", "bound", "closed"}:
        raise ValueError(f"unsupported cross-project phase: {phase}")
    if actor_role not in {"source", "target"}:
        raise ValueError("actor role must be source or target")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    if phase == "closed":
        if outcome not in COLLABORATION_OUTCOMES:
            raise ValueError("closed collaboration requires a supported outcome")
    elif outcome is not None:
        raise ValueError("only a closed collaboration may have an outcome")
    if reconciliation is not None and phase != "closed":
        raise ValueError("only a closed collaboration may carry reconciliation evidence")

    with operation(root, f"cross-project-{phase}") as plane:
        _active_run(plane, owner, run_id)
        local_workspace_id = workspace_id(plane.workspace_root)
        actor = (
            reconciliation.get("by")
            if reconciliation is not None
            else source if actor_role == "source" else target
        )
        if not isinstance(actor, dict):
            raise ValueError("cross-project reconciliation actor is malformed")
        if actor.get("workspace_id") != local_workspace_id:
            raise ValueError("actor role does not match the current workspace")
        if actor.get("owner") != owner or actor.get("run_id") != run_id:
            raise ValueError("actor role does not match the exact active Run")

        message_id = _message_id(collaboration_id, phase, local_workspace_id)
        extension = {
            "protocol": EXTENSION_PROTOCOL,
            "protocol_version": EXTENSION_VERSION,
            "collaboration_id": collaboration_id,
            "phase": phase,
            "kind": kind,
            "actor_role": actor_role,
            "source": source,
            "target": target,
            "outcome": outcome,
        }
        if reconciliation is not None:
            extension["reconciliation"] = reconciliation
        event_payload: dict[str, object] = {
            "message_id": message_id,
            "owner": owner,
            "run_id": run_id,
            "source_owner": source.get("owner"),
            "source_run_id": source.get("run_id"),
            "target_owner": target.get("owner"),
            "target_run_id": target.get("run_id"),
            "interaction_kind": "request" if phase == "opened" else "notice",
            "topic": "general",
            "requires_ack": False,
            "cross_project": extension,
        }
        record_facts = materialized(
            {
                "schema": 1,
                "message_id": message_id,
                "interaction_kind": "cross-project-collaboration",
                "source_owner": source.get("owner"),
                "source_run_id": source.get("run_id"),
                "target_owner": target.get("owner"),
                "target_run_id": target.get("run_id"),
                "requires_ack": False,
                "cross_project": extension,
            }
        )
        message_path = plane.state_root / "messages" / f"{message_id}.json"
        if message_path.exists():
            existing = read_json(message_path, base=plane.state_root)
            existing_facts = {key: value for key, value in existing.items() if key != "event"}
            if existing_facts != record_facts:
                raise ValueError("cross-project phase retry differs from the original exact facts")
            record = existing
            stored_event = record.get("event")
            if not isinstance(stored_event, dict):
                raise ValueError("cross-project phase lacks its exact event intent")
            event = stored_event
        else:
            event = build_event("message-sent", payload=event_payload)
            record = {**record_facts, "event": event}
            write_json_exclusive(message_path, record, base=plane.state_root)

        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise ValueError("cross-project phase event id is malformed")
        existing_event_path = _event_path(plane, event_id)
        if existing_event_path is None:
            write_event(plane, event)
        else:
            existing_event = read_json(existing_event_path, base=plane.state_root)
            if existing_event != event:
                raise ValueError("cross-project phase event differs from its durable intent")
        return {
            "message_id": message_id,
            "collaboration_id": collaboration_id,
            "phase": phase,
            "kind": kind,
            "actor_role": actor_role,
            "source_workspace_id": source.get("workspace_id"),
            "target_workspace_id": target.get("workspace_id"),
            "source_owner": source.get("owner"),
            "source_run_id": source.get("run_id"),
            "target_owner": target.get("owner"),
            "target_run_id": target.get("run_id"),
            "target_task_id": target.get("task_id"),
            "outcome": outcome,
            "reconciled": reconciliation is not None,
        }


def _bound_extension(
    plane: ControlPlane,
    *,
    collaboration_id: str,
) -> dict[str, object]:
    local_workspace_id = workspace_id(plane.workspace_root)
    message_id = _message_id(collaboration_id, "bound", local_workspace_id)
    path = plane.state_root / "messages" / f"{message_id}.json"
    if not path.exists():
        raise ValueError("target workspace has no exact bound collaboration record")
    record = read_json(path, base=plane.state_root)
    extension = record.get("cross_project")
    if not isinstance(extension, dict):
        raise ValueError("bound collaboration record is malformed")
    if (
        extension.get("protocol") != EXTENSION_PROTOCOL
        or extension.get("protocol_version") != EXTENSION_VERSION
        or extension.get("collaboration_id") != collaboration_id
        or extension.get("phase") != "bound"
        or extension.get("actor_role") != "target"
        or extension.get("outcome") is not None
        or extension.get("kind") not in COLLABORATION_KINDS
    ):
        raise ValueError("bound collaboration record has inconsistent exact facts")
    source = extension.get("source")
    target = extension.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValueError("bound collaboration participants are malformed")
    source_workspace_id = source.get("workspace_id")
    source_owner = source.get("owner")
    source_run_id = source.get("run_id")
    target_workspace_id = target.get("workspace_id")
    target_task_id = target.get("task_id")
    target_owner = target.get("owner")
    target_run_id = target.get("run_id")
    if (
        not isinstance(source_workspace_id, str)
        or _workspace_id(source_workspace_id, "source workspace id") == local_workspace_id
        or not isinstance(source_owner, str)
        or require_slug(source_owner, "source owner") != source_owner
        or not isinstance(source_run_id, str)
        or require_identifier(source_run_id, "source run id") != source_run_id
        or target_workspace_id != local_workspace_id
        or not isinstance(target_task_id, str)
        or require_identifier(target_task_id, "target task id") != target_task_id
        or not isinstance(target_owner, str)
        or require_slug(target_owner, "target owner") != target_owner
        or not isinstance(target_run_id, str)
        or require_identifier(target_run_id, "target run id") != target_run_id
    ):
        raise ValueError("bound collaboration participant identity is inconsistent")
    event = record.get("event")
    if (
        record.get("message_id") != message_id
        or record.get("source_owner") != source_owner
        or record.get("source_run_id") != source_run_id
        or record.get("target_owner") != target_owner
        or record.get("target_run_id") != target_run_id
        or not isinstance(event, dict)
        or event.get("message_id") != message_id
        or event.get("cross_project") != extension
    ):
        raise ValueError("bound collaboration record and event intent disagree")
    return extension


def open_collaboration(
    root: Path,
    *,
    collaboration_id: str,
    source_owner: str,
    source_run_id: str,
    target_task_id: str,
    kind: str,
    target_workspace_id: str | None = None,
    target_owner: str | None = None,
) -> dict[str, object]:
    source_owner = require_slug(source_owner, "source owner")
    source_run_id = require_identifier(source_run_id, "source run id")
    target_task_id = require_identifier(target_task_id, "target task id")
    if target_workspace_id is not None:
        target_workspace_id = _workspace_id(target_workspace_id, "target workspace id")
    if target_owner is not None:
        target_owner = require_slug(target_owner, "target owner")
    source = {
        "workspace_id": workspace_id(root),
        "owner": source_owner,
        "run_id": source_run_id,
    }
    target = {
        "workspace_id": target_workspace_id,
        "task_id": target_task_id,
        "owner": target_owner,
        "run_id": None,
    }
    if target_workspace_id == source["workspace_id"]:
        raise ValueError("cross-project collaboration must target another workspace")
    return _record_phase(
        root,
        collaboration_id=collaboration_id,
        phase="opened",
        kind=kind,
        actor_role="source",
        owner=source_owner,
        run_id=source_run_id,
        source=source,
        target=target,
    )


def bind_collaboration(
    root: Path,
    *,
    collaboration_id: str,
    source_workspace_id: str,
    source_owner: str,
    source_run_id: str,
    target_owner: str,
    target_run_id: str,
    target_task_id: str,
    kind: str,
) -> dict[str, object]:
    source = {
        "workspace_id": _workspace_id(source_workspace_id, "source workspace id"),
        "owner": require_slug(source_owner, "source owner"),
        "run_id": require_identifier(source_run_id, "source run id"),
    }
    target_workspace_id = workspace_id(root)
    if source["workspace_id"] == target_workspace_id:
        raise ValueError("cross-project collaboration must bind another workspace")
    target = {
        "workspace_id": target_workspace_id,
        "task_id": require_identifier(target_task_id, "target task id"),
        "owner": require_slug(target_owner, "target owner"),
        "run_id": require_identifier(target_run_id, "target run id"),
    }
    return _record_phase(
        root,
        collaboration_id=collaboration_id,
        phase="bound",
        kind=kind,
        actor_role="target",
        owner=target_owner,
        run_id=target_run_id,
        source=source,
        target=target,
    )


def close_collaboration(
    root: Path,
    *,
    collaboration_id: str,
    actor_role: str,
    owner: str,
    run_id: str,
    source_workspace_id: str,
    source_owner: str,
    source_run_id: str,
    target_workspace_id: str,
    target_owner: str,
    target_run_id: str,
    target_task_id: str,
    kind: str,
    outcome: str,
) -> dict[str, object]:
    source = {
        "workspace_id": _workspace_id(source_workspace_id, "source workspace id"),
        "owner": require_slug(source_owner, "source owner"),
        "run_id": require_identifier(source_run_id, "source run id"),
    }
    target = {
        "workspace_id": _workspace_id(target_workspace_id, "target workspace id"),
        "task_id": require_identifier(target_task_id, "target task id"),
        "owner": require_slug(target_owner, "target owner"),
        "run_id": require_identifier(target_run_id, "target run id"),
    }
    if source["workspace_id"] == target["workspace_id"]:
        raise ValueError("cross-project collaboration must connect distinct workspaces")
    return _record_phase(
        root,
        collaboration_id=collaboration_id,
        phase="closed",
        kind=kind,
        actor_role=actor_role,
        owner=owner,
        run_id=run_id,
        source=source,
        target=target,
        outcome=outcome,
    )


def reconcile_closed_collaboration(
    root: Path,
    *,
    collaboration_id: str,
    owner: str,
    run_id: str,
    outcome: str,
) -> dict[str, object]:
    """Close a bound relation after its exact target Run has already terminated.

    Reconciliation is target-workspace only. It preserves the original source and target binding;
    the new same-owner active Run is recorded separately as the reconciler and gains no authority
    over either workspace from this diagnostic action.
    """

    collaboration_id = require_identifier(collaboration_id, "collaboration id")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    if outcome not in COLLABORATION_OUTCOMES:
        raise ValueError("closed collaboration requires a supported outcome")

    with operation(root, "cross-project-reconcile-close") as plane:
        _active_run(plane, owner, run_id)
        extension = _bound_extension(plane, collaboration_id=collaboration_id)
        source = extension["source"]
        target = extension["target"]
        assert isinstance(source, dict) and isinstance(target, dict)
        if target.get("owner") != owner:
            raise ValueError("late close requires an active successor Run of the bound target owner")
        target_run_id = target.get("run_id")
        if not isinstance(target_run_id, str):
            raise ValueError("bound collaboration target Run is malformed")
        if target_run_id == run_id:
            raise ValueError("active bound target Run must use the normal close operation")
        target_run = read_json(
            plane.state_root / "runs" / f"{target_run_id}.json",
            base=plane.state_root,
        )
        if target_run.get("owner") != owner or target_run.get("status") != "closed":
            raise ValueError("late close requires the exact bound target Run to be terminal")
        target_outcome = target_run.get("outcome")
        if target_outcome not in {"completed", "failed", "abandoned"}:
            raise ValueError("bound target Run has an unsupported terminal outcome")
        reconciliation = {
            "basis": "bound-target-run-terminal",
            "by": {
                "workspace_id": workspace_id(plane.workspace_root),
                "owner": owner,
                "run_id": run_id,
            },
            "target_run_status": "closed",
            "target_run_outcome": target_outcome,
        }
        kind = extension["kind"]
        assert isinstance(kind, str)

    return _record_phase(
        root,
        collaboration_id=collaboration_id,
        phase="closed",
        kind=kind,
        actor_role="target",
        owner=owner,
        run_id=run_id,
        source=source,
        target=target,
        outcome=outcome,
        reconciliation=reconciliation,
    )
