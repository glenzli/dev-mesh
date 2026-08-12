"""Stable control-plane failures and CLI serialization."""

from __future__ import annotations

import errno
import json


STABLE_ERROR_CODES = frozenset(
    {
        "namespace_missing",
        "legacy_cutover_required",
        "split_brain",
        "unsupported_protocol",
        "marker_invalid",
        "state_missing",
        "stale_writer",
        "cutover_facts_changed",
        "cutover_confirmation_required",
        "git_fact_unavailable",
        "legacy_invalid",
        "lock_busy",
        "permission_denied",
        "read_only_filesystem",
        "missing_path",
        "already_exists",
        "os_error",
        "operation_failed",
    }
)


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        if code not in STABLE_ERROR_CODES:
            raise ValueError(f"unregistered stable protocol error code: {code}")
        super().__init__(message)
        self.code = code


def error_json(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if code is None and isinstance(error, TimeoutError):
        code = "lock_busy"
    if code is None and isinstance(error, OSError):
        code = {
            errno.EACCES: "permission_denied",
            errno.EPERM: "permission_denied",
            errno.EROFS: "read_only_filesystem",
            errno.ENOENT: "missing_path",
            errno.EEXIST: "already_exists",
        }.get(error.errno, "os_error")
    if code is None:
        code = "operation_failed"
    if code not in STABLE_ERROR_CODES:
        code = "operation_failed"
    return json.dumps(
        {
            "error": {
                "code": code,
                "message": str(error),
            }
        },
        ensure_ascii=False,
        sort_keys=True,
    )
