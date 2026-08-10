"""Agent-run and handoff observability derived from immutable coordination events."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .state import (
    coordination_guard,
    emit_event,
    initialize,
    now,
    read_json,
    replace_json,
    require_text,
    validate_slug,
    write_json_exclusive,
)


CORRELATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,159}$")
RUN_OUTCOMES = {"abandoned", "completed", "failed"}


def correlation_id(value: str, label: str) -> str:
    if not CORRELATION_ID.fullmatch(value):
        raise ValueError(
            f"{label} must use lowercase letters, digits, and hyphens"
        )
    return value


def optional_correlation_id(value: str | None, label: str) -> str | None:
    if value is None or not value.strip():
        return None
    return correlation_id(value.strip(), label)


def _bounded_text(value: str, label: str, limit: int) -> str:
    normalized = require_text(value, label)
    if len(normalized) > limit:
        raise ValueError(f"{label} must be at most {limit} characters")
    return normalized


def _event_records(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return [
        (path, read_json(path))
        for path in sorted((location / "events").glob("*.json"))
    ]


def _run_path(location: Path, run_id: str) -> Path:
    return location / "runs" / f"{correlation_id(run_id, 'run id')}.json"


def _handoff_path(location: Path, handoff_id: str) -> Path:
    return location / "handoffs" / f"{correlation_id(handoff_id, 'handoff id')}.json"


def _event_path(location: Path, value: object) -> Path | None:
    if not isinstance(value, str) or Path(value).name != value:
        return None
    path = location / "events" / value
    return path if path.is_file() else None


def require_joined_run(location: Path, run_id: str, owner: str) -> dict[str, object]:
    path = _run_path(location, run_id)
    if not path.is_file():
        raise ValueError(f"agent run is not joined: {run_id}")
    record = read_json(path)
    normalized_owner = validate_slug(owner, "owner")
    if record.get("owner") != normalized_owner:
        raise ValueError(f"agent run {run_id!r} does not belong to {owner!r}")
    if record.get("status") != "active":
        raise ValueError(f"agent run is not active: {run_id}")
    return record


def command_agent_join(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    run_id = correlation_id(arguments.run, "run id")
    owner = validate_slug(arguments.owner, "owner")
    task = _bounded_text(arguments.task, "task summary", 500)
    parent = optional_correlation_id(arguments.parent_owner, "parent agent id")
    details: dict[str, object] = {
        "run_id": run_id,
        "owner": owner,
        "task_summary": task,
    }
    if parent is not None:
        details["parent_agent_id"] = parent

    with coordination_guard(location, "agent-join"):
        run_path = _run_path(location, run_id)
        if run_path.exists():
            existing = read_json(run_path)
            if not all(existing.get(key) == value for key, value in details.items()):
                raise ValueError(
                    f"agent run already exists with different metadata: {run_id}"
                )
            if existing.get("status") != "active":
                raise ValueError(f"closed agent run cannot be joined again: {run_id}")
            prior_event = _event_path(location, existing.get("joined_event"))
            if prior_event is not None:
                print(prior_event)
                return 0
            path = emit_event(location, "agent-joined", None, details)
            existing["joined_event"] = path.name
            replace_json(run_path, existing)
            print(path)
            return 0
        record = {
            "schema": 1,
            **details,
            "status": "active",
            "joined_at": now(),
        }
        write_json_exclusive(run_path, record)
        path = emit_event(location, "agent-joined", None, details)
        record["joined_event"] = path.name
        replace_json(run_path, record)
    print(path)
    return 0


def command_agent_leave(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    run_id = correlation_id(arguments.run, "run id")
    owner = validate_slug(arguments.owner, "owner")
    summary = _bounded_text(arguments.summary, "run summary", 1000)
    outcome = arguments.outcome
    if outcome not in RUN_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(sorted(RUN_OUTCOMES))}")
    details: dict[str, object] = {
        "run_id": run_id,
        "owner": owner,
        "outcome": outcome,
        "summary": summary,
    }

    with coordination_guard(location, "agent-leave"):
        run_path = _run_path(location, run_id)
        if not run_path.is_file():
            raise ValueError(f"agent run is not joined: {run_id}")
        record = read_json(run_path)
        if record.get("owner") != owner:
            raise ValueError(f"agent run {run_id!r} does not belong to {owner!r}")
        if record.get("status") == "closed":
            if not all(record.get(key) == value for key, value in details.items()):
                raise ValueError(
                    f"agent run already closed with different metadata: {run_id}"
                )
            prior_event = _event_path(location, record.get("left_event"))
            if prior_event is not None:
                print(prior_event)
                return 0
        elif record.get("status") != "active":
            raise ValueError(f"agent run has invalid status: {record.get('status')!r}")
        path = emit_event(location, "agent-left", None, details)
        record.update(details)
        record["status"] = "closed"
        record["left_at"] = now()
        record["left_event"] = path.name
        replace_json(run_path, record)
    print(path)
    return 0


def record_handoff_offered(
    location: Path,
    *,
    handoff_id: str,
    source_run_id: str,
    source_owner: str,
    target_owner: str,
    message_id: str,
) -> Path:
    normalized_handoff = correlation_id(handoff_id, "handoff id")
    normalized_run = correlation_id(source_run_id, "source run id")
    normalized_source = validate_slug(source_owner, "source owner")
    normalized_target = validate_slug(target_owner, "target owner")
    require_joined_run(location, normalized_run, normalized_source)
    details: dict[str, object] = {
        "handoff_id": normalized_handoff,
        "run_id": normalized_run,
        "source_run_id": normalized_run,
        "owner": normalized_source,
        "source_owner": normalized_source,
        "target_owner": normalized_target,
        "message_id": message_id,
    }
    handoff_path = _handoff_path(location, normalized_handoff)
    if handoff_path.exists():
        record = read_json(handoff_path)
        if all(record.get(key) == value for key, value in details.items()):
            prior_event = _event_path(location, record.get("offered_event"))
            if prior_event is not None:
                return prior_event
            path = emit_event(location, "handoff-offered", None, details)
            record["offered_event"] = path.name
            replace_json(handoff_path, record)
            return path
        raise ValueError(f"handoff id already exists with different metadata: {handoff_id}")
    record = {
        "schema": 1,
        **details,
        "status": "offered",
        "offered_at": now(),
    }
    write_json_exclusive(handoff_path, record)
    path = emit_event(location, "handoff-offered", None, details)
    record["offered_event"] = path.name
    replace_json(handoff_path, record)
    return path


def record_handoff_accepted(
    location: Path,
    *,
    handoff_id: str,
    target_run_id: str,
    target_owner: str,
    message_id: str,
) -> Path:
    normalized_handoff = correlation_id(handoff_id, "handoff id")
    normalized_run = correlation_id(target_run_id, "target run id")
    normalized_target = validate_slug(target_owner, "target owner")
    require_joined_run(location, normalized_run, normalized_target)
    handoff_path = _handoff_path(location, normalized_handoff)
    if not handoff_path.is_file():
        raise ValueError(f"handoff was not offered: {handoff_id}")
    offer = read_json(handoff_path)
    if offer.get("message_id") != message_id:
        raise ValueError("handoff acknowledgement does not match its source message")
    if offer.get("target_owner") != normalized_target:
        raise ValueError(
            f"handoff targets {offer.get('target_owner')!r}, not {normalized_target!r}"
        )
    details: dict[str, object] = {
        "handoff_id": normalized_handoff,
        "run_id": normalized_run,
        "source_run_id": offer.get("source_run_id"),
        "target_run_id": normalized_run,
        "owner": normalized_target,
        "source_owner": offer.get("source_owner"),
        "target_owner": normalized_target,
        "message_id": message_id,
    }
    if offer.get("status") == "accepted":
        if (
            offer.get("target_run_id") == normalized_run
            and offer.get("target_owner") == normalized_target
            and offer.get("message_id") == message_id
        ):
            prior_event = _event_path(location, offer.get("accepted_event"))
            if prior_event is not None:
                return prior_event
        raise ValueError(f"handoff already accepted by another run: {handoff_id}")
    if offer.get("status") != "offered":
        raise ValueError(f"handoff has invalid status: {offer.get('status')!r}")
    path = emit_event(location, "handoff-accepted", None, details)
    offer["target_run_id"] = normalized_run
    offer["status"] = "accepted"
    offer["accepted_at"] = now()
    offer["accepted_event"] = path.name
    replace_json(handoff_path, offer)
    return path


def coverage_report(location: Path) -> dict[str, object]:
    runs: dict[str, dict[str, list[dict[str, object]]]] = {}
    handoffs: dict[str, dict[str, list[dict[str, object]]]] = {}
    issues: list[dict[str, object]] = []
    for _, record in _event_records(location):
        event = record.get("event")
        run_id = record.get("run_id")
        if event in {"agent-joined", "agent-left"} and isinstance(run_id, str):
            entry = runs.setdefault(run_id, {"joined": [], "left": []})
            entry["joined" if event == "agent-joined" else "left"].append(record)
        handoff_id = record.get("handoff_id")
        if event in {"handoff-offered", "handoff-accepted"} and isinstance(
            handoff_id, str
        ):
            entry = handoffs.setdefault(handoff_id, {"offered": [], "accepted": []})
            entry["offered" if event == "handoff-offered" else "accepted"].append(record)

    for path in sorted((location / "runs").glob("*.json")):
        snapshot = read_json(path)
        run_id = snapshot.get("run_id")
        if not isinstance(run_id, str):
            issues.append({"kind": "run-snapshot-missing-id", "snapshot": path.name})
            continue
        lifecycle = runs.get(run_id, {"joined": [], "left": []})
        if not lifecycle["joined"]:
            issues.append({"kind": "run-snapshot-without-join-event", "run_id": run_id})
        if snapshot.get("status") == "closed" and not lifecycle["left"]:
            issues.append({"kind": "closed-run-without-left-event", "run_id": run_id})

    for path in sorted((location / "handoffs").glob("*.json")):
        snapshot = read_json(path)
        handoff_id = snapshot.get("handoff_id")
        if not isinstance(handoff_id, str):
            issues.append(
                {"kind": "handoff-snapshot-missing-id", "snapshot": path.name}
            )
            continue
        lifecycle = handoffs.get(handoff_id, {"offered": [], "accepted": []})
        if not lifecycle["offered"]:
            issues.append(
                {
                    "kind": "handoff-snapshot-without-offer-event",
                    "handoff_id": handoff_id,
                }
            )
        if snapshot.get("status") == "accepted" and not lifecycle["accepted"]:
            issues.append(
                {
                    "kind": "accepted-handoff-without-accept-event",
                    "handoff_id": handoff_id,
                }
            )

    open_runs: list[dict[str, object]] = []
    for run_id, lifecycle in sorted(runs.items()):
        joined = lifecycle["joined"]
        left = lifecycle["left"]
        if len(joined) != 1:
            issues.append(
                {
                    "kind": "invalid-join-count",
                    "run_id": run_id,
                    "count": len(joined),
                }
            )
        if len(left) > 1:
            issues.append(
                {
                    "kind": "duplicate-agent-left",
                    "run_id": run_id,
                    "count": len(left),
                }
            )
        if not left and joined:
            open_runs.append(
                {
                    "run_id": run_id,
                    "owner": joined[0].get("owner"),
                    "joined_at": joined[0].get("at"),
                }
            )
        if left and not joined:
            issues.append({"kind": "agent-left-without-join", "run_id": run_id})

    pending_handoffs: list[dict[str, object]] = []
    for handoff_id, lifecycle in sorted(handoffs.items()):
        offered = lifecycle["offered"]
        accepted = lifecycle["accepted"]
        if len(offered) != 1:
            issues.append(
                {
                    "kind": "invalid-handoff-offer-count",
                    "handoff_id": handoff_id,
                    "count": len(offered),
                }
            )
        if len(accepted) > 1:
            issues.append(
                {
                    "kind": "duplicate-handoff-acceptance",
                    "handoff_id": handoff_id,
                    "count": len(accepted),
                }
            )
        if offered and not accepted:
            pending_handoffs.append(
                {
                    "handoff_id": handoff_id,
                    "source_run_id": offered[0].get("source_run_id"),
                    "source_owner": offered[0].get("source_owner"),
                    "target_owner": offered[0].get("target_owner"),
                    "offered_at": offered[0].get("at"),
                }
            )
        if accepted and not offered:
            issues.append(
                {
                    "kind": "handoff-accepted-without-offer",
                    "handoff_id": handoff_id,
                }
            )

    joined_count = sum(bool(value["joined"]) for value in runs.values())
    closed_count = sum(bool(value["left"]) for value in runs.values())
    offered_count = sum(bool(value["offered"]) for value in handoffs.values())
    accepted_count = sum(bool(value["accepted"]) for value in handoffs.values())
    return {
        "summary": {
            "runs_joined": joined_count,
            "runs_closed": closed_count,
            "runs_open": len(open_runs),
            "handoffs_offered": offered_count,
            "handoffs_accepted": accepted_count,
            "handoffs_pending": len(pending_handoffs),
            "issue_count": len(issues),
        },
        "open_runs": open_runs,
        "pending_handoffs": pending_handoffs,
        "issues": issues,
    }


def command_coverage(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    print(json.dumps(coverage_report(location), indent=2, ensure_ascii=False))
    return 0
