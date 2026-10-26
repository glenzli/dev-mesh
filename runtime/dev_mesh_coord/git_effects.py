"""Inherited-FD fencing for recoverable per-transaction Git mutations."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Callable, TypeVar

from .control_plane import ControlPlane
from .storage import require_identifier
from . import file_locks


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
    return file_locks.acquire(path)


def _acquire(path: Path, message: str) -> int:
    try:
        return _open_path(path)
    except BlockingIOError as error:
        raise GitEffectInFlight(message) from error


@contextmanager
def _fence(path: Path, message: str):
    descriptor = _acquire(path, message)
    try:
        yield descriptor
    finally:
        file_locks.release(descriptor)


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

    return _is_locked(_path(plane, transaction_id))


def _is_locked(path: Path) -> bool:
    try:
        descriptor = _open_path(path)
    except BlockingIOError:
        return True
    file_locks.release(descriptor)
    return False


def require_idle(plane: ControlPlane, transaction_id: str) -> None:
    if is_in_flight(plane, transaction_id):
        raise GitEffectInFlight(
            f"transaction {transaction_id} still has an in-flight Git effect"
        )


def is_canonical_in_flight(plane: ControlPlane) -> bool:
    return _is_locked(_canonical_path(plane))


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

    inherited = tuple(kwargs.pop("pass_fds", ()))
    with transaction_fence(plane, transaction_id) as descriptor:
        return runner(
            root,
            *arguments,
            pass_fds=(*inherited, descriptor),
            **kwargs,
        )
