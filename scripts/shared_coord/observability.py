"""Structured contention metrics derived from immutable coordination events."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .state import read_json


def _string_values(record: dict[str, object], key: str) -> list[str]:
    value = record.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _resource_metrics() -> dict[str, int]:
    return {
        "contention_events": 0,
        "queue_requests": 0,
        "blocked_transitions": 0,
        "exclusive_requests": 0,
        "activations": 0,
        "refresh_conflicts": 0,
        "attention_events": 0,
        "wait_duration_ms": 0,
    }


def _record_resource_event(
    metrics: dict[str, dict[str, int]],
    resources: list[str],
    event: str,
    mode: object,
    wait_ms: int,
) -> None:
    for resource in resources:
        entry = metrics.setdefault(resource, _resource_metrics())
        entry["contention_events"] += 1
        if event == "queue-requested":
            entry["queue_requests"] += 1
            if mode == "exclusive":
                entry["exclusive_requests"] += 1
        elif event == "queue-blocked":
            entry["blocked_transitions"] += 1
        elif event == "queue-activated":
            entry["activations"] += 1
            entry["wait_duration_ms"] += wait_ms
        elif event == "refresh-conflicted":
            entry["refresh_conflicts"] += 1
        elif event in {
            "cleanup-needs-attention",
            "group-needs-attention",
            "queue-needs-attention",
        }:
            entry["attention_events"] += 1


def _ranked_metrics(metrics: dict[str, dict[str, int]]) -> list[dict[str, object]]:
    ranked: list[dict[str, object]] = []
    for resource, values in metrics.items():
        score = (
            values["queue_requests"] * 2
            + values["blocked_transitions"]
            + values["exclusive_requests"] * 3
            + values["refresh_conflicts"] * 4
            + values["attention_events"] * 3
        )
        recommendation = None
        if (
            values["queue_requests"] >= 3
            or values["exclusive_requests"] >= 2
            or values["refresh_conflicts"] >= 2
            or values["attention_events"] >= 2
        ):
            recommendation = "review semantic ownership and task decomposition"
        ranked.append(
            {
                "resource": resource,
                **values,
                "score": score,
                "recommended_review": recommendation,
            }
        )
    return sorted(
        ranked,
        key=lambda item: (-int(item["score"]), str(item["resource"])),
    )


def hotspot_report(location: Path) -> dict[str, object]:
    event_counts: Counter[str] = Counter()
    transaction_counts: Counter[str] = Counter()
    request_counts: Counter[str] = Counter()
    path_metrics: dict[str, dict[str, int]] = {}
    semantic_metrics: dict[str, dict[str, int]] = {}
    total_wait_ms = 0
    activated_requests = 0

    for path in sorted((location / "events").glob("*.json")):
        record = read_json(path)
        event = str(record.get("event", "unknown"))
        event_counts[event] += 1
        transaction_id = record.get("transaction_id")
        if isinstance(transaction_id, str):
            transaction_counts[transaction_id] += 1
        request_id = record.get("request_id")
        if isinstance(request_id, str):
            request_counts[request_id] += 1
        wait_value = record.get("wait_duration_ms", 0)
        wait_ms = wait_value if isinstance(wait_value, int) and wait_value >= 0 else 0
        if event == "queue-activated":
            total_wait_ms += wait_ms
            activated_requests += 1
        paths = _string_values(record, "paths")
        if event == "refresh-conflicted":
            paths.extend(_string_values(record, "conflicts"))
        _record_resource_event(path_metrics, sorted(set(paths)), event, record.get("mode"), wait_ms)
        _record_resource_event(
            semantic_metrics,
            sorted(set(_string_values(record, "semantic_resources"))),
            event,
            record.get("mode"),
            wait_ms,
        )

    return {
        "events": dict(event_counts.most_common()),
        "transactions_by_activity": dict(transaction_counts.most_common()),
        "requests_by_activity": dict(request_counts.most_common()),
        "queue": {
            "requested": event_counts["queue-requested"],
            "blocked_transitions": event_counts["queue-blocked"],
            "activated": activated_requests,
            "cancelled": event_counts["queue-cancelled"],
            "average_wait_duration_ms": (
                total_wait_ms // activated_requests if activated_requests else 0
            ),
        },
        "path_hotspots": _ranked_metrics(path_metrics),
        "semantic_hotspots": _ranked_metrics(semantic_metrics),
    }
