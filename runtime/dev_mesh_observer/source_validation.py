"""Bounded validation for coordination event and authority snapshot sources."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from dev_mesh_coord.constants import (
    AUTHORITY_EFFECTS,
    CLAIM_PROJECTION_MODES,
    EVENT_SCHEMA,
    MAX_EVENT_BYTES,
    PAUSE_BLOCKER_KINDS,
    PROTOCOL,
    PROTOCOL_VERSION,
)
from dev_mesh_coord.errors import ProtocolError
from dev_mesh_coord.storage import ensure_safe_target


MAX_SNAPSHOT_BYTES = 512 * 1024

SNAPSHOT_STATUSES = {
    ("run", "current"): {"active", "closed"},
    ("claim", "current"): {
        "active",
        "paused",
        "pending-arbitration",
        "pending-baseline",
        "completing",
        "transaction",
        "released",
        "published",
        "aborted",
    },
    ("claim", "archive"): {"released", "completed", "published", "aborted"},
    ("handoff", "current"): {"offered", "accepted", "rejected", "withdrawn"},
    ("contention", "active"): {
        "awaiting-decision",
        "awaiting-acks",
        "decision-rejected",
        "finalizing",
        "completed",
        "cancelled",
    },
    ("contention", "archive"): {"completed", "cancelled"},
    ("transaction", "active"): {
        "initializing",
        "initialization-needs-attention",
        "active",
        "prepared",
        "ready",
        "conflicted",
        "refreshing",
        "refresh-needs-attention",
        "publishing",
        "published",
        "aborting",
        "aborted",
    },
    ("transaction", "archive"): {"published", "aborted"},
    ("direct-commit", "active"): {"staging", "committing", "needs-attention", "completed"},
    # A direct-commit preflight can deliberately retain an attempted-but-never
    # materialized commit in the archive.  It has no active Git authority, but
    # remains useful immutable audit evidence; treating it as an unsupported
    # envelope makes every later collection look unhealthy forever.
    ("direct-commit", "archive"): {"completed", "needs-attention"},
    ("cleanup", "active"): {
        "planned",
        "removing-worktree",
        "worktree-removed",
        "removing-branch",
        "branch-removed",
        "archive-pending",
        "needs-attention",
        "completed",
    },
    ("cleanup", "archive"): {"completed"},
    ("work", "active"): {"waiting", "diverted", "finalizing", "resumed"},
    ("work", "archive"): {"resumed"},
    ("work-result", "current"): {"recorded"},
}

SNAPSHOT_REQUIRED_FIELDS = {
    "run": ("run_id", "owner", "joined_at"),
    "claim": ("scope", "owner", "run_id", "created_at", "heartbeat_at"),
    "handoff": ("handoff_id", "source_owner", "source_run_id", "target_owner", "offered_at"),
    "contention": ("contention_id", "opened_at"),
    "transaction": ("transaction_id", "owner", "run_id", "created_at"),
    "direct-commit": (
        "direct_commit_id",
        "scope",
        "owner",
        "run_id",
        "canonical_branch",
        "base_revision",
        "created_at",
    ),
    "cleanup": ("cleanup_id", "transaction_id", "owner", "run_id", "created_at"),
    "work": ("work_state_id", "scope", "owner", "run_id", "suspended_at"),
    "work-result": ("result_id", "scope", "owner", "run_id", "completed_at"),
}

SNAPSHOT_TIME_FIELDS = {
    "run": ("joined_at",),
    "claim": ("created_at", "heartbeat_at"),
    "handoff": ("offered_at",),
    "contention": ("opened_at",),
    "transaction": ("created_at",),
    "direct-commit": ("created_at",),
    "cleanup": ("created_at",),
    "work": ("suspended_at",),
    "work-result": ("completed_at",),
}

SNAPSHOT_ID_FIELDS = {
    "run": "run_id",
    "claim": "scope",
    "handoff": "handoff_id",
    "contention": "contention_id",
    "transaction": "transaction_id",
    "direct-commit": "direct_commit_id",
    "cleanup": "cleanup_id",
    "work": "work_state_id",
    "work-result": "result_id",
}

EVENT_IDENTITY_FIELDS = {
    "agent-joined": ("run_id",),
    "agent-left": ("run_id",),
    "run-authority-recovered": ("run_id", "source_run_id"),
    "claim-created": ("scope",),
    "claim-requested": ("scope",),
    "claim-activated": ("scope",),
    "claim-baseline-required": ("scope",),
    "claim-baseline-accepted": ("scope",),
    "claim-updated": ("scope",),
    "claim-paused": ("scope",),
    "claim-resumed": ("scope",),
    "claim-released": ("scope",),
    "claim-completed": ("scope", "result_id"),
    "message-sent": ("message_id",),
    "message-acknowledged": ("message_id",),
    "handoff-offered": ("handoff_id",),
    "handoff-accepted": ("handoff_id",),
    "handoff-rejected": ("handoff_id",),
    "handoff-withdrawn": ("handoff_id",),
    "contention-opened": ("contention_id",),
    "contention-coordinator-renewed": ("contention_id",),
    "contention-coordinator-acquired": ("contention_id",),
    "contention-decision-proposed": ("contention_id",),
    "contention-decision-responded": ("contention_id",),
    "contention-completed": ("contention_id",),
    "contention-cancelled": ("contention_id",),
    "work-suspended": ("work_state_id",),
    "work-resumed": ("work_state_id",),
    "transaction-created": ("transaction_id",),
    "transaction-prepared": ("transaction_id",),
    "transaction-validated": ("transaction_id",),
    "transaction-refreshed": ("transaction_id",),
    "transaction-conflicted": ("transaction_id",),
    "transaction-handed-off": ("transaction_id",),
    "transaction-published": ("transaction_id",),
    "transaction-aborted": ("transaction_id",),
    "direct-commit-started": ("direct_commit_id", "scope"),
    "direct-commit-completed": ("direct_commit_id", "scope"),
    "cleanup-authorized": ("cleanup_id", "transaction_id"),
    "cleanup-completed": ("cleanup_id", "transaction_id"),
    "cleanup-needs-attention": ("cleanup_id", "transaction_id"),
    "audit-correction": ("scope", "supersedes_event_id"),
}


def _valid_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def event_source(path: Path, state_root: Path) -> tuple[bytes, str]:
    ensure_safe_target(state_root, path, may_not_exist=False)
    try:
        if path.stat().st_size > MAX_EVENT_BYTES:
            raise ProtocolError(
                "marker_invalid",
                f"event exceeds the {MAX_EVENT_BYTES}-byte protocol limit: {path}",
            )
        encoded = path.read_bytes()
    except OSError as error:
        raise ProtocolError("marker_invalid", f"cannot read event {path}: {error}") from error
    if len(encoded) > MAX_EVENT_BYTES:
        raise ProtocolError(
            "marker_invalid",
            f"event exceeds the {MAX_EVENT_BYTES}-byte protocol limit: {path}",
        )
    return encoded, hashlib.sha256(encoded).hexdigest()


def event_record(
    path: Path,
    encoded: bytes,
    *,
    protocol_version: str = PROTOCOL_VERSION,
    event_schema: int = EVENT_SCHEMA,
) -> dict[str, object]:
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("marker_invalid", f"cannot decode event {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError("marker_invalid", f"event record must be an object: {path}")
    record: dict[str, object] = value
    event = record.get("event")
    if (
        record.get("schema") != event_schema
        or record.get("protocol") != PROTOCOL
        or record.get("protocol_version") != protocol_version
        or not isinstance(event, str)
        or event not in AUTHORITY_EFFECTS
        or record.get("authority_effect") != AUTHORITY_EFFECTS[event]
        or not isinstance(record.get("event_id"), str)
        or not _valid_time(record.get("at"))
        or "transaction_id" not in record
    ):
        raise ProtocolError("marker_invalid", f"unsupported event envelope: {path}")
    event_id = str(record["event_id"])
    suffix = f"-{event_id}-{event}.json"
    timestamp_prefix = path.name[: -len(suffix)] if path.name.endswith(suffix) else ""
    if not timestamp_prefix.isdigit():
        raise ProtocolError("marker_invalid", f"event id does not match source path: {path}")
    for field in EVENT_IDENTITY_FIELDS[event]:
        if not isinstance(record.get(field), str) or not str(record[field]):
            raise ProtocolError("marker_invalid", f"event lacks required {field}: {path}")
    if not isinstance(record.get("owner"), str) or not isinstance(record.get("run_id"), str):
        raise ProtocolError("marker_invalid", f"event lacks exact owner/run identity: {path}")
    return record


def snapshot_record(
    path: Path,
    state_root: Path,
    kind: str,
    lifecycle: str,
    *,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, object]:
    ensure_safe_target(state_root, path, may_not_exist=False)
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise ProtocolError(
                "marker_invalid",
                f"snapshot exceeds the {MAX_SNAPSHOT_BYTES}-byte Observer limit: {path}",
            )
        encoded = path.read_bytes()
    except OSError as error:
        raise ProtocolError("marker_invalid", f"cannot read snapshot {path}: {error}") from error
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ProtocolError(
            "marker_invalid",
            f"snapshot exceeds the {MAX_SNAPSHOT_BYTES}-byte Observer limit: {path}",
        )
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("marker_invalid", f"cannot decode snapshot {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError("marker_invalid", f"snapshot must be an object: {path}")
    record: dict[str, object] = value
    if (
        record.get("schema") != 1
        or record.get("protocol") != PROTOCOL
        or record.get("protocol_version") != protocol_version
        or record.get("status") not in SNAPSHOT_STATUSES[(kind, lifecycle)]
    ):
        raise ProtocolError("marker_invalid", f"unsupported {kind} snapshot envelope: {path}")
    for field in SNAPSHOT_REQUIRED_FIELDS[kind]:
        if not isinstance(record.get(field), str) or not str(record[field]):
            raise ProtocolError("marker_invalid", f"{kind} snapshot lacks required {field}: {path}")
    for field in SNAPSHOT_TIME_FIELDS[kind]:
        if not _valid_time(record.get(field)):
            raise ProtocolError("marker_invalid", f"{kind} snapshot has invalid {field}: {path}")
    object_id = str(record[SNAPSHOT_ID_FIELDS[kind]])
    if kind in {"work", "claim"} and lifecycle == "archive":
        suffix = f"-{object_id}.json"
        prefix = path.name[: -len(suffix)] if path.name.endswith(suffix) else ""
        path_matches = bool(prefix) and (kind == "claim" or prefix.isdigit())
    elif kind == "direct-commit" and lifecycle == "archive":
        # Preflight-rejected commits can use a bounded explanatory suffix so
        # operators can retain multiple audit records without pretending an
        # active commit exists. The durable id remains the complete leading
        # component of the file name.
        path_matches = path.name == f"{object_id}.json" or (
            path.name.startswith(f"{object_id}-") and path.name.endswith(".json")
        )
    else:
        path_matches = path.name == f"{object_id}.json"
    if not path_matches:
        raise ProtocolError("marker_invalid", f"{kind} snapshot id does not match source path: {path}")
    if kind == "claim" and record.get("status") == "paused":
        pause = record.get("pause")
        if (
            not isinstance(pause, dict)
            or pause.get("blocker_kind") not in PAUSE_BLOCKER_KINDS
        ):
            raise ProtocolError("marker_invalid", f"paused Claim metadata is invalid: {path}")
    if kind in {"claim", "work-result"}:
        projection_mode = record.get("projection_mode", "git-tree")
        if projection_mode not in CLAIM_PROJECTION_MODES:
            raise ProtocolError(
                "marker_invalid", f"{kind} snapshot projection mode is invalid: {path}"
            )
        if projection_mode == "workspace-bytes":
            if kind == "claim" and record.get("status") in {
                "active",
                "paused",
                "completing",
            } and not isinstance(record.get("workspace_base"), dict):
                raise ProtocolError(
                    "marker_invalid", f"workspace-bytes Claim lacks its baseline: {path}"
                )
            if kind == "work-result" and (
                not isinstance(record.get("workspace_bytes_sha256"), str)
                or not isinstance(record.get("workspace_file_count"), int)
                or not isinstance(record.get("workspace_missing_path_count"), int)
                or not isinstance(record.get("workspace_total_bytes"), int)
            ):
                raise ProtocolError(
                    "marker_invalid", f"workspace-bytes Work Result is incomplete: {path}"
                )
    return record
