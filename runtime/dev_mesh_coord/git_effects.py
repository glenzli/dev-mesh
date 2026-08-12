"""Inherited-FD fencing for recoverable per-transaction Git mutations."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, TypeVar

from .control_plane import ControlPlane
from .storage import require_identifier

try:
    import fcntl
except ImportError:  # pragma: no cover - the release target is POSIX.
    fcntl = None  # type: ignore[assignment]


Result = TypeVar("Result")


class GitEffectInFlight(RuntimeError):
    pass


def _path(plane: ControlPlane, transaction_id: str) -> Path:
    transaction_id = require_identifier(transaction_id, "transaction id")
    return plane.state_root / "locks" / f"git-effect-{transaction_id}.lock"


def _canonical_path(plane: ControlPlane) -> Path:
    return plane.state_root / "locks" / "canonical-git-effect.lock"


def _open_path(path: Path) -> int:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise RuntimeError("transaction Git effect lock parent is unsafe")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("transaction Git effect lock path is unsafe")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RuntimeError("transaction Git effect lock is not a regular file")
    return descriptor


def _open(plane: ControlPlane, transaction_id: str) -> int:
    return _open_path(_path(plane, transaction_id))


def _try_lock(descriptor: int, message: str) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise GitEffectInFlight(message) from error
        raise


@contextmanager
def _fence(path: Path, message: str):
    if fcntl is None:  # pragma: no cover
        raise RuntimeError("Git effect fencing requires POSIX advisory locks")
    descriptor = _open_path(path)
    acquired = False
    try:
        _try_lock(descriptor, message)
        acquired = True
        yield descriptor
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def transaction_fence(plane: ControlPlane, transaction_id: str):
    transaction_id = require_identifier(transaction_id, "transaction id")
    return _fence(
        _path(plane, transaction_id),
        f"transaction {transaction_id} still has an in-flight Git effect",
    )


def canonical_fence(plane: ControlPlane):
    return _fence(
        _canonical_path(plane),
        "canonical Git still has an in-flight effect",
    )


def is_in_flight(plane: ControlPlane, transaction_id: str) -> bool:
    """Probe without taking authority; a surviving child keeps the FD lock."""

    if fcntl is None:  # pragma: no cover
        raise RuntimeError("Git effect fencing requires POSIX advisory locks")
    descriptor = _open(plane, transaction_id)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            return False
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def require_idle(plane: ControlPlane, transaction_id: str) -> None:
    if is_in_flight(plane, transaction_id):
        raise GitEffectInFlight(
            f"transaction {transaction_id} still has an in-flight Git effect"
        )


def is_canonical_in_flight(plane: ControlPlane) -> bool:
    if fcntl is None:  # pragma: no cover
        raise RuntimeError("Git effect fencing requires POSIX advisory locks")
    descriptor = _open_path(_canonical_path(plane))
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            return False
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def require_canonical_idle(plane: ControlPlane) -> None:
    if is_canonical_in_flight(plane):
        raise GitEffectInFlight("canonical Git still has an in-flight effect")


def run(
    plane: ControlPlane,
    transaction_id: str,
    runner: Callable[..., Result],
    root: Path,
    *arguments: str,
    **kwargs: object,
) -> Result:
    """Run one mutation while the child inherits the transaction fence FD."""

    if fcntl is None:  # pragma: no cover
        raise RuntimeError("Git effect fencing requires POSIX advisory locks")
    inherited = tuple(kwargs.pop("pass_fds", ()))
    with transaction_fence(plane, transaction_id) as descriptor:
        return runner(
            root,
            *arguments,
            pass_fds=(*inherited, descriptor),
            **kwargs,
        )
