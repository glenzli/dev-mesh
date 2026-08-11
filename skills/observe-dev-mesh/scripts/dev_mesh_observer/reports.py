"""Cross-workspace reports derived from the Observer event mirror."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta

from .analytics import build_coordination_analytics
from .project_overview import build_project_overview
from .state_projection import project_active_contentions


DURATION = re.compile(r"^(\d+)([mhdw])$")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_since(value: str, *, current: datetime | None = None) -> datetime:
    match = DURATION.fullmatch(value.strip().lower())
    if match:
        amount = int(match.group(1))
        if amount <= 0:
            raise ValueError("duration must be greater than zero")
        unit = match.group(2)
        seconds = {
            "m": 60,
            "h": 60 * 60,
            "d": 24 * 60 * 60,
            "w": 7 * 24 * 60 * 60,
        }[unit]
        return (current or datetime.now(UTC)) - timedelta(seconds=amount * seconds)
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError("since must be an ISO timestamp or duration such as 48h or 7d")
    return parsed


def build_report(
    connection: sqlite3.Connection,
    *,
    since: datetime,
    limit: int = 10,
) -> dict[str, object]:
    if limit < 1 or limit > 100:
        raise ValueError("report limit must be between 1 and 100")
    workspace_rows = list(
        connection.execute(
            "SELECT workspace_id, workspace_root FROM workspaces ORDER BY workspace_root"
        )
    )
    workspace_names = {
        str(row["workspace_id"]): str(row["workspace_root"])
        for row in workspace_rows
    }
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT workspace_id, event_at, event_type, run_id, handoff_id,
                   transaction_id, owner, payload_json
            FROM events
            ORDER BY event_at, source_name
            """
        )
    ]
    window_rows = [
        row
        for row in rows
        if (parsed := _parse_time(row["event_at"])) is not None and parsed >= since
    ]

    joined: set[tuple[str, str]] = set()
    left: set[tuple[str, str]] = set()
    offered: set[tuple[str, str]] = set()
    accepted: set[tuple[str, str]] = set()
    for row in rows:
        workspace_id = str(row["workspace_id"])
        event = row["event_type"]
        run_id = row["run_id"]
        handoff_id = row["handoff_id"]
        if isinstance(run_id, str):
            if event == "agent-joined":
                joined.add((workspace_id, run_id))
            elif event == "agent-left":
                left.add((workspace_id, run_id))
        if isinstance(handoff_id, str):
            if event == "handoff-offered":
                offered.add((workspace_id, handoff_id))
            elif event == "handoff-accepted":
                accepted.add((workspace_id, handoff_id))

    event_counts = Counter(str(row["event_type"] or "unknown") for row in window_rows)
    workspace_counts = Counter(str(row["workspace_id"]) for row in window_rows)
    owner_counts = Counter(
        str(row["owner"]) for row in window_rows if isinstance(row["owner"], str)
    )
    resource_counts: Counter[str] = Counter()
    for row in window_rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for field in ("paths", "semantic_resources", "resources"):
            values = payload.get(field, [])
            if isinstance(values, list):
                resource_counts.update(
                    value for value in values if isinstance(value, str)
                )

    issue_count = int(
        connection.execute("SELECT COUNT(*) FROM collection_issues").fetchone()[0]
    )
    generated_at = datetime.now(UTC)
    coordination_state = project_active_contentions(
        connection,
        current=generated_at,
    )
    return {
        "generated_at": _iso(generated_at),
        "since": _iso(since),
        "summary": {
            "registered_workspaces": len(workspace_rows),
            "active_workspaces_in_window": len(workspace_counts),
            "events_in_window": len(window_rows),
            "runs_joined": len(joined),
            "runs_closed": len(joined & left),
            "runs_open": len(joined - left),
            "handoffs_offered": len(offered),
            "handoffs_accepted": len(offered & accepted),
            "handoffs_pending": len(offered - accepted),
            "collection_issues": issue_count,
        },
        "event_types": dict(event_counts.most_common()),
        "workspace_activity": [
            {
                "workspace_id": workspace_id,
                "workspace_root": workspace_names.get(workspace_id),
                "events": count,
            }
            for workspace_id, count in workspace_counts.most_common(limit)
        ],
        "owner_activity": [
            {"owner": owner, "events": count}
            for owner, count in owner_counts.most_common(limit)
        ],
        "resource_activity": [
            {"resource": resource, "events": count}
            for resource, count in resource_counts.most_common(limit)
        ],
        "open_runs": [
            {
                "workspace_id": workspace_id,
                "workspace_root": workspace_names.get(workspace_id),
                "run_id": run_id,
            }
            for workspace_id, run_id in sorted(joined - left)
        ][:limit],
        "pending_handoffs": [
            {
                "workspace_id": workspace_id,
                "workspace_root": workspace_names.get(workspace_id),
                "handoff_id": handoff_id,
            }
            for workspace_id, handoff_id in sorted(offered - accepted)
        ][:limit],
        "coordination_analytics": build_coordination_analytics(
            rows,
            since=since,
            workspace_names=workspace_names,
            limit=limit,
            current=generated_at,
        ),
        "coordination_state": coordination_state,
        "project_overview": build_project_overview(
            workspace_names=workspace_names,
            rows=rows,
            window_rows=window_rows,
            coordination_state=coordination_state,
        ),
    }
