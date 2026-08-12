"""Diagnostic waiting and diverted-work intervals."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from .control_plane import ControlPlane, operation
from .events import build_event, emit, materialized, write_event
from .storage import now, read_json, replace_json, require_identifier, require_slug, require_text, write_json_exclusive


def _event_by_id(plane: ControlPlane, event_id: str) -> dict[str, object] | None:
    matches = sorted((plane.state_root / "events").glob(f"*-{event_id}-work-resumed.json"))
    if len(matches) > 1:
        raise ValueError("work resume event id has multiple immutable records")
    return read_json(matches[0], base=plane.state_root) if matches else None


def _matches_resume_actor(
    plane: ControlPlane, record: dict[str, object], *, owner: str, run_id: str
) -> bool:
    if record.get("owner") != owner:
        return False
    source_run_id = record.get("run_id")
    if source_run_id == run_id:
        return True
    if not isinstance(source_run_id, str) or record.get("status") not in {"finalizing", "resumed"}:
        return False
    source = read_json(
        plane.state_root / "runs" / f"{source_run_id}.json",
        base=plane.state_root,
    )
    return (
        source.get("owner") == owner
        and source.get("status") == "closed"
        and source.get("outcome") in {"failed", "abandoned"}
        and source.get("authority_recovered_to") == run_id
    )


def _archive_resume(
    plane: ControlPlane, path: Path, record: dict[str, object], event: dict[str, object]
) -> dict[str, object]:
    if (
        event.get("event") != "work-resumed"
        or event.get("work_state_id") != record.get("work_state_id")
        or event.get("owner") != record.get("owner")
        or event.get("run_id") != record.get("run_id")
        or event.get("disposition") != record.get("disposition")
        or event.get("status") != "resumed"
    ):
        raise ValueError("work resume intent is malformed or stale")
    event_id = event.get("event_id")
    if not isinstance(event_id, str):
        raise ValueError("work resume intent lacks an event id")
    existing = _event_by_id(plane, event_id)
    if existing is None:
        write_event(plane, event)
    elif existing != event:
        raise ValueError("work resume event differs from its durable intent")
    if record.get("status") == "finalizing":
        record.pop("terminal_event", None)
        record.update(
            {
                "status": "resumed",
                "resume_evidence": event.get("evidence"),
                "resumed_at": event.get("at"),
                "terminal_event_id": event_id,
            }
        )
        replace_json(path, record, base=plane.state_root)
    elif record.get("status") != "resumed" or record.get("terminal_event_id") != event_id:
        raise ValueError("work resume snapshot is not an exact retryable terminal state")
    destination = (
        plane.state_root
        / "work"
        / "archive"
        / f"{time.time_ns()}-{record['work_state_id']}.json"
    )
    os.replace(path, destination)
    return {**record, "archive": str(destination)}


def suspend(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    disposition: str,
    reason: str,
    contention_id: str | None = None,
    blocked_by_owner: str | None = None,
    alternate_scope: str | None = None,
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    reason = require_text(reason, "suspension reason", 1000)
    if contention_id is not None:
        contention_id = require_identifier(contention_id, "contention id")
    if blocked_by_owner is not None:
        blocked_by_owner = require_slug(blocked_by_owner, "blocked owner")
    if alternate_scope is not None:
        alternate_scope = require_slug(alternate_scope, "alternate scope")
    if disposition not in {"waiting", "diverted"}:
        raise ValueError("disposition must be waiting or diverted")
    if disposition == "waiting" and alternate_scope is not None:
        raise ValueError("waiting work cannot declare alternate scope")
    if disposition == "diverted" and alternate_scope is None:
        raise ValueError("diverted work requires alternate scope")
    with operation(root, "work-suspend") as plane:
        claim = read_json(plane.state_root / "claims" / f"{scope}.json", base=plane.state_root)
        if claim.get("owner") != owner or claim.get("run_id") != run_id:
            raise ValueError("work disposition must match an exact owned claim")
        run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
        if run.get("owner") != owner or run.get("status") != "active":
            raise ValueError("work suspension requires the exact active owner Run")
        for path in (plane.state_root / "work" / "active").glob("*.json"):
            existing = read_json(path, base=plane.state_root)
            if existing.get("scope") == scope and existing.get("owner") == owner:
                raise ValueError("work scope already has an active disposition")
        identifier = f"work-{uuid.uuid4().hex}"
        record = materialized(
            {
                "schema": 1,
                "work_state_id": identifier,
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "disposition": disposition,
                "status": disposition,
                "reason": reason,
                "contention_id": contention_id,
                "blocked_by_owner": blocked_by_owner,
                "alternate_scope": alternate_scope,
                "suspended_at": now(),
            }
        )
        path = plane.state_root / "work" / "active" / f"{identifier}.json"
        write_json_exclusive(path, record, base=plane.state_root)
        emit(
            plane,
            "work-suspended",
            payload={
                "work_state_id": identifier,
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "disposition": disposition,
                "reason": reason,
                "contention_id": contention_id,
                "blocked_by_owner": blocked_by_owner,
                "alternate_scope": alternate_scope,
            },
        )
        return record


def resume(
    root: Path, *, work_state_id: str, owner: str, run_id: str, evidence: str
) -> dict[str, object]:
    work_state_id = require_identifier(work_state_id, "work state id")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    evidence = require_text(evidence, "resume evidence", 1000)
    with operation(root, "work-resume") as plane:
        path = plane.state_root / "work" / "active" / f"{work_state_id}.json"
        if not path.exists():
            archived = sorted(
                (plane.state_root / "work" / "archive").glob(f"*-{work_state_id}.json")
            )
            if len(archived) != 1:
                raise FileNotFoundError(path)
            record = read_json(archived[0], base=plane.state_root)
            if (
                not _matches_resume_actor(plane, record, owner=owner, run_id=run_id)
                or record.get("status") != "resumed"
                or record.get("resume_evidence") != evidence
            ):
                raise ValueError("archived work resume does not match the exact retry")
            return {**record, "archive": str(archived[0])}
        record = read_json(path, base=plane.state_root)
        if not _matches_resume_actor(plane, record, owner=owner, run_id=run_id):
            raise ValueError("work disposition belongs to another exact owner Run")
        run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
        if run.get("owner") != owner or run.get("status") != "active":
            raise ValueError("work resume requires the exact active owner Run")
        if record.get("status") == "finalizing":
            terminal_event = record.get("terminal_event")
            if not isinstance(terminal_event, dict) or terminal_event.get("evidence") != evidence:
                raise ValueError("work already has a different terminal resume intent")
            return _archive_resume(plane, path, record, terminal_event)
        if record.get("status") == "resumed":
            event_id = record.get("terminal_event_id")
            if not isinstance(event_id, str):
                raise ValueError("resumed work snapshot lacks terminal event evidence")
            terminal_event = _event_by_id(plane, event_id)
            if not isinstance(terminal_event, dict) or terminal_event.get("evidence") != evidence:
                raise ValueError("resumed work snapshot differs from the exact retry")
            return _archive_resume(plane, path, record, terminal_event)
        if record.get("status") not in {"waiting", "diverted"}:
            raise ValueError("only suspended work can resume")
        terminal_event = build_event(
            "work-resumed",
            payload={
                "work_state_id": work_state_id,
                "scope": record.get("scope"),
                "owner": owner,
                "run_id": record.get("run_id"),
                "disposition": record.get("disposition"),
                "status": "resumed",
                "reason_code": "dependency-rechecked",
                "evidence": evidence,
            },
        )
        record.update({"status": "finalizing", "terminal_event": terminal_event})
        replace_json(path, record, base=plane.state_root)
        return _archive_resume(plane, path, record, terminal_event)
