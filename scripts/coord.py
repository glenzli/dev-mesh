#!/usr/bin/env python3
"""Small, optional coordination helper for agents sharing one workspace."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

try:  # POSIX, including the supported macOS development environment.
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the msvcrt fallback below.
    fcntl = None  # type: ignore[assignment]

try:  # Windows fallback so the helper does not regress into a permanent mkdir lock.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl above.
    msvcrt = None  # type: ignore[assignment]


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CLAIM_KINDS = {"standard", "transaction"}
INTENTS = {
    "additive",
    "contract",
    "delete",
    "generated",
    "local-edit",
    "move",
    "read",
    "refactor",
}
MESSAGE_TYPES = {
    "conflict",
    "decision",
    "handoff",
    "info",
    "takeover",
    "validation",
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def root_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def state_root(root: Path, state_directory: str) -> Path:
    relative = Path(state_directory)
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        raise ValueError("state directory must be a relative workspace subdirectory")
    return root / relative


def initialize(root: Path, state_directory: str) -> Path:
    location = state_root(root, state_directory)
    for relative in (
        "claims",
        "groups/active",
        "groups/archive",
        "messages",
        "acks",
        "tasks",
        "waiting",
        "handoffs",
        "archive/claims",
        "archive/messages",
        "guard-events",
    ):
        (location / relative).mkdir(parents=True, exist_ok=True)
    return location


def validate_slug(value: str, label: str) -> str:
    if not SLUG.fullmatch(value):
        raise ValueError(f"{label} must use lowercase letters, digits, and hyphens")
    return value


def normalize_paths(root: Path, values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise ValueError(f"claimed path is outside the workspace: {value}") from error
        if relative == Path("."):
            raise ValueError("claim semantic subtrees instead of the whole workspace")
        text = relative.as_posix()
        if text not in normalized:
            normalized.append(text)
    return normalized


def paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def read_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"coordination record is not an object: {path}")
    return value


def write_text_exclusive(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise FileExistsError(f"coordination record already exists: {path}") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_json_exclusive(path: Path, value: dict[str, object]) -> None:
    write_text_exclusive(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def replace_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def guard_lock_path(location: Path) -> Path:
    return location / ".claim-guard.lock"


def legacy_guard_path(location: Path) -> Path:
    return location / ".claim-guard"


def read_guard_metadata(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def process_is_alive(value: object) -> bool | None:
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def try_advisory_lock(descriptor: int) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        return True
    if msvcrt is not None:  # pragma: no cover - exercised on Windows hosts.
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    raise RuntimeError("this platform has no supported advisory file-lock primitive")


def release_advisory_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised on Windows hosts.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    raise RuntimeError("this platform has no supported advisory file-lock primitive")


def write_guard_metadata(descriptor: int, value: dict[str, object]) -> None:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    os.write(descriptor, payload)
    os.fsync(descriptor)


def legacy_guard_diagnostic(location: Path) -> str:
    legacy = legacy_guard_path(location)
    if not legacy.exists():
        return ""
    try:
        modified_at = datetime.fromtimestamp(legacy.stat().st_mtime, UTC).isoformat(
            timespec="seconds"
        )
    except OSError:
        modified_at = "unknown"
    return (
        f"legacy mkdir guard exists at {legacy} (modified {modified_at}). "
        "It may belong to an older helper process, so this helper will not remove it. "
        "Run guard-status for diagnostics; only an owner-confirmed "
        "guard-recover --confirm-legacy-owner-inactive may remove an empty legacy guard."
    )


@contextmanager
def claim_guard(location: Path, operation: str) -> Iterator[None]:
    legacy_diagnostic = legacy_guard_diagnostic(location)
    if legacy_diagnostic:
        raise RuntimeError(legacy_diagnostic)

    guard = guard_lock_path(location)
    descriptor = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + 3.0
    acquired = False
    try:
        while True:
            if try_advisory_lock(descriptor):
                acquired = True
                break
            if time.monotonic() >= deadline:
                metadata = read_guard_metadata(guard)
                detail = json.dumps(metadata, ensure_ascii=False) if metadata else "unavailable"
                raise RuntimeError(
                    f"another coordination update is active at {guard}; retry shortly "
                    f"or run guard-status. Last guard metadata: {detail}"
                )
            time.sleep(0.05)
        write_guard_metadata(
            descriptor,
            {
                "schema": 1,
                "pid": os.getpid(),
                "started_at": now(),
                "operation": operation,
            },
        )
        yield
    finally:
        if acquired:
            release_advisory_lock(descriptor)
        os.close(descriptor)


def active_claims(location: Path) -> list[tuple[Path, dict[str, object]]]:
    claims: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((location / "claims").glob("*.json")):
        claims.append((path, read_json(path)))
    return claims


def active_transaction_scopes(location: Path) -> list[tuple[Path, dict[str, object]]]:
    transactions: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((location / "transactions" / "active").glob("*.json")):
        transactions.append((path, read_json(path)))
    return transactions


def materializing_group_scopes(location: Path) -> list[tuple[Path, dict[str, object]]]:
    scopes: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((location / "groups" / "active").glob("*.json")):
        group = read_json(path)
        if group.get("status") == "active" and group.get("claims_promoted") is True:
            continue
        members = group.get("members", [])
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            planned = member.get("planned_transaction")
            if isinstance(planned, dict):
                group_scope = dict(planned)
                group_scope["_coord_record_kind"] = "transaction group"
                scopes.append((path, group_scope))
    return scopes


def claim_conflicts(
    location: Path,
    requested_paths: list[str],
    ignored_claim: Path | None = None,
) -> list[str]:
    conflicts: list[str] = []
    for existing_path, existing in (
        active_claims(location)
        + active_transaction_scopes(location)
        + materializing_group_scopes(location)
    ):
        if existing_path == ignored_claim:
            continue
        existing_paths = existing.get("paths", [])
        if not isinstance(existing_paths, list):
            existing_paths = []
        overlapping = sorted(
            {
                f"{requested} ↔ {current}"
                for requested in requested_paths
                for current in existing_paths
                if isinstance(current, str) and paths_overlap(requested, current)
            }
        )
        if overlapping:
            owner = existing.get("owner", "unknown")
            explicit_kind = existing.get("_coord_record_kind")
            if isinstance(explicit_kind, str):
                record_kind = explicit_kind
            elif "transaction_id" in existing:
                record_kind = "transaction"
            else:
                record_kind = "claim"
            conflicts.append(
                f"{record_kind} {existing_path.stem} (owner {owner}): "
                f"{', '.join(overlapping)}"
            )
    return conflicts


def validate_overlap(arguments: argparse.Namespace, conflicts: list[str]) -> int:
    if conflicts and not arguments.allow_overlap:
        print("claim conflicts with active work:", file=sys.stderr)
        for conflict in conflicts:
            print(f"- {conflict}", file=sys.stderr)
        print("send a message or choose an independent scope", file=sys.stderr)
        return 2
    if conflicts and not arguments.reason.strip():
        raise ValueError("--allow-overlap requires a non-empty --reason")
    return 0


def require_text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} cannot be empty")
    return value


def validate_scope_list(values: list[str], label: str, scope: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = validate_slug(value, label)
        if candidate == scope:
            raise ValueError(f"{label} cannot reference its own scope {scope!r}")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def normalize_resources(values: list[str], label: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = value.strip()
        if not candidate:
            raise ValueError(f"{label} cannot contain an empty value")
        if "\n" in candidate or "\r" in candidate:
            raise ValueError(f"{label} values must fit on one line")
        if len(candidate) > 240:
            raise ValueError(f"{label} values must be at most 240 characters")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def validate_transaction_metadata(
    kind: str,
    steward: str | None,
    participants: list[str],
) -> tuple[str | None, list[str]]:
    if kind not in CLAIM_KINDS:
        raise ValueError(f"claim kind must be one of {', '.join(sorted(CLAIM_KINDS))}")
    if kind == "standard":
        if steward is not None or participants:
            raise ValueError("transaction steward and participants require --kind transaction")
        return None, []
    if steward is None:
        raise ValueError("--kind transaction requires --transaction-steward")
    return validate_slug(steward, "transaction steward"), [
        validate_slug(participant, "transaction participant") for participant in participants
    ]


def parse_checkpoint(
    value: str,
    root: Path,
    claimed_paths: list[str],
) -> dict[str, object]:
    try:
        checkpoint = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"--checkpoint must be a JSON object: {error}") from error
    if not isinstance(checkpoint, dict):
        raise ValueError("--checkpoint must be a JSON object")

    required_text = ("base_revision", "validation", "known_failure", "next_safe_owner")
    for key in required_text:
        if not isinstance(checkpoint.get(key), str) or not checkpoint[key].strip():
            raise ValueError(f"checkpoint requires a non-empty string field {key!r}")

    owned_paths = checkpoint.get("owned_paths")
    if not isinstance(owned_paths, list) or not all(isinstance(path, str) for path in owned_paths):
        raise ValueError("checkpoint requires an owned_paths string array")
    normalized_owned_paths = normalize_paths(root, owned_paths)
    if sorted(normalized_owned_paths) != sorted(claimed_paths):
        raise ValueError("checkpoint owned_paths must exactly match the active claim paths")
    checkpoint["owned_paths"] = normalized_owned_paths
    return checkpoint


def record_metadata_from_claim_arguments(
    arguments: argparse.Namespace,
    scope: str,
) -> dict[str, object]:
    kind = arguments.kind
    first_release = arguments.first_release
    if first_release is not None:
        first_release = require_text(first_release, "first_release")
    depends_on = validate_scope_list(arguments.depends_on, "depends_on scope", scope)
    steward, participants = validate_transaction_metadata(
        kind,
        arguments.transaction_steward,
        arguments.participants,
    )
    metadata: dict[str, object] = {
        "status": "active",
        "kind": kind,
        "intent": arguments.intent,
    }
    semantic_writes = normalize_resources(arguments.semantic_writes, "semantic_writes")
    sensitive_to = normalize_resources(arguments.sensitive_to, "sensitive_to")
    validation_plan = normalize_resources(arguments.validation, "validation")
    if semantic_writes:
        metadata["semantic_writes"] = semantic_writes
    if sensitive_to:
        metadata["sensitive_to"] = sensitive_to
    if validation_plan:
        metadata["validation"] = validation_plan
    if first_release is not None:
        metadata["first_release"] = first_release
    if depends_on:
        metadata["depends_on"] = depends_on
    if steward is not None:
        metadata["transaction_steward"] = steward
    if participants:
        metadata["participants"] = participants
    return metadata


def apply_update_metadata(
    record: dict[str, object],
    arguments: argparse.Namespace,
    scope: str,
) -> bool:
    metadata_requested = any(
        (
            arguments.first_release is not None,
            arguments.clear_first_release,
            arguments.depends_on is not None,
            arguments.kind is not None,
            arguments.transaction_steward is not None,
            arguments.clear_transaction_steward,
            arguments.participants is not None,
            arguments.intent is not None,
            arguments.semantic_writes is not None,
            arguments.sensitive_to is not None,
            arguments.validation is not None,
        )
    )
    if not metadata_requested:
        return False
    if arguments.first_release is not None and arguments.clear_first_release:
        raise ValueError("choose either --first-release or --clear-first-release")
    if arguments.transaction_steward is not None and arguments.clear_transaction_steward:
        raise ValueError(
            "choose either --transaction-steward or --clear-transaction-steward"
        )

    kind_value = arguments.kind if arguments.kind is not None else record.get("kind", "standard")
    if not isinstance(kind_value, str):
        raise ValueError("claim kind is malformed")
    steward_value = (
        arguments.transaction_steward
        if arguments.transaction_steward is not None
        else record.get("transaction_steward")
    )
    if arguments.clear_transaction_steward:
        steward_value = None
    if steward_value is not None and not isinstance(steward_value, str):
        raise ValueError("transaction steward is malformed")
    participants_value = (
        arguments.participants
        if arguments.participants is not None
        else record.get("participants", [])
    )
    if not isinstance(participants_value, list) or not all(
        isinstance(value, str) for value in participants_value
    ):
        raise ValueError("transaction participants are malformed")
    steward, participants = validate_transaction_metadata(
        kind_value,
        steward_value,
        participants_value,
    )

    record["kind"] = kind_value
    if steward is None:
        record.pop("transaction_steward", None)
        record.pop("participants", None)
    else:
        record["transaction_steward"] = steward
        if participants:
            record["participants"] = participants
        else:
            record.pop("participants", None)

    if arguments.first_release is not None:
        record["first_release"] = require_text(arguments.first_release, "first_release")
    elif arguments.clear_first_release:
        record.pop("first_release", None)

    if arguments.depends_on is not None:
        depends_on = validate_scope_list(arguments.depends_on, "depends_on scope", scope)
        if depends_on:
            record["depends_on"] = depends_on
        else:
            record.pop("depends_on", None)

    if arguments.intent is not None:
        record["intent"] = arguments.intent

    for argument_name, record_name in (
        ("semantic_writes", "semantic_writes"),
        ("sensitive_to", "sensitive_to"),
        ("validation", "validation"),
    ):
        values = getattr(arguments, argument_name)
        if values is None:
            continue
        normalized = normalize_resources(values, record_name)
        if normalized:
            record[record_name] = normalized
        else:
            record.pop(record_name, None)
    return True


def command_init(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    print(location)
    print(f"Keep {arguments.state_dir}/ local; add it to an ignore rule when needed.")
    return 0


def command_claim(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    task = require_text(arguments.task, "task")
    requested_paths = normalize_paths(arguments.root, arguments.paths)
    if not requested_paths:
        raise ValueError("at least one likely write path is required")
    claim_path = location / "claims" / f"{scope}.json"
    with claim_guard(location, "claim"):
        if claim_path.exists():
            existing = read_json(claim_path)
            if existing.get("owner") == arguments.owner:
                raise ValueError(
                    f"claim {scope!r} already exists for this owner; use the update command"
                )
            raise ValueError(
                f"claim {scope!r} already belongs to {existing.get('owner')!r}"
            )
        conflicts = claim_conflicts(location, requested_paths)
        validation = validate_overlap(arguments, conflicts)
        if validation != 0:
            return validation
        record: dict[str, object] = {
            "schema": 1,
            "scope": scope,
            "owner": owner,
            "task": task,
            "paths": requested_paths,
            "created_at": now(),
            "heartbeat_at": now(),
        }
        record.update(record_metadata_from_claim_arguments(arguments, scope))
        if conflicts:
            if arguments.pending_on_conflict:
                record["status"] = "pending-arbitration"
            record["overlap_reason"] = arguments.reason
            record["overlaps"] = conflicts
        write_json_exclusive(claim_path, record)
    print(claim_path)
    return 0


def command_update(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    claim_path = location / "claims" / f"{scope}.json"
    with claim_guard(location, "update"):
        record = read_json(claim_path)
        if record.get("owner") != owner:
            raise ValueError(f"claim belongs to {record.get('owner')!r}, not {owner!r}")
        if record.get("status", "active") == "paused":
            raise ValueError("claim is paused; resume it or release it before updating work")
        existing_paths = record.get("paths", [])
        if not isinstance(existing_paths, list) or not all(
            isinstance(path, str) for path in existing_paths
        ):
            raise ValueError(f"claim has invalid paths: {claim_path}")
        requested_paths = list(existing_paths)
        for path in normalize_paths(arguments.root, arguments.add_paths):
            if path not in requested_paths:
                requested_paths.append(path)
        for path in normalize_paths(arguments.root, arguments.remove_paths):
            if path in requested_paths:
                requested_paths.remove(path)
        if not requested_paths:
            raise ValueError("an active claim must retain at least one likely write path")
        task = arguments.task.strip()
        metadata_changed = apply_update_metadata(record, arguments, scope)
        if not task and not arguments.add_paths and not arguments.remove_paths and not metadata_changed:
            raise ValueError(
                "update requires task, path, or coordination-metadata changes"
            )

        conflicts = claim_conflicts(location, requested_paths, ignored_claim=claim_path)
        validation = validate_overlap(arguments, conflicts)
        if validation != 0:
            return validation
        record["paths"] = requested_paths
        if task:
            record["task"] = task
        record["updated_at"] = now()
        record["heartbeat_at"] = now()
        if conflicts:
            record["overlap_reason"] = arguments.reason
            record["overlaps"] = conflicts
        else:
            record.pop("overlap_reason", None)
            record.pop("overlaps", None)
        replace_json(claim_path, record)
    print(claim_path)
    return 0


def command_heartbeat(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    owner = validate_slug(arguments.owner, "owner")
    path = location / "claims" / f"{validate_slug(arguments.scope, 'scope')}.json"
    with claim_guard(location, "heartbeat"):
        record = read_json(path)
        if record.get("owner") != owner:
            raise ValueError(f"claim belongs to {record.get('owner')!r}, not {owner!r}")
        if record.get("status", "active") == "paused":
            raise ValueError("paused claims cannot heartbeat; resume or release the claim")
        record["heartbeat_at"] = now()
        replace_json(path, record)
    print(path)
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    claims = active_claims(location)
    if not claims:
        print("no active claims")
    for path, claim in claims:
        paths = claim.get("paths", [])
        joined = ", ".join(paths) if isinstance(paths, list) else "?"
        print(
            f"{path.stem}: status={claim.get('status', 'active')} "
            f"owner={claim.get('owner')} task={claim.get('task')} "
            f"intent={claim.get('intent', 'local-edit')} "
            f"heartbeat={claim.get('heartbeat_at')} paths=[{joined}] "
            f"semantic_writes={claim.get('semantic_writes', [])}"
        )
    return 0


def write_message(
    location: Path,
    recipient: str,
    sender: str,
    subject: str,
    body: str,
    message_type: str = "info",
    requires_ack: bool = False,
    reply_to: str | None = None,
) -> Path:
    if message_type not in MESSAGE_TYPES:
        raise ValueError(
            f"message type must be one of {', '.join(sorted(MESSAGE_TYPES))}"
        )
    if reply_to is not None:
        reply_to = require_text(reply_to, "reply_to")
    destination = location / "messages" / recipient
    destination.mkdir(parents=True, exist_ok=True)
    subject_slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40] or "message"
    message_id = f"{time.time_ns()}-{sender}-{subject_slug}"
    path = destination / f"{message_id}.md"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            f"---\nfrom: {sender}\nto: {recipient}\ncreated_at: {now()}\n"
            f"message_id: {message_id}\n"
            f"type: {message_type}\n"
            f"requires_ack: {'true' if requires_ack else 'false'}\n"
            f"subject: {json.dumps(subject, ensure_ascii=False)}\n"
        )
        if reply_to is not None:
            stream.write(f"reply_to: {json.dumps(reply_to, ensure_ascii=False)}\n")
        stream.write(
            "---\n\n"
            f"{body.rstrip()}\n"
        )
    return path


def command_message(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    recipient = validate_slug(arguments.to, "recipient")
    sender = validate_slug(arguments.from_owner, "sender")
    subject = require_text(arguments.subject, "message subject")
    path = write_message(
        location,
        recipient,
        sender,
        subject,
        arguments.body,
        message_type=arguments.type,
        requires_ack=arguments.requires_ack,
        reply_to=arguments.reply_to,
    )
    print(path)
    return 0


def command_pause(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    path = location / "claims" / f"{scope}.json"
    with claim_guard(location, "pause"):
        record = read_json(path)
        if record.get("owner") != owner:
            raise ValueError(f"claim belongs to {record.get('owner')!r}, not {owner!r}")
        if record.get("status", "active") == "paused":
            raise ValueError("claim is already paused")
        claimed_paths = record.get("paths")
        if not isinstance(claimed_paths, list) or not all(
            isinstance(value, str) for value in claimed_paths
        ):
            raise ValueError("claim has invalid paths")
        record["checkpoint"] = parse_checkpoint(
            arguments.checkpoint,
            arguments.root,
            claimed_paths,
        )
        record["resume_condition"] = require_text(
            arguments.resume_condition,
            "resume_condition",
        )
        retain_reason = arguments.retain_paths_reason.strip()
        if retain_reason:
            record["pause_retained_paths_reason"] = retain_reason
        else:
            record.pop("pause_retained_paths_reason", None)
        record["status"] = "paused"
        record["paused_at"] = now()
        record["heartbeat_at"] = now()
        replace_json(path, record)
    print(path)
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    path = location / "claims" / f"{scope}.json"
    with claim_guard(location, "resume"):
        record = read_json(path)
        if record.get("owner") != owner:
            raise ValueError(f"claim belongs to {record.get('owner')!r}, not {owner!r}")
        if record.get("status", "active") != "paused":
            raise ValueError("only a paused claim can be resumed")
        record["status"] = "active"
        record["resumed_at"] = now()
        record["heartbeat_at"] = now()
        replace_json(path, record)
    print(path)
    return 0


def command_takeover_request(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    requester = validate_slug(arguments.requester, "requester")
    reason = require_text(arguments.reason, "takeover request reason")
    path = location / "claims" / f"{scope}.json"
    with claim_guard(location, "takeover-request"):
        record = read_json(path)
        owner = record.get("owner")
        if not isinstance(owner, str):
            raise ValueError("claim owner is malformed")
        if owner == requester:
            raise ValueError("claim owner cannot request takeover from itself")
        heartbeat = record.get("heartbeat_at", record.get("created_at", "unknown"))
        message = write_message(
            location,
            scope,
            requester,
            "Takeover request (no ownership transfer)",
            "\n".join(
                (
                    f"Requester: {requester}",
                    f"Current owner: {owner}",
                    f"Observed heartbeat: {heartbeat}",
                    f"Reason: {reason}",
                    "This request is auditable only. It does not release, alter, or transfer the claim; the owner or user must authorize a handoff.",
                )
            ),
            message_type="takeover",
            requires_ack=True,
        )
    print(message)
    return 0


def guard_status(location: Path) -> dict[str, object]:
    legacy = legacy_guard_path(location)
    guard = guard_lock_path(location)
    result: dict[str, object] = {
        "legacy_guard_exists": legacy.exists(),
        "legacy_guard_path": str(legacy),
        "guard_path": str(guard),
        "last_metadata": read_guard_metadata(guard),
    }
    if legacy.exists():
        result["legacy_diagnostic"] = legacy_guard_diagnostic(location)
    descriptor = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        available = try_advisory_lock(descriptor)
        result["advisory_lock_available"] = available
        if available:
            release_advisory_lock(descriptor)
    finally:
        os.close(descriptor)
    metadata = result.get("last_metadata")
    if isinstance(metadata, dict):
        result["last_metadata_pid_alive"] = process_is_alive(metadata.get("pid"))
    return result


def write_guard_event(location: Path, event: dict[str, object]) -> Path:
    path = location / "guard-events" / f"{time.time_ns()}-recovery.json"
    write_json_exclusive(path, event)
    return path


def command_guard_status(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    print(json.dumps(guard_status(location), indent=2, ensure_ascii=False))
    return 0


def command_guard_recover(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    reason = require_text(arguments.reason, "guard recovery reason")
    legacy = legacy_guard_path(location)
    removed_legacy_guard = False
    if legacy.exists():
        if not arguments.confirm_legacy_owner_inactive:
            raise ValueError(
                "legacy guard recovery requires --confirm-legacy-owner-inactive; "
                "the helper will never remove another possibly active mkdir guard automatically"
            )
        try:
            legacy.rmdir()
        except OSError as error:
            raise RuntimeError(
                f"legacy guard could not be removed safely ({error}); do not force-delete it"
            ) from error
        removed_legacy_guard = True
    with claim_guard(location, "guard-recover"):
        event = write_guard_event(
            location,
            {
                "schema": 1,
                "event": "guard-recovery-verified",
                "at": now(),
                "pid": os.getpid(),
                "reason": reason,
                "removed_legacy_guard": removed_legacy_guard,
                "diagnostics": guard_status(location),
            },
        )
    print(event)
    return 0


def message_metadata(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {"message_id": path.stem, "requires_ack": False}
    metadata: dict[str, object] = {"message_id": path.stem, "requires_ack": False}
    for line in lines[1:]:
        if line == "---":
            break
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        value = raw_value.strip()
        try:
            metadata[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            metadata[key.strip()] = value
    return metadata


def acknowledgement_path(location: Path, owner: str, message_id: str) -> Path:
    return location / "acks" / owner / f"{message_id}.json"


def find_message(location: Path, message_id: str) -> Path:
    matches = [
        path
        for path in (location / "messages").glob("*/*.md")
        if path.stem == message_id
    ]
    if not matches:
        raise ValueError(f"message does not exist: {message_id}")
    if len(matches) > 1:
        raise ValueError(f"message id is ambiguous: {message_id}")
    return matches[0]


def command_ack(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    owner = validate_slug(arguments.owner, "owner")
    message_id = require_text(arguments.message_id, "message_id")
    source = find_message(location, message_id)
    metadata = message_metadata(source)
    destination = acknowledgement_path(location, owner, message_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with claim_guard(location, "ack"):
        if destination.exists():
            existing = read_json(destination)
            if existing.get("owner") != owner:
                raise ValueError(f"acknowledgement belongs to {existing.get('owner')!r}")
            print(destination)
            return 0
        write_json_exclusive(
            destination,
            {
                "schema": 1,
                "message_id": message_id,
                "message_type": metadata.get("type", "info"),
                "owner": owner,
                "acknowledged_at": now(),
                "source": source.relative_to(location).as_posix(),
                "note": arguments.note.strip(),
            },
        )
    print(destination)
    return 0


def command_inbox(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scopes = [validate_slug(arguments.scope, "scope"), "all"]
    owner = (
        validate_slug(arguments.owner, "owner")
        if arguments.owner is not None
        else None
    )
    if arguments.action_required and owner is None:
        raise ValueError("--action-required requires --owner")
    found = False
    for scope in dict.fromkeys(scopes):
        for path in sorted((location / "messages" / scope).glob("*.md")):
            metadata = message_metadata(path)
            message_id = str(metadata.get("message_id", path.stem))
            acknowledged = (
                owner is not None
                and acknowledgement_path(location, owner, message_id).exists()
            )
            requires_ack = metadata.get("requires_ack") is True
            if arguments.action_required and (not requires_ack or acknowledged):
                continue
            found = True
            state = ""
            if owner is not None and requires_ack:
                state = " [ACKNOWLEDGED]" if acknowledged else " [ACTION REQUIRED]"
            print(f"### {path}{state}")
            print(path.read_text(encoding="utf-8").rstrip())
    if not found:
        print("inbox empty")
    return 0


def command_release(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    path = location / "claims" / f"{scope}.json"
    with claim_guard(location, "release"):
        record = read_json(path)
        if record.get("owner") != owner:
            raise ValueError(f"claim belongs to {record.get('owner')!r}, not {owner!r}")
        record["released_at"] = now()
        record["summary"] = arguments.summary
        replace_json(path, record)
        archive_name = f"{time.time_ns()}-{scope}.json"
        destination = location / "archive" / "claims" / archive_name
        shutil.move(path, destination)
    print(destination)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    def add_root(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--root", type=root_path, default=root_path("."))
        subparser.add_argument("--state-dir", default=".agent-coordination")

    init_parser = subparsers.add_parser("init")
    add_root(init_parser)
    init_parser.set_defaults(handler=command_init)

    claim_parser = subparsers.add_parser("claim")
    add_root(claim_parser)
    claim_parser.add_argument("--scope", required=True)
    claim_parser.add_argument("--owner", required=True)
    claim_parser.add_argument("--task", required=True)
    claim_parser.add_argument("--paths", nargs="+", required=True)
    claim_parser.add_argument("--first-release")
    claim_parser.add_argument("--depends-on", nargs="*", default=[])
    claim_parser.add_argument("--intent", choices=sorted(INTENTS), default="local-edit")
    claim_parser.add_argument("--semantic-writes", nargs="*", default=[])
    claim_parser.add_argument("--sensitive-to", nargs="*", default=[])
    claim_parser.add_argument("--validation", nargs="*", default=[])
    claim_parser.add_argument("--kind", choices=sorted(CLAIM_KINDS), default="standard")
    claim_parser.add_argument("--transaction-steward")
    claim_parser.add_argument("--participants", nargs="*", default=[])
    claim_parser.add_argument("--allow-overlap", action="store_true")
    claim_parser.add_argument(
        "--pending-on-conflict",
        action="store_true",
        help="record an overlapping request without granting direct write authority",
    )
    claim_parser.add_argument("--reason", default="")
    claim_parser.set_defaults(handler=command_claim)

    update_parser = subparsers.add_parser("update")
    add_root(update_parser)
    update_parser.add_argument("--scope", required=True)
    update_parser.add_argument("--owner", required=True)
    update_parser.add_argument("--task", default="")
    update_parser.add_argument("--add-paths", nargs="*", default=[])
    update_parser.add_argument("--remove-paths", nargs="*", default=[])
    update_parser.add_argument("--first-release")
    update_parser.add_argument("--clear-first-release", action="store_true")
    update_parser.add_argument("--depends-on", nargs="*", default=None)
    update_parser.add_argument("--intent", choices=sorted(INTENTS))
    update_parser.add_argument("--semantic-writes", nargs="*", default=None)
    update_parser.add_argument("--sensitive-to", nargs="*", default=None)
    update_parser.add_argument("--validation", nargs="*", default=None)
    update_parser.add_argument("--kind", choices=sorted(CLAIM_KINDS))
    update_parser.add_argument("--transaction-steward")
    update_parser.add_argument("--clear-transaction-steward", action="store_true")
    update_parser.add_argument("--participants", nargs="*", default=None)
    update_parser.add_argument("--allow-overlap", action="store_true")
    update_parser.add_argument("--reason", default="")
    update_parser.set_defaults(handler=command_update)

    heartbeat_parser = subparsers.add_parser("heartbeat")
    add_root(heartbeat_parser)
    heartbeat_parser.add_argument("--scope", required=True)
    heartbeat_parser.add_argument("--owner", required=True)
    heartbeat_parser.set_defaults(handler=command_heartbeat)

    pause_parser = subparsers.add_parser("pause")
    add_root(pause_parser)
    pause_parser.add_argument("--scope", required=True)
    pause_parser.add_argument("--owner", required=True)
    pause_parser.add_argument("--checkpoint", required=True)
    pause_parser.add_argument("--resume-condition", required=True)
    pause_parser.add_argument("--retain-paths-reason", default="")
    pause_parser.set_defaults(handler=command_pause)

    resume_parser = subparsers.add_parser("resume")
    add_root(resume_parser)
    resume_parser.add_argument("--scope", required=True)
    resume_parser.add_argument("--owner", required=True)
    resume_parser.set_defaults(handler=command_resume)

    status_parser = subparsers.add_parser("status")
    add_root(status_parser)
    status_parser.set_defaults(handler=command_status)

    message_parser = subparsers.add_parser("message")
    add_root(message_parser)
    message_parser.add_argument("--to", required=True)
    message_parser.add_argument("--from-owner", required=True)
    message_parser.add_argument("--subject", required=True)
    message_parser.add_argument("--body", required=True)
    message_parser.add_argument("--type", choices=sorted(MESSAGE_TYPES), default="info")
    message_parser.add_argument("--requires-ack", action="store_true")
    message_parser.add_argument("--reply-to")
    message_parser.set_defaults(handler=command_message)

    takeover_parser = subparsers.add_parser("takeover-request")
    add_root(takeover_parser)
    takeover_parser.add_argument("--scope", required=True)
    takeover_parser.add_argument("--requester", required=True)
    takeover_parser.add_argument("--reason", required=True)
    takeover_parser.set_defaults(handler=command_takeover_request)

    guard_status_parser = subparsers.add_parser("guard-status")
    add_root(guard_status_parser)
    guard_status_parser.set_defaults(handler=command_guard_status)

    guard_recover_parser = subparsers.add_parser("guard-recover")
    add_root(guard_recover_parser)
    guard_recover_parser.add_argument("--reason", required=True)
    guard_recover_parser.add_argument(
        "--confirm-legacy-owner-inactive",
        action="store_true",
        help="explicitly acknowledge that an old mkdir guard owner is no longer active",
    )
    guard_recover_parser.set_defaults(handler=command_guard_recover)

    inbox_parser = subparsers.add_parser("inbox")
    add_root(inbox_parser)
    inbox_parser.add_argument("--scope", required=True)
    inbox_parser.add_argument("--owner")
    inbox_parser.add_argument("--action-required", action="store_true")
    inbox_parser.set_defaults(handler=command_inbox)

    ack_parser = subparsers.add_parser("ack")
    add_root(ack_parser)
    ack_parser.add_argument("--owner", required=True)
    ack_parser.add_argument("--message-id", required=True)
    ack_parser.add_argument("--note", default="")
    ack_parser.set_defaults(handler=command_ack)

    release_parser = subparsers.add_parser("release")
    add_root(release_parser)
    release_parser.add_argument("--scope", required=True)
    release_parser.add_argument("--owner", required=True)
    release_parser.add_argument("--summary", required=True)
    release_parser.set_defaults(handler=command_release)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"coordination error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
