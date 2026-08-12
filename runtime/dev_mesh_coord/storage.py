"""Safe local persistence and advisory locking."""

from __future__ import annotations

import errno
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .errors import ProtocolError

try:
    import fcntl
except ImportError:  # pragma: no cover - the release target is POSIX.
    fcntl = None  # type: ignore[assignment]


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
ATOMIC_TEMP_NAME = re.compile(r"^\..+\.json\.\d+\.\d+\.tmp$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_slug(value: str, label: str) -> str:
    if not SLUG.fullmatch(value):
        raise ValueError(f"{label} must use lowercase letters, digits, and hyphens")
    return value


def require_identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters or is too long")
    return value


def require_text(value: str, label: str, limit: int = 1000) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    if len(normalized) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return normalized


def ensure_regular_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ProtocolError("marker_invalid", f"{label} must be a regular directory: {path}")


def ensure_safe_target(base: Path, path: Path, *, may_not_exist: bool = True) -> None:
    """Reject path escape and symlinks from the owned state root to the target."""

    ensure_regular_directory(base, "state root")
    base_real = base.resolve(strict=True)
    parent_real = path.parent.resolve(strict=True)
    try:
        parent_real.relative_to(base_real)
    except ValueError as error:
        raise ProtocolError("marker_invalid", f"state path escapes active root: {path}") from error
    current = base
    for part in path.parent.relative_to(base).parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ProtocolError("marker_invalid", f"unsafe state parent: {current}")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ProtocolError("marker_invalid", f"state record is not a regular file: {path}")
    elif not may_not_exist:
        raise FileNotFoundError(path)


def read_json(path: Path, *, base: Path | None = None) -> dict[str, object]:
    if base is not None:
        ensure_safe_target(base, path, may_not_exist=False)
    elif path.is_symlink() or not path.is_file():
        raise ProtocolError("marker_invalid", f"record must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("marker_invalid", f"cannot read JSON record {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError("marker_invalid", f"JSON record must be an object: {path}")
    return value


def json_bytes(value: dict[str, object]) -> bytes:
    """Return the exact canonical bytes persisted for one JSON record."""

    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace_json(path: Path, value: dict[str, object], *, base: Path) -> None:
    ensure_safe_target(base, path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_json_exclusive(path: Path, value: dict[str, object], *, base: Path) -> None:
    ensure_safe_target(base, path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise FileExistsError(f"coordination record already exists: {path}") from error
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_temp_residues(base: Path) -> list[Path]:
    """Report exact producer temp-file shapes without deleting audit evidence."""

    ensure_regular_directory(base, "state root")
    return sorted(
        path
        for path in base.rglob(".*.tmp")
        if not path.is_symlink()
        and path.is_file()
        and ATOMIC_TEMP_NAME.fullmatch(path.name)
    )


@contextmanager
def advisory_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ProtocolError("marker_invalid", f"lock parent is unsafe: {path.parent}")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProtocolError("marker_invalid", f"lock path is unsafe: {path}")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    try:
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("Dev Mesh coordination requires POSIX advisory locks")
        while not acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"coordination lock is busy: {path}") from error
                time.sleep(0.05)
        yield
    finally:
        if acquired and fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
