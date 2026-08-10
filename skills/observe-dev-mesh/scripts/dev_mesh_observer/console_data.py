"""Bounded read models for the local Observer console."""

from __future__ import annotations

import json
import sqlite3


MAX_QUERY_TEXT = 256
MAX_EVENT_LIMIT = 500


def _bounded(value: str | None, *, name: str) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > MAX_QUERY_TEXT:
        raise ValueError(f"{name} is too long")
    return value


def bounded_limit(value: str | int | None, *, default: int = 100) -> int:
    try:
        limit = default if value is None else int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("limit must be an integer") from error
    if limit < 1 or limit > MAX_EVENT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_LIMIT}")
    return limit


def list_events(
    connection: sqlite3.Connection,
    *,
    filters: dict[str, str | None],
    limit: int,
) -> list[dict[str, object]]:
    columns = {
        "workspace_id": "e.workspace_id",
        "event": "e.event_type",
        "owner": "e.owner",
        "run_id": "e.run_id",
        "handoff_id": "e.handoff_id",
        "scope": "e.scope",
        "transaction_id": "e.transaction_id",
    }
    conditions: list[str] = []
    parameters: list[object] = []
    for name, column in columns.items():
        value = _bounded(filters.get(name), name=name)
        if value is not None:
            conditions.append(f"{column} = ?")
            parameters.append(value)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    parameters.append(bounded_limit(limit))
    rows = connection.execute(
        f"""
        SELECT e.workspace_id, w.workspace_root, e.source_name, e.digest,
               e.event_at, e.event_type, e.run_id, e.handoff_id,
               e.contention_id, e.request_id, e.transaction_id,
               e.scope, e.owner, e.ingested_at
        FROM events e
        JOIN workspaces w ON w.workspace_id = e.workspace_id
        {where}
        ORDER BY COALESCE(e.event_at, e.ingested_at) DESC, e.source_name DESC
        LIMIT ?
        """,
        parameters,
    )
    return [dict(row) for row in rows]


def event_detail(
    connection: sqlite3.Connection,
    *,
    workspace_id: str | None,
    source_name: str | None,
) -> dict[str, object] | None:
    workspace = _bounded(workspace_id, name="workspace_id")
    source = _bounded(source_name, name="source_name")
    if workspace is None or source is None:
        raise ValueError("workspace_id and source_name are required")
    row = connection.execute(
        """
        SELECT e.*, w.workspace_root
        FROM events e
        JOIN workspaces w ON w.workspace_id = e.workspace_id
        WHERE e.workspace_id = ? AND e.source_name = ?
        """,
        (workspace, source),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(str(result.pop("payload_json")))
    except json.JSONDecodeError:
        result["payload"] = None
    return result


def list_issues(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT i.issue_id, i.workspace_id, w.workspace_root, i.source_name,
               i.kind, i.detail, i.expected_digest, i.observed_digest,
               i.first_detected_at, i.last_detected_at, i.occurrences
        FROM collection_issues i
        JOIN workspaces w ON w.workspace_id = i.workspace_id
        ORDER BY i.last_detected_at DESC, i.issue_id DESC
        LIMIT ?
        """,
        (bounded_limit(limit),),
    )
    return [dict(row) for row in rows]
