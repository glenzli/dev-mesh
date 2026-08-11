"""Diagnostic projections of mutable coordination snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


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


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def project_active_contentions(
    connection: sqlite3.Connection,
    *,
    current: datetime | None = None,
    workspace_id: str | None = None,
) -> dict[str, object]:
    """Project current contention state as diagnostics, never as authority."""

    now = (current or datetime.now(UTC)).astimezone(UTC)
    parameters: list[object] = []
    where = ""
    if workspace_id:
        where = "WHERE c.workspace_id=?"
        parameters.append(workspace_id)
    rows = connection.execute(
        f"""
        SELECT c.*, w.workspace_root
        FROM active_contentions c
        JOIN workspaces w ON w.workspace_id = c.workspace_id
        {where}
        ORDER BY c.collected_at DESC, c.contention_id
        """,
        parameters,
    )
    contentions: list[dict[str, object]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        participants = payload.get("participants", [])
        owners = sorted(
            {
                str(item["owner"])
                for item in participants
                if isinstance(item, dict) and isinstance(item.get("owner"), str)
            }
        )
        responses = payload.get("responses", {})
        if not isinstance(responses, dict):
            responses = {}
        lease_until = _parse_time(row["lease_until"])
        lease_expired = lease_until is not None and lease_until <= now
        status = str(row["status"] or payload.get("status") or "open")
        request = payload.get("request", {})
        if not isinstance(request, dict):
            request = {}
        request_status = payload.get("request_status") or request.get("status")
        stalled = bool(
            lease_expired
            or status == "needs-decision"
            or (status == "scheduled" and request_status == "needs-attention")
        )
        recommendation = payload.get("recommendation", {})
        if not isinstance(recommendation, dict):
            recommendation = {}
        contentions.append(
            {
                "workspace_id": str(row["workspace_id"]),
                "workspace_root": str(row["workspace_root"]),
                "contention_id": str(row["contention_id"]),
                "status": status,
                "stalled": stalled,
                "coordinator": row["coordinator"],
                "coordinator_epoch": row["coordinator_epoch"],
                "lease_until": row["lease_until"],
                "lease_expired": lease_expired,
                "scopes": _strings(payload.get("scopes")),
                "owners": owners,
                "missing_responses": [
                    owner for owner in owners if owner not in responses
                ],
                "paths": _strings(payload.get("paths")),
                "semantic_resources": _strings(
                    payload.get("semantic_resources")
                ),
                "recommendation": recommendation.get("recommendation"),
                "recommendation_reason": recommendation.get("reason"),
                "request_id": payload.get("request_id"),
                "request_status": request_status,
                "created_at": payload.get("created_at"),
                "collected_at": row["collected_at"],
            }
        )
    contentions.sort(
        key=lambda item: (
            not bool(item["stalled"]),
            str(item.get("created_at") or ""),
            str(item["contention_id"]),
        )
    )
    return {
        "summary": {
            "active": len(contentions),
            "stalled": sum(bool(item["stalled"]) for item in contentions),
            "lease_expired": sum(
                bool(item["lease_expired"]) for item in contentions
            ),
            "awaiting_responses": sum(
                bool(item["missing_responses"]) for item in contentions
            ),
        },
        "active_contentions": contentions,
        "stalled_contentions": [
            item for item in contentions if bool(item["stalled"])
        ],
    }
