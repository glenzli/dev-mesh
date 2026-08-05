"""Durable local state and atomic coordination primitives."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

try:  # POSIX, including macOS and Linux.
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt.
    fcntl = None  # type: ignore[assignment]

try:  # Windows fallback.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl.
    msvcrt = None  # type: ignore[assignment]


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
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
MERGEABLE_INTENTS = {"additive", "local-edit"}
EXCLUSIVE_INTENTS = {"contract", "delete", "generated", "move", "refactor"}
TRANSACTION_MODES = {"ordered-tx", "parallel-tx"}
TEST_CRASH_ENV = "SHARED_COORD_TEST_CRASH_POINT"


def crash_if_testing(point: str) -> None:
    """Terminate only when an integration test explicitly selects this boundary."""
    if os.environ.get(TEST_CRASH_ENV) == point:
        os._exit(86)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def root_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def validate_slug(value: str, label: str) -> str:
    if not SLUG.fullmatch(value):
        raise ValueError(f"{label} must use lowercase letters, digits, and hyphens")
    return value


def require_text(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    return normalized


def state_root(root: Path, state_directory: str) -> Path:
    relative = Path(state_directory)
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        raise ValueError("state directory must be a relative workspace subdirectory")
    return root / relative


def initialize(root: Path, state_directory: str) -> Path:
    location = state_root(root, state_directory)
    for relative in (
        "claims",
        "cleanups/active",
        "cleanups/archive",
        "groups/active",
        "groups/archive",
        "transactions/active",
        "transactions/archive",
        "events",
        "checkouts",
        "waiting",
        "handoffs",
        "messages",
        "acks",
        "archive/claims",
        "archive/messages",
        "metrics",
        "locks",
    ):
        (location / relative).mkdir(parents=True, exist_ok=True)
    return location


def normalize_paths(root: Path, values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = (
            (root / value).resolve()
            if not Path(value).is_absolute()
            else Path(value).resolve()
        )
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise ValueError(f"path is outside the workspace: {value}") from error
        if relative == Path("."):
            raise ValueError("declare semantic subtrees instead of the whole workspace")
        text = relative.as_posix()
        if text not in normalized:
            normalized.append(text)
    return normalized


def string_list(record: dict[str, object], key: str) -> list[str]:
    value = record.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"record field {key!r} must be a string array")
    return list(value)


def paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


def overlapping_pairs(left: list[str], right: list[str]) -> list[str]:
    return sorted(
        {
            f"{left_path} ↔ {right_path}"
            for left_path in left
            for right_path in right
            if paths_overlap(left_path, right_path)
        }
    )


def read_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"coordination record is not an object: {path}")
    return value


def write_text_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _try_lock(descriptor: int) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        return True
    if msvcrt is not None:  # pragma: no cover - Windows only.
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


def _unlock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows only.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    raise RuntimeError("this platform has no supported advisory file-lock primitive")


@contextmanager
def coordination_guard(location: Path, operation: str) -> Iterator[None]:
    legacy = location / ".claim-guard"
    if legacy.exists():
        raise RuntimeError(
            f"legacy mkdir guard exists at {legacy}; verify its owner before recovery"
        )
    path = location / ".claim-guard.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + 5.0
    acquired = False
    try:
        while not acquired:
            acquired = _try_lock(descriptor)
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"another coordination update is active at {path}")
            time.sleep(0.05)
        metadata = json.dumps(
            {
                "schema": 1,
                "pid": os.getpid(),
                "started_at": now(),
                "operation": operation,
            },
            indent=2,
        ).encode("utf-8") + b"\n"
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, metadata)
        os.fsync(descriptor)
        yield
    finally:
        if acquired:
            _unlock(descriptor)
        os.close(descriptor)


def emit_event(
    location: Path,
    event: str,
    transaction_id: str | None,
    details: dict[str, object] | None = None,
) -> Path:
    identifier = transaction_id or "coord"
    path = location / "events" / f"{time.time_ns()}-{identifier}-{event}.json"
    record: dict[str, object] = {
        "schema": 1,
        "event": event,
        "transaction_id": transaction_id,
        "at": now(),
    }
    if details:
        record.update(details)
    write_json_exclusive(path, record)
    return path


def active_claims(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return [
        (path, read_json(path))
        for path in sorted((location / "claims").glob("*.json"))
    ]


def active_transactions(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return [
        (path, read_json(path))
        for path in sorted((location / "transactions" / "active").glob("*.json"))
    ]


def claim_path(location: Path, scope: str) -> Path:
    return location / "claims" / f"{validate_slug(scope, 'scope')}.json"


def transaction_path(location: Path, transaction_id: str) -> Path:
    return (
        location
        / "transactions"
        / "active"
        / f"{validate_slug(transaction_id, 'transaction id')}.json"
    )


def read_transaction(location: Path, transaction_id: str) -> tuple[Path, dict[str, object]]:
    path = transaction_path(location, transaction_id)
    if not path.exists():
        raise ValueError(f"active transaction does not exist: {transaction_id}")
    return path, read_json(path)


def archive_claim(location: Path, source: Path, record: dict[str, object]) -> Path:
    destination = location / "archive" / "claims" / f"{time.time_ns()}-{source.name}"
    replace_json(source, record)
    shutil.move(source, destination)
    return destination


def archive_transaction(
    location: Path,
    source: Path,
    record: dict[str, object],
) -> Path:
    destination = (
        location
        / "transactions"
        / "archive"
        / f"{time.time_ns()}-{source.name}"
    )
    replace_json(source, record)
    shutil.move(source, destination)
    return destination


def transaction_is_committed(location: Path, transaction_id: str) -> bool:
    for path in (location / "transactions" / "archive").glob(f"*-{transaction_id}.json"):
        if read_json(path).get("status") == "committed":
            return True
    return False
