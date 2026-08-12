"""Bounded, presentation-ready projections for the local Dev Mesh Console."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dev_mesh_coord.constants import PROTOCOL, PROTOCOL_VERSION
from dev_mesh_coord.storage import now

from .reports import ACTIVE_STATUSES, build_report


ALLOWED_WINDOWS = {1, 6, 12, 24, 48, 168, 720}
MAX_TIMELINE_EVENTS = 400
DETAIL_FIELDS = (
    "status",
    "outcome",
    "decision",
    "reason_code",
    "canonical_branch",
    "branch",
    "source_owner",
    "target_owner",
    "work_state_id",
    "direct_commit_id",
)


def _project_event(row: sqlite3.Row) -> dict[str, object]:
    record = json.loads(row["record_json"])
    details = {
        field: record[field]
        for field in DETAIL_FIELDS
        if record.get(field) is not None
    }
    return {
        "event_id": str(row["event_id"]),
        "at": str(row["at"]),
        "event": str(row["event"]),
        "authority_effect": str(row["authority_effect"]),
        "workspace_id": str(row["workspace_id"]),
        "owner": row["owner"],
        "run_id": row["run_id"],
        "scope": row["scope"],
        "transaction_id": row["transaction_id"],
        "contention_id": row["contention_id"],
        "handoff_id": row["handoff_id"],
        "details": details,
    }


def _active_snapshot(record: dict[str, object], kind: str, lifecycle: str) -> bool:
    return (
        lifecycle in {"active", "current"}
        and str(record.get("status") or "") in ACTIVE_STATUSES.get(kind, set())
    )


def build_dashboard(
    connection: sqlite3.Connection,
    *,
    workspace: str | None = None,
    window_hours: int = 48,
    event_limit: int = 240,
    stale_after_seconds: int = 1800,
) -> dict[str, object]:
    """Build one bounded Console payload without weakening Observer authority boundaries."""

    if window_hours not in ALLOWED_WINDOWS:
        raise ValueError(f"unsupported observation window: {window_hours}")
    if event_limit < 1 or event_limit > MAX_TIMELINE_EVENTS:
        raise ValueError(f"event limit must be between 1 and {MAX_TIMELINE_EVENTS}")

    workspace_rows = list(
        connection.execute(
            """
            SELECT workspace_id, root, last_collected_at, last_error, not_observed_since
            FROM workspaces
            WHERE protocol_version = ?
            ORDER BY root
            """,
            (PROTOCOL_VERSION,),
        )
    )
    known_ids = {str(row["workspace_id"]) for row in workspace_rows}
    if workspace is not None and workspace not in known_ids:
        raise ValueError(f"unknown workspace: {workspace}")

    generated = datetime.now(UTC)
    since = generated - timedelta(hours=window_hours)
    since_text = since.isoformat(timespec="microseconds").replace("+00:00", "Z")
    arguments: list[object] = [PROTOCOL_VERSION, since_text]
    where = "protocol_version = ? AND at >= ?"
    if workspace is not None:
        where += " AND workspace_id = ?"
        arguments.append(workspace)

    event_rows = list(
        connection.execute(
            f"""
            SELECT workspace_id, event_id, at, event, authority_effect, owner, run_id,
                   scope, transaction_id, contention_id, handoff_id, record_json
            FROM events
            WHERE {where}
            ORDER BY at DESC, event_id DESC
            LIMIT ?
            """,
            (*arguments, event_limit),
        )
    )
    events = [_project_event(row) for row in reversed(event_rows)]

    event_counts_by_workspace: Counter[str] = Counter()
    event_kinds_by_workspace: dict[str, Counter[str]] = defaultdict(Counter)
    for row in connection.execute(
        f"""
        SELECT workspace_id, event, COUNT(*) AS count
        FROM events
        WHERE {where}
        GROUP BY workspace_id, event
        """,
        tuple(arguments),
    ):
        identifier = str(row["workspace_id"])
        count = int(row["count"])
        event_counts_by_workspace[identifier] += count
        event_kinds_by_workspace[identifier][str(row["event"])] += count

    active_counts_by_workspace: dict[str, Counter[str]] = defaultdict(Counter)
    active_details: list[dict[str, object]] = []
    snapshot_arguments: list[object] = [PROTOCOL_VERSION]
    snapshot_where = "protocol_version = ?"
    if workspace is not None:
        snapshot_where += " AND workspace_id = ?"
        snapshot_arguments.append(workspace)
    for row in connection.execute(
        f"""
        SELECT workspace_id, kind, object_id, lifecycle, status, record_json
        FROM snapshots
        WHERE {snapshot_where}
        ORDER BY workspace_id, kind, object_id
        """,
        tuple(snapshot_arguments),
    ):
        record = json.loads(row["record_json"])
        kind = str(row["kind"])
        lifecycle = str(row["lifecycle"])
        if not _active_snapshot(record, kind, lifecycle):
            continue
        identifier = str(row["workspace_id"])
        active_counts_by_workspace[identifier][kind] += 1
        active_details.append(
            {
                "workspace_id": identifier,
                "kind": kind,
                "object_id": str(row["object_id"]),
                "status": row["status"],
                "owner": record.get("owner"),
                "run_id": record.get("run_id") or record.get("owner_run_id"),
                "scope": record.get("scope"),
            }
        )

    operational = build_report(
        connection,
        workspace=workspace,
        stale_after_seconds=stale_after_seconds,
    )
    diagnostics_by_workspace: Counter[str] = Counter()
    for item in operational["diagnostics"]:
        if isinstance(item, dict) and isinstance(item.get("workspace_id"), str):
            diagnostics_by_workspace[str(item["workspace_id"])] += 1

    projects = []
    for row in workspace_rows:
        identifier = str(row["workspace_id"])
        root = str(row["root"])
        project = {
            "workspace_id": identifier,
            "name": Path(root).name,
            "root": root,
            "last_collected_at": row["last_collected_at"],
            "collection_error": row["last_error"],
            "not_observed_since": row["not_observed_since"],
            "event_count": event_counts_by_workspace[identifier],
            "event_counts": dict(sorted(event_kinds_by_workspace[identifier].items())),
            "active": dict(sorted(active_counts_by_workspace[identifier].items())),
            "diagnostic_count": diagnostics_by_workspace[identifier],
        }
        projects.append(project)

    return {
        "schema": 1,
        "kind": "dev-mesh.console.dashboard",
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": now(),
        "selection": {
            "workspace_id": workspace,
            "window_hours": window_hours,
            "since": since_text,
            "event_limit": event_limit,
            "events_truncated": len(event_rows) == event_limit,
        },
        "operational": operational,
        "projects": projects,
        "events": events,
        "active_details": active_details,
    }
