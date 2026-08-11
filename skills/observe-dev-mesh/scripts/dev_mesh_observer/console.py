"""Localhost-only HTTP console for the Observer catalog."""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .collector import LiveCollector
from .console_data import (
    bounded_anchor,
    bounded_limit,
    bounded_page,
    event_detail,
    event_page,
    list_issues,
)
from .graph import build_collaboration_graph
from .reports import build_report, parse_since
from .store import ObserverStore
from .storyline import build_collaboration_storyline


STATIC_ROOT = Path(__file__).with_name("web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/analytics-view.js": ("analytics-view.js", "text/javascript; charset=utf-8"),
    "/project-overview.js": ("project-overview.js", "text/javascript; charset=utf-8"),
    "/storyline-focus.js": ("storyline-focus.js", "text/javascript; charset=utf-8"),
    "/storyline-layout.js": ("storyline-layout.js", "text/javascript; charset=utf-8"),
    "/storyline-view.js": ("storyline-view.js", "text/javascript; charset=utf-8"),
    "/graph-view.js": ("graph-view.js", "text/javascript; charset=utf-8"),
    "/preferences.js": ("preferences.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


@dataclass(frozen=True)
class ConsoleConfig:
    data_dir: Path
    max_depth: int
    collect_interval: float


class ObserverConsole(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        host: str,
        port: int,
        *,
        data_dir: Path,
        max_depth: int,
        collect_interval: float = 0,
    ) -> None:
        validate_loopback_host(host)
        if port < 0 or port > 65535:
            raise ValueError("port must be between 0 and 65535")
        self.config = ConsoleConfig(
            data_dir=data_dir,
            max_depth=max_depth,
            collect_interval=collect_interval,
        )
        self.collector = LiveCollector(
            data_dir=data_dir,
            max_depth=max_depth,
            interval_seconds=collect_interval,
        )
        super().__init__((host, port), ConsoleHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.collector.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self.collector.stop()

    def server_close(self) -> None:
        self.collector.stop()
        super().server_close()


def validate_loopback_host(host: str) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Observer console only supports 127.0.0.1 or localhost")


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ObserverConsole

    def log_message(self, format_string: str, *arguments: object) -> None:
        message = format_string % arguments
        print(f"observer console: {message}", file=sys.stderr)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=()",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _valid_authority(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            parsed = urlsplit(f"//{value}")
            port = parsed.port
        except ValueError:
            return False
        expected_port = int(self.server.server_address[1])
        effective_port = 80 if port is None else port
        return (
            parsed.hostname in {"127.0.0.1", "localhost"}
            and effective_port == expected_port
        )

    def _request_authority_allowed(self) -> bool:
        if self._valid_authority(self.headers.get("Host")):
            return True
        self._error(HTTPStatus.MISDIRECTED_REQUEST, "invalid console host")
        return False

    def _post_origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlsplit(origin)
        authority = parsed.netloc
        if parsed.scheme == "http" and self._valid_authority(authority):
            return True
        self._error(HTTPStatus.FORBIDDEN, "invalid console origin")
        return False

    def _read_json_object(self, *, maximum_bytes: int = 8192) -> dict[str, object]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if content_length < 0 or content_length > maximum_bytes:
            raise ValueError("request body is too large")
        if content_length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _workspace_request(self, payload: dict[str, object]) -> tuple[Path, int]:
        root_value = payload.get("root")
        if not isinstance(root_value, str) or not root_value.strip():
            raise ValueError("workspace root is required")
        root_text = root_value.strip()
        if len(root_text) > 4096 or "\x00" in root_text:
            raise ValueError("workspace root is invalid")
        root = Path(root_text).expanduser()
        if not root.is_absolute():
            raise ValueError("workspace root must be absolute or start with ~")
        depth_value = payload.get("max_depth", self.server.config.max_depth)
        if isinstance(depth_value, bool) or not isinstance(depth_value, int):
            raise ValueError("max_depth must be an integer")
        if depth_value < 0 or depth_value > 20:
            raise ValueError("max depth must be between 0 and 20")
        return root, depth_value

    @staticmethod
    def _single(parameters: dict[str, list[str]], name: str) -> str | None:
        values = parameters.get(name, [])
        if len(values) > 1:
            raise ValueError(f"{name} must appear at most once")
        return values[0] if values else None

    def do_GET(self) -> None:  # noqa: N802
        if not self._request_authority_allowed():
            return
        try:
            self._handle_get()
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except (OSError, sqlite3.Error) as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def _handle_get(self) -> None:
        request = urlsplit(self.path)
        if request.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[request.path]
            self._send_bytes(
                HTTPStatus.OK,
                (STATIC_ROOT / filename).read_bytes(),
                content_type,
            )
            return
        if request.path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if not request.path.startswith("/api/v1/"):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        parameters = parse_qs(
            request.query,
            keep_blank_values=False,
            max_num_fields=24,
        )
        with ObserverStore(self.server.config.data_dir) as store:
            if request.path == "/api/v1/status":
                status = store.status()
                collector = self.server.collector.status()
                collector["pending_events"] = status["summary"]["pending_events"]
                status["collector"] = collector
                self._send_json(HTTPStatus.OK, status)
                return
            if request.path == "/api/v1/graph":
                since = self._single(parameters, "since") or "48h"
                workspace_id = self._single(parameters, "workspace_id")
                limit = bounded_limit(
                    self._single(parameters, "limit"), default=120
                )
                if limit < 10 or limit > 300:
                    raise ValueError("graph limit must be between 10 and 300")
                self._send_json(
                    HTTPStatus.OK,
                    build_collaboration_graph(
                        store.connection,
                        since=parse_since(since),
                        workspace_id=workspace_id,
                        limit=limit,
                    ),
                )
                return
            if request.path == "/api/v1/storyline":
                since = self._single(parameters, "since") or "48h"
                workspace_id = self._single(parameters, "workspace_id")
                if not workspace_id:
                    raise ValueError("storyline workspace_id is required")
                limit = bounded_limit(
                    self._single(parameters, "limit"), default=28
                )
                if limit < 8 or limit > 60:
                    raise ValueError("storyline limit must be between 8 and 60")
                self._send_json(
                    HTTPStatus.OK,
                    build_collaboration_storyline(
                        store.connection,
                        since=parse_since(since),
                        workspace_id=workspace_id,
                        limit=limit,
                    ),
                )
                return
            if request.path == "/api/v1/report":
                since = self._single(parameters, "since") or "48h"
                limit = bounded_limit(self._single(parameters, "limit"), default=10)
                if limit > 100:
                    raise ValueError("report limit must be between 1 and 100")
                self._send_json(
                    HTTPStatus.OK,
                    build_report(
                        store.connection,
                        since=parse_since(since),
                        limit=limit,
                    ),
                )
                return
            if request.path == "/api/v1/events":
                filters = {
                    name: self._single(parameters, name)
                    for name in (
                        "workspace_id",
                        "event",
                        "owner",
                        "run_id",
                        "handoff_id",
                        "scope",
                        "transaction_id",
                    )
                }
                limit = bounded_limit(self._single(parameters, "limit"), default=25)
                page = bounded_page(self._single(parameters, "page"))
                anchor = bounded_anchor(self._single(parameters, "anchor"))
                self._send_json(
                    HTTPStatus.OK,
                    event_page(
                        store.connection,
                        filters=filters,
                        limit=limit,
                        page=page,
                        anchor=anchor,
                    ),
                )
                return
            if request.path == "/api/v1/event":
                detail = event_detail(
                    store.connection,
                    workspace_id=self._single(parameters, "workspace_id"),
                    source_name=self._single(parameters, "source_name"),
                )
                if detail is None:
                    self._error(HTTPStatus.NOT_FOUND, "event not found")
                else:
                    self._send_json(HTTPStatus.OK, detail)
                return
            if request.path == "/api/v1/issues":
                limit = bounded_limit(self._single(parameters, "limit"))
                self._send_json(
                    HTTPStatus.OK,
                    {"issues": list_issues(store.connection, limit=limit)},
                )
                return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_authority_allowed() or not self._post_origin_allowed():
            return
        try:
            self._handle_post()
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except (OSError, sqlite3.Error) as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def _handle_post(self) -> None:
        request = urlsplit(self.path)
        if request.path not in {"/api/v1/collect", "/api/v1/workspaces"}:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if self.headers.get("X-Dev-Mesh-Console") != "1":
            self._error(HTTPStatus.FORBIDDEN, "console request header required")
            return
        payload = self._read_json_object()
        if request.path == "/api/v1/collect":
            result = self.server.collector.collect_now()
        else:
            root, max_depth = self._workspace_request(payload)
            result = self.server.collector.add_workspace(
                root,
                max_depth=max_depth,
            )
        self._send_json(HTTPStatus.OK, result)


def serve_console(
    *,
    data_dir: Path,
    host: str,
    port: int,
    max_depth: int,
    collect_interval: float,
) -> None:
    server = ObserverConsole(
        host,
        port,
        data_dir=data_dir,
        max_depth=max_depth,
        collect_interval=collect_interval,
    )
    print(f"Observer console: {server.url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
