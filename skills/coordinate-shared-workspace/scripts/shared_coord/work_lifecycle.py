"""Diagnostic waiting and diverted-work lifecycle for collaboration traces."""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from .event_contract import work_event_details
from .lifecycle import optional_correlation_id, require_joined_run
from .state import (
    claim_path,
    coordination_guard,
    emit_event,
    initialize,
    now,
    read_json,
    read_transaction,
    replace_json,
    require_text,
    string_list,
    validate_slug,
    write_json_exclusive,
)


WORK_DISPOSITIONS = {"diverted", "waiting"}


def _bounded_text(value: str, label: str, limit: int) -> str:
    normalized = require_text(value, label)
    if len(normalized) > limit:
        raise ValueError(f"{label} must be at most {limit} characters")
    return normalized


def _active_path(location: Path, scope: str) -> Path:
    return location / "work" / "active" / f"{scope}.json"


def _event_path(location: Path, value: object) -> Path | None:
    if not isinstance(value, str) or Path(value).name != value:
        return None
    path = location / "events" / value
    return path if path.is_file() else None


def _anchor(
    location: Path,
    scope: str,
    owner: str,
    transaction_id: str | None,
) -> dict[str, object]:
    if transaction_id is not None:
        _, transaction = read_transaction(location, transaction_id)
        if transaction.get("scope") != scope:
            raise ValueError(
                f"transaction scope is {transaction.get('scope')!r}, not {scope!r}"
            )
        if transaction.get("owner") != owner:
            raise ValueError(
                f"transaction belongs to {transaction.get('owner')!r}, not {owner!r}"
            )
        return transaction

    path = claim_path(location, scope)
    if not path.is_file():
        raise ValueError(
            f"active claim does not exist for {scope!r}; provide --transaction "
            "when the claim has been promoted"
        )
    claim = read_json(path)
    if claim.get("owner") != owner:
        raise ValueError(f"claim belongs to {claim.get('owner')!r}, not {owner!r}")
    return claim


def _optional_ids(arguments: argparse.Namespace) -> dict[str, str]:
    result: dict[str, str] = {}
    for argument, field, label in (
        (arguments.request, "request_id", "request id"),
        (arguments.contention, "contention_id", "contention id"),
        (arguments.transaction, "transaction_id", "transaction id"),
        (arguments.run, "run_id", "run id"),
        (arguments.alternate_run, "alternate_run_id", "alternate run id"),
    ):
        value = optional_correlation_id(argument, label)
        if value is not None:
            result[field] = value
    return result


