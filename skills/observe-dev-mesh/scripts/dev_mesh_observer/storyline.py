"""Database facade for the owner-aware collaboration storyline."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .state_projection import project_active_contentions
from .storyline_projection import StorylineProjection


MAX_STORY_EVENTS = 10_000


def _payload(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def build_collaboration_storyline(
    connection: sqlite3.Connection,
    *,
    since: datetime,
    workspace_id: str,
    limit: int = 36,
    page: int = 1,
) -> dict[str, object]:
    """Return compressed Git context, owner work spans, and exact relations."""
    if not workspace_id:
        raise ValueError("storyline workspace_id is required")
    if limit < 8 or limit > 300:
        raise ValueError("storyline limit must be between 8 and 300")
    if page < 1:
        raise ValueError("storyline page must be positive")

    workspace = connection.execute(
        "SELECT workspace_root FROM workspaces WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    workspace_root = str(workspace["workspace_root"]) if workspace else None
    since_text = since.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    rows = list(
        connection.execute(
            """
            SELECT e.*
            FROM events e
            WHERE e.workspace_id = ?
              AND COALESCE(e.event_at, e.ingested_at) >= ?
            ORDER BY COALESCE(e.event_at, e.ingested_at), e.source_name
            LIMIT ?
            """,
            (workspace_id, since_text, MAX_STORY_EVENTS),
        )
    )

    projection = StorylineProjection(workspace_id, workspace_root)
    for row in rows:
        projection.consume(
            event=str(row["event_type"] or "unknown"),
            at=_text(row["event_at"]) or _text(row["ingested_at"]),
            source_name=str(row["source_name"]),
            payload=_payload(row["payload_json"]),
            row_owner=_text(row["owner"]),
            row_scope=_text(row["scope"]),
            row_run_id=_text(row["run_id"]),
            row_handoff_id=_text(row["handoff_id"]),
            row_contention_id=_text(row["contention_id"]),
            row_transaction_id=_text(row["transaction_id"]),
        )

    active = project_active_contentions(connection, workspace_id=workspace_id)
    snapshots = active.get("active_contentions", [])
    if isinstance(snapshots, list):
        projection.annotate_active_contentions(
            snapshot for snapshot in snapshots if isinstance(snapshot, dict)
        )
    result = projection.serialise(
        limit=limit,
        source_events=len(rows),
        page=page,
    )
    result["since"] = since.isoformat()
    return result
