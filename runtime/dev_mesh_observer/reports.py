"""Read-only diagnostic projections over the versioned Observer catalog."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from dev_mesh_coord.constants import PROTOCOL, PROTOCOL_VERSION
from dev_mesh_coord.storage import now

from .collaboration_semantics import (
    has_distinct_participants,
    is_collaboration_record,
)
from .diagnostics import MAX_DIAGNOSTICS, project_diagnostics
from .source_validation import SNAPSHOT_STATUSES


ACTIVE_STATUSES = {
    "run": {"active"},
    "claim": set(SNAPSHOT_STATUSES[("claim", "current")]),
    "handoff": {"offered"},
    "contention": set(SNAPSHOT_STATUSES[("contention", "active")]),
    "transaction": set(SNAPSHOT_STATUSES[("transaction", "active")]),
    "cleanup": set(SNAPSHOT_STATUSES[("cleanup", "active")]),
    "work": set(SNAPSHOT_STATUSES[("work", "active")]),
    "direct-commit": set(SNAPSHOT_STATUSES[("direct-commit", "active")]),
}

MAX_PENDING_ACKNOWLEDGEMENTS = 64


def _acknowledgement_projection(
    events: list[dict[str, object]],
    snapshots: list[dict[str, object]],
) -> dict[str, object]:
    required: dict[tuple[str, str], dict[str, object]] = {}
    acknowledged: set[tuple[str, str]] = set()
    closed_runs: set[tuple[str, str, str]] = set()
    terminal_handoff_messages: set[tuple[str, str]] = set()
    for item in snapshots:
        record = item["record"]
        workspace_id = str(item["workspace_id"])
        if item["kind"] == "run" and record.get("status") == "closed":
            owner = record.get("owner")
            run_id = record.get("run_id")
            if isinstance(owner, str) and isinstance(run_id, str):
                closed_runs.add((workspace_id, owner, run_id))
        elif item["kind"] == "handoff" and record.get("status") in {
            "accepted",
            "rejected",
            "withdrawn",
        }:
            message_id = record.get("message_id")
            if isinstance(message_id, str):
                terminal_handoff_messages.add((workspace_id, message_id))

    for item in events:
        record = item["record"]
        message_id = record.get("message_id")
        if not isinstance(message_id, str):
            continue
        identity = (str(item["workspace_id"]), message_id)
        if item["event"] == "message-sent" and record.get("requires_ack") is True:
            required[identity] = {
                "workspace_id": identity[0],
                "message_id": message_id,
                "at": str(item["at"]),
                "source_owner": record.get("source_owner"),
                "source_run_id": record.get("run_id"),
                "target_owner": record.get("target_owner"),
                "topic": record.get("topic"),
                "handoff_id": record.get("handoff_id"),
            }
        elif item["event"] == "message-acknowledged":
            acknowledged.add(identity)

    pending: list[dict[str, object]] = []
    lifecycle_resolved: list[dict[str, object]] = []
    historical: list[dict[str, object]] = []
    for identity, value in required.items():
        if identity in acknowledged:
            continue
        if identity in terminal_handoff_messages:
            lifecycle_resolved.append({**value, "classification": "lifecycle-resolved"})
            continue
        source_owner = value.get("source_owner")
        source_run_id = value.get("source_run_id")
        if (
            isinstance(source_owner, str)
            and isinstance(source_run_id, str)
            and (identity[0], source_owner, source_run_id) in closed_runs
        ):
            historical.append({**value, "classification": "historical-unacknowledged"})
            continue
        pending.append({**value, "classification": "pending"})

    def order(item: dict[str, object]) -> tuple[str, str]:
        return str(item["at"]), str(item["message_id"])

    pending.sort(key=order)
    lifecycle_resolved.sort(key=order)
    historical.sort(key=order)
    acknowledged_required = sum(identity in acknowledged for identity in required)
    return {
        "count": len(pending),
        "requested": len(required),
        "acknowledged": acknowledged_required,
        "lifecycle_resolved": len(lifecycle_resolved),
        "historical": len(historical),
        "oldest_at": pending[0]["at"] if pending else None,
        "shown": min(len(pending), MAX_PENDING_ACKNOWLEDGEMENTS),
        "truncated": len(pending) > MAX_PENDING_ACKNOWLEDGEMENTS,
        "items": pending[:MAX_PENDING_ACKNOWLEDGEMENTS],
        "lifecycle_resolved_items": lifecycle_resolved[:MAX_PENDING_ACKNOWLEDGEMENTS],
        "historical_items": historical[:MAX_PENDING_ACKNOWLEDGEMENTS],
    }


def build_report(
    connection: sqlite3.Connection,
    *,
    workspace: str | None = None,
    stale_after_seconds: int = 1800,
) -> dict[str, object]:
    event_where = "protocol_version = ?"
    snapshot_where = "protocol_version = ?"
    event_arguments: tuple[object, ...] = (PROTOCOL_VERSION,)
    snapshot_arguments: tuple[object, ...] = (PROTOCOL_VERSION,)
    if workspace:
        event_where += " AND workspace_id = ?"
        snapshot_where += " AND workspace_id = ?"
        event_arguments += (workspace,)
        snapshot_arguments += (workspace,)
    events = [
        {**dict(row), "record": json.loads(row["record_json"])}
        for row in connection.execute(
            f"SELECT * FROM events WHERE {event_where} ORDER BY at, event_id", event_arguments
        )
    ]
    snapshots = [
        {**dict(row), "record": json.loads(row["record_json"])}
        for row in connection.execute(
            f"SELECT * FROM snapshots WHERE {snapshot_where}", snapshot_arguments
        )
    ]
    finding_where = "protocol_version = ?"
    finding_arguments: tuple[object, ...] = (PROTOCOL_VERSION,)
    if workspace:
        finding_where += " AND workspace_id = ?"
        finding_arguments += (workspace,)
    active_finding_where = finding_where + " AND resolved_at IS NULL"
    integrity_count_rows = list(
        connection.execute(
            f"""
            SELECT code, COUNT(*) AS count
            FROM integrity_findings
            WHERE {active_finding_where}
            GROUP BY code
            """,
            finding_arguments,
        )
    )
    integrity_counts = {
        str(row["code"]): int(row["count"]) for row in integrity_count_rows
    }
    integrity_total = sum(integrity_counts.values())
    integrity_by_workspace: dict[str, int] = {
        str(row["workspace_id"]): int(row["count"])
        for row in connection.execute(
            f"""
            SELECT workspace_id, COUNT(*) AS count
            FROM integrity_findings
            WHERE {active_finding_where}
            GROUP BY workspace_id
            """,
            finding_arguments,
        )
    }
    integrity_historical_total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM integrity_findings WHERE {finding_where}",
            finding_arguments,
        ).fetchone()[0]
    )
    integrity_findings = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT * FROM integrity_findings
            WHERE {active_finding_where}
            ORDER BY observed_at, object_id
            LIMIT ?
            """,
            (*finding_arguments, MAX_DIAGNOSTICS),
        )
    ]
    workspaces = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM workspaces WHERE protocol_version = ? ORDER BY root", (PROTOCOL_VERSION,)
        )
        if workspace is None or row["workspace_id"] == workspace
    ]

    event_counts = Counter(str(item["event"]) for item in events)
    snapshot_counts = Counter(str(item["kind"]) for item in snapshots)
    active_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    lifecycle_status_counts: Counter[str] = Counter()
    work_result_projection_modes: Counter[str] = Counter()
    run_events: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    collaborative_run_keys: set[tuple[str, str, str]] = set()
    true_contentions = {
        (str(item["workspace_id"]), str(item["record"].get("contention_id")))
        for item in snapshots
        if item["kind"] == "contention"
        and has_distinct_participants(item["record"])
    }
    hot_paths: Counter[str] = Counter()
    owner_edges: Counter[tuple[str, str, str]] = Counter()
    for item in events:
        record = item["record"]
        owner = record.get("owner")
        run_id = record.get("run_id")
        if isinstance(owner, str) and isinstance(run_id, str):
            run_key = (str(item["workspace_id"]), owner, run_id)
            run_events[run_key].append(str(item["event"]))
            contention_key = (
                str(item["workspace_id"]),
                str(record.get("contention_id")),
            )
            if is_collaboration_record(
                str(item["event"]),
                record,
                true_contention=contention_key in true_contentions,
            ):
                collaborative_run_keys.add(run_key)
        if item["event"] == "claim-requested":
            for path in record.get("paths", []) if isinstance(record.get("paths"), list) else []:
                if isinstance(path, str):
                    hot_paths[path] += 1
        source = record.get("source_owner")
        target = record.get("target_owner")
        if (
            isinstance(source, str)
            and isinstance(target, str)
            and is_collaboration_record(
                str(item["event"]),
                record,
                true_contention=(
                    str(item["workspace_id"]),
                    str(record.get("contention_id")),
                )
                in true_contentions,
            )
        ):
            owner_edges[(source, target, str(item["event"]))] += 1

    for item in snapshots:
        kind = str(item["kind"])
        lifecycle = str(item.get("lifecycle") or "current")
        status = str(item.get("status") or "unspecified")
        status_counts[f"{kind}:{status}"] += 1
        lifecycle_status_counts[f"{kind}:{lifecycle}:{status}"] += 1
        if lifecycle in {"active", "current"} and status in ACTIVE_STATUSES.get(kind, set()):
            active_counts[kind] += 1
        if kind == "work-result":
            work_result_projection_modes[
                str(item["record"].get("projection_mode", "git-tree"))
            ] += 1
        if kind == "contention":
            participants = item["record"].get("participants", [])
            exact_participants = [
                participant
                for participant in participants
                if isinstance(participant, dict)
                and isinstance(participant.get("owner"), str)
                and isinstance(participant.get("run_id"), str)
            ] if isinstance(participants, list) else []
            workspace_id_value = str(item["workspace_id"])
            if not has_distinct_participants(item["record"]):
                continue
            for participant in exact_participants:
                collaborative_run_keys.add(
                    (
                        workspace_id_value,
                        str(participant["owner"]),
                        str(participant["run_id"]),
                    )
                )
            trigger_scope = item["record"].get("trigger_scope")
            trigger = next(
                (
                    participant
                    for participant in exact_participants
                    if participant.get("scope") == trigger_scope
                ),
                None,
            )
            if trigger is not None:
                source = str(trigger["owner"])
                for participant in exact_participants:
                    target = str(participant["owner"])
                    if target != source:
                        owner_edges[(source, target, "contention")] += 1

    closed_run_keys = {
        (
            str(item["workspace_id"]),
            str(item["record"].get("owner")),
            str(item["record"].get("run_id")),
        ): item
        for item in snapshots
        if item["kind"] == "run" and item["record"].get("status") == "closed"
    }
    non_collaborative_runs = [
        {
            "workspace_id": workspace_id_value,
            "owner": owner,
            "run_id": run_id,
            "outcome": closed_run_keys[(workspace_id_value, owner, run_id)]["record"].get("outcome"),
            "events": values,
        }
        for (workspace_id_value, owner, run_id), values in sorted(run_events.items())
        if (workspace_id_value, owner, run_id) in closed_run_keys
        and (workspace_id_value, owner, run_id) not in collaborative_run_keys
    ]
    collection_errors = [item for item in workspaces if item.get("last_error")]
    not_observed_workspaces = [item for item in workspaces if item.get("not_observed_since")]
    acknowledgement_projection = _acknowledgement_projection(events, snapshots)
    pending_acknowledgements = list(acknowledgement_projection["items"])
    diagnostics, diagnostic_summary, cutover_readiness = project_diagnostics(
        events,
        snapshots,
        integrity_findings,
        integrity_counts=integrity_counts,
        integrity_by_workspace=integrity_by_workspace,
        stale_after_seconds=stale_after_seconds,
        collection_errors=collection_errors,
        not_observed_workspaces=not_observed_workspaces,
        pending_acknowledgements=pending_acknowledgements,
    )
    return {
        "schema": 1,
        "kind": "dev-mesh.observer.report",
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": now(),
        "workspace_count": len(workspaces),
        "workspaces": workspaces,
        "event_count": len(events),
        "event_counts": dict(sorted(event_counts.items())),
        "snapshots": dict(sorted(snapshot_counts.items())),
        "active": dict(sorted(active_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "lifecycle_status_counts": dict(sorted(lifecycle_status_counts.items())),
        "transaction_outcomes": {
            "published": event_counts["transaction-published"],
            "aborted": event_counts["transaction-aborted"],
            "conflicted": event_counts["transaction-conflicted"],
            "refreshed": event_counts["transaction-refreshed"],
        },
        "direct_commit": {
            "started": event_counts["direct-commit-started"],
            "completed": event_counts["direct-commit-completed"],
            "active": active_counts["direct-commit"],
            "outcomes": {
                "completed": event_counts["direct-commit-completed"],
                "needs_attention": status_counts["direct-commit:needs-attention"],
            },
        },
        "work_results": {
            "recorded": status_counts["work-result:recorded"],
            "projection_modes": dict(sorted(work_result_projection_modes.items())),
            "completed_events": event_counts["claim-completed"],
            "awaiting_baseline_acknowledgement": status_counts[
                "claim:pending-baseline"
            ],
            "completion_pending": status_counts["claim:completing"],
        },
        "interaction_counts": {
            key: event_counts[key]
            for key in (
                "message-sent",
                "message-acknowledged",
                "handoff-offered",
                "handoff-accepted",
                "handoff-rejected",
                "handoff-withdrawn",
            )
        },
        "pending_acknowledgements": acknowledgement_projection,
        "contention": {
            "opened": event_counts["contention-opened"],
            "completed": event_counts["contention-completed"],
            "cancelled": event_counts["contention-cancelled"],
            "resolved": event_counts["contention-completed"]
            + event_counts["contention-cancelled"],
            "active": active_counts["contention"],
            "conflicts": event_counts["claim-requested"],
            "hot_paths": [
                {"path": path, "count": count} for path, count in hot_paths.most_common(20)
            ],
        },
        "non_collaborative_runs": non_collaborative_runs,
        "owner_edges": [
            {"source": source, "target": target, "event": event, "count": count}
            for (source, target, event), count in sorted(owner_edges.items())
        ],
        "integrity": {
            "total": integrity_total,
            "historical_total": integrity_historical_total,
            "resolved_total": integrity_historical_total - integrity_total,
            "counts": dict(sorted(integrity_counts.items())),
            "source_mutations": integrity_counts.get("event.source-mutated", 0),
            "shown": min(integrity_total, MAX_DIAGNOSTICS),
            "truncated": integrity_total > MAX_DIAGNOSTICS,
            "findings": integrity_findings[:MAX_DIAGNOSTICS],
        },
        "diagnostics": diagnostics,
        "diagnostic_summary": diagnostic_summary,
        "cutover_readiness": cutover_readiness,
    }
