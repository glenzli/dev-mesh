"""Notice, request, and explicit handoff lifecycles."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

from .constants import INTERACTION_KINDS, INTERACTION_TOPICS
from .control_plane import ControlPlane, operation
from .events import build_event, emit, materialized, write_event
from .storage import (
    now,
    read_json,
    replace_json,
    require_identifier,
    require_slug,
    require_text,
    write_json_exclusive,
)


def _message_path(plane: ControlPlane, message_id: str) -> Path:
    return plane.state_root / "messages" / f"{message_id}.json"


def _handoff_path(plane: ControlPlane, handoff_id: str) -> Path:
    return plane.state_root / "handoffs" / f"{handoff_id}.json"


def _matching_event(
    plane: ControlPlane, event: str, *, message_id: str, handoff_id: str | None
) -> dict[str, object] | None:
    matches: list[dict[str, object]] = []
    for path in sorted((plane.state_root / "events").glob(f"*-{event}.json")):
        candidate = read_json(path, base=plane.state_root)
        if candidate.get("event") != event or candidate.get("message_id") != message_id:
            continue
        if handoff_id is not None and candidate.get("handoff_id") != handoff_id:
            continue
        matches.append(candidate)
    if len(matches) > 1:
        raise ValueError(f"message has multiple {event} events")
    return matches[0] if matches else None


def _assert_same_message(record: dict[str, object], expected: dict[str, object]) -> None:
    fields = (
        "message_id",
        "source_owner",
        "target_owner",
        "source_run_id",
        "interaction_kind",
        "topic",
        "requires_ack",
        "subject",
        "body",
        "handoff_id",
    )
    if any(record.get(field) != expected.get(field) for field in fields):
        raise ValueError("stable handoff id already belongs to another message")


def _ensure_event(
    plane: ControlPlane,
    event: str,
    *,
    message_id: str,
    handoff_id: str | None,
    payload: dict[str, object],
) -> None:
    existing = _matching_event(
        plane, event, message_id=message_id, handoff_id=handoff_id
    )
    if existing is None:
        emit(plane, event, payload=payload)
        return
    if any(existing.get(key) != value for key, value in payload.items()):
        raise ValueError(f"existing {event} event differs from the exact message retry")


def send(
    root: Path,
    *,
    source_owner: str,
    target_owner: str,
    subject: str,
    body: str,
    interaction_kind: str,
    source_run_id: str,
    topic: str = "general",
    requires_ack: bool = False,
    handoff_id: str | None = None,
) -> dict[str, object]:
    source_owner = require_slug(source_owner, "source owner")
    target_owner = require_slug(target_owner, "target owner")
    subject = require_text(subject, "subject", 300)
    body = require_text(body, "body", 4000)
    if interaction_kind not in INTERACTION_KINDS:
        raise ValueError(f"unsupported interaction kind: {interaction_kind}")
    if topic not in INTERACTION_TOPICS:
        raise ValueError(f"unsupported interaction topic: {topic}")
    if interaction_kind == "notice" and requires_ack:
        raise ValueError("notice cannot require acknowledgement")
    source_run_id = require_identifier(source_run_id, "source run id")
    if interaction_kind == "handoff":
        if not requires_ack:
            raise ValueError("handoff requires acknowledgement and a source run")
        if handoff_id is None:
            raise ValueError("handoff requires a caller-supplied stable handoff id")
    elif handoff_id is not None:
        raise ValueError("handoff id is only valid for handoff interactions")
    if handoff_id is not None:
        handoff_id = require_identifier(handoff_id, "handoff id")
    message_id = (
        f"msg-handoff-{hashlib.sha256(handoff_id.encode('utf-8')).hexdigest()[:32]}"
        if handoff_id is not None
        else f"msg-{time.time_ns()}-{uuid.uuid4().hex[:10]}"
    )
    with operation(root, "interaction-send") as plane:
        run = read_json(plane.state_root / "runs" / f"{source_run_id}.json", base=plane.state_root)
        if run.get("owner") != source_owner or run.get("status") != "active":
            raise ValueError("source run is not active for the source owner")
        record = materialized(
            {
                "schema": 1,
                "message_id": message_id,
                "source_owner": source_owner,
                "target_owner": target_owner,
                "source_run_id": source_run_id,
                "interaction_kind": interaction_kind,
                "topic": topic,
                "requires_ack": requires_ack,
                "subject": subject,
                "body": body,
                "handoff_id": handoff_id,
                "created_at": now(),
            }
        )
        message_path = _message_path(plane, message_id)
        repairing = message_path.exists()
        if repairing:
            existing_message = read_json(message_path, base=plane.state_root)
            _assert_same_message(existing_message, record)
            record = existing_message
        else:
            # A message without a handoff is inert. Persisting it first guarantees that an offered
            # handoff snapshot can never point at a missing message after a crash.
            write_json_exclusive(message_path, record, base=plane.state_root)
        handoff: dict[str, object] | None = None
        if handoff_id is not None:
            expected_handoff = materialized(
                {
                    "schema": 1,
                    "handoff_id": handoff_id,
                    "message_id": message_id,
                    "source_owner": source_owner,
                    "source_run_id": source_run_id,
                    "target_owner": target_owner,
                    "status": "offered",
                    "offered_at": now(),
                }
            )
            handoff_path = _handoff_path(plane, handoff_id)
            if handoff_path.exists():
                handoff = read_json(handoff_path, base=plane.state_root)
                if any(
                    handoff.get(field) != expected_handoff.get(field)
                    for field in (
                        "handoff_id",
                        "message_id",
                        "source_owner",
                        "source_run_id",
                        "target_owner",
                    )
                ):
                    raise ValueError("stable handoff id already belongs to another offer")
            else:
                handoff = expected_handoff
                write_json_exclusive(handoff_path, handoff, base=plane.state_root)
        message_event_payload = {
            "message_id": message_id,
            "source_owner": source_owner,
            "target_owner": target_owner,
            "owner": source_owner,
            "run_id": source_run_id,
            "interaction_kind": interaction_kind,
            "topic": topic,
            "requires_ack": requires_ack,
            "handoff_id": handoff_id,
        }
        if repairing:
            _ensure_event(
                plane,
                "message-sent",
                message_id=message_id,
                handoff_id=handoff_id,
                payload=message_event_payload,
            )
        else:
            emit(plane, "message-sent", payload=message_event_payload)
        if handoff_id is not None and handoff is not None:
            handoff_event_payload = {
                "handoff_id": handoff_id,
                "message_id": message_id,
                "source_owner": source_owner,
                "target_owner": target_owner,
                "owner": source_owner,
                "run_id": source_run_id,
            }
            if repairing:
                _ensure_event(
                    plane,
                    "handoff-offered",
                    message_id=message_id,
                    handoff_id=handoff_id,
                    payload=handoff_event_payload,
                )
            else:
                emit(plane, "handoff-offered", payload=handoff_event_payload)
        return record


def acknowledge(
    root: Path,
    *,
    message_id: str,
    target_owner: str,
    target_run_id: str,
    note: str = "Acknowledged",
) -> dict[str, object]:
    message_id = require_identifier(message_id, "message id")
    target_owner = require_slug(target_owner, "target owner")
    target_run_id = require_identifier(target_run_id, "target run id")
    note = require_text(note, "acknowledgement note", 1000)
    with operation(root, "interaction-ack") as plane:
        message = read_json(_message_path(plane, message_id), base=plane.state_root)
        if message.get("target_owner") != target_owner:
            raise ValueError("message targets another owner")
        if not message.get("requires_ack"):
            raise ValueError("message does not require acknowledgement")
        kind = message.get("interaction_kind")
        run = read_json(plane.state_root / "runs" / f"{target_run_id}.json", base=plane.state_root)
        if run.get("owner") != target_owner or run.get("status") != "active":
            raise ValueError("target run is not active for target owner")
        ack_path = plane.state_root / "acks" / f"{message_id}-{target_owner}.json"
        existing_ack = read_json(ack_path, base=plane.state_root) if ack_path.exists() else None
        if existing_ack is not None and (
            existing_ack.get("owner") != target_owner or existing_ack.get("run_id") != target_run_id
        ):
            raise ValueError("message was acknowledged by a different exact Run")
        handoff_id = message.get("handoff_id")
        handoff_path: Path | None = None
        handoff: dict[str, object] | None = None
        if kind == "handoff" and isinstance(handoff_id, str):
            handoff_path = _handoff_path(plane, handoff_id)
            handoff = read_json(handoff_path, base=plane.state_root)
            if handoff.get("status") == "accepted" and ack_path.exists():
                assert existing_ack is not None
                return existing_ack
            if handoff.get("status") != "offered":
                raise ValueError("handoff is already terminal")
        if existing_ack is not None:
            ack = existing_ack
        else:
            ack = materialized(
                {
                    "schema": 1,
                    "message_id": message_id,
                    "owner": target_owner,
                    "run_id": target_run_id,
                    "note": note,
                    "acknowledged_at": now(),
                }
            )
            write_json_exclusive(ack_path, ack, base=plane.state_root)
            emit(
                plane,
                "message-acknowledged",
                payload={
                    "message_id": message_id,
                    "owner": target_owner,
                    "run_id": target_run_id,
                    "interaction_kind": kind,
                },
            )
        if handoff_path is not None and handoff is not None and isinstance(handoff_id, str):
            handoff.update(
                {
                    "status": "accepted",
                    "target_run_id": target_run_id,
                    "accepted_at": now(),
                }
            )
            replace_json(handoff_path, handoff, base=plane.state_root)
            emit(
                plane,
                "handoff-accepted",
                payload={
                    "handoff_id": handoff_id,
                    "message_id": message_id,
                    "source_owner": handoff.get("source_owner"),
                    "target_owner": target_owner,
                    "target_run_id": target_run_id,
                    "owner": target_owner,
                    "run_id": target_run_id,
                    "status": "accepted",
                    "reason_code": "target-accepted",
                },
            )
        return ack


def reject(
    root: Path,
    *,
    handoff_id: str,
    target_owner: str,
    target_run_id: str,
    reason_code: str,
    reason: str,
) -> dict[str, object]:
    return _terminal_handoff(
        root,
        handoff_id=handoff_id,
        actor=target_owner,
        actor_run_id=target_run_id,
        expected_role="target_owner",
        status="rejected",
        event="handoff-rejected",
        reason_code=reason_code,
        reason=reason,
    )


def withdraw(
    root: Path,
    *,
    handoff_id: str,
    source_owner: str,
    source_run_id: str,
    reason_code: str,
    reason: str,
) -> dict[str, object]:
    return _terminal_handoff(
        root,
        handoff_id=handoff_id,
        actor=source_owner,
        actor_run_id=source_run_id,
        expected_role="source_owner",
        status="withdrawn",
        event="handoff-withdrawn",
        reason_code=reason_code,
        reason=reason,
    )


def _terminal_handoff(
    root: Path,
    *,
    handoff_id: str,
    actor: str,
    actor_run_id: str,
    expected_role: str,
    status: str,
    event: str,
    reason_code: str,
    reason: str,
) -> dict[str, object]:
    handoff_id = require_identifier(handoff_id, "handoff id")
    actor = require_slug(actor, "actor")
    actor_run_id = require_identifier(actor_run_id, "actor run id")
    reason_code = require_identifier(reason_code, "reason code")
    reason = require_text(reason, "reason", 1000)
    with operation(root, event) as plane:
        path = _handoff_path(plane, handoff_id)
        handoff = read_json(path, base=plane.state_root)
        if handoff.get(expected_role) != actor:
            raise ValueError(f"handoff {expected_role} does not match actor")
        expected_run = handoff.get("source_run_id") if expected_role == "source_owner" else actor_run_id
        if expected_run != actor_run_id:
            raise ValueError("handoff source Run does not match actor Run")
        run = read_json(plane.state_root / "runs" / f"{actor_run_id}.json", base=plane.state_root)
        if run.get("owner") != actor or run.get("status") != "active":
            raise ValueError("handoff terminal action requires an exact active Run")
        if handoff.get("status") != "offered":
            raise ValueError("handoff is already terminal")
        terminal_event = build_event(
            event,
            payload={
                "handoff_id": handoff_id,
                "source_owner": handoff.get("source_owner"),
                "target_owner": handoff.get("target_owner"),
                "owner": actor,
                "run_id": actor_run_id,
                "status": status,
                "reason_code": reason_code,
                "reason": reason,
            },
        )
        handoff.update(
            {
                "status": status,
                "reason_code": reason_code,
                "reason": reason,
                f"{status}_at": now(),
            }
        )
        replace_json(path, handoff, base=plane.state_root)
        write_event(plane, terminal_event)
        return handoff
