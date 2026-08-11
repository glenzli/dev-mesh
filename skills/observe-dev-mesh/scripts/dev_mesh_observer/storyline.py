"""Human-scale collaboration storyline derived from explicit coordination facts."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from .state_projection import project_active_contentions


MAX_STORY_EVENTS = 10_000
CLAIM_EVENTS = {
    "claim-created",
    "claim-updated",
    "claim-paused",
    "claim-resumed",
    "claim-released",
}
TRANSACTION_STATUS = {
    "transaction-recorded": "observed",
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


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _timestamp(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def build_collaboration_storyline(
    connection: sqlite3.Connection,
    *,
    since: datetime,
    workspace_id: str,
    limit: int = 28,
) -> dict[str, object]:
    """Collapse event detail into chronological work slices and intersections.

    Cross-slice links are created only from stable protocol correlations. The
    same-lane sequence relation is presentation order, not a causal claim.
    """

    if not workspace_id:
        raise ValueError("storyline workspace_id is required")
    if limit < 8 or limit > 60:
        raise ValueError("storyline limit must be between 8 and 60")

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

    nodes: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str], dict[str, Any]] = {}
    consumed_events: set[str] = set()
    active_claims: dict[tuple[str, str], str] = {}
    claim_episodes: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    run_nodes: dict[str, str] = {}
    handoff_nodes: dict[str, str] = {}
    contention_nodes: dict[str, str] = {}
    decision_nodes: dict[tuple[str, int], str] = {}
    transaction_nodes: dict[str, str] = {}

    def add_node(
        identifier: str,
        kind: str,
        label: str,
        owner: str,
        at: object,
        *,
        status: str = "observed",
    ) -> dict[str, Any]:
        normalized_at = _timestamp(at)
        current = nodes.setdefault(
            identifier,
            {
                "id": identifier,
                "type": kind,
                "owner": owner,
                "label": label,
                "status": status,
                "started_at": normalized_at,
                "last_at": normalized_at,
                "event_count": 0,
                "details": {
                    "event_types": set(),
                    "source_events": set(),
                    "paths": set(),
                    "semantic_resources": set(),
                    "scopes": set(),
                    "runs": set(),
                },
            },
        )
        if normalized_at:
            if current["started_at"] is None or normalized_at < current["started_at"]:
                current["started_at"] = normalized_at
            if current["last_at"] is None or normalized_at > current["last_at"]:
                current["last_at"] = normalized_at
        return current

    def touch(
        item: dict[str, Any],
        *,
        event: str,
        source_name: str,
        at: object,
        payload: dict[str, object],
    ) -> None:
        normalized_at = _timestamp(at)
        if normalized_at:
            if item["started_at"] is None or normalized_at < item["started_at"]:
                item["started_at"] = normalized_at
            if item["last_at"] is None or normalized_at > item["last_at"]:
                item["last_at"] = normalized_at
        item["event_count"] += 1
        details = item["details"]
        details["event_types"].add(event)
        details["source_events"].add(source_name)
        details["paths"].update(_strings(payload.get("paths")))
        details["paths"].update(_strings(payload.get("actual_paths")))
        details["paths"].update(_strings(payload.get("conflicts")))
        details["semantic_resources"].update(
            _strings(payload.get("semantic_resources"))
        )
        details["scopes"].update(_strings(payload.get("scopes")))
        consumed_events.add(source_name)

    def add_link(
        source: str,
        target: str,
        kind: str,
        *,
        evidence: str,
    ) -> None:
        if source == target:
            return
        key = (source, target, kind)
        links.setdefault(
            key,
            {
                "id": f"{kind}:{source}:{target}",
                "source": source,
                "target": target,
                "type": kind,
                "evidence": evidence,
            },
        )

    for row in rows:
        event = str(row["event_type"] or "unknown")
        source_name = str(row["source_name"])
        payload = _payload(row["payload_json"])
        at = row["event_at"] or row["ingested_at"]
        owner = _text(row["owner"]) or _text(payload.get("owner"))
        run_id = _text(row["run_id"]) or _text(payload.get("run_id"))

        if event in {"agent-joined", "agent-left"} and run_id and owner:
            identifier = run_nodes.setdefault(
                run_id, f"run:{workspace_id}:{run_id}"
            )
            task = _text(payload.get("task_summary")) or run_id
            item = add_node(
                identifier,
                "run",
                task,
                owner,
                at,
                status="active" if event == "agent-joined" else "completed",
            )
            touch(
                item,
                event=event,
                source_name=source_name,
                at=at,
                payload=payload,
            )
            item["details"]["runs"].add(run_id)
            if parent := _text(payload.get("parent_agent_id")):
                item["details"]["parent_owner"] = parent
            if event == "agent-left":
                item["status"] = "completed"
                for field in ("outcome", "summary"):
                    if value := _text(payload.get(field)):
                        item["details"][field] = value

        if event in CLAIM_EVENTS and owner:
            scope = _text(row["scope"]) or _text(payload.get("scope"))
            if scope:
                key = (owner, scope)
                identifier = active_claims.get(key)
                if identifier is None:
                    episode = len(claim_episodes[key]) + 1
                    identifier = f"claim:{workspace_id}:{owner}:{scope}:{episode}"
                    active_claims[key] = identifier
                    claim_episodes[key].append(identifier)
                item = add_node(
                    identifier,
                    "claim",
                    scope,
                    owner,
                    at,
                    status="active",
                )
                touch(
                    item,
                    event=event,
                    source_name=source_name,
                    at=at,
                    payload=payload,
                )
                item["details"]["scopes"].add(scope)
                if intent := _text(payload.get("intent")):
                    item["details"]["intent"] = intent
                if event == "claim-paused":
                    item["status"] = "paused"
                elif event == "claim-resumed":
                    item["status"] = "active"
                elif event == "claim-released":
                    item["status"] = "completed"
                    if summary := _text(payload.get("summary")):
                        item["details"]["summary"] = summary
                    active_claims.pop(key, None)

        handoff_id = _text(row["handoff_id"]) or _text(payload.get("handoff_id"))
        if event in {"handoff-offered", "handoff-accepted"} and handoff_id:
            identifier = handoff_nodes.setdefault(
                handoff_id, f"handoff:{workspace_id}:{handoff_id}"
            )
            source_owner = _text(payload.get("source_owner"))
            target_owner = _text(payload.get("target_owner"))
            item = add_node(
                identifier,
                "handoff",
                handoff_id,
                "__coordination__",
                at,
                status="accepted" if event == "handoff-accepted" else "offered",
            )
            touch(
                item,
                event=event,
                source_name=source_name,
                at=at,
                payload=payload,
            )
            item["details"]["handoff_id"] = handoff_id
            item["details"]["source_owner"] = source_owner
            item["details"]["target_owner"] = target_owner
            source_run = _text(payload.get("source_run_id"))
            target_run = _text(payload.get("target_run_id"))
            if source_run:
                item["details"]["source_run_id"] = source_run
            if target_run:
                item["details"]["target_run_id"] = target_run

        contention_id = _text(row["contention_id"]) or _text(
            payload.get("contention_id")
        )
        if contention_id and event.startswith("contention-"):
            identifier = contention_nodes.setdefault(
                contention_id,
                f"contention:{workspace_id}:{contention_id}",
            )
            item = add_node(
                identifier,
                "contention",
                contention_id,
                "__coordination__",
                at,
                status="active",
            )
            if event == "contention-opened" or event == "contention-participant-joined":
                touch(
                    item,
                    event=event,
                    source_name=source_name,
                    at=at,
                    payload=payload,
                )
            item["details"]["contention_id"] = contention_id
            item["details"].setdefault("owners", set()).update(
                _strings(payload.get("owners"))
            )
            item["details"]["scopes"].update(_strings(payload.get("scopes")))
            item["details"]["paths"].update(_strings(payload.get("paths")))
            item["details"]["semantic_resources"].update(
                _strings(payload.get("semantic_resources"))
            )
            for field in ("recommendation", "reason", "coordinator", "request_id"):
                if value := _text(payload.get(field)):
                    item["details"][field] = value
            if event in {
                "contention-decision-proposed",
                "contention-decision-accepted",
                "contention-decision-rejected",
                "contention-decision-invalidated",
                "contention-enacted",
                "contention-completed",
            }:
                revision_value = payload.get("decision_revision", 0)
                revision = revision_value if isinstance(revision_value, int) else 0
                decision_id = decision_nodes.setdefault(
                    (contention_id, revision),
                    f"decision:{workspace_id}:{contention_id}:{revision}",
                )
                mode = _text(payload.get("mode")) or _text(
                    payload.get("recommendation")
                ) or "decision"
                decision = add_node(
                    decision_id,
                    "decision",
                    mode,
                    "__coordination__",
                    at,
                    status="proposed",
                )
                touch(
                    decision,
                    event=event,
                    source_name=source_name,
                    at=at,
                    payload=payload,
                )
                decision["details"]["contention_id"] = contention_id
                decision["details"]["decision_revision"] = revision
                for field in ("reason", "coordinator", "request_id"):
                    if value := _text(payload.get(field)):
                        decision["details"][field] = value
                if event.endswith("accepted"):
                    decision["status"] = "accepted"
                elif event.endswith("rejected"):
                    decision["status"] = "rejected"
                elif event.endswith("invalidated"):
                    decision["status"] = "invalidated"
                elif event.endswith("enacted"):
                    decision["status"] = "enacted"
                elif event.endswith("completed"):
                    decision["status"] = "completed"
                    item["status"] = "completed"
                add_link(
                    identifier,
                    decision_id,
                    "decision",
                    evidence="contention_id",
                )

        transaction_id = _text(row["transaction_id"]) or _text(
            payload.get("transaction_id")
        )
        if transaction_id and event in TRANSACTION_STATUS:
            identifier = transaction_nodes.setdefault(
                transaction_id,
                f"transaction:{workspace_id}:{transaction_id}",
            )
            item = add_node(
                identifier,
                "transaction",
                transaction_id,
                owner or "__system__",
                at,
                status=TRANSACTION_STATUS[event],
            )
            touch(
                item,
                event=event,
                source_name=source_name,
                at=at,
                payload=payload,
            )
            item["status"] = TRANSACTION_STATUS[event]
            item["details"]["transaction_id"] = transaction_id
            if candidate := _text(payload.get("candidate")):
                item["details"]["candidate"] = candidate
            if event == "publish-completed":
                candidate = _text(payload.get("candidate"))
                publish_id = f"publish:{workspace_id}:{transaction_id}"
                publish = add_node(
                    publish_id,
                    "publish",
                    candidate[:12] if candidate else transaction_id,
                    "__system__",
                    at,
                    status="published",
                )
                touch(
                    publish,
                    event=event,
                    source_name=source_name,
                    at=at,
                    payload=payload,
                )
                publish["details"]["transaction_id"] = transaction_id
                if candidate:
                    publish["details"]["candidate"] = candidate
                add_link(
                    identifier,
                    publish_id,
                    "publishes",
                    evidence="transaction_id",
                )

    # Handoff transitions are exact because lifecycle events carry run ids.
    for handoff_id, identifier in handoff_nodes.items():
        details = nodes[identifier]["details"]
        source_run = details.get("source_run_id")
        target_run = details.get("target_run_id")
        if isinstance(source_run, str) and source_run in run_nodes:
            add_link(
                run_nodes[source_run],
                identifier,
                "handoff",
                evidence="source_run_id",
            )
        if isinstance(target_run, str) and target_run in run_nodes:
            add_link(
                identifier,
                run_nodes[target_run],
                "handoff",
                evidence="target_run_id",
            )

    # Mutable contention state only annotates an immutable intersection.
    active_projection = project_active_contentions(
        connection, workspace_id=workspace_id
    )
    active_contentions = active_projection["active_contentions"]
    assert isinstance(active_contentions, list)
    for snapshot in active_contentions:
        contention_id = str(snapshot["contention_id"])
        identifier = contention_nodes.get(contention_id)
        if identifier is None:
            identifier = f"contention:{workspace_id}:{contention_id}"
            contention_nodes[contention_id] = identifier
            item = add_node(
                identifier,
                "contention",
                contention_id,
                "__coordination__",
                snapshot.get("created_at"),
                status="stalled" if snapshot["stalled"] else "active",
            )
        else:
            item = nodes[identifier]
            item["status"] = "stalled" if snapshot["stalled"] else "active"
        details = item["details"]
        details["scopes"].update(snapshot.get("scopes", []))
        details["paths"].update(snapshot.get("paths", []))
        details["semantic_resources"].update(
            snapshot.get("semantic_resources", [])
        )
        details.setdefault("owners", set()).update(snapshot.get("owners", []))
        for field in (
            "coordinator",
            "lease_until",
            "recommendation",
            "recommendation_reason",
            "request_id",
            "request_status",
        ):
            if snapshot.get(field) is not None:
                details[field] = snapshot[field]
        details["missing_responses"] = snapshot.get("missing_responses", [])

    # A contention scope can be attached to the matching claim episode without
    # guessing from path similarity or event proximity. Run this after current
    # state is projected so an open contention outside the time window can
    # still connect to claim episodes that are inside it.
    for identifier in contention_nodes.values():
        item = nodes[identifier]
        scopes = item["details"]["scopes"]
        owners = item["details"].get("owners", set())
        if not isinstance(scopes, set) or not isinstance(owners, set):
            continue
        for owner in sorted(owners):
            for scope in sorted(scopes):
                episodes = claim_episodes.get((owner, scope), [])
                if episodes:
                    add_link(
                        episodes[-1],
                        identifier,
                        "intersects",
                        evidence="owner+scope",
                    )

    # Chronological links are deliberately lane-local presentation order.
    by_owner: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in nodes.values():
        by_owner[str(item["owner"])].append(item)
    for owner_nodes in by_owner.values():
        owner_nodes.sort(
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item["id"]),
            )
        )
        for source, target in zip(owner_nodes, owner_nodes[1:]):
            add_link(
                str(source["id"]),
                str(target["id"]),
                "sequence",
                evidence="lane-order",
            )

    ordered = sorted(
        nodes.values(),
        key=lambda item: (
            str(item.get("started_at") or ""),
            str(item["id"]),
        ),
    )
    total_nodes = len(ordered)
    if total_nodes > limit:
        important = [
            item
            for item in ordered
            if item["status"] in {"active", "stalled", "conflicted", "rejected"}
        ]
        selected_ids = {str(item["id"]) for item in important[-limit:]}
        for item in reversed(ordered):
            if len(selected_ids) >= limit:
                break
            selected_ids.add(str(item["id"]))
        visible = [item for item in ordered if str(item["id"]) in selected_ids]
    else:
        visible = ordered
    visible_ids = {str(item["id"]) for item in visible}
    visible_links = [
        item
        for item in links.values()
        if item["source"] in visible_ids and item["target"] in visible_ids
    ]

    def serialise(item: dict[str, Any]) -> dict[str, Any]:
        details = item["details"]
        for key, value in list(details.items()):
            if isinstance(value, set):
                details[key] = sorted(value)[:40]
        return item

    visible = [serialise(item) for item in visible]
    lane_order = {"__coordination__": 0, "__system__": 1}
    lane_first_activity: dict[str, str] = {}
    lanes: list[dict[str, object]] = []
    for owner, owner_nodes in by_owner.items():
        present = [item for item in owner_nodes if str(item["id"]) in visible_ids]
        if not present:
            continue
        visible_starts = [
            str(item["started_at"])
            for item in present
            if item.get("started_at")
        ]
        lane_first_activity[owner] = min(visible_starts, default="\uffff")
        statuses = {str(item["status"]) for item in present}
        lane_status = (
            "stalled"
            if "stalled" in statuses
            else "active"
            if "active" in statuses
            else "observed"
        )
        lanes.append(
            {
                "id": owner,
                "kind": (
                    "coordination"
                    if owner == "__coordination__"
                    else "system"
                    if owner == "__system__"
                    else "agent"
                ),
                "label": owner,
                "status": lane_status,
                "node_count": len(present),
            }
        )
    lanes.sort(
        key=lambda lane: (
            lane_order.get(str(lane["id"]), 2),
            lane_first_activity[str(lane["id"])]
            if str(lane["id"]) not in lane_order
            else "",
            str(lane["label"]),
        )
    )

    return {
        "workspace_id": workspace_id,
        "workspace_root": workspace_root,
        "since": since.isoformat(),
        "summary": {
            "actors": sum(lane["kind"] == "agent" for lane in lanes),
            "total_nodes": total_nodes,
            "visible_nodes": len(visible),
            "visible_links": len(visible_links),
            "intersections": sum(item["type"] == "contention" for item in visible),
            "transactions": sum(item["type"] == "transaction" for item in visible),
            "source_events": len(rows),
            "collapsed_events": max(0, len(rows) - len(consumed_events)),
            "truncated": total_nodes > len(visible),
        },
        "lanes": lanes,
        "nodes": visible,
        "links": sorted(
            visible_links,
            key=lambda item: (
                str(item["type"]),
                str(item["source"]),
                str(item["target"]),
            ),
        ),
    }
