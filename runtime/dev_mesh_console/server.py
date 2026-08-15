"""Loopback-only HTTP surface for the Dev Mesh Console."""

from __future__ import annotations

import ipaddress
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .state import ConsoleState


MAX_REQUEST_BYTES = 4096
STATIC_ROOT = Path(__file__).with_name("web")


def require_loopback_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Console host must be a literal loopback address") from error
    if not address.is_loopback:
        raise ValueError("Console host must be a loopback address")
    return host


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, state: ConsoleState):
        require_loopback_host(host)
        self.state = state
        self.expected_hosts: set[str] = set()
        super().__init__((host, port), ConsoleHandler)
        actual_port = int(self.server_address[1])
        self.expected_hosts = {
            f"127.0.0.1:{actual_port}",
            f"[::1]:{actual_port}",
        }

    def server_close(self) -> None:
        self.state.close()
        super().server_close()


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer

    def log_message(self, format: str, *arguments: object) -> None:
        return

    def _headers(self, *, content_type: str, length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, value: object, *, status: int = 200) -> None:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._headers(content_type="application/json; charset=utf-8", length=len(encoded), status=status)
        self.wfile.write(encoded)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message}}, status=status)

    def _same_origin(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in self.server.expected_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{item}" for item in self.server.expected_hosts}

    def _body(self) -> dict[str, object]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if size < 0 or size > MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds 4096 bytes")
        encoded = self.rfile.read(size)
        try:
            value = json.loads(encoded or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be a JSON object") from error
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        if not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "origin_rejected", "request origin is not allowed")
            return
        target = urlsplit(self.path)
        if target.path == "/api/dashboard":
            try:
                query = parse_qs(target.query, keep_blank_values=False)
                workspace = query.get("workspace", [None])[0]
                window = int(query.get("window", ["48"])[0])
                limit = int(query.get("limit", ["240"])[0])
                value = self.server.state.dashboard(
                    workspace=workspace,
                    window_hours=window,
                    event_limit=limit,
                )
            except (ValueError, OSError) as error:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_query", str(error))
                return
            self._json(value)
            return
        if target.path == "/api/health":
            self._json({"status": "ok", "collector": self.server.state.status()})
            return
        if target.path == "/api/roots":
            self._json({"roots": [str(item) for item in self.server.state.registry.roots()]})
            return
        self._static(target.path)

    def do_POST(self) -> None:
        if not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "origin_rejected", "request origin is not allowed")
            return
        target = urlsplit(self.path)
        try:
            value = self._body()
            if target.path == "/api/collect":
                if value:
                    raise ValueError("collect request does not accept fields")
                self._json(self.server.state.collect())
                return
            if target.path == "/api/roots":
                if set(value) != {"path"} or not isinstance(value.get("path"), str):
                    raise ValueError("root request requires exactly one string path")
                roots = self.server.state.registry.add(str(value["path"]))
                result = self.server.state.collect()
                self._json({"roots": [str(item) for item in roots], "collection": result})
                return
            if target.path == "/api/actions/run-close/preview":
                if set(value) != {"workspace_id", "run_id"} or not all(
                    isinstance(value.get(field), str) for field in value
                ):
                    raise ValueError("run close preview requires workspace_id and run_id")
                self._json(
                    self.server.state.preview_run_close(
                        workspace_id=str(value["workspace_id"]),
                        run_id=str(value["run_id"]),
                    )
                )
                return
            if target.path == "/api/actions/run-close":
                required = {
                    "workspace_id",
                    "run_id",
                    "review_token",
                    "reviewer",
                    "outcome",
                    "reason_code",
                    "evidence",
                }
                if set(value) != required or not all(
                    isinstance(value.get(field), str) for field in required
                ):
                    raise ValueError("reviewed run close request has missing or unsupported fields")
                self._json(
                    self.server.state.close_run_after_review(
                        workspace_id=str(value["workspace_id"]),
                        run_id=str(value["run_id"]),
                        review_token=str(value["review_token"]),
                        reviewer=str(value["reviewer"]),
                        outcome=str(value["outcome"]),
                        reason_code=str(value["reason_code"]),
                        evidence=str(value["evidence"]),
                    )
                )
                return
        except RuntimeError as error:
            self._error(HTTPStatus.CONFLICT, "collection_busy", str(error))
            return
        except (ValueError, OSError) as error:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "unknown API route")

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (STATIC_ROOT / relative).resolve()
        try:
            candidate.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "unknown asset")
            return
        if candidate.is_symlink() or not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not_found", "unknown asset")
            return
        encoded = candidate.read_bytes()
        media = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if media.startswith("text/") or media in {"application/javascript", "application/json"}:
            media += "; charset=utf-8"
        self._headers(content_type=media, length=len(encoded))
        self.wfile.write(encoded)
