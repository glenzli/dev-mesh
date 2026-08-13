"""Bounded, presentation-ready projections for the local Dev Mesh Console."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

from dev_mesh_coord.constants import PROTOCOL, PROTOCOL_VERSION
from dev_mesh_coord.cross_project import (
    COLLABORATION_KINDS,
    EXTENSION_PROTOCOL,
    EXTENSION_VERSION,
    WORKSPACE_ID,
)
from dev_mesh_coord.storage import now

from .reports import ACTIVE_STATUSES, build_report


ALLOWED_WINDOWS = {1, 6, 12, 24, 48, 168, 720}
MAX_TIMELINE_EVENTS = 400
DETAIL_FIELDS = (
    "status",
    "outcome",
    "decision",
    "revision",
    "accepted",
    "reason_code",
    "canonical_branch",
    "branch",
    "actual_path_count",
    "source_owner",
    "source_run_id",
    "target_owner",
    "target_run_id",
    "message_id",
    "interaction_kind",
    "work_state_id",
    "direct_commit_id",
)

PROJECT_RELATION_SAMPLE_LIMIT = 4


def _cross_project_envelope(record: dict[str, object]) -> dict[str, object] | None:
    value = record.get("cross_project")
    if not isinstance(value, dict):
        return None
    source = value.get("source")
    target = value.get("target")
    if (
        value.get("protocol") != EXTENSION_PROTOCOL
        or value.get("protocol_version") != EXTENSION_VERSION
        or not isinstance(value.get("collaboration_id"), str)
        or value.get("phase") not in {"opened", "bound", "closed"}
        or value.get("kind") not in COLLABORATION_KINDS
        or value.get("actor_role") not in {"source", "target"}
        or not isinstance(source, dict)
        or not isinstance(target, dict)
        or not isinstance(source.get("workspace_id"), str)
        or WORKSPACE_ID.fullmatch(str(source["workspace_id"])) is None
    ):
        return None
    target_workspace_id = target.get("workspace_id")
    if target_workspace_id is not None and (
        not isinstance(target_workspace_id, str)
        or WORKSPACE_ID.fullmatch(target_workspace_id) is None
    ):
        return None
    return value


def _project_collaboration(
    connection: sqlite3.Connection,
    *,
    since: str,
    workspace_names: dict[str, str],
) -> dict[str, object]:
    """Separate explicit cross-task collaboration from same-Run workspace hints."""

    identities: dict[tuple[str, str], dict[str, dict[str, object]]] = defaultdict(dict)
    for row in connection.execute(
        """
        SELECT workspace_id, owner, run_id, COUNT(*) AS event_count, MAX(at) AS latest_at
        FROM events
        WHERE protocol_version = ? AND at >= ?
          AND owner IS NOT NULL AND run_id IS NOT NULL
        GROUP BY workspace_id, owner, run_id
        ORDER BY owner, run_id, workspace_id
        """,
        (PROTOCOL_VERSION, since),
    ):
        identities[(str(row["owner"]), str(row["run_id"]))][str(row["workspace_id"])] = {
            "event_count": int(row["event_count"]),
            "latest_at": str(row["latest_at"]),
        }

    relations: dict[tuple[str, str], dict[str, object]] = {}

    def relation(left: str, right: str) -> dict[str, object]:
        key = tuple(sorted((left, right)))
        return relations.setdefault(
            key,
            {
                "source_workspace_id": key[0],
                "target_workspace_id": key[1],
                "same_run_hint_count": 0,
                "collaboration_count": 0,
                "open_collaboration_count": 0,
                "completed_collaboration_count": 0,
                "latest_at": None,
                "samples": [],
                "directions": Counter(),
            },
        )

    for (owner, run_id), workspaces in identities.items():
        workspace_ids = sorted(workspaces)
        if len(workspace_ids) < 2:
            continue
        for left, right in combinations(workspace_ids, 2):
            edge = relation(left, right)
            edge["same_run_hint_count"] = int(edge["same_run_hint_count"]) + 1
            latest_at = max(
                str(workspaces[left]["latest_at"]),
                str(workspaces[right]["latest_at"]),
            )
            edge["latest_at"] = max(str(edge["latest_at"] or ""), latest_at)
            samples = edge["samples"]
            if isinstance(samples, list) and len(samples) < PROJECT_RELATION_SAMPLE_LIMIT:
                samples.append(
                    {"owner": owner, "run_id": run_id, "evidence": "same-run-hint"}
                )

    cross_project_relations: dict[str, dict[str, object]] = {}
    for row in connection.execute(
        """
        SELECT workspace_id, at, owner, run_id, record_json
        FROM events
        WHERE protocol_version = ? AND at >= ?
          AND event = 'message-sent'
        ORDER BY at, event_id
        """,
        (PROTOCOL_VERSION, since),
    ):
        record = json.loads(row["record_json"])
        cross_project = _cross_project_envelope(record)
        if cross_project is not None:
            collaboration_id = str(cross_project["collaboration_id"])
            source = cross_project["source"]
            target = cross_project["target"]
            assert isinstance(source, dict) and isinstance(target, dict)
            candidate = {
                "source_workspace_id": source.get("workspace_id"),
                "target_workspace_id": target.get("workspace_id"),
                "source_owner": source.get("owner"),
                "source_run_id": source.get("run_id"),
                "target_owner": target.get("owner"),
                "target_run_id": target.get("run_id"),
                "kind": cross_project.get("kind"),
            }
            relation_record = cross_project_relations.setdefault(
                collaboration_id,
                {**candidate, "latest_at": str(row["at"]), "phases": set()},
            )
            for key, value in candidate.items():
                if value is None:
                    continue
                existing = relation_record.get(key)
                if existing is not None and existing != value:
                    relation_record["invalid"] = True
                else:
                    relation_record[key] = value
            relation_record["latest_at"] = max(
                str(relation_record["latest_at"]), str(row["at"])
            )
            phases = relation_record["phases"]
            if isinstance(phases, set):
                phases.add(str(cross_project["phase"]))
            actor_role = str(cross_project["actor_role"])
            actor = source if actor_role == "source" else target
            if (
                actor.get("workspace_id") != str(row["workspace_id"])
                or actor.get("owner") != row["owner"]
                or actor.get("run_id") != row["run_id"]
            ):
                relation_record["invalid"] = True
            if cross_project.get("phase") == "closed":
                relation_record["outcome"] = cross_project.get("outcome")
            continue
    for collaboration_id, collaboration in cross_project_relations.items():
        source_workspace = collaboration.get("source_workspace_id")
        target_workspace = collaboration.get("target_workspace_id")
        phases = collaboration.get("phases")
        participant_confirmed = isinstance(phases, set) and bool(
            phases.intersection({"bound", "closed"})
        )
        if (
            collaboration.get("invalid")
            or not participant_confirmed
            or not isinstance(source_workspace, str)
            or not isinstance(target_workspace, str)
            or source_workspace == target_workspace
            or source_workspace not in workspace_names
            or target_workspace not in workspace_names
        ):
            continue
        edge = relation(source_workspace, target_workspace)
        edge["collaboration_count"] = int(edge["collaboration_count"]) + 1
        closed = isinstance(phases, set) and "closed" in phases
        if closed:
            if collaboration.get("outcome") == "completed":
                edge["completed_collaboration_count"] = int(
                    edge["completed_collaboration_count"]
                ) + 1
        else:
            edge["open_collaboration_count"] = int(edge["open_collaboration_count"]) + 1
        edge["latest_at"] = max(
            str(edge["latest_at"] or ""), str(collaboration["latest_at"])
        )
        directions = edge["directions"]
        if isinstance(directions, Counter):
            directions[(source_workspace, target_workspace)] += 1
        samples = edge["samples"]
        if isinstance(samples, list) and len(samples) < PROJECT_RELATION_SAMPLE_LIMIT:
            samples.append(
                {
                    "owner": collaboration.get("source_owner"),
                    "run_id": collaboration.get("source_run_id"),
                    "collaboration_id": collaboration_id,
                    "kind": collaboration.get("kind"),
                    "evidence": "cross-project-collaboration",
                }
            )

    edges = []
    related_workspace_ids: set[str] = set()
    for edge in relations.values():
        source = str(edge["source_workspace_id"])
        target = str(edge["target_workspace_id"])
        related_workspace_ids.update((source, target))
        directions = edge.pop("directions")
        edge["directions"] = [
            {
                "source_workspace_id": direction[0],
                "target_workspace_id": direction[1],
                "count": count,
            }
            for direction, count in sorted(directions.items())
        ] if isinstance(directions, Counter) else []
        edges.append(edge)
    edges.sort(
        key=lambda edge: (
            str(edge["latest_at"] or ""),
            int(edge["collaboration_count"]),
            int(edge["same_run_hint_count"]),
        ),
        reverse=True,
    )
    nodes = [
        {"workspace_id": identifier, "name": workspace_names.get(identifier, identifier)}
        for identifier in sorted(
            related_workspace_ids,
            key=lambda value: (workspace_names.get(value, value), value),
        )
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "project_count": len(nodes),
        "relation_count": len(edges),
        "collaboration_relation_count": sum(
            int(edge["collaboration_count"]) > 0 for edge in edges
        ),
        "inferred_relation_count": sum(
            int(edge["collaboration_count"]) == 0
            and int(edge["same_run_hint_count"]) > 0
            for edge in edges
        ),
    }


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
    timeline_arguments: list[object] = [PROTOCOL_VERSION, since_text]
    timeline_where = "protocol_version = ? AND at >= ?"
    if workspace is not None:
        timeline_where += " AND workspace_id = ?"
        timeline_arguments.append(workspace)

    event_rows = list(
        connection.execute(
            f"""
            SELECT workspace_id, event_id, at, event, authority_effect, owner, run_id,
                   scope, transaction_id, contention_id, handoff_id, record_json
            FROM events
            WHERE {timeline_where}
            ORDER BY at DESC, event_id DESC
            LIMIT ?
            """,
            (*timeline_arguments, event_limit),
        )
    )
    events = [_project_event(row) for row in reversed(event_rows)]

    event_counts_by_workspace: Counter[str] = Counter()
    event_kinds_by_workspace: dict[str, Counter[str]] = defaultdict(Counter)
    for row in connection.execute(
        f"""
        SELECT workspace_id, event, COUNT(*) AS count
        FROM events
        WHERE protocol_version = ? AND at >= ?
        GROUP BY workspace_id, event
        """,
        (PROTOCOL_VERSION, since_text),
    ):
        identifier = str(row["workspace_id"])
        count = int(row["count"])
        event_counts_by_workspace[identifier] += count
        event_kinds_by_workspace[identifier][str(row["event"])] += count

    active_counts_by_workspace: dict[str, Counter[str]] = defaultdict(Counter)
    active_details: list[dict[str, object]] = []
    displayed_contentions = {
        (str(event["workspace_id"]), str(event["contention_id"]))
        for event in events
        if event.get("contention_id")
    }
    displayed_transactions = {
        (str(event["workspace_id"]), str(event["transaction_id"]))
        for event in events
        if event.get("transaction_id")
    }
    displayed_handoffs = {
        (str(event["workspace_id"]), str(event["handoff_id"]))
        for event in events
        if event.get("handoff_id")
    }
    contention_participants: dict[tuple[str, str], list[dict[str, str]]] = {}
    transaction_details: dict[tuple[str, str], dict[str, object]] = {}
    handoff_participants: dict[tuple[str, str], dict[str, str]] = {}
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
        identifier = str(row["workspace_id"])
        if kind == "contention":
            contention_id = record.get("contention_id")
            key = (identifier, str(contention_id))
            if isinstance(contention_id, str) and key in displayed_contentions:
                contention_participants[key] = [
                    {
                        "owner": str(participant["owner"]),
                        "run_id": str(participant["run_id"]),
                        "scope": str(participant["scope"]),
                    }
                    for participant in record.get("participants", [])
                    if isinstance(participant, dict)
                    and isinstance(participant.get("owner"), str)
                    and isinstance(participant.get("run_id"), str)
                    and isinstance(participant.get("scope"), str)
                ]
        if kind == "transaction":
            transaction_id = record.get("transaction_id")
            key = (identifier, str(transaction_id))
            if isinstance(transaction_id, str) and key in displayed_transactions:
                transaction_details[key] = {
                    field: record[field]
                    for field in ("branch", "canonical_branch", "actual_path_count")
                    if record.get(field) is not None
                }
        if kind == "handoff":
            handoff_id = record.get("handoff_id")
            key = (identifier, str(handoff_id))
            if isinstance(handoff_id, str) and key in displayed_handoffs:
                handoff_participants[key] = {
                    field: str(record[field])
                    for field in (
                        "source_owner",
                        "source_run_id",
                        "target_owner",
                        "target_run_id",
                    )
                    if isinstance(record.get(field), str)
                }
        if not _active_snapshot(record, kind, lifecycle):
            continue
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

    for event in events:
        contention_id = event.get("contention_id")
        if contention_id:
            participants = contention_participants.get(
                (str(event["workspace_id"]), str(contention_id))
            )
            if participants:
                event["details"]["contention_participants"] = participants
        transaction_id = event.get("transaction_id")
        if transaction_id:
            details = transaction_details.get(
                (str(event["workspace_id"]), str(transaction_id)),
                {},
            )
            for field, value in details.items():
                event["details"].setdefault(field, value)
        handoff_id = event.get("handoff_id")
        if handoff_id:
            participants = handoff_participants.get(
                (str(event["workspace_id"]), str(handoff_id)),
                {},
            )
            for field, value in participants.items():
                event["details"].setdefault(field, value)

    # Messages are addressed to an owner, not an arbitrary Run. Once an acknowledgement
    # supplies the exact receiving Run, project that identity onto both ends of the visible
    # exchange so the Console can draw a factual Run-to-Run relationship.
    message_participants: dict[tuple[str, str], dict[str, str]] = {}
    for event in events:
        message_id = event["details"].get("message_id")
        if not isinstance(message_id, str):
            continue
        key = (str(event["workspace_id"]), message_id)
        participants = message_participants.setdefault(key, {})
        if event["event"] == "message-sent":
            if isinstance(event.get("owner"), str) and isinstance(event.get("run_id"), str):
                participants.setdefault("source_owner", str(event["owner"]))
                participants.setdefault("source_run_id", str(event["run_id"]))
            target_owner = event["details"].get("target_owner")
            if isinstance(target_owner, str):
                participants.setdefault("target_owner", target_owner)
        elif event["event"] == "message-acknowledged":
            if isinstance(event.get("owner"), str) and isinstance(event.get("run_id"), str):
                participants.setdefault("target_owner", str(event["owner"]))
                participants.setdefault("target_run_id", str(event["run_id"]))

    for event in events:
        message_id = event["details"].get("message_id")
        if not isinstance(message_id, str):
            continue
        for field, value in message_participants.get(
            (str(event["workspace_id"]), message_id),
            {},
        ).items():
            event["details"].setdefault(field, value)

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
    workspace_names: dict[str, str] = {}
    for row in workspace_rows:
        identifier = str(row["workspace_id"])
        root = str(row["root"])
        workspace_names[identifier] = Path(root).name
        project = {
            "workspace_id": identifier,
            "name": workspace_names[identifier],
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

    project_collaboration = _project_collaboration(
        connection,
        since=since_text,
        workspace_names=workspace_names,
    )

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
        "project_collaboration": project_collaboration,
        "events": events,
        "active_details": active_details,
    }
