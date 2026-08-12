"""Infra Discovery registration and Unix status-socket lifecycle."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import socket
import stat
import struct
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .facility_status import (
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    REQUEST_SCHEMA,
    SERVICE_INSTANCE_ID,
    SERVICE_KIND,
    error_response,
)


DISCOVERY_SCHEMA = "infra.discovery.registration"
DISCOVERY_VERSION = "20260812.1"
UNIX_SOCKET_BINDING = "infra.local.unix-socket"
REQUEST_LIMIT = 512
RESPONSE_LIMIT = 256 * 1024
MANIFEST_LIMIT = 64 * 1024
ENDPOINT_BIND_ATTEMPTS = 8


class InfraDiscoveryError(RuntimeError):
    """The local discovery or publication boundary is unsafe."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r}")


def _runtime_root() -> Path:
    override = os.environ.get("INFRA_PROTOCOL_RUNTIME_DIR")
    if override:
        root = Path(override)
        if not root.is_absolute():
            raise InfraDiscoveryError("INFRA_PROTOCOL_RUNTIME_DIR must be absolute")
        return root
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        confstr = libc.confstr
        confstr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
        confstr.restype = ctypes.c_size_t
        length = int(confstr(65537, None, 0))  # _CS_DARWIN_USER_TEMP_DIR
        if length <= 1:
            raise InfraDiscoveryError("Darwin user temporary directory is unavailable")
        buffer = ctypes.create_string_buffer(length)
        written = int(confstr(65537, buffer, length))
        if written == 0 or written > length:
            raise InfraDiscoveryError("Darwin user temporary directory is unavailable")
        return Path(os.fsdecode(buffer.value)) / "infra-protocol"
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_RUNTIME_DIR")
        if not base or not Path(base).is_absolute():
            raise InfraDiscoveryError(
                "Linux requires XDG_RUNTIME_DIR or INFRA_PROTOCOL_RUNTIME_DIR"
            )
        _validate_directory(Path(base), exact_mode=True)
        return Path(base) / "infra-protocol"
    raise InfraDiscoveryError("Infra Discovery Unix binding is unavailable on this platform")


def _validate_directory(path: Path, *, exact_mode: bool) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise InfraDiscoveryError(f"cannot inspect discovery directory {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InfraDiscoveryError(f"discovery directory is unsafe: {path}")
    if info.st_uid != os.geteuid():
        raise InfraDiscoveryError(f"discovery directory has the wrong owner: {path}")
    if exact_mode and stat.S_IMODE(info.st_mode) != 0o700:
        raise InfraDiscoveryError(f"discovery directory must use mode 0700: {path}")


def _prepare_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _validate_directory(path, exact_mode=False)
        path.chmod(0o700)
        _validate_directory(path, exact_mode=True)
    except InfraDiscoveryError:
        raise
    except OSError as error:
        raise InfraDiscoveryError(f"cannot prepare discovery directory {path}") from error


def _ensure_local_filesystem(path: Path) -> None:
    if sys.platform != "darwin":
        return

    class _Fsid(ctypes.Structure):
        _fields_ = [("value", ctypes.c_int32 * 2)]

    class _StatFs(ctypes.Structure):
        _fields_ = [
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", _Fsid),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    statfs = libc.statfs
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFs)]
    statfs.restype = ctypes.c_int
    information = _StatFs()
    if statfs(os.fsencode(path), ctypes.byref(information)) != 0:
        code = ctypes.get_errno()
        raise InfraDiscoveryError(f"cannot inspect discovery filesystem: {path}") from OSError(
            code, os.strerror(code)
        )
    if information.f_flags & 0x00001000 == 0:  # Darwin MNT_LOCAL
        raise InfraDiscoveryError("Infra Discovery runtime root must use a local filesystem")


def _validate_socket_path(path: Path) -> None:
    capacity = 104 if sys.platform == "darwin" else 108
    required = len(os.fsencode(str(path))) + 1
    if required > capacity:
        raise InfraDiscoveryError(
            f"Unix socket path requires {required} bytes; maximum is {capacity}"
        )


def _peer_uid(connection: socket.socket) -> int:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        result = libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid))
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return int(uid.value)
    if sys.platform.startswith("linux"):
        option = getattr(socket, "SO_PEERCRED", 17)
        credentials = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", credentials)
        return int(uid)
    raise InfraDiscoveryError("peer-user verification is unavailable on this platform")


