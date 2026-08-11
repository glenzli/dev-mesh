"""Read bounded mutable coordination state without importing the coordinator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


MAX_STATE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StateIssue:
    source_name: str
    kind: str
    detail: str
    observed_digest: str = ""


@dataclass(frozen=True)
class ActiveContention:
    source_name: str
    digest: str
    payload_json: str
    record: dict[str, object]


@dataclass(frozen=True)
class ActiveContentionScan:
    records: tuple[ActiveContention, ...]
    issues: tuple[StateIssue, ...]
    complete: bool


def scan_active_contentions(coordination_path: Path) -> ActiveContentionScan:
    """Read one fixed state directory; never follow links or broaden the source root."""

    active_path = coordination_path / "contentions" / "active"
    if not active_path.exists():
        return ActiveContentionScan((), (), True)
    if active_path.is_symlink() or not active_path.is_dir():
        return ActiveContentionScan(
            (),
            (
                StateIssue(
                    "contentions/active",
                    "unsafe-state-source",
                    "Active contention source must be a non-symlink directory",
                ),
            ),
            False,
        )

    records: list[ActiveContention] = []
    issues: list[StateIssue] = []
    for path in sorted(active_path.glob("*.json")):
        source_name = f"contentions/active/{path.name}"
        if path.is_symlink() or not path.is_file():
            issues.append(
                StateIssue(
                    source_name,
                    "unsafe-state-source",
                    "Active contention must be a regular non-symlink file",
                )
            )
            continue
        try:
            size = path.stat().st_size
            if size > MAX_STATE_BYTES:
                issues.append(
                    StateIssue(
                        source_name,
                        "state-too-large",
                        f"State is {size} bytes; limit is {MAX_STATE_BYTES}",
                    )
                )
                continue
            raw = path.read_bytes()
        except OSError as error:
            issues.append(
                StateIssue(source_name, "state-read-failed", str(error))
            )
            continue
        digest = hashlib.sha256(raw).hexdigest()
        try:
            decoded = raw.decode("utf-8")
            record = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            issues.append(
                StateIssue(
                    source_name,
                    "malformed-state",
                    str(error),
                    digest,
                )
            )
            continue
        if not isinstance(record, dict) or not isinstance(
            record.get("contention_id"), str
        ):
            issues.append(
                StateIssue(
                    source_name,
                    "malformed-state",
                    "Active contention must be an object with a contention_id",
                    digest,
                )
            )
            continue
        records.append(ActiveContention(source_name, digest, decoded, record))
    return ActiveContentionScan(tuple(records), tuple(issues), not issues)
