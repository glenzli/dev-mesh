"""Immutable coordination event production."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from .constants import AUTHORITY_EFFECTS, EVENT_SCHEMA, MAX_EVENT_BYTES, PROTOCOL, PROTOCOL_VERSION
from .control_plane import ControlPlane
from .storage import json_bytes, now, write_json_exclusive


def materialized(value: dict[str, object]) -> dict[str, object]:
    return {
        **value,
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
    }


def build_event(
    event: str,
    *,
    transaction_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        effect = AUTHORITY_EFFECTS[event]
    except KeyError as error:
        raise ValueError(f"event has no authority classification: {event}") from error
    event_id = str(uuid.uuid4())
    record: dict[str, object] = {
        **(payload or {}),
        "schema": EVENT_SCHEMA,
        "event_id": event_id,
        "event": event,
        "at": now(),
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "authority_effect": effect,
        "transaction_id": transaction_id,
    }
    encoded = json_bytes(record)
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError(f"event exceeds the {MAX_EVENT_BYTES}-byte protocol limit")
    return record


def write_event(plane: ControlPlane, record: dict[str, object]) -> tuple[Path, dict[str, object]]:
    event = str(record["event"])
    event_id = str(record["event_id"])
    if len(json_bytes(record)) > MAX_EVENT_BYTES:
        raise ValueError(f"event exceeds the {MAX_EVENT_BYTES}-byte protocol limit")
    path = plane.state_root / "events" / f"{time.time_ns()}-{event_id}-{event}.json"
    write_json_exclusive(path, record, base=plane.state_root)
    return path, record


def emit(
    plane: ControlPlane,
    event: str,
    *,
    transaction_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[Path, dict[str, object]]:
    return write_event(
        plane,
        build_event(event, transaction_id=transaction_id, payload=payload),
    )