class DiscoveryRuntime:
    """Verified owner-only runtime directories for one publisher."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or _runtime_root()).expanduser()
        if not self.root.is_absolute():
            raise InfraDiscoveryError("Infra Discovery runtime root must be absolute")
        _prepare_directory(self.root)
        _ensure_local_filesystem(self.root)
        self.registrations = self.root / "registrations"
        self.sockets = self.root / "sockets"
        _prepare_directory(self.registrations)
        _prepare_directory(self.sockets)

    def resolve_socket(self, endpoint: str) -> Path:
        if not endpoint.startswith("sockets/") or not endpoint.endswith(".sock"):
            raise InfraDiscoveryError("Unix socket endpoint is invalid")
        opaque = endpoint[len("sockets/") : -len(".sock")]
        if (
            not 1 <= len(opaque.encode("ascii", errors="ignore")) <= 16
            or not opaque[0].isalnum()
            or any(
                not (character.isascii() and (character.isalnum() or character in "._-"))
                for character in opaque
            )
        ):
            raise InfraDiscoveryError("Unix socket endpoint is invalid")
        path = self.sockets / f"{opaque}.sock"
        _validate_socket_path(path)
        return path


class _PublicationAuthority:
    def __init__(self, path: Path) -> None:
        import fcntl

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            try:
                os.close(descriptor)
            except (NameError, OSError):
                pass
            if error.errno in {errno.EAGAIN, errno.EACCES}:
                raise InfraDiscoveryError("Dev Mesh Observer publication authority is held") from error
            raise InfraDiscoveryError("cannot acquire Observer publication authority") from error
        self.descriptor = descriptor

    def close(self) -> None:
        import fcntl

        descriptor = getattr(self, "descriptor", None)
        if descriptor is None:
            return
        self.descriptor = None
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class ObserverFacilityService:
    """Own one status socket, stable registration, and request sequence."""

    def __init__(
        self,
        snapshot_factory: Callable[[dict[str, str], int], dict[str, object]],
        *,
        runtime_root: Path | None = None,
    ) -> None:
        self.snapshot_factory = snapshot_factory
        self.runtime = DiscoveryRuntime(runtime_root)
        self.service = {
            "kind": SERVICE_KIND,
            "instance_id": SERVICE_INSTANCE_ID,
            "generation": f"gen_{uuid.uuid4().hex}",
        }
        self.endpoint = ""
        self.socket_path = self.runtime.sockets
        self._select_endpoint()
        self.manifest_path = (
            self.runtime.registrations / f"{SERVICE_KIND}--{SERVICE_INSTANCE_ID}.json"
        )
        self._authority: _PublicationAuthority | None = None
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._serve_thread: threading.Thread | None = None
        self._sequence_lock = threading.Lock()
        self._sequence = 0
        self._started_once = False

    def _select_endpoint(self) -> None:
        self.endpoint = f"sockets/dm-{uuid.uuid4().hex[:12]}.sock"
        self.socket_path = self.runtime.resolve_socket(self.endpoint)

    def _bind_listener(self) -> socket.socket:
        last_collision: OSError | None = None
        for attempt in range(ENDPOINT_BIND_ATTEMPTS):
            if attempt:
                self._select_endpoint()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self.socket_path))
            except OSError as error:
                listener.close()
                if error.errno == errno.EADDRINUSE:
                    last_collision = error
                    continue
                raise
            return listener
        raise InfraDiscoveryError("cannot allocate a unique Observer Unix socket endpoint") from last_collision

    def start(self) -> None:
        if self._listener is not None:
            return
        if self._started_once:
            self.service["generation"] = f"gen_{uuid.uuid4().hex}"
            self._select_endpoint()
        authority_path = (
            self.runtime.registrations / f".{SERVICE_KIND}--{SERVICE_INSTANCE_ID}.publisher.lock"
        )
        authority = _PublicationAuthority(authority_path)
        listener: socket.socket | None = None
        bound_path: Path | None = None
        try:
            listener = self._bind_listener()
            bound_path = self.socket_path
            self.socket_path.chmod(0o600)
            listener.listen(8)
            listener.settimeout(0.5)
            self._write_manifest()
        except Exception:
            if listener is not None:
                listener.close()
            authority.close()
            if bound_path is not None and bound_path.exists() and not bound_path.is_symlink():
                bound_path.unlink()
            raise
        self._authority = authority
        self._listener = listener
        self._started_once = True
        self._stop.clear()
        self._serve_thread = threading.Thread(
            target=self._serve_loop,
            name="dev-mesh-observer-facility-status",
            daemon=True,
        )
        self._serve_thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        if self._serve_thread is not None and self._serve_thread is not threading.current_thread():
            self._serve_thread.join(timeout=3)
        self._serve_thread = None
        if self._authority is not None:
            self._authority.close()
            self._authority = None
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
            self.socket_path.unlink()

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            self._sequence += 1
            return self._sequence

    def _manifest(self) -> dict[str, object]:
        return {
            "schema": DISCOVERY_SCHEMA,
            "schema_version": DISCOVERY_VERSION,
            "service": dict(self.service),
            "offers": [
                {
                    "protocol": PROTOCOL_ID,
                    "protocol_versions": [PROTOCOL_VERSION],
                    "binding": UNIX_SOCKET_BINDING,
                    "endpoint": self.endpoint,
                }
            ],
        }

    def _write_manifest(self) -> None:
        payload = json.dumps(
            self._manifest(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        if len(payload) > MANIFEST_LIMIT:
            raise InfraDiscoveryError("Observer discovery manifest exceeds 64 KiB")
        temporary = self.runtime.registrations / f".{self.manifest_path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.manifest_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if self._stop.is_set() or error.errno in {errno.EBADF, errno.EINVAL}:
                    return
                continue
            with connection:
                self._handle_connection(connection)

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            if _peer_uid(connection) != os.geteuid():
                return
            connection.settimeout(2.0)
            request = bytearray()
            while b"\n" not in request and len(request) < REQUEST_LIMIT:
                chunk = connection.recv(REQUEST_LIMIT - len(request))
                if not chunk:
                    break
                request.extend(chunk)
            if b"\n" not in request:
                response = error_response("invalid_request")
            else:
                line, trailing = bytes(request).split(b"\n", 1)
                response = error_response("invalid_request") if trailing else self._dispatch(line)
            payload = json.dumps(
                response, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            if len(payload) > RESPONSE_LIMIT:
                payload = json.dumps(
                    error_response("snapshot_unavailable"), separators=(",", ":")
                ).encode("utf-8") + b"\n"
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
        except (OSError, TimeoutError):
            return

    def _dispatch(self, line: bytes) -> dict[str, object]:
        try:
            request = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(request, dict) or request != {
                "schema": REQUEST_SCHEMA,
                "schema_version": PROTOCOL_VERSION,
                "operation": "snapshot",
            }:
                return error_response("invalid_request")
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return error_response("invalid_request")
        try:
            return self.snapshot_factory(self.service, self._next_sequence())
        except Exception:
            return error_response("snapshot_unavailable")
