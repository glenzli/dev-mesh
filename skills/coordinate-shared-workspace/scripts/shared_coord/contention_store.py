"""Durable contention record identity, lookup, and participant projection."""

from __future__ import annotations

import time
from pathlib import Path

from .state import read_json, string_list, validate_slug


def make_contention_id() -> str:
    return validate_slug(f"contention-{time.time_ns():x}"[-63:], "contention id")


def contention_path(location: Path, contention_id: str) -> Path:
    return (
        location
        / "contentions"
        / "active"
        / f"{validate_slug(contention_id, 'contention id')}.json"
    )


def active_contentions(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return sorted(
        (
            (path, read_json(path))
            for path in (location / "contentions" / "active").glob("*.json")
        ),
        key=lambda item: (int(item[1].get("sequence", 0)), item[0].name),
    )


def read_contention(
    location: Path,
    contention_id: str,
) -> tuple[Path, dict[str, object]]:
    path = contention_path(location, contention_id)
    if not path.exists():
        raise ValueError(f"active contention does not exist: {contention_id}")
    return path, read_json(path)


def participant_scopes(record: dict[str, object]) -> list[str]:
    return string_list(record, "scopes")


def participant_owners(record: dict[str, object]) -> list[str]:
    participants = record.get("participants", [])
    if not isinstance(participants, list):
        raise ValueError("contention participants are malformed")
    owners: list[str] = []
    for participant in participants:
        if not isinstance(participant, dict) or not isinstance(
            participant.get("owner"), str
        ):
            raise ValueError("contention participant is malformed")
        owner = str(participant["owner"])
        if owner not in owners:
            owners.append(owner)
    return sorted(owners)
