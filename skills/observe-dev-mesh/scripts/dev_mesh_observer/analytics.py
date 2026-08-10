"""Derived collaboration diagnostics for the cross-workspace Observer."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Iterable, Mapping


CONFLICT_KINDS = {
    "contention-opened": ("contentions", 2),
    "queue-blocked": ("queue_blocked", 2),
    "refresh-conflicted": ("refresh_conflicts", 5),
    "contention-decision-rejected": ("decision_rejections", 3),
    "cleanup-needs-attention": ("attention", 4),
    "group-needs-attention": ("attention", 4),
    "queue-needs-attention": ("attention", 4),
}

TRANSACTION_PREFIXES = (
    "cleanup-",
    "materialize-",
    "publish-",
    "refresh-",
    "transaction-",
)
TRANSACTION_EVENTS = {"group-member-terminal"}
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
ATTENTION_EVENTS = {
    "cleanup-needs-attention",
    "group-needs-attention",
    "queue-needs-attention",
}
COLLABORATION_EVENTS = {
    "handoff-accepted",
    "handoff-offered",
    "message-acknowledged",
    "message-sent",
}
PROTOCOL_PREFIXES = (
    "claim-",
    "cleanup-",
    "contention-",
    "group-",
    "handoff-",
    "materialize-",
    "message-",
    "publish-",
    "queue-",
    "refresh-",
    "transaction-",
)


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


def _payload(row: Mapping[str, object]) -> dict[str, object]:
    try:
        value = json.loads(str(row.get("payload_json", "{}")))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _normalize(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for sequence, row in enumerate(rows):
        payload = _payload(row)
        event = str(row.get("event_type") or "unknown")
        events.append(
            {
                "sequence": sequence,
                "workspace_id": str(row.get("workspace_id") or ""),
                "event": event,
                "at": _parse_time(row.get("event_at")),
                "at_text": row.get("event_at"),
                "run_id": row.get("run_id"),
                "handoff_id": row.get("handoff_id"),
                "transaction_id": row.get("transaction_id"),
                "owner": row.get("owner"),
                "payload": payload,
            }
        )
    return events


def _in_window(event: Mapping[str, object], since: datetime) -> bool:
    at = event.get("at")
    return isinstance(at, datetime) and at >= since


def _conflict_analysis(
    events: list[dict[str, object]],
    *,
    since: datetime,
    limit: int,
) -> dict[str, object]:
    summary = {
        "signals": 0,
        "contentions": 0,
        "queue_blocked": 0,
        "refresh_conflicts": 0,
        "decision_rejections": 0,
        "attention": 0,
    }
    resources: dict[str, dict[str, int | str]] = {}
    for item in events:
        if not _in_window(item, since):
            continue
        event = str(item["event"])
        classification = CONFLICT_KINDS.get(event)
        if classification is None:
            continue
        kind, weight = classification
        summary["signals"] += 1
        summary[kind] += 1
        payload = item["payload"]
        assert isinstance(payload, dict)
        paths = _strings(payload.get("paths"))
        if event == "refresh-conflicted":
            paths.extend(_strings(payload.get("conflicts")))
        for path in sorted(set(paths)):
            metrics = resources.setdefault(
                path,
                {
                    "resource": path,
                    "signals": 0,
                    "contentions": 0,
                    "queue_blocked": 0,
                    "refresh_conflicts": 0,
                    "decision_rejections": 0,
                    "attention": 0,
                    "score": 0,
                },
            )
            metrics["signals"] = int(metrics["signals"]) + 1
            metrics[kind] = int(metrics[kind]) + 1
            metrics["score"] = int(metrics["score"]) + weight
    ranked = sorted(
        resources.values(),
        key=lambda value: (
            -int(value["score"]),
            -int(value["signals"]),
            str(value["resource"]),
        ),
    )
    return {"summary": summary, "resource_hotspots": ranked[:limit]}


def _is_transaction_event(event: str, transaction_id: object) -> bool:
    return isinstance(transaction_id, str) and bool(transaction_id) and (
        event in TRANSACTION_EVENTS or event.startswith(TRANSACTION_PREFIXES)
    )


def _transaction_analysis(
    events: list[dict[str, object]],
    *,
    since: datetime,
    workspace_names: Mapping[str, str],
    limit: int,
) -> dict[str, object]:
    window_ids = {
        str(item["transaction_id"])
        for item in events
        if _in_window(item, since)
        and _is_transaction_event(str(item["event"]), item["transaction_id"])
    }
    transactions: dict[str, dict[str, object]] = {}
    for item in events:
        transaction_id = item["transaction_id"]
        event = str(item["event"])
        if str(transaction_id) not in window_ids or not _is_transaction_event(
            event, transaction_id
        ):
            continue
        identifier = str(transaction_id)
        record = transactions.setdefault(
            identifier,
            {
                "transaction_id": identifier,
                "workspace_id": item["workspace_id"],
                "workspace_root": workspace_names.get(str(item["workspace_id"])),
                "events": [],
                "paths": set(),
                "first_at": None,
                "last_at": None,
                "status": "observed",
                "activated": False,
                "prepared": False,
                "validated": False,
                "published": False,
                "aborted": False,
                "conflicted": False,
                "attention": False,
                "handed_off": False,
            },
        )
        cast_events = record["events"]
        assert isinstance(cast_events, list)
        cast_events.append(event)
        payload = item["payload"]
        assert isinstance(payload, dict)
        cast_paths = record["paths"]
        assert isinstance(cast_paths, set)
        for field in ("actual_paths", "conflicts", "paths"):
            cast_paths.update(_strings(payload.get(field)))
        at = item["at"]
        if isinstance(at, datetime):
            if record["first_at"] is None or at < record["first_at"]:
                record["first_at"] = at
            if record["last_at"] is None or at >= record["last_at"]:
                record["last_at"] = at
                record["last_event"] = event
                if event in TRANSACTION_STATUS:
                    record["status"] = TRANSACTION_STATUS[event]
        if event == "transaction-activated":
            record["activated"] = True
        elif event == "transaction-prepared":
            record["prepared"] = True
        elif event == "transaction-validated":
            record["validated"] = True
        elif event == "publish-completed":
            record["published"] = True
        elif event == "transaction-aborted":
            record["aborted"] = True
        elif event == "refresh-conflicted":
            record["conflicted"] = True
        elif event in ATTENTION_EVENTS:
            record["attention"] = True
        elif event == "transaction-handed-off":
            record["handed_off"] = True

    rows: list[dict[str, object]] = []
    for record in transactions.values():
        first = record.pop("first_at")
        last = record.pop("last_at")
        paths = record.pop("paths")
        if record["aborted"]:
            record["status"] = "aborted"
        elif record["published"]:
            record["status"] = "published"
        elif record["attention"] and record["status"] not in {"conflicted"}:
            record["status"] = "needs-attention"
        record["first_at"] = first.isoformat() if isinstance(first, datetime) else None
        record["last_at"] = last.isoformat() if isinstance(last, datetime) else None
        record["duration_ms"] = (
            max(0, int((last - first).total_seconds() * 1000))
            if isinstance(first, datetime) and isinstance(last, datetime)
            else None
        )
        record["paths"] = sorted(paths) if isinstance(paths, set) else []
        rows.append(record)
    rows.sort(key=lambda value: str(value.get("last_at") or ""), reverse=True)
    return {
        "summary": {
            "observed": len(rows),
            "activated": sum(bool(item["activated"]) for item in rows),
            "prepared": sum(bool(item["prepared"]) for item in rows),
            "validated": sum(bool(item["validated"]) for item in rows),
            "published": sum(bool(item["published"]) for item in rows),
            "aborted": sum(bool(item["aborted"]) for item in rows),
            "conflicted": sum(bool(item["conflicted"]) for item in rows),
            "attention": sum(bool(item["attention"]) for item in rows),
            "handed_off": sum(bool(item["handed_off"]) for item in rows),
        },
        "recent": rows[:limit],
    }


def _event_references_run(item: Mapping[str, object], run_id: str) -> bool:
    if item.get("run_id") == run_id:
        return True
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return False
    return run_id in {
        payload.get("run_id"),
        payload.get("source_run_id"),
        payload.get("target_run_id"),
    }


def _within_run(item: Mapping[str, object], run: Mapping[str, object]) -> bool:
    at = item.get("at")
    joined = run.get("joined_at")
    ended = run.get("ended_at")
    return (
        isinstance(at, datetime)
        and isinstance(joined, datetime)
        and isinstance(ended, datetime)
        and joined <= at <= ended
    )


def _protocol_use_analysis(
    events: list[dict[str, object]],
    *,
    since: datetime,
    current: datetime,
    workspace_names: Mapping[str, str],
    limit: int,
) -> dict[str, object]:
    runs: dict[tuple[str, str], dict[str, object]] = {}
    for item in events:
        run_id = item["run_id"]
        if not isinstance(run_id, str) or not run_id:
            continue
        key = (str(item["workspace_id"]), run_id)
        if item["event"] == "agent-joined":
            payload = item["payload"]
            assert isinstance(payload, dict)
            run = runs.setdefault(
                key,
                {
                    "workspace_id": key[0],
                    "workspace_root": workspace_names.get(key[0]),
                    "run_id": run_id,
                    "owner": item["owner"],
                    "joined_at": item["at"],
                    "left_at": None,
                    "ended_at": current,
                    "open": True,
                    "parent_agent_id": payload.get("parent_agent_id"),
                    "task_summary": payload.get("task_summary"),
                    "signals": set(),
                    "protocol_events": 0,
                },
            )
            if run["joined_at"] is None:
                run["joined_at"] = item["at"]
        elif item["event"] == "agent-left" and key in runs:
            runs[key]["left_at"] = item["at"]
            runs[key]["ended_at"] = item["at"] or current
            runs[key]["open"] = False

    selected = [
        run
        for run in runs.values()
        if isinstance(run.get("joined_at"), datetime) and run["joined_at"] >= since
    ]
    for run in selected:
        signals = run["signals"]
        assert isinstance(signals, set)
        if isinstance(run.get("parent_agent_id"), str):
            signals.add("parent-child")
        owner = run.get("owner")
        for item in events:
            if item["workspace_id"] != run["workspace_id"] or not _within_run(item, run):
                continue
            event = str(item["event"])
            if _event_references_run(item, str(run["run_id"])) and event in COLLABORATION_EVENTS:
                signals.add(event)
            if item.get("owner") == owner and event.startswith(PROTOCOL_PREFIXES):
                run["protocol_events"] = int(run["protocol_events"]) + 1
                if event in COLLABORATION_EVENTS or event.startswith("contention-"):
                    signals.add(event)
                if _is_transaction_event(event, item.get("transaction_id")):
                    signals.add("transaction")
            payload = item.get("payload")
            if event.startswith("contention-") and isinstance(payload, dict):
                participants = set(_strings(payload.get("owners")))
                participants.update(_strings(payload.get("participants")))
                if isinstance(owner, str) and owner in participants:
                    signals.add("contention")

    for index, left in enumerate(selected):
        left_joined = left.get("joined_at")
        left_ended = left.get("ended_at")
        if not isinstance(left_joined, datetime) or not isinstance(left_ended, datetime):
            continue
        for right in selected[index + 1 :]:
            if left["workspace_id"] != right["workspace_id"] or left.get("owner") == right.get("owner"):
                continue
            right_joined = right.get("joined_at")
            right_ended = right.get("ended_at")
            if not isinstance(right_joined, datetime) or not isinstance(right_ended, datetime):
                continue
            if left_joined <= right_ended and right_joined <= left_ended:
                assert isinstance(left["signals"], set)
                assert isinstance(right["signals"], set)
                left["signals"].add("concurrent-run")
                right["signals"].add("concurrent-run")

    summarized: list[dict[str, object]] = []
    for run in selected:
        signals = run["signals"]
        assert isinstance(signals, set)
        if signals:
            classification = "collaborative"
        elif run["open"]:
            classification = "open-unclassified"
        elif int(run["protocol_events"]) > 0:
            classification = "solo-protocol"
        else:
            classification = "lifecycle-only"
        joined = run["joined_at"]
        ended = run["ended_at"]
        summarized.append(
            {
                "workspace_id": run["workspace_id"],
                "workspace_root": run["workspace_root"],
                "run_id": run["run_id"],
                "owner": run["owner"],
                "open": run["open"],
                "classification": classification,
                "signals": sorted(signals),
                "protocol_events": run["protocol_events"],
                "duration_ms": (
                    max(0, int((ended - joined).total_seconds() * 1000))
                    if isinstance(joined, datetime) and isinstance(ended, datetime)
                    else None
                ),
            }
        )
    solo = sorted(
        (item for item in summarized if item["classification"] == "solo-protocol"),
        key=lambda item: int(item.get("duration_ms") or 0),
        reverse=True,
    )
    return {
        "heuristic": "closed run with protocol events but no observed collaboration signal",
        "summary": {
            "runs_analyzed": len(summarized),
            "collaborative": sum(item["classification"] == "collaborative" for item in summarized),
            "solo_protocol": len(solo),
            "lifecycle_only": sum(item["classification"] == "lifecycle-only" for item in summarized),
            "open_unclassified": sum(item["classification"] == "open-unclassified" for item in summarized),
        },
        "solo_runs": solo[:limit],
    }


def build_coordination_analytics(
    rows: Iterable[Mapping[str, object]],
    *,
    since: datetime,
    workspace_names: Mapping[str, str],
    limit: int,
    current: datetime | None = None,
) -> dict[str, object]:
    """Project immutable events into bounded diagnostic views without granting authority."""

    events = _normalize(rows)
    now = (current or datetime.now(UTC)).astimezone(UTC)
    return {
        "conflicts": _conflict_analysis(events, since=since, limit=limit),
        "transactions": _transaction_analysis(
            events,
            since=since,
            workspace_names=workspace_names,
            limit=limit,
        ),
        "protocol_use": _protocol_use_analysis(
            events,
            since=since,
            current=now,
            workspace_names=workspace_names,
            limit=limit,
        ),
    }
