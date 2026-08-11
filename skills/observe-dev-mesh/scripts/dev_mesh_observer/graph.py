"""Bounded causal graph projection for human-scale collaboration inspection."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .state_projection import project_active_contentions


MAX_GRAPH_EVENTS = 10_000
TRANSACTION_STATUS = {
    "transaction-activated": "active",
    "transaction-handed-off": "paused",
    "transaction-resumed": "active",
    "transaction-prepared": "prepared",
    "transaction-validated": "ready",
    "refresh-started": "refreshing",
    "refresh-conflicted": "conflicted",
    "refresh-completed": "prepared",
    "publish-started": "publishing",
    "publish-completed": "published",
    "transaction-aborted": "aborted",
}


def _payload(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def build_collaboration_graph(
    connection: sqlite3.Connection,
    *,
    since: datetime,
    workspace_id: str | None = None,
    limit: int = 120,
) -> dict[str, object]:
    """Aggregate immutable events into a bounded entity graph."""

    if limit < 10 or limit > 300:
        raise ValueError("graph limit must be between 10 and 300")
    workspace_rows = list(
        connection.execute(
            "SELECT workspace_id, workspace_root FROM workspaces ORDER BY workspace_root"
        )
    )
    workspace_names = {
        str(row["workspace_id"]): str(row["workspace_root"])
        for row in workspace_rows
    }
    since_text = since.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    conditions = ["COALESCE(e.event_at, e.ingested_at) >= ?"]
    parameters: list[object] = [since_text]
    if workspace_id:
        conditions.append("e.workspace_id = ?")
        parameters.append(workspace_id)
    parameters.append(MAX_GRAPH_EVENTS)
    rows = list(
        connection.execute(
            f"""
            SELECT e.*
            FROM events e
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(e.event_at, e.ingested_at) DESC, e.source_name DESC
            LIMIT ?
            """,
            parameters,
        )
    )
    rows.reverse()

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    joined_runs: set[tuple[str, str]] = set()
    left_runs: set[tuple[str, str]] = set()

    def node(
        identifier: str,
        kind: str,
        label: str,
        workspace: str,
        *,
        status: str = "observed",
        subtitle: str | None = None,
    ) -> dict[str, Any]:
        current = nodes.setdefault(
            identifier,
            {
                "id": identifier,
                "type": kind,
                "label": label,
                "subtitle": subtitle,
                "status": status,
                "workspace_id": workspace,
                "workspace_root": workspace_names.get(workspace),
                "first_at": None,
                "last_at": None,
                "event_count": 0,
                "details": {
                    "runs": set(),
                    "claims": set(),
                    "paths": set(),
                    "semantic_resources": set(),
                    "tasks": set(),
                },
            },
        )
        if status not in {"observed", "closed"}:
            current["status"] = status
        if subtitle and not current.get("subtitle"):
            current["subtitle"] = subtitle
        return current

    def agent(workspace: str, owner: str) -> dict[str, Any]:
        return node(
            f"agent:{workspace}:{owner}",
            "agent",
            owner,
            workspace,
        )

    def touch(item: dict[str, Any], at: object) -> None:
        value = _text(at)
        if value:
            if item["first_at"] is None or value < item["first_at"]:
                item["first_at"] = value
            if item["last_at"] is None or value > item["last_at"]:
                item["last_at"] = value
        item["event_count"] += 1

    def edge(
        source: str,
        target: str,
        kind: str,
        at: object,
    ) -> None:
        key = (source, target, kind)
        current = edges.setdefault(
            key,
            {
                "id": f"{kind}:{source}:{target}",
                "source": source,
                "target": target,
                "type": kind,
                "count": 0,
                "first_at": None,
                "last_at": None,
            },
        )
        value = _text(at)
        if value:
            if current["first_at"] is None or value < current["first_at"]:
                current["first_at"] = value
            if current["last_at"] is None or value > current["last_at"]:
                current["last_at"] = value
        current["count"] += 1

    for row in rows:
        workspace = str(row["workspace_id"])
        event = str(row["event_type"] or "unknown")
        payload = _payload(row["payload_json"])
        at = row["event_at"] or row["ingested_at"]
        owner = _text(row["owner"]) or _text(payload.get("owner"))
        owner_node = agent(workspace, owner) if owner else None
        if owner_node is not None:
            touch(owner_node, at)
            details = owner_node["details"]
            details["paths"].update(_strings(payload.get("paths")))
            details["semantic_resources"].update(
                _strings(payload.get("semantic_resources"))
            )
            if _text(row["scope"]):
                details["claims"].add(str(row["scope"]))

        run_id = _text(row["run_id"]) or _text(payload.get("run_id"))
        if event == "agent-joined" and owner and run_id:
            joined_runs.add((workspace, run_id))
            assert owner_node is not None
            owner_node["details"]["runs"].add(run_id)
            task = _text(payload.get("task_summary"))
            if task:
                owner_node["details"]["tasks"].add(task)
            parent = _text(payload.get("parent_agent_id"))
            if parent:
                parent_node = agent(workspace, parent)
                edge(parent_node["id"], owner_node["id"], "delegated", at)
        elif event == "agent-left" and run_id:
            left_runs.add((workspace, run_id))

        if event == "message-sent" and owner_node is not None:
            target_owner = (
                _text(payload.get("target_owner"))
                or _text(payload.get("to"))
                or _text(row["scope"])
            )
            if target_owner:
                target = agent(workspace, target_owner)
                edge(owner_node["id"], target["id"], "message", at)

        handoff_id = _text(row["handoff_id"]) or _text(payload.get("handoff_id"))
        if handoff_id and event in {"handoff-offered", "handoff-accepted"}:
            handoff = node(
                f"handoff:{workspace}:{handoff_id}",
                "handoff",
                handoff_id,
                workspace,
                status="accepted" if event == "handoff-accepted" else "offered",
            )
            touch(handoff, at)
            source_owner = _text(payload.get("source_owner")) or owner
            target_owner = _text(payload.get("target_owner"))
            if source_owner:
                source = agent(workspace, source_owner)
                edge(source["id"], handoff["id"], "handoff-offered", at)
            if target_owner:
                target = agent(workspace, target_owner)
                edge(handoff["id"], target["id"], "handoff-target", at)

        contention_id = _text(row["contention_id"]) or _text(
            payload.get("contention_id")
        )
        if contention_id:
            contention = node(
                f"contention:{workspace}:{contention_id}",
                "contention",
                contention_id,
                workspace,
                status="active",
                subtitle=_text(payload.get("recommendation")),
            )
            touch(contention, at)
            contention["details"]["paths"].update(_strings(payload.get("paths")))
            contention["details"]["semantic_resources"].update(
                _strings(payload.get("semantic_resources"))
            )
            contention["details"]["claims"].update(
                _strings(payload.get("scopes"))
            )
            participants = _strings(payload.get("owners"))
            if owner:
                participants.append(owner)
            for participant in sorted(set(participants)):
                participant_node = agent(workspace, participant)
                edge(participant_node["id"], contention["id"], "contends", at)
            if event in {"contention-completed", "contention-enacted"}:
                contention["status"] = "completed" if event.endswith("completed") else "enacted"

        transaction_id = _text(row["transaction_id"]) or _text(
            payload.get("transaction_id")
        )
        if transaction_id:
            transaction = node(
                f"transaction:{workspace}:{transaction_id}",
                "transaction",
                transaction_id,
                workspace,
                status=TRANSACTION_STATUS.get(event, "observed"),
            )
            touch(transaction, at)
            for field in ("actual_paths", "conflicts", "paths"):
                transaction["details"]["paths"].update(
                    _strings(payload.get(field))
                )
            if owner_node is not None:
                edge(owner_node["id"], transaction["id"], "owns", at)
            if contention_id:
                edge(
                    f"contention:{workspace}:{contention_id}",
                    transaction["id"],
                    "materializes",
                    at,
                )
            candidate = _text(payload.get("candidate"))
            if event == "publish-completed" and candidate:
                commit = node(
                    f"commit:{workspace}:{candidate}",
                    "commit",
                    candidate[:12],
                    workspace,
                    status="published",
                    subtitle="canonical commit",
                )
                touch(commit, at)
                edge(transaction["id"], commit["id"], "publishes", at)

    open_runs = joined_runs - left_runs
    for item in nodes.values():
        if item["type"] != "agent":
            continue
        runs = item["details"]["runs"]
        workspace = item["workspace_id"]
        item["status"] = (
            "active"
            if any((workspace, run_id) in open_runs for run_id in runs)
            else "closed"
        )

    active = project_active_contentions(
        connection, workspace_id=workspace_id
    )["active_contentions"]
    assert isinstance(active, list)
    for snapshot in active:
        workspace = str(snapshot["workspace_id"])
        identifier = f"contention:{workspace}:{snapshot['contention_id']}"
        contention = node(
            identifier,
            "contention",
            str(snapshot["contention_id"]),
            workspace,
            status="stalled" if snapshot["stalled"] else "active",
            subtitle=_text(snapshot.get("recommendation")),
        )
        contention["details"]["claims"].update(snapshot.get("scopes", []))
        contention["details"]["paths"].update(snapshot.get("paths", []))
        contention["details"]["semantic_resources"].update(
            snapshot.get("semantic_resources", [])
        )
        contention["details"]["missing_responses"] = snapshot.get(
            "missing_responses", []
        )
        contention["details"]["lease_until"] = snapshot.get("lease_until")
        contention["details"]["recommendation_reason"] = snapshot.get(
            "recommendation_reason"
        )
        for participant in snapshot.get("owners", []):
            participant_node = agent(workspace, str(participant))
            edge(
                participant_node["id"],
                contention["id"],
                "contends",
                snapshot.get("created_at"),
            )

    type_priority = {
        "contention": 0,
        "transaction": 1,
        "handoff": 2,
        "agent": 3,
        "commit": 4,
    }
    status_priority = {"stalled": 0, "conflicted": 0, "active": 1, "offered": 1}
    ordered = sorted(
        nodes.values(),
        key=lambda item: (
            status_priority.get(str(item["status"]), 2),
            type_priority.get(str(item["type"]), 9),
            str(item.get("label") or ""),
        ),
    )
    total_nodes = len(ordered)
    visible = ordered[:limit]
    visible_ids = {str(item["id"]) for item in visible}
    visible_edges = [
        value
        for value in edges.values()
        if value["source"] in visible_ids and value["target"] in visible_ids
    ]
    for item in visible:
        details = item["details"]
        for key in ("runs", "claims", "paths", "semantic_resources", "tasks"):
            value = details.get(key)
            if isinstance(value, set):
                details[key] = sorted(value)[:40]
    return {
        "since": since.isoformat(),
        "workspace_id": workspace_id,
        "summary": {
            "total_nodes": total_nodes,
            "visible_nodes": len(visible),
            "visible_edges": len(visible_edges),
            "truncated": total_nodes > len(visible),
            "source_events": len(rows),
        },
        "nodes": visible,
        "edges": sorted(
            visible_edges,
            key=lambda item: (
                str(item["type"]),
                str(item["source"]),
                str(item["target"]),
            ),
        ),
    }
