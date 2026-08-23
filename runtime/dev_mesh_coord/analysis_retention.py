"""Authority-free, decontented analysis evidence retained during version cutover."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .errors import ProtocolError
from .storage import read_json


MAX_RETAINED_EVENTS = 4096
RETENTION_POLICY = "bounded-event-envelope-v1"

SCALAR_FIELDS = (
    "event_id",
    "at",
    "event",
    "authority_effect",
    "owner",
    "run_id",
    "scope",
    "status",
    "outcome",
    "decision",
    "disposition",
    "reason_code",
    "message_id",
    "handoff_id",
    "contention_id",
    "work_state_id",
    "transaction_id",
    "direct_commit_id",
    "result_id",
    "source_owner",
    "source_run_id",
    "target_owner",
    "target_run_id",
    "blocked_by_owner",
    "alternate_scope",
)
LIST_FIELDS = ("owners", "participant_run_ids", "scopes")
ACTOR_FIELDS = ("workspace_id", "owner", "run_id")
CROSS_PROJECT_FIELDS = (
    "protocol",
    "protocol_version",
    "collaboration_id",
    "phase",
    "kind",
    "actor_role",
    "outcome",
)


def _bounded_strings(value: object, *, limit: int = 128) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:limit] if isinstance(item, str)]


def _actor(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        field: str(value[field])
        for field in ACTOR_FIELDS
        if isinstance(value.get(field), str)
    }


def _cross_project(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    projected: dict[str, object] = {
        field: value[field]
        for field in CROSS_PROJECT_FIELDS
        if isinstance(value.get(field), str)
    }
    for role in ("source", "target"):
        actor = _actor(value.get(role))
        if actor:
            projected[role] = actor
    return projected or None


def _conflicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    projected = []
    for item in value[:128]:
        if not isinstance(item, dict):
            continue
        conflict: dict[str, object] = {
            field: item[field]
            for field in ("scope", "owner", "run_id", "status", "contention_id")
            if isinstance(item.get(field), str)
        }
        if isinstance(item.get("physical_overlap_count"), int):
            conflict["physical_overlap_count"] = item["physical_overlap_count"]
        semantic = _bounded_strings(item.get("semantic_resources"), limit=64)
        if semantic:
            conflict["semantic_resources"] = semantic
        if conflict:
            projected.append(conflict)
    return projected


def project_event(record: dict[str, object]) -> dict[str, object]:
    """Remove content-bearing fields while retaining coordination identity and causality."""

    projected: dict[str, object] = {
        field: record[field]
        for field in SCALAR_FIELDS
        if isinstance(record.get(field), (str, int, bool))
    }
    for field in LIST_FIELDS:
        values = _bounded_strings(record.get(field))
        if values:
            projected[field] = values
    conflicts = _conflicts(record.get("conflicts"))
    if conflicts:
        projected["conflicts"] = conflicts
    cross_project = _cross_project(record.get("cross_project"))
    if cross_project is not None:
        projected["cross_project"] = cross_project
    return projected


def build_analysis_retention(
    state_root: Path,
    *,
    source_version: str,
    source_event_schema: int,
    source_state_sha256: str,
    recorded_at: str,
) -> dict[str, object]:
    """Build one deterministic, bounded record from immutable source events only."""

    events: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    invalid_event_count = 0
    event_root = state_root / "events"
    for path in sorted(event_root.glob("*.json")) if event_root.is_dir() else []:
        try:
            record = read_json(path, base=state_root)
        except (OSError, ProtocolError):
            invalid_event_count += 1
            continue
        event = record.get("event")
        if not isinstance(event, str):
            invalid_event_count += 1
            continue
        counts[event] += 1
        events.append(project_event(record))
    events.sort(key=lambda item: (str(item.get("at", "")), str(item.get("event_id", ""))))
    retained = events[-MAX_RETAINED_EVENTS:]
    return {
        "schema": 1,
        "kind": "dev-mesh.coordination.analysis-retention",
        "authority": "none",
        "policy": RETENTION_POLICY,
        "source_version": source_version,
        "source_event_schema": source_event_schema,
        "source_state_sha256": source_state_sha256,
        "recorded_at": recorded_at,
        "total_event_count": len(events) + invalid_event_count,
        "valid_event_count": len(events),
        "invalid_event_count": invalid_event_count,
        "retained_event_count": len(retained),
        "omitted_event_count": max(0, len(events) - len(retained)),
        "events_truncated": len(events) > len(retained),
        "event_counts": dict(sorted(counts.items())),
        "events": retained,
    }
