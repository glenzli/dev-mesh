"""Query immutable coordination events and derive workflow-level diagnostics."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .contention import contention_status
from .contention_store import active_contentions
from .state import initialize, read_json


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _matches(record: dict[str, object], key: str, expected: str | None) -> bool:
    if expected is None:
        return True
    if record.get(key) == expected:
        return True
    plural = {
        "contention_id": "contention_ids",
        "owner": "owners",
        "request_id": "request_ids",
        "scope": "scopes",
        "transaction_id": "transaction_ids",
    }.get(key)
    value = record.get(plural, []) if plural else []
    return isinstance(value, list) and expected in value


def event_log(
    location: Path,
    *,
    contention_id: str | None = None,
    request_id: str | None = None,
    transaction_id: str | None = None,
    scope: str | None = None,
    owner: str | None = None,
    event: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    if limit < 1 or limit > 5000:
        raise ValueError("log limit must be between 1 and 5000")
    matched: list[dict[str, object]] = []
    for path in sorted((location / "events").glob("*.json")):
        record = read_json(path)
        if not all(
            (
                _matches(record, "contention_id", contention_id),
                _matches(record, "request_id", request_id),
                _matches(record, "transaction_id", transaction_id),
                _matches(record, "scope", scope),
                _matches(record, "owner", owner)
                or record.get("coordinator") == owner
                or record.get("prior_coordinator") == owner,
                event is None or record.get("event") == event,
            )
        ):
            continue
        item = dict(record)
        item["event_file"] = path.name
        matched.append(item)
    total = len(matched)
    if total > limit:
        matched = matched[-limit:]
    return {
        "filters": {
            key: value
            for key, value in {
                "contention_id": contention_id,
                "request_id": request_id,
                "transaction_id": transaction_id,
                "scope": scope,
                "owner": owner,
                "event": event,
            }.items()
            if value is not None
        },
        "total_matched": total,
        "returned": len(matched),
        "truncated": total > len(matched),
        "events": matched,
    }


def _record_locations(location: Path) -> dict[str, tuple[str, dict[str, object]]]:
    records: dict[str, tuple[str, dict[str, object]]] = {}
    for _, record in active_contentions(location):
        records[str(record["contention_id"])] = ("active", record)
    for path in sorted((location / "contentions" / "archive").glob("*.json")):
        record = read_json(path)
        records[str(record["contention_id"])] = ("archive", record)
    return records


def workflow_report(location: Path) -> dict[str, object]:
    event_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    contention_events: dict[str, list[dict[str, object]]] = defaultdict(list)
    for path in sorted((location / "events").glob("*.json")):
        record = read_json(path)
        event = str(record.get("event", "unknown"))
        event_counts[event] += 1
        contention_id = record.get("contention_id")
        if isinstance(contention_id, str):
            contention_events[contention_id].append(record)
        if event == "contention-decision-proposed" and isinstance(
            record.get("mode"), str
        ):
            decision_counts[str(record["mode"])] += 1

    active_status = {
        str(record["contention_id"]): record for record in contention_status(location)
    }
    workflows: list[dict[str, object]] = []
    stalled: list[dict[str, object]] = []
    awaiting_responses = 0
    for contention_id, (location_kind, record) in sorted(_record_locations(location).items()):
        events = contention_events.get(contention_id, [])
        opened = next(
            (item for item in events if item.get("event") == "contention-opened"),
            None,
        )
        proposed = [
            item
            for item in events
            if item.get("event") == "contention-decision-proposed"
        ]
        enacted = next(
            (item for item in events if item.get("event") == "contention-enacted"),
            None,
        )
        completed = next(
            (item for item in events if item.get("event") == "contention-completed"),
            None,
        )
        opened_at = _parse_time(opened.get("at") if opened else record.get("created_at"))
        first_decision_at = _parse_time(proposed[0].get("at")) if proposed else None
        enacted_at = _parse_time(enacted.get("at")) if enacted else None
        completed_at = _parse_time(completed.get("at")) if completed else None

        def elapsed(end: datetime | None) -> int | None:
            if opened_at is None or end is None:
                return None
            return max(0, int((end - opened_at).total_seconds() * 1000))

        decision = record.get("decision", {})
        workflow = {
            "contention_id": contention_id,
            "record_location": location_kind,
            "status": record.get("status"),
            "scopes": record.get("scopes", []),
            "owners": sorted(
                {
                    str(item.get("owner"))
                    for item in record.get("participants", [])
                    if isinstance(item, dict) and isinstance(item.get("owner"), str)
                }
            ),
            "mode": decision.get("mode") if isinstance(decision, dict) else None,
            "decision_revisions": len(proposed),
            "rejections": sum(
                1
                for item in events
                if item.get("event") == "contention-decision-rejected"
            ),
            "coordinator_changes": sum(
                1
                for item in events
                if item.get("event")
                in {
                    "contention-coordinator-acquired",
                    "contention-coordinator-handed-off",
                }
            ),
            "time_to_first_decision_ms": elapsed(first_decision_at),
            "time_to_enact_ms": elapsed(enacted_at),
            "total_coordination_ms": (
                record.get("coordination_duration_ms")
                if isinstance(record.get("coordination_duration_ms"), int)
                else elapsed(completed_at)
            ),
            "event_count": len(events),
            "request_id": record.get("request_id"),
        }
        workflows.append(workflow)
        active = active_status.get(contention_id)
        if active is not None and active.get("missing_responses"):
            awaiting_responses += 1
        if active is not None and (
            active.get("coordinator_lease_expired") is True
            or active.get("status") == "needs-decision"
            or (
                active.get("status") == "scheduled"
                and active.get("request_status") == "needs-attention"
            )
        ):
            stalled.append(
                {
                    "contention_id": contention_id,
                    "status": active.get("status"),
                    "lease_expired": active.get("coordinator_lease_expired"),
                    "missing_responses": active.get("missing_responses", []),
                    "request_id": active.get("request_id"),
                    "request_status": active.get("request_status"),
                }
            )

    completed_workflows = [
        workflow
        for workflow in workflows
        if isinstance(workflow.get("total_coordination_ms"), int)
    ]
    total_duration = sum(
        int(workflow["total_coordination_ms"]) for workflow in completed_workflows
    )
    return {
        "summary": {
            "contentions": len(workflows),
            "active": sum(1 for item in workflows if item["record_location"] == "active"),
            "completed": sum(
                1 for item in workflows if item.get("status") == "completed"
            ),
            "stalled": len(stalled),
            "awaiting_responses": awaiting_responses,
            "decision_rejections": event_counts["contention-decision-rejected"],
            "coordinator_takeovers": event_counts["contention-coordinator-acquired"],
            "coordinator_handoffs": event_counts[
                "contention-coordinator-handed-off"
            ],
            "average_coordination_duration_ms": (
                total_duration // len(completed_workflows)
                if completed_workflows
                else 0
            ),
        },
        "decisions": dict(decision_counts.most_common()),
        "events": dict(event_counts.most_common()),
        "stalled_contentions": stalled,
        "workflows": workflows,
    }


def command_log(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    report = event_log(
        location,
        contention_id=arguments.contention,
        request_id=arguments.request,
        transaction_id=arguments.transaction,
        scope=arguments.scope,
        owner=arguments.owner,
        event=arguments.event,
        limit=arguments.limit,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def command_workflow_report(arguments) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    print(json.dumps(workflow_report(location), indent=2, ensure_ascii=False))
    return 0
