"""Semantic projection of coordination facts that involve distinct work actors."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


CONTENTION_EVENTS = {
    "claim-requested",
    "contention-opened",
    "contention-coordinator-acquired",
    "contention-decision-proposed",
    "contention-decision-responded",
    "contention-completed",
    "contention-cancelled",
}

TRANSACTION_EVENTS = {
    "transaction-created",
    "transaction-handed-off",
    "transaction-prepared",
    "transaction-validated",
    "transaction-refreshed",
    "transaction-conflicted",
    "transaction-published",
    "transaction-aborted",
}

INTERACTION_EVENTS = {
    "message-sent",
    "message-acknowledged",
    "handoff-offered",
    "handoff-accepted",
    "handoff-rejected",
    "handoff-withdrawn",
}

DEPENDENCY_EVENTS = {"work-suspended", "work-resumed"}
RECOVERY_EVENTS = {"run-authority-recovered"}

COORDINATION_CANDIDATE_EVENTS = frozenset(
    CONTENTION_EVENTS
    | TRANSACTION_EVENTS
    | INTERACTION_EVENTS
    | DEPENDENCY_EVENTS
    | RECOVERY_EVENTS
)


def _identity(owner: object, run_id: object) -> tuple[str, str] | None:
    if not isinstance(owner, str) or not isinstance(run_id, str):
        return None
    return owner, run_id


def participant_identities(record: dict[str, object]) -> set[tuple[str, str]]:
    participants = record.get("contention_participants", record.get("participants", []))
    if not isinstance(participants, list):
        return set()
    return {
        identity
        for item in participants
        if isinstance(item, dict)
        for identity in [_identity(item.get("owner"), item.get("run_id"))]
        if identity is not None
    }


def has_distinct_participants(record: dict[str, object]) -> bool:
    return len(participant_identities(record)) > 1


def _interaction_identities(record: dict[str, object]) -> set[tuple[str, str]]:
    result = {
        identity
        for identity in (
            _identity(record.get("source_owner"), record.get("source_run_id")),
            _identity(record.get("target_owner"), record.get("target_run_id")),
        )
        if identity is not None
    }
    return result


def is_distinct_interaction(record: dict[str, object]) -> bool:
    source_owner = record.get("source_owner")
    target_owner = record.get("target_owner")
    if not isinstance(source_owner, str) or not isinstance(target_owner, str):
        return False
    if source_owner != target_owner:
        return True
    return len(_interaction_identities(record)) > 1


def is_collaboration_record(
    event_name: str,
    record: dict[str, object],
    *,
    true_contention: bool = False,
    include_cross_project: bool = True,
) -> bool:
    """Classify one event by cross-actor causality rather than lifecycle volume."""

    if event_name in CONTENTION_EVENTS | TRANSACTION_EVENTS:
        return true_contention or has_distinct_participants(record)
    if event_name in INTERACTION_EVENTS:
        if not include_cross_project and record.get("cross_project_phase") is not None:
            return False
        return is_distinct_interaction(record)
    if event_name in DEPENDENCY_EVENTS:
        if true_contention:
            return True
        blocked_by_owner = record.get("blocked_by_owner")
        return isinstance(blocked_by_owner, str) and blocked_by_owner != record.get("owner")
    if event_name in RECOVERY_EVENTS:
        source = record.get("source_run_id")
        target = record.get("run_id")
        return isinstance(source, str) and isinstance(target, str) and source != target
    return False


def project_coordination_events(events: Iterable[dict[str, object]]) -> dict[str, object]:
    """Keep only events that explain a relation between distinct Runs or owners."""

    values = list(events)
    true_contentions = {
        (
            str(event.get("workspace_id")),
            str(
                event.get("contention_id")
                or (
                    event.get("details", {}).get("contention_id")
                    if isinstance(event.get("details"), dict)
                    else None
                )
            ),
        )
        for event in values
        if (
            event.get("contention_id")
            or (
                event.get("details", {}).get("contention_id")
                if isinstance(event.get("details"), dict)
                else None
            )
        )
        and has_distinct_participants(
            event.get("details") if isinstance(event.get("details"), dict) else {}
        )
    }
    selected: list[dict[str, object]] = []
    relations: set[tuple[str, str, str]] = set()
    identities: set[tuple[str, str, str]] = set()
    by_workspace: Counter[str] = Counter()

    for event in values:
        details = event.get("details")
        record = dict(details) if isinstance(details, dict) else {}
        record.setdefault("owner", event.get("owner"))
        record.setdefault("run_id", event.get("run_id"))
        contention_key = (
            str(event.get("workspace_id")),
            str(event.get("contention_id") or record.get("contention_id")),
        )
        if not is_collaboration_record(
            str(event.get("event")),
            record,
            true_contention=contention_key in true_contentions,
            include_cross_project=False,
        ):
            continue
        selected.append(event)
        workspace_id = str(event.get("workspace_id"))
        by_workspace[workspace_id] += 1

        relation_kind = "event"
        relation_id = str(event.get("event_id"))
        for kind, field in (
            ("contention", "contention_id"),
            ("transaction", "transaction_id"),
            ("handoff", "handoff_id"),
            ("message", "message_id"),
            ("dependency", "work_state_id"),
        ):
            value = event.get(field) or record.get(field)
            if isinstance(value, str):
                relation_kind, relation_id = kind, value
                break
        relations.add((workspace_id, relation_kind, relation_id))

        event_identity = _identity(event.get("owner"), event.get("run_id"))
        related = participant_identities(record) | _interaction_identities(record)
        if event_identity is not None:
            related.add(event_identity)
        identities.update((workspace_id, owner, run_id) for owner, run_id in related)

    return {
        "events": selected,
        "event_count": len(selected),
        "relation_count": len(relations),
        "participant_count": len(identities),
        "participant_identities": [
            {"workspace_id": workspace_id, "owner": owner, "run_id": run_id}
            for workspace_id, owner, run_id in sorted(identities)
        ],
        "event_counts_by_workspace": dict(sorted(by_workspace.items())),
        "relation_counts_by_workspace": dict(
            sorted(
                Counter(
                    workspace_id
                    for workspace_id, _kind, _identifier in relations
                ).items()
            )
        ),
    }
