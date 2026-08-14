"""Contention-local distributed coordination with fenced coordinator epochs."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .constants import CONTENTION_DECISIONS, MAX_CONTENTION_PARTICIPANTS
from .control_plane import ControlPlane, operation
from .events import build_event, emit, materialized, write_event
from .storage import (
    now,
    read_json,
    replace_json,
    require_identifier,
    require_slug,
    require_text,
    write_json_exclusive,
)


DEFAULT_LEASE_SECONDS = 300


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("contention lease timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _expires(seconds: int) -> str:
    if seconds < 30 or seconds > 3600:
        raise ValueError("lease seconds must be between 30 and 3600")
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _path(plane: ControlPlane, contention_id: str, *, active: bool = True) -> Path:
    directory = "active" if active else "archive"
    return plane.state_root / "contentions" / directory / f"{contention_id}.json"


def _claim(plane: ControlPlane, scope: str) -> dict[str, object]:
    return read_json(plane.state_root / "claims" / f"{scope}.json", base=plane.state_root)


def _event_by_id(plane: ControlPlane, event_id: str) -> dict[str, object] | None:
    matches = sorted((plane.state_root / "events").glob(f"*-{event_id}-*.json"))
    if len(matches) > 1:
        raise ValueError("contention event id has multiple immutable records")
    return read_json(matches[0], base=plane.state_root) if matches else None


def _complete_open(
    plane: ControlPlane,
    *,
    path: Path,
    record: dict[str, object],
    trigger: dict[str, object],
) -> dict[str, object]:
    """Complete only the exact durable open intent already selected by ``record``."""

    scope = trigger.get("scope")
    if (
        not isinstance(scope, str)
        or record.get("trigger_scope") != scope
        or record.get("status") != "awaiting-decision"
    ):
        raise ValueError("existing contention does not match the pending trigger Claim")
    participant = next(
        (
            item
            for item in record.get("participants", [])
            if isinstance(item, dict) and item.get("scope") == scope
        ),
        None,
    )
    if (
        participant is None
        or participant.get("owner") != trigger.get("owner")
        or participant.get("run_id") != trigger.get("run_id")
    ):
        raise ValueError("existing contention has a stale trigger participant")
    event = record.get("opened_event")
    event_id = record.get("opened_event_id")
    if not isinstance(event, dict) or not isinstance(event_id, str):
        raise ValueError("contention opening intent is missing exact event evidence")
    if (
        event.get("event_id") != event_id
        or event.get("event") != "contention-opened"
        or event.get("contention_id") != record.get("contention_id")
        or event.get("owner") != trigger.get("owner")
        or event.get("run_id") != trigger.get("run_id")
        or event.get("scopes") != record.get("scopes")
        or event.get("participant_run_ids") != record.get("participant_run_ids")
    ):
        raise ValueError("contention opening intent is malformed or stale")
    existing_event = _event_by_id(plane, event_id)
    if existing_event is None:
        write_event(plane, event)
    elif existing_event != event:
        raise ValueError("contention opening event differs from its durable intent")
    contention_id = str(record["contention_id"])
    correlated = trigger.get("contention_id")
    if correlated not in {None, contention_id}:
        raise ValueError("pending Claim is correlated to another contention")
    if correlated is None:
        trigger["contention_id"] = contention_id
        trigger["contention_opened_at"] = event.get("at")
        replace_json(
            plane.state_root / "claims" / f"{scope}.json",
            trigger,
            base=plane.state_root,
        )
    record.pop("opened_event", None)
    replace_json(path, record, base=plane.state_root)
    return record


def open_for_claim(root: Path, *, scope: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    with operation(root, "contention-open") as plane:
        return open_for_claim_in_operation(plane, scope=scope, lease_seconds=lease_seconds)


def open_for_claim_in_operation(
    plane: ControlPlane, *, scope: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> dict[str, object]:
    """Open idempotently while the caller already holds the coordination lock."""

    trigger = _claim(plane, scope)
    if trigger.get("status") != "pending-arbitration":
        raise ValueError("only a pending-arbitration claim opens contention")
    trigger_owner = trigger.get("owner")
    trigger_run_id = trigger.get("run_id")
    if not isinstance(trigger_owner, str) or not isinstance(trigger_run_id, str):
        raise ValueError("pending Claim has no exact owner Run")
    trigger_run = read_json(
        plane.state_root / "runs" / f"{trigger_run_id}.json",
        base=plane.state_root,
    )
    if trigger_run.get("owner") != trigger_owner or trigger_run.get("status") != "active":
        raise ValueError("contention requires the pending Claim's exact active Run")
    for path in sorted((plane.state_root / "contentions" / "active").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        if scope in record.get("scopes", []):
            if record.get("trigger_scope") != scope:
                raise ValueError("pending Claim appears only as a non-trigger contention participant")
            if "opened_event" not in record:
                contention_id = record.get("contention_id")
                if trigger.get("contention_id") == contention_id:
                    return record
                raise ValueError("existing contention lacks a recoverable opening intent")
            return _complete_open(plane, path=path, record=record, trigger=trigger)
    conflict_scopes = {
        str(item.get("scope"))
        for item in trigger.get("conflicts", [])
        if isinstance(item, dict) and isinstance(item.get("scope"), str)
    }
    scopes = sorted({scope, *conflict_scopes})
    if len(scopes) > MAX_CONTENTION_PARTICIPANTS:
        raise ValueError(
            f"contention exceeds {MAX_CONTENTION_PARTICIPANTS} participants; decompose the scope"
        )
    participants: list[dict[str, object]] = []
    for candidate in scopes:
        claim = _claim(plane, candidate)
        participants.append(
            {
                "scope": candidate,
                "owner": claim.get("owner"),
                "run_id": claim.get("run_id"),
                "status": claim.get("status"),
            }
        )
    contention_id = f"contention-{uuid.uuid4().hex}"
    coordinator = {
        "owner": trigger.get("owner"),
        "run_id": trigger.get("run_id"),
        "epoch": 1,
        "lease_expires_at": _expires(lease_seconds),
    }
    opened_event = build_event(
        "contention-opened",
        payload={
            "contention_id": contention_id,
            "scopes": scopes,
            "owners": sorted({str(item["owner"]) for item in participants}),
            "participant_run_ids": sorted(str(item["run_id"]) for item in participants),
            "owner": coordinator["owner"],
            "run_id": coordinator["run_id"],
            "epoch": 1,
        },
    )
    record = materialized(
        {
            "schema": 1,
            "contention_id": contention_id,
            "status": "awaiting-decision",
            "trigger_scope": scope,
            "scopes": scopes,
            "owners": sorted({str(item["owner"]) for item in participants}),
            "participant_run_ids": sorted(str(item["run_id"]) for item in participants),
            "participants": participants,
            "coordinator": coordinator,
            "decision_revision": 0,
            "responses": {},
            "opened_at": now(),
            "opened_event_id": opened_event["event_id"],
            "opened_event": opened_event,
        }
    )
    path = _path(plane, contention_id)
    write_json_exclusive(path, record, base=plane.state_root)
    return _complete_open(plane, path=path, record=record, trigger=trigger)


def _active(plane: ControlPlane, contention_id: str) -> tuple[Path, dict[str, object]]:
    contention_id = require_identifier(contention_id, "contention id")
    path = _path(plane, contention_id)
    return path, read_json(path, base=plane.state_root)


def _assert_mutable(record: dict[str, object]) -> None:
    if record.get("status") in {"finalizing", "completed", "cancelled"}:
        raise ValueError("contention has terminal intent or state; reconcile before further mutation")


def _terminal_matches(record: dict[str, object], event: dict[str, object]) -> bool:
    coordinator = record.get("coordinator")
    if not isinstance(coordinator, dict):
        return False
    expected_status = "cancelled" if event.get("event") == "contention-cancelled" else "completed"
    return (
        event.get("contention_id") == record.get("contention_id")
        and event.get("status") == expected_status
        and event.get("revision") == record.get("decision_revision")
        and event.get("decision") == record.get("decision")
        and event.get("coordinator_epoch") == coordinator.get("epoch")
        and event.get("coordinator_run_id") == coordinator.get("run_id")
    )


def _archive_terminal(
    plane: ControlPlane,
    path: Path,
    record: dict[str, object],
    event: dict[str, object],
) -> dict[str, object]:
    if not _terminal_matches(record, event):
        raise ValueError("contention terminal evidence does not match the fenced decision state")
    status = str(event["status"])
    record.pop("terminal_event", None)
    record.update(
        {
            "status": status,
            f"{status}_at": event.get("at"),
            "terminal_event_id": event.get("event_id"),
        }
    )
    replace_json(path, record, base=plane.state_root)
    destination = _path(plane, str(record["contention_id"]), active=False)
    os.replace(path, destination)
    return {**record, "archive": str(destination)}


def _assert_coordinator(
    plane: ControlPlane,
    record: dict[str, object],
    owner: str,
    run_id: str,
    epoch: int,
) -> dict[str, object]:
    coordinator = record.get("coordinator")
    if not isinstance(coordinator, dict):
        raise ValueError("contention coordinator is malformed")
    if (
        coordinator.get("owner") != owner
        or coordinator.get("run_id") != run_id
        or coordinator.get("epoch") != epoch
    ):
        raise ValueError("contention coordinator owner, Run, or epoch is stale")
    run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
    if run.get("owner") != owner or run.get("status") != "active":
        raise ValueError("contention coordinator Run is not active")
    return coordinator


def select_wait(
    root: Path,
    *,
    contention_id: str,
    scope: str,
    owner: str,
    run_id: str,
    reason: str,
) -> dict[str, object]:
    """Let the non-authoritative trigger Claim wait without two-party consensus.

    Waiting constrains only the pending participant.  It never changes the
    active Claim, so coordinator proposal/response/enact would add ceremony
    without protecting another authority boundary.
    """

    contention_id = require_identifier(contention_id, "contention id")
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    reason = require_text(reason, "wait reason", 1000)
    with operation(root, "contention-wait") as plane:
        archive = _path(plane, contention_id, active=False)
        if archive.exists():
            completed = read_json(archive, base=plane.state_root)
            participant = next(
                (
                    item
                    for item in completed.get("participants", [])
                    if isinstance(item, dict) and item.get("scope") == scope
                ),
                None,
            )
            if (
                completed.get("decision") != "wait"
                or completed.get("trigger_scope") != scope
                or not isinstance(participant, dict)
                or participant.get("owner") != owner
                or participant.get("run_id") != run_id
                or completed.get("decision_reason") != reason
            ):
                raise ValueError("archived contention does not match this wait selection")
            return {**completed, "archive": str(archive), "event_emitted": False}

        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        if record.get("status") not in {"awaiting-decision", "decision-rejected"}:
            raise ValueError("contention already has an in-flight shared decision")
        if record.get("trigger_scope") != scope:
            raise ValueError("only the pending trigger Claim may select unilateral wait")
        claim = _claim(plane, scope)
        if (
            claim.get("owner") != owner
            or claim.get("run_id") != run_id
            or claim.get("status") != "pending-arbitration"
            or claim.get("contention_id") != contention_id
        ):
            raise ValueError("wait requires the exact pending trigger Claim")
        actor_run = read_json(
            plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root
        )
        if actor_run.get("owner") != owner or actor_run.get("status") != "active":
            raise ValueError("wait selection requires the exact active trigger Run")
        coordinator = record.get("coordinator")
        if not isinstance(coordinator, dict):
            raise ValueError("contention coordinator is malformed")
        revision = int(record.get("decision_revision", 0)) + 1
        terminal_event = build_event(
            "contention-completed",
            payload={
                "contention_id": contention_id,
                "owner": owner,
                "run_id": run_id,
                "epoch": coordinator.get("epoch"),
                "coordinator_epoch": coordinator.get("epoch"),
                "coordinator_run_id": coordinator.get("run_id"),
                "revision": revision,
                "decision": "wait",
                "status": "completed",
                "reason_code": "pending-participant-selected-wait",
                "reason": reason,
                "scopes": record.get("scopes"),
                "owners": record.get("owners"),
            },
        )
        record.update(
            {
                "status": "finalizing",
                "decision_revision": revision,
                "decision": "wait",
                "decision_reason": reason,
                "responses": {},
                "terminal_event": terminal_event,
            }
        )
        replace_json(path, record, base=plane.state_root)
        write_event(plane, terminal_event)
        return _archive_terminal(plane, path, record, terminal_event)


def renew(
    root: Path,
    *,
    contention_id: str,
    owner: str,
    run_id: str,
    epoch: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    with operation(root, "contention-renew") as plane:
        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        coordinator = _assert_coordinator(plane, record, owner, run_id, epoch)
        coordinator["lease_expires_at"] = _expires(lease_seconds)
        record["coordinator"] = coordinator
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "contention-coordinator-renewed",
            payload={
                "contention_id": contention_id,
                "owner": owner,
                "run_id": coordinator.get("run_id"),
                "epoch": epoch,
            },
        )
        return record


def acquire(
    root: Path,
    *,
    contention_id: str,
    owner: str,
    run_id: str,
    expected_epoch: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    with operation(root, "contention-acquire") as plane:
        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        coordinator = record.get("coordinator")
        if not isinstance(coordinator, dict) or coordinator.get("epoch") != expected_epoch:
            raise ValueError("contention epoch changed")
        if datetime.now(UTC) < _parse_time(coordinator.get("lease_expires_at")):
            raise ValueError("contention coordinator lease has not expired")
        participants = record.get("participants", [])
        if not any(
            isinstance(item, dict) and item.get("owner") == owner and item.get("run_id") == run_id
            for item in participants
        ):
            raise ValueError("only an exact contention participant may acquire the next epoch")
        run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
        if run.get("owner") != owner or run.get("status") != "active":
            raise ValueError("contention acquisition requires the exact active participant Run")
        previous_owner = coordinator.get("owner")
        record["coordinator"] = {
            "owner": owner,
            "run_id": run_id,
            "epoch": expected_epoch + 1,
            "lease_expires_at": _expires(lease_seconds),
        }
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "contention-coordinator-acquired",
            payload={
                "contention_id": contention_id,
                "owner": owner,
                "run_id": run_id,
                "previous_owner": previous_owner,
                "epoch": expected_epoch + 1,
            },
        )
        return record


def propose(
    root: Path,
    *,
    contention_id: str,
    owner: str,
    run_id: str,
    epoch: int,
    decision: str,
    reason: str,
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    if decision not in CONTENTION_DECISIONS:
        raise ValueError(f"unsupported contention decision: {decision}")
    reason = require_text(reason, "decision reason", 1000)
    with operation(root, "contention-propose") as plane:
        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        coordinator = _assert_coordinator(plane, record, owner, run_id, epoch)
        participant_claims = [_claim(plane, str(scope)) for scope in record.get("scopes", [])]
        if decision == "parallel-tx":
            if any(
                claim.get("projection_mode", "git-tree") != "git-tree"
                for claim in participant_claims
            ):
                raise ValueError(
                    "workspace-bytes Claims must select wait, handoff, or exclusive coordination"
                )
            if any(claim.get("intent") == "exclusive-refactor" for claim in participant_claims):
                raise ValueError("exclusive-refactor Claims cannot select parallel transactions")
            if any(
                not any(isinstance(item, str) for item in claim.get("semantic_writes", []))
                for claim in participant_claims
            ):
                raise ValueError(
                    "parallel-tx branch offload requires semantic writes from every Claim"
                )
            for index, left in enumerate(participant_claims):
                left_writes = set(item for item in left.get("semantic_writes", []) if isinstance(item, str))
                left_sensitive = set(item for item in left.get("sensitive_to", []) if isinstance(item, str))
                for right in participant_claims[index + 1 :]:
                    right_writes = set(item for item in right.get("semantic_writes", []) if isinstance(item, str))
                    right_sensitive = set(item for item in right.get("sensitive_to", []) if isinstance(item, str))
                    if (left_writes & right_writes) or (left_writes & right_sensitive) or (left_sensitive & right_writes):
                        raise ValueError("parallel transaction Claims are not semantically independent")
        revision = int(record.get("decision_revision", 0)) + 1
        record.update(
            {
                "status": "awaiting-acks",
                "decision_revision": revision,
                "decision": decision,
                "decision_reason": reason,
                "responses": {},
                "proposed_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "contention-decision-proposed",
            payload={
                "contention_id": contention_id,
                "owner": owner,
                "run_id": coordinator.get("run_id"),
                "epoch": epoch,
                "revision": revision,
                "decision": decision,
                "reason": reason,
            },
        )
        return record


def respond(
    root: Path,
    *,
    contention_id: str,
    scope: str,
    owner: str,
    run_id: str,
    revision: int,
    accept: bool,
    reason: str = "",
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    reason = require_text(reason, "response reason", 1000) if reason.strip() else ""
    with operation(root, "contention-respond") as plane:
        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        if record.get("status") != "awaiting-acks" or record.get("decision_revision") != revision:
            raise ValueError("contention decision revision is not current")
        participant = next(
            (
                item
                for item in record.get("participants", [])
                if isinstance(item, dict)
                and item.get("scope") == scope
                and item.get("owner") == owner
                and item.get("run_id") == run_id
            ),
            None,
        )
        if participant is None:
            raise ValueError("only an exact contention participant may respond")
        run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
        if run.get("owner") != owner or run.get("status") != "active":
            raise ValueError("contention participant Run is not active")
        responses = record.get("responses")
        if not isinstance(responses, dict):
            raise ValueError("contention responses are malformed")
        existing = responses.get(scope)
        if (
            isinstance(existing, dict)
            and existing.get("accepted") is accept
            and existing.get("reason") == reason
            and existing.get("owner") == owner
            and existing.get("run_id") == run_id
        ):
            return {**record, "event_emitted": False}
        responses[scope] = {
            "accepted": accept,
            "owner": owner,
            "run_id": run_id,
            "reason": reason,
            "at": now(),
        }
        record["responses"] = responses
        if not accept:
            record["status"] = "decision-rejected"
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "contention-decision-responded",
            payload={
                "contention_id": contention_id,
                "owner": owner,
                "run_id": run_id,
                "scope": scope,
                "revision": revision,
                "accepted": accept,
                "reason": reason,
            },
        )
        return record


def enact(
    root: Path, *, contention_id: str, owner: str, run_id: str, epoch: int
) -> dict[str, object]:
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    with operation(root, "contention-enact") as plane:
        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        coordinator = _assert_coordinator(plane, record, owner, run_id, epoch)
        if record.get("status") != "awaiting-acks":
            raise ValueError("contention has no accepted decision to enact")
        responses = record.get("responses")
        if not isinstance(responses, dict):
            raise ValueError("contention responses are malformed")
        missing = [
            str(participant.get("scope"))
            for participant in record.get("participants", [])
            if isinstance(participant, dict)
            and (
                not isinstance(responses.get(str(participant.get("scope"))), dict)
                or responses[str(participant.get("scope"))].get("accepted") is not True
                or responses[str(participant.get("scope"))].get("owner") != participant.get("owner")
                or responses[str(participant.get("scope"))].get("run_id") != participant.get("run_id")
            )
        ]
        if missing:
            raise ValueError("contention decision lacks participant acceptance: " + ", ".join(map(str, missing)))
        for participant in record.get("participants", []):
            if not isinstance(participant, dict) or not isinstance(participant.get("scope"), str):
                raise ValueError("contention participant is malformed")
            current = _claim(plane, str(participant["scope"]))
            participant_run = read_json(
                plane.state_root / "runs" / f"{participant.get('run_id')}.json",
                base=plane.state_root,
            )
            if (
                current.get("owner") != participant.get("owner")
                or current.get("run_id") != participant.get("run_id")
                or current.get("status") not in {"active", "paused", "pending-arbitration"}
                or participant_run.get("owner") != participant.get("owner")
                or participant_run.get("status") != "active"
            ):
                raise ValueError("contention participants changed; open a fresh decision")
        terminal_event = build_event(
            "contention-completed",
            payload={
                "contention_id": contention_id,
                "owner": owner,
                "run_id": coordinator.get("run_id"),
                "epoch": epoch,
                "coordinator_epoch": epoch,
                "coordinator_run_id": coordinator.get("run_id"),
                "revision": record.get("decision_revision"),
                "decision": record.get("decision"),
                "status": "completed",
                "reason_code": "decision-enacted",
                "scopes": record.get("scopes"),
                "owners": record.get("owners"),
            },
        )
        record.update(
            {
                "status": "finalizing",
                "terminal_event": terminal_event,
            }
        )
        replace_json(path, record, base=plane.state_root)
        write_event(plane, terminal_event)
        return _archive_terminal(plane, path, record, terminal_event)


def cancel(
    root: Path,
    *,
    contention_id: str,
    scope: str,
    owner: str,
    run_id: str,
    reason_code: str,
    reason: str,
) -> dict[str, object]:
    """End one arbitration slice without releasing or transferring any Claim."""

    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    reason_code = require_identifier(reason_code, "reason code")
    reason = require_text(reason, "cancellation reason", 1000)
    with operation(root, "contention-cancel") as plane:
        path, record = _active(plane, contention_id)
        _assert_mutable(record)
        participant = next(
            (
                item
                for item in record.get("participants", [])
                if isinstance(item, dict)
                and item.get("scope") == scope
                and item.get("owner") == owner
                and item.get("run_id") == run_id
            ),
            None,
        )
        if participant is None:
            raise ValueError("only an exact active participant may cancel contention")
        run = read_json(plane.state_root / "runs" / f"{run_id}.json", base=plane.state_root)
        if run.get("owner") != owner or run.get("status") != "active":
            raise ValueError("contention cancellation requires the exact active participant Run")
        coordinator = record.get("coordinator")
        if not isinstance(coordinator, dict):
            raise ValueError("contention coordinator is malformed")
        terminal_event = build_event(
            "contention-cancelled",
            payload={
                "contention_id": contention_id,
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "status": "cancelled",
                "reason_code": reason_code,
                "reason": reason,
                "revision": record.get("decision_revision"),
                "decision": record.get("decision"),
                "coordinator_epoch": coordinator.get("epoch"),
                "coordinator_run_id": coordinator.get("run_id"),
                "scopes": record.get("scopes"),
                "owners": record.get("owners"),
            },
        )
        record.update(
            {
                "status": "finalizing",
                "terminal_event": terminal_event,
                "cancelled_by_scope": scope,
                "cancelled_by_owner": owner,
                "cancelled_by_run_id": run_id,
                "reason_code": reason_code,
                "reason": reason,
                "cancelled_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        write_event(plane, terminal_event)
        return _archive_terminal(plane, path, record, terminal_event)


def reconcile(root: Path) -> list[dict[str, object]]:
    """Repair only provable terminal event/snapshot ordering gaps."""

    updates: list[dict[str, object]] = []
    with operation(root, "contention-reconcile") as plane:
        terminal_events: dict[str, list[dict[str, object]]] = {}
        for event_path in sorted((plane.state_root / "events").glob("*.json")):
            event = read_json(event_path, base=plane.state_root)
            contention_id = event.get("contention_id")
            if (
                isinstance(contention_id, str)
                and event.get("event") in {"contention-completed", "contention-cancelled"}
            ):
                terminal_events.setdefault(contention_id, []).append(event)

        for path in sorted((plane.state_root / "contentions" / "active").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            contention_id = str(record.get("contention_id"))
            events = terminal_events.get(contention_id, [])
            if len(events) > 1:
                raise ValueError("contention has multiple mutually exclusive terminal events")
            terminal = events[0] if events else None
            intent = record.get("terminal_event")
            if record.get("status") == "finalizing":
                if not isinstance(intent, dict) or not _terminal_matches(record, intent):
                    raise ValueError("contention finalization intent is malformed or stale")
                if terminal is None:
                    write_event(plane, intent)
                    terminal = intent
                    terminal_events[contention_id] = [intent]
                elif terminal != intent:
                    raise ValueError("contention terminal event differs from its durable intent")
                result = _archive_terminal(plane, path, record, terminal)
                updates.append(
                    {
                        "contention_id": contention_id,
                        "action": "terminal-intent-completed",
                        "result": result,
                    }
                )
                continue
            if record.get("status") in {"completed", "cancelled"}:
                if terminal is None or not _terminal_matches(record, terminal):
                    raise ValueError("active terminal contention lacks matching terminal evidence")
                result = _archive_terminal(plane, path, record, terminal)
                updates.append(
                    {
                        "contention_id": contention_id,
                        "action": "terminal-snapshot-archived",
                        "result": result,
                    }
                )
                continue
            if terminal is not None:
                raise ValueError(
                    "terminal contention evidence exists without a matching durable terminal intent"
                )

        for path in sorted((plane.state_root / "contentions" / "archive").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            contention_id = str(record.get("contention_id"))
            status = record.get("status")
            events = terminal_events.get(contention_id, [])
            if len(events) > 1:
                raise ValueError("archived contention has multiple mutually exclusive terminal events")
            if status not in {"completed", "cancelled"} or events:
                continue
            event_name = "contention-cancelled" if status == "cancelled" else "contention-completed"
            coordinator = record.get("coordinator")
            coordinator_owner = coordinator.get("owner") if isinstance(coordinator, dict) else None
            coordinator_run_id = coordinator.get("run_id") if isinstance(coordinator, dict) else None
            coordinator_epoch = coordinator.get("epoch") if isinstance(coordinator, dict) else None
            _, event = emit(
                plane,
                event_name,
                payload={
                    "contention_id": contention_id,
                    "owner": record.get("cancelled_by_owner") or coordinator_owner,
                    "run_id": record.get("cancelled_by_run_id") or coordinator_run_id,
                    "status": status,
                    "reason_code": "terminal-snapshot-reconciled",
                    "revision": record.get("decision_revision"),
                    "decision": record.get("decision"),
                    "coordinator_epoch": coordinator_epoch,
                    "coordinator_run_id": coordinator_run_id,
                    "scopes": record.get("scopes"),
                    "owners": record.get("owners"),
                    "recovered": True,
                },
            )
            record["reconciled_terminal_event_id"] = event["event_id"]
            record["reconciled_at"] = now()
            replace_json(path, record, base=plane.state_root)
            updates.append({"contention_id": contention_id, "action": "terminal-event-appended"})
    return updates


def get_decision(plane: ControlPlane, contention_id: str) -> dict[str, object]:
    active = _path(plane, contention_id)
    archived = _path(plane, contention_id, active=False)
    path = active if active.exists() else archived
    return read_json(path, base=plane.state_root)
