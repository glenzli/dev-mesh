"""Presentation-only cross-project identity projection for Observer."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any


GENERIC_OWNER_IDS = {
    "__canonical__",
    "__system__",
    "agent",
    "root",
    "system",
    "unknown",
}
MAX_EVENT_TYPES = 12
MAX_RUN_IDS = 12
MAX_TASKS = 4


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _moment(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _payload(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _task(payload: Mapping[str, object]) -> str | None:
    for field in ("task", "task_summary"):
        value = _text(payload.get(field))
        if value is not None:
            return value[:240]
    return None


def _seconds(value: datetime) -> int:
    return int(value.timestamp())


def build_cross_project_projection(
    *,
    workspace_names: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Infer exact-owner identity across projects without inventing causality."""

    activity: dict[tuple[str, str], dict[str, Any]] = {}
    ignored_generic_events = 0
    for row in rows:
        workspace_id = str(row.get("workspace_id") or "")
        owner = _text(row.get("owner"))
        at = _moment(row.get("event_at"))
        if workspace_id not in workspace_names or owner is None or at is None:
            continue
        if owner.casefold() in GENERIC_OWNER_IDS:
            ignored_generic_events += 1
            continue
        key = (workspace_id, owner)
        episode = activity.setdefault(
            key,
            {
                "id": f"activity:{workspace_id}:{owner}",
                "workspace_id": workspace_id,
                "workspace_root": workspace_names[workspace_id],
                "owner": owner,
                "started_at": at,
                "last_at": at,
                "event_count": 0,
                "run_ids": set(),
                "event_types": set(),
                "tasks": [],
            },
        )
        episode["started_at"] = min(episode["started_at"], at)
        episode["last_at"] = max(episode["last_at"], at)
        episode["event_count"] += 1
        run_id = _text(row.get("run_id"))
        if run_id and len(episode["run_ids"]) < MAX_RUN_IDS:
            episode["run_ids"].add(run_id)
        event_type = _text(row.get("event_type"))
        if event_type and len(episode["event_types"]) < MAX_EVENT_TYPES:
            episode["event_types"].add(event_type)
        task = _task(_payload(row.get("payload_json")))
        if task and task not in episode["tasks"] and len(episode["tasks"]) < MAX_TASKS:
            episode["tasks"].append(task)

    workspaces_by_owner: defaultdict[str, set[str]] = defaultdict(set)
    for workspace_id, owner in activity:
        workspaces_by_owner[owner].add(workspace_id)
    shared_owners = {
        owner for owner, workspaces in workspaces_by_owner.items() if len(workspaces) > 1
    }

    episodes: list[dict[str, object]] = []
    by_owner: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for (workspace_id, owner), raw in activity.items():
        if owner not in shared_owners:
            continue
        episode = {
            "id": raw["id"],
            "workspace_id": workspace_id,
            "workspace_root": raw["workspace_root"],
            "owner": owner,
            "started_at": raw["started_at"].isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "last_at": raw["last_at"].isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "event_count": raw["event_count"],
            "run_ids": sorted(raw["run_ids"]),
            "event_types": sorted(raw["event_types"]),
            "tasks": list(raw["tasks"]),
            "identity_quality": "owner-exact",
            "authority": "presentation-only",
        }
        episodes.append(episode)
        by_owner[owner].append(episode)

    relations: list[dict[str, object]] = []
    for owner, owner_episodes in sorted(by_owner.items()):
        ordered = sorted(
            owner_episodes,
            key=lambda item: (
                str(item["started_at"]),
                str(item["workspace_id"]),
            ),
        )
        for index, (left, right) in enumerate(zip(ordered, ordered[1:]), 1):
            left_start = _moment(left["started_at"])
            left_end = _moment(left["last_at"])
            right_start = _moment(right["started_at"])
            right_end = _moment(right["last_at"])
            if None in {left_start, left_end, right_start, right_end}:
                continue
            overlap = min(left_end, right_end) - max(left_start, right_start)
            overlap_seconds = max(0, int(overlap.total_seconds()))
            gap_seconds = 0
            if overlap_seconds == 0:
                gap_seconds = max(
                    0,
                    min(
                        abs(_seconds(right_start) - _seconds(left_end)),
                        abs(_seconds(left_start) - _seconds(right_end)),
                    ),
                )
            temporal_relation = "overlap" if overlap_seconds else "sequence"
            confidence = (
                "strong"
                if overlap_seconds > 0 or gap_seconds <= 30 * 60
                else "moderate"
            )
            relations.append(
                {
                    "id": f"owner-link:{owner}:{index}",
                    "kind": "owner-identity",
                    "owner": owner,
                    "episode_ids": [left["id"], right["id"]],
                    "workspace_ids": [
                        left["workspace_id"],
                        right["workspace_id"],
                    ],
                    "temporal_relation": temporal_relation,
                    "overlap_seconds": overlap_seconds,
                    "gap_seconds": gap_seconds,
                    "confidence": confidence,
                    "evidence": ["owner-exact", f"time-{temporal_relation}"],
                    "authority": "presentation-only",
                    "directed": False,
                }
            )

    episodes.sort(
        key=lambda item: (
            str(item["started_at"]),
            str(item["workspace_root"]),
            str(item["owner"]),
        )
    )
    active_workspace_ids = {str(item["workspace_id"]) for item in episodes}
    projects = [
        {
            "workspace_id": workspace_id,
            "workspace_root": workspace_root,
            "episode_count": sum(
                str(item["workspace_id"]) == workspace_id for item in episodes
            ),
        }
        for workspace_id, workspace_root in sorted(
            workspace_names.items(), key=lambda item: item[1]
        )
        if workspace_id in active_workspace_ids
    ]
    return {
        "tracking_supported": True,
        "causal_tracking_supported": False,
        "authority": "presentation-only",
        "observed_relations": len(relations),
        "summary": {
            "owners": len(shared_owners),
            "projects": len(projects),
            "episodes": len(episodes),
            "inferred_relations": len(relations),
            "explicit_relations": 0,
            "ignored_generic_events": ignored_generic_events,
        },
        "projects": projects,
        "episodes": episodes,
        "relations": relations,
        "reason": (
            "exact non-generic owner identity with temporal evidence; "
            "cross-project causality remains unproven"
        ),
    }
