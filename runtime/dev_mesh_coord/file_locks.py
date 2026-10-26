"""Process locks whose ownership can survive in an inherited child handle."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
else:
    import fcntl


def validate_filename(path: Path) -> None:
    """Reject Win32 aliases and alternate streams before opening local state."""

    if os.name != "nt":
        return
    name = path.name
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    if (
        any(character in name for character in '<>:"/\\|?*')
        or any(ord(character) < 32 for character in name)
        or name.endswith((".", " "))
        or name.split(".", 1)[0].upper() in reserved
    ):
        raise ValueError(f"state filename is not supported on Windows: {name!r}; use a portable id")


def acquire(path: Path) -> int:
    """Acquire once without waiting; close with release(), never unlink the file."""

    validate_filename(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"lock must be a regular file: {path}")
    if os.name == "nt":
        # A byte-range lock is not inherited on Windows. Denying *all* sharing
        # instead keeps ownership until the last inherited file handle closes.
        handle = _create_file(
            str(path), 0xC0000000, 0, None, 4, 0x00200080, None,
        )  # READ|WRITE, no sharing, OPEN_ALWAYS, OPEN_REPARSE_POINT|NORMAL
        if handle == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            if code == 32:  # ERROR_SHARING_VIOLATION is contention, not permissions.
                raise BlockingIOError(errno.EAGAIN, "file lock is held", str(path))
            raise ctypes.WinError(code)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            _close_handle(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"lock must be a regular file: {path}")
        if os.name != "nt":
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise BlockingIOError(errno.EAGAIN, "file lock is held", str(path)) from error
                raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def release(descriptor: int) -> None:
    try:
        if os.name != "nt":
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