def command_work_suspend(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    disposition = arguments.disposition
    if disposition not in WORK_DISPOSITIONS:
        raise ValueError(
            f"work disposition must be one of {', '.join(sorted(WORK_DISPOSITIONS))}"
        )
    reason = _bounded_text(arguments.reason, "work suspension reason", 1000)
    identifiers = _optional_ids(arguments)
    alternate_scope = (
        validate_slug(arguments.alternate_scope, "alternate scope")
        if arguments.alternate_scope
        else None
    )
    if disposition == "diverted" and not (
        alternate_scope or identifiers.get("alternate_run_id")
    ):
        raise ValueError(
            "diverted work requires --alternate-scope or --alternate-run"
        )
    if disposition == "waiting" and (
        alternate_scope or identifiers.get("alternate_run_id")
    ):
        raise ValueError("waiting work cannot declare an alternate task")
    if alternate_scope == scope:
        raise ValueError("alternate scope must differ from the suspended scope")

    blocked_by_owners = sorted(
        {validate_slug(value, "blocking owner") for value in arguments.blocked_by_owner}
    )
    blocked_by_scopes = sorted(
        {validate_slug(value, "blocking scope") for value in arguments.blocked_by_scope}
    )
    transaction_id = identifiers.get("transaction_id")
    anchor = _anchor(location, scope, owner, transaction_id)
    if "run_id" in identifiers:
        require_joined_run(location, identifiers["run_id"], owner)
    if "alternate_run_id" in identifiers:
        require_joined_run(location, identifiers["alternate_run_id"], owner)

    record: dict[str, object] = {
        "schema": 1,
        "trace_schema": 1,
        "work_state_id": validate_slug(
            f"work-{time.time_ns():x}"[-63:], "work state id"
        ),
        "scope": scope,
        "owner": owner,
        "disposition": disposition,
        "reason": reason,
        "blocked_by_owners": blocked_by_owners,
        "blocked_by_scopes": blocked_by_scopes,
        "paths": string_list(anchor, "paths"),
        "semantic_resources": sorted(
            set(string_list(anchor, "semantic_writes"))
            | set(string_list(anchor, "sensitive_to"))
        ),
        "authority_effect": "none",
        "sequence": time.time_ns(),
        "suspended_at": now(),
        **identifiers,
    }
    if alternate_scope is not None:
        record["alternate_scope"] = alternate_scope

    path = _active_path(location, scope)
    with coordination_guard(location, "work-suspend"):
        if path.exists():
            existing = read_json(path)
            comparable = (
                "scope",
                "owner",
                "disposition",
                "reason",
                "blocked_by_owners",
                "blocked_by_scopes",
                "request_id",
                "contention_id",
                "transaction_id",
                "run_id",
                "alternate_scope",
                "alternate_run_id",
            )
            if any(existing.get(key) != record.get(key) for key in comparable):
                raise ValueError(f"scope already has active work disposition: {scope}")
            prior = _event_path(location, existing.get("suspended_event"))
            if prior is not None:
                print(prior)
                return 0
            record = existing
        else:
            write_json_exclusive(path, record)
        event = emit_event(
            location,
            "work-suspended",
            transaction_id,
            work_event_details(record, authority_effect="none"),
        )
        record["suspended_event"] = event.name
        replace_json(path, record)
    print(event)
    return 0


def _matching_archive(
    location: Path,
    scope: str,
    owner: str,
    evidence: str,
) -> Path | None:
    for path in sorted(
        (location / "work" / "archive").glob(f"*-{scope}.json"),
        reverse=True,
    ):
        record = read_json(path)
        if record.get("owner") == owner and record.get("resume_evidence") == evidence:
            return path
    return None


def command_work_resume(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    scope = validate_slug(arguments.scope, "scope")
    owner = validate_slug(arguments.owner, "owner")
    evidence = _bounded_text(arguments.evidence, "work resume evidence", 1000)
    path = _active_path(location, scope)
    with coordination_guard(location, "work-resume"):
        if not path.is_file():
            archived = _matching_archive(location, scope, owner, evidence)
            if archived is None:
                raise ValueError(f"scope has no active work disposition: {scope}")
            record = read_json(archived)
            prior = _event_path(location, record.get("resumed_event"))
            print(prior or archived)
            return 0

        record = read_json(path)
        if record.get("owner") != owner:
            raise ValueError(
                f"work disposition belongs to {record.get('owner')!r}, not {owner!r}"
            )
        prior_evidence = record.get("resume_evidence")
        if prior_evidence is not None and prior_evidence != evidence:
            raise ValueError("work resumption is already recorded with different evidence")
        record["resume_evidence"] = evidence
        record["resumed_at"] = now()
        sequence = record.get("sequence")
        if isinstance(sequence, int):
            record["suspension_duration_ms"] = max(
                0, (time.time_ns() - sequence) // 1_000_000
            )
        replace_json(path, record)
        prior = _event_path(location, record.get("resumed_event"))
        if prior is None:
            prior = emit_event(
                location,
                "work-resumed",
                (
                    str(record["transaction_id"])
                    if isinstance(record.get("transaction_id"), str)
                    else None
                ),
                work_event_details(
                    record,
                    authority_effect="none",
                    resume_evidence=evidence,
                    suspension_duration_ms=record.get("suspension_duration_ms"),
                ),
            )
            record["resumed_event"] = prior.name
            replace_json(path, record)
        archive = (
            location
            / "work"
            / "archive"
            / f"{time.time_ns()}-{scope}.json"
        )
        shutil.move(path, archive)
    print(prior)
    return 0
