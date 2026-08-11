"""Per-workspace summaries and presentation-only cross-project identity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cross_project import build_cross_project_projection


COLLABORATION_EVENTS = {
    "message-sent",
    "message-acknowledged",
    "handoff-offered",
    "handoff-accepted",
    "contention-opened",
    "contention-decision-proposed",
    "contention-decision-accepted",
    "contention-decision-rejected",
    "contention-enacted",
    "contention-completed",
}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def build_project_overview(
    *,
    workspace_names: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
    window_rows: Sequence[Mapping[str, Any]],
    coordination_state: Mapping[str, object],
) -> dict[str, object]:
    """Summarize each workspace and attach bounded cross-project inference."""

    projects: dict[str, dict[str, Any]] = {
        workspace_id: {
            "workspace_id": workspace_id,
            "workspace_root": workspace_root,
            "events_in_window": 0,
            "last_activity_at": None,
            "active_runs": 0,
            "pending_handoffs": 0,
            "active_contentions": 0,
            "stalled_contentions": 0,
            "transactions_observed": 0,
            "transaction_conflicts": 0,
            "collaboration_signals": 0,
        }
        for workspace_id, workspace_root in workspace_names.items()
    }
    joined: set[tuple[str, str]] = set()
    left: set[tuple[str, str]] = set()
    offered: set[tuple[str, str]] = set()
    accepted: set[tuple[str, str]] = set()
    transactions: dict[str, set[str]] = {
        workspace_id: set() for workspace_id in workspace_names
    }
    conflicted_transactions: dict[str, set[str]] = {
        workspace_id: set() for workspace_id in workspace_names
    }

    for row in rows:
        workspace_id = str(row.get("workspace_id") or "")
        if workspace_id not in projects:
            continue
        event = _text(row.get("event_type"))
        run_id = _text(row.get("run_id"))
        handoff_id = _text(row.get("handoff_id"))
        if run_id:
            if event == "agent-joined":
                joined.add((workspace_id, run_id))
            elif event == "agent-left":
                left.add((workspace_id, run_id))
        if handoff_id:
            if event == "handoff-offered":
                offered.add((workspace_id, handoff_id))
            elif event == "handoff-accepted":
                accepted.add((workspace_id, handoff_id))

    for workspace_id, _ in joined - left:
        projects[workspace_id]["active_runs"] += 1
    for workspace_id, _ in offered - accepted:
        projects[workspace_id]["pending_handoffs"] += 1

    for row in window_rows:
        workspace_id = str(row.get("workspace_id") or "")
        project = projects.get(workspace_id)
        if project is None:
            continue
        project["events_in_window"] += 1
        event_at = _text(row.get("event_at"))
        if event_at and (
            project["last_activity_at"] is None
            or event_at > project["last_activity_at"]
        ):
            project["last_activity_at"] = event_at
        event = _text(row.get("event_type"))
        transaction_id = _text(row.get("transaction_id"))
        if event in COLLABORATION_EVENTS or transaction_id:
            project["collaboration_signals"] += 1
        if transaction_id:
            transactions[workspace_id].add(transaction_id)
            if event == "refresh-conflicted":
                conflicted_transactions[workspace_id].add(transaction_id)

    for workspace_id, values in transactions.items():
        projects[workspace_id]["transactions_observed"] = len(values)
        projects[workspace_id]["transaction_conflicts"] = len(
            conflicted_transactions[workspace_id]
        )

    active_contentions = coordination_state.get("active_contentions", [])
    if isinstance(active_contentions, list):
        for contention in active_contentions:
            if not isinstance(contention, dict):
                continue
            workspace_id = str(contention.get("workspace_id") or "")
            project = projects.get(workspace_id)
            if project is None:
                continue
            project["active_contentions"] += 1
            if contention.get("stalled") is True:
                project["stalled_contentions"] += 1

    ordered = sorted(
        projects.values(),
        key=lambda project: (
            -int(project["stalled_contentions"]),
            -int(project["active_contentions"]),
            -int(project["active_runs"]),
            -int(project["events_in_window"]),
            str(project["workspace_root"]),
        ),
    )
    return {
        "summary": {
            "projects": len(ordered),
            "active_projects": sum(
                int(project["events_in_window"]) > 0 for project in ordered
            ),
            "stalled_projects": sum(
                int(project["stalled_contentions"]) > 0 for project in ordered
            ),
        },
        "projects": ordered,
        "cross_project": build_cross_project_projection(
            workspace_names=workspace_names,
            rows=window_rows,
        ),
    }
