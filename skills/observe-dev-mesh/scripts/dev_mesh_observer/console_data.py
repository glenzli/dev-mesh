"""Bounded read models for the local Observer console."""

from __future__ import annotations

import json
import sqlite3


MAX_QUERY_TEXT = 256
MAX_EVENT_LIMIT = 500
MAX_EVENT_PAGE = 10_000
MAX_SQLITE_ROWID = 2**63 - 1


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


def bounded_page(value: str | int | None, *, default: int = 1) -> int:
    try:
        page = default if value is None else int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("page must be an integer") from error
    if page < 1 or page > MAX_EVENT_PAGE:
        raise ValueError(f"page must be between 1 and {MAX_EVENT_PAGE}")
    return page


def bounded_anchor(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        anchor = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("anchor must be an integer") from error
    if anchor < 0 or anchor > MAX_SQLITE_ROWID:
        raise ValueError(f"anchor must be between 0 and {MAX_SQLITE_ROWID}")
    return anchor


def _event_filter(
    filters: dict[str, str | None],
) -> tuple[list[str], list[object]]:
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
    return conditions, parameters


def event_page(
    connection: sqlite3.Connection,
    *,
    filters: dict[str, str | None],
    limit: int,
    page: int,
    anchor: int | None,
) -> dict[str, object]:
    page_size = bounded_limit(limit)
    requested_page = bounded_page(page)
    snapshot_anchor = bounded_anchor(anchor)
    if snapshot_anchor is None:
        snapshot_anchor = int(
            connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]
        )

    filter_conditions, filter_parameters = _event_filter(filters)
    snapshot_conditions = [*filter_conditions, "e.rowid <= ?"]
    snapshot_parameters = [*filter_parameters, snapshot_anchor]
    snapshot_where = f"WHERE {' AND '.join(snapshot_conditions)}"
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM events e {snapshot_where}",
            snapshot_parameters,
        ).fetchone()[0]
    )
    page_count = max(1, (total + page_size - 1) // page_size)
    current_page = min(requested_page, page_count)
    offset = (current_page - 1) * page_size

    newer_conditions = [*filter_conditions, "e.rowid > ?"]
    newer_parameters = [*filter_parameters, snapshot_anchor]
    newer_where = f"WHERE {' AND '.join(newer_conditions)}"
    newer = int(
        connection.execute(
            f"SELECT COUNT(*) FROM events e {newer_where}",
            newer_parameters,
        ).fetchone()[0]
    )

    query_parameters = [*snapshot_parameters, page_size, offset]
    rows = connection.execute(
        f"""
        SELECT e.workspace_id, w.workspace_root, e.source_name, e.digest,
               e.event_at, e.event_type, e.run_id, e.handoff_id,
               e.contention_id, e.request_id, e.transaction_id,
               e.scope, e.owner, e.ingested_at
        FROM events e
        JOIN workspaces w ON w.workspace_id = e.workspace_id
        {snapshot_where}
        ORDER BY COALESCE(e.event_at, e.ingested_at) DESC,
                 e.source_name DESC, e.workspace_id DESC, e.rowid DESC
        LIMIT ? OFFSET ?
        """,
        query_parameters,
    )
    return {
        "events": [dict(row) for row in rows],
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total": total,
            "pages": page_count,
            "anchor": snapshot_anchor,
            "newer": newer,
            "has_previous": current_page > 1,
            "has_next": current_page < page_count,
        },
    }


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
    workspace_id: str | None = None,
) -> list[dict[str, object]]:
    workspace = _bounded(workspace_id, name="workspace_id")
    where = "WHERE i.workspace_id = ?" if workspace is not None else ""
    parameters: list[object] = [workspace] if workspace is not None else []
    parameters.append(bounded_limit(limit))
    rows = connection.execute(
        f"""
        SELECT i.issue_id, i.workspace_id, w.workspace_root, i.source_name,
               i.kind, i.detail, i.expected_digest, i.observed_digest,
               i.first_detected_at, i.last_detected_at, i.occurrences
        FROM collection_issues i
        JOIN workspaces w ON w.workspace_id = i.workspace_id
        {where}
        ORDER BY i.last_detected_at DESC, i.issue_id DESC
        LIMIT ?
        """,
        parameters,
    )
    return [dict(row) for row in rows]


def count_issues(
    connection: sqlite3.Connection,
    *,
    workspace_id: str | None = None,
) -> int:
    workspace = _bounded(workspace_id, name="workspace_id")
    if workspace is None:
        return int(
            connection.execute("SELECT COUNT(*) FROM collection_issues").fetchone()[0]
        )
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM collection_issues WHERE workspace_id = ?",
            (workspace,),
        ).fetchone()[0]
    )
