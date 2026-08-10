"""Short-lived contention coordinator leases with participant-only fencing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contention_store import participant_owners, read_contention
from .state import emit_event, now, replace_json, require_text, validate_slug


DEFAULT_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 3600


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is malformed")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} is malformed") from error


def validate_lease_seconds(value: int) -> int:
    if value < 1 or value > MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease seconds must be between 1 and {MAX_LEASE_SECONDS}"
        )
    return value


def new_lease(owner: str, epoch: int, seconds: int) -> dict[str, object]:
    current = _utc_now()
    return {
        "owner": owner,
        "epoch": epoch,
        "acquired_at": _timestamp(current),
        "heartbeat_at": _timestamp(current),
        "lease_seconds": seconds,
        "lease_until": _timestamp(current + timedelta(seconds=seconds)),
    }


def coordinator_lease(record: dict[str, object]) -> dict[str, object]:
    value = record.get("coordinator")
    if not isinstance(value, dict):
        raise ValueError("contention coordinator lease is malformed")
    return value


def lease_expired(record: dict[str, object]) -> bool:
    return _parse_timestamp(
        coordinator_lease(record).get("lease_until"),
        "coordinator lease deadline",
    ) <= _utc_now()


def assert_coordinator(
    record: dict[str, object],
    owner: str,
    epoch: int,
    *,
    require_live_lease: bool = True,
) -> dict[str, object]:
    owner = validate_slug(owner, "coordinator owner")
    lease = coordinator_lease(record)
    if lease.get("owner") != owner or lease.get("epoch") != epoch:
        raise ValueError(
            "stale contention coordinator token; inspect the current owner and epoch"
        )
    if require_live_lease and lease_expired(record):
        raise ValueError("contention coordinator lease expired; acquire a new epoch")
    return lease


def renew_coordination(
    location: Path,
    contention_id: str,
    owner: str,
    epoch: int,
    lease_seconds: int,
) -> dict[str, object]:
    path, record = read_contention(location, contention_id)
    assert_coordinator(record, owner, epoch)
    seconds = validate_lease_seconds(lease_seconds)
    current = _utc_now()
    lease = coordinator_lease(record)
    lease["heartbeat_at"] = _timestamp(current)
    lease["lease_seconds"] = seconds
    lease["lease_until"] = _timestamp(current + timedelta(seconds=seconds))
    record["updated_at"] = now()
    replace_json(path, record)
    emit_event(
        location,
        "contention-coordinator-renewed",
        None,
        {
            "contention_id": contention_id,
            "coordinator": owner,
            "coordinator_epoch": epoch,
            "lease_until": lease["lease_until"],
        },
    )
    return record


def acquire_coordination(
    location: Path,
    contention_id: str,
    owner: str,
    expected_epoch: int,
    lease_seconds: int,
) -> dict[str, object]:
    path, record = read_contention(location, contention_id)
    owner = validate_slug(owner, "coordinator owner")
    if owner not in participant_owners(record):
        raise ValueError("only a contention participant may acquire coordination")
    lease = coordinator_lease(record)
    if lease.get("epoch") != expected_epoch:
        raise ValueError("contention coordinator epoch changed; inspect before retrying")
    if not lease_expired(record):
        raise ValueError("contention coordinator lease is still active")
    next_epoch = expected_epoch + 1
    prior_owner = lease.get("owner")
    record["coordinator"] = new_lease(
        owner,
        next_epoch,
        validate_lease_seconds(lease_seconds),
    )
    record["updated_at"] = now()
    replace_json(path, record)
    emit_event(
        location,
        "contention-coordinator-acquired",
        None,
        {
            "contention_id": contention_id,
            "coordinator": owner,
            "coordinator_epoch": next_epoch,
            "prior_coordinator": prior_owner,
            "prior_epoch": expected_epoch,
            "paths": record["paths"],
            "semantic_resources": record["semantic_resources"],
        },
    )
    return record


def handoff_coordination(
    location: Path,
    contention_id: str,
    owner: str,
    epoch: int,
    next_owner: str,
    lease_seconds: int,
    reason: str,
) -> dict[str, object]:
    path, record = read_contention(location, contention_id)
    assert_coordinator(record, owner, epoch)
    reason = require_text(reason, "coordination handoff reason")
    next_owner = validate_slug(next_owner, "next coordinator")
    if next_owner not in participant_owners(record):
        raise ValueError("next coordinator must be a contention participant")
    next_epoch = epoch + 1
    record["coordinator"] = new_lease(
        next_owner,
        next_epoch,
        validate_lease_seconds(lease_seconds),
    )
    record["updated_at"] = now()
    replace_json(path, record)
    emit_event(
        location,
        "contention-coordinator-handed-off",
        None,
        {
            "contention_id": contention_id,
            "coordinator": next_owner,
            "coordinator_epoch": next_epoch,
            "prior_coordinator": owner,
            "prior_epoch": epoch,
            "reason": reason,
            "paths": record["paths"],
            "semantic_resources": record["semantic_resources"],
        },
    )
    return record
