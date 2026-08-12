"""Read-only diagnostic projections over the versioned Observer catalog."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from dev_mesh_coord.constants import PROTOCOL, PROTOCOL_VERSION
from dev_mesh_coord.storage import now

from .diagnostics import MAX_DIAGNOSTICS, project_diagnostics
from .source_validation import SNAPSHOT_STATUSES


COLLABORATION_EVENTS = {
    "claim-requested",
    "contention-opened",
    "contention-coordinator-acquired",
    "contention-decision-proposed",
    "contention-decision-responded",
    "contention-completed",
    "message-sent",
    "message-acknowledged",
    "handoff-offered",
    "handoff-accepted",
    "handoff-rejected",
    "handoff-withdrawn",
    "work-suspended",
    "transaction-created",
    "transaction-handed-off",
    "transaction-published",
    "transaction-aborted",
}

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
    run_events: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    hot_paths: Counter[str] = Counter()
    owner_edges: Counter[tuple[str, str, str]] = Counter()
    for item in events:
        record = item["record"]
        owner = record.get("owner")
        run_id = record.get("run_id")
        if isinstance(owner, str) and isinstance(run_id, str):
            run_events[(str(item["workspace_id"]), owner, run_id)].append(str(item["event"]))
        if item["event"] == "claim-requested":
            for path in record.get("paths", []) if isinstance(record.get("paths"), list) else []:
                if isinstance(path, str):
                    hot_paths[path] += 1
        source = record.get("source_owner")
        target = record.get("target_owner")
        if isinstance(source, str) and isinstance(target, str):
            owner_edges[(source, target, str(item["event"]))] += 1

    for item in snapshots:
        kind = str(item["kind"])
        lifecycle = str(item.get("lifecycle") or "current")
        status = str(item.get("status") or "unspecified")
        status_counts[f"{kind}:{status}"] += 1
        lifecycle_status_counts[f"{kind}:{lifecycle}:{status}"] += 1
        if lifecycle in {"active", "current"} and status in ACTIVE_STATUSES.get(kind, set()):
            active_counts[kind] += 1

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
        and not (set(values) & COLLABORATION_EVENTS)
    ]
    collection_errors = [item for item in workspaces if item.get("last_error")]
    not_observed_workspaces = [item for item in workspaces if item.get("not_observed_since")]
    diagnostics, diagnostic_summary, cutover_readiness = project_diagnostics(
        events,
        snapshots,
        integrity_findings,
        integrity_counts=integrity_counts,
        integrity_by_workspace=integrity_by_workspace,
        stale_after_seconds=stale_after_seconds,
        collection_errors=collection_errors,
        not_observed_workspaces=not_observed_workspaces,
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
