"""Defensive owner-aware projection for a Git collaboration storyline."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping


CLAIM_EVENTS = {
    "claim-created",
    "claim-updated",
    "claim-paused",
    "claim-resumed",
    "claim-released",
}
TRANSACTION_STATUS = {
    "transaction-recorded": "observed",
    "materialize-started": "materializing",
    "materialize-completed": "materialized",
    "transaction-activated": "active",
    "transaction-handed-off": "paused",
    "transaction-resumed": "active",
    "transaction-prepared": "prepared",
    "transaction-validated": "ready",
    "refresh-started": "refreshing",
    "refresh-conflicted": "conflicted",
    "refresh-completed": "prepared",
    "publish-started": "publishing",
    "publish-completed": "published",
    "transaction-aborted": "aborted",
}
TRANSACTION_CHECKPOINTS = {
    "transaction-prepared": "prepared",
    "transaction-validated": "validated",
    "refresh-started": "refresh",
    "refresh-conflicted": "conflict",
    "refresh-completed": "refreshed",
    "publish-started": "publishing",
}
IMPORTANT_STATUSES = {
    "active",
    "blocked",
    "conflicted",
    "paused",
    "publishing",
    "stalled",
    "waiting",
}
FOCUS_KIND_PRIORITY = {
    "contention": 6,
    "waits-for": 6,
    "blocked": 6,
    "reassigned": 5,
    "handoff": 5,
    "fork": 4,
    "rejoin": 4,
    "diverts-to": 3,
    "message": 2,
}
FOCUS_STATUS_BONUS = {
    "stalled": 4,
    "conflicted": 4,
    "blocked": 3,
    "waiting": 3,
    "active": 2,
}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _timestamp(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _quality(payload: Mapping[str, object], *, owner: str | None) -> str:
    if payload.get("trace_schema") == 1:
        return "native"
    return "derived" if owner is not None else "legacy-unknown"


def _bounded_details(payload: Mapping[str, object]) -> dict[str, object]:
    details: dict[str, object] = {}
    for field in (
        "actor_owner",
        "alternate_run_id",
        "alternate_scope",
        "base_revision",
        "branch",
        "candidate",
        "canonical_branch",
        "contention_id",
        "decision_revision",
        "disposition",
        "group_id",
        "handoff_id",
        "intent",
        "message_id",
        "mode",
        "outcome",
        "reason",
        "recommendation",
        "request_id",
        "request_status",
        "resume_evidence",
        "run_id",
        "scope",
        "source_owner",
        "steward",
        "summary",
        "target_owner",
        "transaction_id",
        "work_owner",
        "work_state_id",
    ):
        value = payload.get(field)
        if value is not None and value != "":
            details[field] = value
    for field in (
        "actual_paths",
        "blocked_by_owners",
        "blocked_by_scopes",
        "blocker_refs",
        "conflicts",
        "owners",
        "paths",
        "scopes",
        "semantic_resources",
        "source_events",
    ):
        value = payload.get(field)
        if isinstance(value, list) and value:
            details[field] = value[:40]
    return details


class StorylineProjection:
    """Build spans and relations without inventing owner or Git causality."""

    def __init__(self, workspace_id: str, workspace_root: str | None) -> None:
        self.workspace_id = workspace_id
        self.workspace_root = workspace_root
        self.spans: dict[str, dict[str, Any]] = {}
        self.markers: dict[str, dict[str, Any]] = {}
        self.relations: dict[str, dict[str, Any]] = {}
        self.claims: dict[tuple[str, str], str] = {}
        self.claim_episodes: defaultdict[tuple[str, str], int] = defaultdict(int)
        self.runs: dict[str, str] = {}
        self.transactions: dict[str, dict[str, Any]] = {}
        self.work_states: dict[str, str] = {}
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.contentions: dict[str, str] = {}
        self.canonical_markers: dict[tuple[str, str], str] = {}
        self.first_activity: dict[str, str] = {}
        self.trace_native = 0
        self.trace_derived = 0
        self.trace_unknown = 0

    def _record_quality(self, quality: str) -> None:
        if quality == "native":
            self.trace_native += 1
        elif quality == "derived":
            self.trace_derived += 1
        else:
            self.trace_unknown += 1

    def _activity(self, owner: str, at: str | None) -> None:
        if owner in {"__canonical__", "__system__"} or at is None:
            return
        current = self.first_activity.get(owner)
        if current is None or at < current:
            self.first_activity[owner] = at

    def add_span(
        self,
        identifier: str,
        *,
        owner: str,
        kind: str,
        label: str,
        at: str | None,
        status: str,
        quality: str,
        source_name: str,
        run_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        span = self.spans.get(identifier)
        if span is None:
            span = {
                "id": identifier,
                "owner": owner,
                "kind": kind,
                "label": label,
                "status": status,
                "started_at": at,
                "ended_at": None,
                "trace_quality": quality,
                "run_id": run_id,
                "run_ids": [run_id] if run_id else [],
                "run_binding": "native" if run_id else "unbound",
                "event_count": 0,
                "details": {"source_events": []},
            }
            self.spans[identifier] = span
        elif run_id:
            run_ids = set(span.get("run_ids", []))
            run_ids.add(run_id)
            span["run_ids"] = sorted(run_ids)
            if span.get("run_binding") != "native" or len(run_ids) > 1:
                span["run_id"] = None
                span["run_binding"] = "mixed"
        if at and (span["started_at"] is None or at < span["started_at"]):
            span["started_at"] = at
        span["status"] = status
        span["event_count"] += 1
        sources = span["details"].setdefault("source_events", [])
        if source_name not in sources and len(sources) < 40:
            sources.append(source_name)
        if details:
            for key, value in details.items():
                if value is not None and value != "" and value != []:
                    span["details"][key] = value
        self._activity(owner, at)
        return span

    def close_span(
        self,
        identifier: str,
        at: str | None,
        *,
        status: str | None = None,
    ) -> None:
        span = self.spans.get(identifier)
        if span is None:
            return
        if at and (span["ended_at"] is None or at > span["ended_at"]):
            span["ended_at"] = at
        if status is not None:
            span["status"] = status

    def add_marker(
        self,
        identifier: str,
        *,
        lane: str,
        kind: str,
        label: str,
        at: str | None,
        status: str,
        quality: str,
        source_name: str,
        owners: Iterable[str] = (),
        details: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        marker = self.markers.setdefault(
            identifier,
            {
                "id": identifier,
                "lane": lane,
                "kind": kind,
                "label": label,
                "at": at,
                "status": status,
                "trace_quality": quality,
                "owners": [],
                "event_count": 0,
                "details": {"source_events": []},
            },
        )
        marker["status"] = status
        marker["event_count"] += 1
        marker["owners"] = sorted(set(marker["owners"]) | set(owners))
        sources = marker["details"].setdefault("source_events", [])
        if source_name not in sources and len(sources) < 40:
            sources.append(source_name)
        if details:
            for key, value in details.items():
                if value is not None and value != "" and value != []:
                    marker["details"][key] = value
        self._activity(lane, at)
        for owner in marker["owners"]:
            self._activity(owner, at)
        return marker

    def add_relation(
        self,
        identifier: str,
        *,
        kind: str,
        at: str | None,
        label: str,
        status: str,
        evidence: str,
        source_owner: str | None = None,
        target_owner: str | None = None,
        owners: Iterable[str] = (),
        source_item: str | None = None,
        target_item: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        relation = {
            "id": identifier,
            "kind": kind,
            "at": at,
            "label": label,
            "status": status,
            "evidence": evidence,
            "source_owner": source_owner,
            "target_owner": target_owner,
            "owners": sorted(set(owners)),
            "source_item": source_item,
            "target_item": target_item,
            "details": dict(details or {}),
        }
        self.relations[identifier] = relation
        for owner in relation["owners"]:
            self._activity(owner, at)
        if source_owner:
            self._activity(source_owner, at)
        if target_owner:
            self._activity(target_owner, at)

    def canonical_checkpoint(
        self,
        *,
        branch: str,
        revision: str,
        at: str | None,
        kind: str,
        source_name: str,
        quality: str,
    ) -> str:
        key = (branch, revision)
        existing = self.canonical_markers.get(key)
        if existing is not None:
            return existing
        identifier = f"canonical:{self.workspace_id}:{branch}:{revision}"
        self.canonical_markers[key] = identifier
        self.add_marker(
            identifier,
            lane="__canonical__",
            kind=kind,
            label=revision[:12],
            at=at,
            status="published" if kind == "publish" else "observed",
            quality=quality,
            source_name=source_name,
            details={"canonical_branch": branch, "revision": revision},
        )
        return identifier

    def consume(
        self,
        *,
        event: str,
        at: str | None,
        source_name: str,
        payload: Mapping[str, object],
        row_owner: str | None,
        row_scope: str | None,
        row_run_id: str | None,
        row_handoff_id: str | None,
        row_contention_id: str | None,
        row_transaction_id: str | None,
    ) -> None:
        owner = _text(payload.get("work_owner")) or row_owner or _text(
            payload.get("owner")
        )
        scope = row_scope or _text(payload.get("scope"))
        run_id = row_run_id or _text(payload.get("run_id"))
        handoff_id = row_handoff_id or _text(payload.get("handoff_id"))
        contention_id = row_contention_id or _text(payload.get("contention_id"))
        transaction_id = row_transaction_id or _text(payload.get("transaction_id"))
        quality = _quality(payload, owner=owner)
        self._record_quality(quality)
        details = _bounded_details(payload)

        if event == "coordinator-initialized":
            branch = _text(payload.get("canonical_branch"))
            revision = _text(payload.get("base_revision"))
            if branch and revision:
                self.canonical_checkpoint(
                    branch=branch,
                    revision=revision,
                    at=at,
                    kind="canonical",
                    source_name=source_name,
                    quality=quality,
                )

        if event in {"agent-joined", "agent-left"} and run_id and owner:
            identifier = self.runs.setdefault(
                run_id, f"run:{self.workspace_id}:{run_id}"
            )
            label = _text(payload.get("task_summary")) or run_id
            session = self.add_span(
                identifier,
                owner=owner,
                kind="session",
                label=label,
                at=at,
                status="active" if event == "agent-joined" else "completed",
                quality=quality,
                source_name=source_name,
                run_id=run_id,
                details=details,
            )
            if event == "agent-joined":
                session["observed_join"] = True
            if event == "agent-left":
                self.close_span(identifier, at, status="completed")

        if event in CLAIM_EVENTS and owner and scope:
            key = (owner, scope)
            identifier = self.claims.get(key)
            if identifier is None:
                self.claim_episodes[key] += 1
                identifier = (
                    f"claim:{self.workspace_id}:{owner}:{scope}:"
                    f"{self.claim_episodes[key]}"
                )
                self.claims[key] = identifier
            status = "active"
            if event == "claim-paused":
                status = "paused"
            elif event == "claim-released":
                status = "completed"
            claim = self.add_span(
                identifier,
                owner=owner,
                kind="claim",
                label=scope,
                at=at,
                status=status,
                quality=quality,
                source_name=source_name,
                run_id=run_id,
                details=details,
            )
            claim.setdefault("first_visible_event", event)
            if event == "claim-released":
                self.close_span(identifier, at, status="completed")
                self.claims.pop(key, None)

        if event in {"work-suspended", "work-resumed"} and owner and scope:
            work_id = _text(payload.get("work_state_id")) or (
                f"legacy-work:{owner}:{scope}:{source_name}"
            )
            identifier = self.work_states.setdefault(
                work_id, f"work:{self.workspace_id}:{work_id}"
            )
            disposition = _text(payload.get("disposition")) or "waiting"
            label = (
                _text(payload.get("alternate_scope"))
                if disposition == "diverted"
                else scope
            ) or scope
            self.add_span(
                identifier,
                owner=owner,
                kind=disposition,
                label=label,
                at=at,
                status=(
                    "completed" if event == "work-resumed" else disposition
                ),
                quality=quality,
                source_name=source_name,
                run_id=run_id,
                details=details,
            )
            if event == "work-suspended":
                blocked_owners = _strings(payload.get("blocked_by_owners"))
                for blocker in blocked_owners:
                    self.add_relation(
                        f"wait:{work_id}:{blocker}",
                        kind="waits-for",
                        at=at,
                        label="waiting" if disposition == "waiting" else "diverted",
                        status=disposition,
                        evidence="blocked_by_owners",
                        source_owner=owner,
                        target_owner=blocker,
                        source_item=identifier,
                        details=details,
                    )
                if disposition == "diverted":
                    self.add_relation(
                        f"divert:{work_id}",
                        kind="diverts-to",
                        at=at,
                        label=label,
                        status="diverted",
                        evidence="alternate_scope|alternate_run_id",
                        source_owner=owner,
                        target_owner=owner,
                        source_item=identifier,
                        details=details,
                    )
            else:
                self.close_span(identifier, at, status="completed")

        if transaction_id and event in TRANSACTION_STATUS:
            self._consume_transaction(
                transaction_id=transaction_id,
                event=event,
                at=at,
                source_name=source_name,
                payload=payload,
                owner=owner,
                quality=quality,
                details=details,
            )

        if event == "message-sent" and owner:
            target = _text(payload.get("target_owner")) or scope
            if target:
                message_id = _text(payload.get("message_id")) or source_name
                self.add_relation(
                    f"message:{self.workspace_id}:{message_id}",
                    kind="message",
                    at=at,
                    label=_text(payload.get("message_type")) or "message",
                    status="sent",
                    evidence="owner+scope",
                    source_owner=owner,
                    target_owner=target,
                    details=details,
                )

        if handoff_id and event in {"handoff-offered", "handoff-accepted"}:
            source_owner = _text(payload.get("source_owner"))
            target_owner = _text(payload.get("target_owner"))
            record = self.handoffs.setdefault(handoff_id, {})
            if source_owner:
                record["source_owner"] = source_owner
            if target_owner:
                record["target_owner"] = target_owner
            if event == "handoff-accepted":
                record["status"] = "accepted"
            source_owner = _text(record.get("source_owner"))
            target_owner = _text(record.get("target_owner"))
            if source_owner and target_owner:
                self.add_relation(
                    f"handoff:{self.workspace_id}:{handoff_id}",
                    kind="handoff",
                    at=at,
                    label="handoff",
                    status=str(record.get("status", "offered")),
                    evidence="source_owner+target_owner+handoff_id",
                    source_owner=source_owner,
                    target_owner=target_owner,
                    details=details,
                )

        if contention_id and event.startswith("contention-"):
            owners = _strings(payload.get("owners"))
            if owner and owner not in owners:
                owners.append(owner)
            identifier = self.contentions.setdefault(
                contention_id,
                f"contention:{self.workspace_id}:{contention_id}",
            )
            status = "completed" if event == "contention-completed" else "active"
            marker = self.add_marker(
                identifier,
                lane="__canonical__",
                kind="contention",
                label=_text(payload.get("mode"))
                or _text(payload.get("recommendation"))
                or contention_id,
                at=at,
                status=status,
                quality=quality,
                source_name=source_name,
                owners=owners,
                details=details,
            )
            if event in {
                "contention-decision-proposed",
                "contention-decision-accepted",
                "contention-decision-rejected",
                "contention-decision-invalidated",
                "contention-enacted",
            }:
                marker["kind"] = "decision"
                marker["status"] = event.removeprefix("contention-decision-")
                if event == "contention-enacted":
                    marker["status"] = "enacted"
            self.add_relation(
                f"intersection:{self.workspace_id}:{contention_id}",
                kind="contention",
                at=marker["at"],
                label=marker["label"],
                status=marker["status"],
                evidence="contention_id+owners",
                owners=owners,
                target_item=identifier,
                details=details,
            )

        if event == "queue-blocked":
            affected = _strings(payload.get("owners"))
            blocker_refs = payload.get("blocker_refs")
            if isinstance(blocker_refs, list):
                for index, blocker in enumerate(blocker_refs):
                    if not isinstance(blocker, dict):
                        continue
                    blocker_owner = _text(blocker.get("owner"))
                    blocker_owners = _strings(blocker.get("owners"))
                    if blocker_owner:
                        blocker_owners.append(blocker_owner)
                    for affected_owner in affected:
                        for target in sorted(set(blocker_owners)):
                            self.add_relation(
                                f"blocked:{source_name}:{index}:{affected_owner}:{target}",
                                kind="blocked",
                                at=at,
                                label="blocked",
                                status="blocked",
                                evidence="queue-blocker-ref",
                                source_owner=affected_owner,
                                target_owner=target,
                                details=details,
                            )

    def _consume_transaction(
        self,
        *,
        transaction_id: str,
        event: str,
        at: str | None,
        source_name: str,
        payload: Mapping[str, object],
        owner: str | None,
        quality: str,
        details: Mapping[str, object],
    ) -> None:
        state = self.transactions.setdefault(
            transaction_id,
            {"segment": 0, "current": None, "forked": False},
        )
        source_owner = _text(payload.get("source_owner")) or _text(
            payload.get("from")
        )
        target_owner = _text(payload.get("target_owner")) or _text(
            payload.get("to")
        )
        resolved_owner = owner or source_owner or "__system__"
        current_id = state.get("current")
        if event == "transaction-handed-off" and source_owner and target_owner:
            if not isinstance(current_id, str):
                current_id = self._new_transaction_segment(
                    transaction_id,
                    source_owner,
                    at,
                    source_name,
                    quality,
                    details,
                    state,
                )
            self.close_span(current_id, at, status="handed-off")
            next_id = self._new_transaction_segment(
                transaction_id,
                target_owner,
                at,
                source_name,
                quality,
                details,
                state,
                status="paused",
            )
            self.add_relation(
                f"reassign:{self.workspace_id}:{transaction_id}:{state['segment']}",
                kind="reassigned",
                at=at,
                label="handoff",
                status="accepted",
                evidence="transaction_id+source_owner+target_owner",
                source_owner=source_owner,
                target_owner=target_owner,
                source_item=current_id,
                target_item=next_id,
                details=details,
            )
            current_id = next_id
        elif not isinstance(current_id, str):
            current_id = self._new_transaction_segment(
                transaction_id,
                resolved_owner,
                at,
                source_name,
                quality,
                details,
                state,
            )
        span = self.add_span(
            current_id,
            owner=str(self.spans[current_id]["owner"]),
            kind="transaction",
            label=_text(payload.get("branch")) or transaction_id,
            at=at,
            status=TRANSACTION_STATUS[event],
            quality=quality,
            source_name=source_name,
            details=details,
        )
        span["details"]["transaction_id"] = transaction_id

        branch = _text(payload.get("branch"))
        base = _text(payload.get("base_revision"))
        canonical = _text(payload.get("canonical_branch"))
        if branch and base and canonical and not state["forked"]:
            checkpoint = self.canonical_checkpoint(
                branch=canonical,
                revision=base,
                at=at,
                kind="base",
                source_name=source_name,
                quality=quality,
            )
            self.add_relation(
                f"fork:{self.workspace_id}:{transaction_id}",
                kind="fork",
                at=at,
                label=branch,
                status="active",
                evidence="transaction_id+branch+base_revision+canonical_branch",
                source_owner="__canonical__",
                target_owner=str(span["owner"]),
                source_item=checkpoint,
                target_item=current_id,
                details=details,
            )
            state["forked"] = True

        if event in TRANSACTION_CHECKPOINTS:
            self.add_marker(
                f"checkpoint:{self.workspace_id}:{transaction_id}:{event}:{source_name}",
                lane=str(span["owner"]),
                kind="transaction-checkpoint",
                label=TRANSACTION_CHECKPOINTS[event],
                at=at,
                status=TRANSACTION_STATUS[event],
                quality=quality,
                source_name=source_name,
                owners=[str(span["owner"])],
                details=details,
            )

        if event == "publish-completed":
            candidate = _text(payload.get("candidate"))
            canonical = _text(payload.get("canonical_branch")) or "canonical"
            self.close_span(current_id, at, status="published")
            if candidate:
                checkpoint = self.canonical_checkpoint(
                    branch=canonical,
                    revision=candidate,
                    at=at,
                    kind="publish",
                    source_name=source_name,
                    quality=quality,
                )
            if candidate and state["forked"]:
                self.add_relation(
                    f"rejoin:{self.workspace_id}:{transaction_id}",
                    kind="rejoin",
                    at=at,
                    label=candidate[:12],
                    status="published",
                    evidence="transaction_id+candidate",
                    source_owner=str(span["owner"]),
                    target_owner="__canonical__",
                    source_item=current_id,
                    target_item=checkpoint,
                    details=details,
                )
            state["current"] = None
        elif event == "transaction-aborted":
            self.close_span(current_id, at, status="aborted")
            state["current"] = None

    def _new_transaction_segment(
        self,
        transaction_id: str,
        owner: str,
        at: str | None,
        source_name: str,
        quality: str,
        details: Mapping[str, object],
        state: dict[str, Any],
        *,
        status: str = "observed",
    ) -> str:
        state["segment"] += 1
        identifier = (
            f"transaction:{self.workspace_id}:{transaction_id}:"
            f"{state['segment']}"
        )
        state["current"] = identifier
        self.add_span(
            identifier,
            owner=owner,
            kind="transaction",
            label=_text(details.get("branch")) or transaction_id,
            at=at,
            status=status,
            quality=quality,
            source_name=source_name,
            details=details,
        )
        return identifier

    def annotate_active_contentions(
        self,
        snapshots: Iterable[Mapping[str, object]],
    ) -> None:
        for snapshot in snapshots:
            contention_id = _text(snapshot.get("contention_id"))
            if contention_id is None:
                continue
            identifier = self.contentions.setdefault(
                contention_id,
                f"contention:{self.workspace_id}:{contention_id}",
            )
            owners = _strings(snapshot.get("owners"))
            marker = self.markers.get(identifier)
            if marker is None:
                marker = self.add_marker(
                    identifier,
                    lane="__canonical__",
                    kind="contention",
                    label=_text(snapshot.get("recommendation")) or contention_id,
                    at=_timestamp(snapshot.get("created_at")),
                    status="stalled" if snapshot.get("stalled") else "active",
                    quality="derived",
                    source_name="active-contention-snapshot",
                    owners=owners,
                    details=snapshot,
                )
            else:
                marker["status"] = (
                    "stalled" if snapshot.get("stalled") else "active"
                )
                marker["owners"] = sorted(set(marker["owners"]) | set(owners))
                marker["details"].update(
                    {
                        key: value
                        for key, value in snapshot.items()
                        if value is not None and value != "" and value != []
                    }
                )
            self.add_relation(
                f"intersection:{self.workspace_id}:{contention_id}",
                kind="contention",
                at=marker["at"],
                label=marker["label"],
                status=marker["status"],
                evidence="active-contention-snapshot",
                owners=marker["owners"],
                target_item=identifier,
                details=marker["details"],
            )

    @staticmethod
    def _relation_owners(relation: Mapping[str, object]) -> list[str]:
        owners = set(_strings(relation.get("owners")))
        for field in ("source_owner", "target_owner"):
            owner = _text(relation.get(field))
            if owner is not None:
                owners.add(owner)
        return sorted(owners - {"__canonical__", "__system__"})

    def _focus(
        self,
        relations: list[dict[str, Any]],
        spans: list[dict[str, Any]],
    ) -> dict[str, object]:
        candidates = [
            relation
            for relation in relations
            if self._relation_owners(relation)
        ]
        if candidates:
            anchor = max(
                candidates,
                key=lambda relation: (
                    FOCUS_KIND_PRIORITY.get(str(relation.get("kind")), 1)
                    + FOCUS_STATUS_BONUS.get(
                        str(relation.get("status")), 0
                    ),
                    str(relation.get("at") or ""),
                    str(relation.get("id") or ""),
                ),
            )
            return {
                "kind": str(anchor.get("kind") or "relation"),
                "status": str(anchor.get("status") or "observed"),
                "at": anchor.get("at"),
                "relation_id": anchor["id"],
                "owners": self._relation_owners(anchor),
                "evidence": anchor.get("evidence"),
            }

        owned_spans = [
            span
            for span in spans
            if span.get("owner") not in {"__canonical__", "__system__"}
        ]
        if not owned_spans:
            return {
                "kind": "empty",
                "status": "observed",
                "at": None,
                "relation_id": None,
                "owners": [],
                "evidence": "none",
            }
        anchor_span = max(
            owned_spans,
            key=lambda span: (
                str(span.get("started_at") or ""),
                str(span.get("id") or ""),
            ),
        )
        return {
            "kind": str(anchor_span.get("kind") or "work"),
            "status": str(anchor_span.get("status") or "observed"),
            "at": anchor_span.get("started_at"),
            "relation_id": None,
            "owners": [str(anchor_span["owner"])],
            "evidence": "latest-owned-work",
        }

    def _run_summary(self) -> tuple[int, int]:
        sessions = [
            span
            for span in self.spans.values()
            if span.get("kind") == "session" and span.get("started_at")
        ]
        points: list[tuple[str, int, int]] = []
        for span in sessions:
            points.append((str(span["started_at"]), 1, 1))
            if span.get("ended_at"):
                points.append((str(span["ended_at"]), 0, -1))
        active = 0
        maximum = 0
        for _, _, delta in sorted(points):
            active += delta
            maximum = max(maximum, active)
        return len(sessions), maximum

    def _infer_presentation_runs(self) -> int:
        """Attach complete legacy claim episodes to one containing run for display only."""
        sessions_by_owner: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in self.spans.values():
            if (
                session.get("kind") != "session"
                or not session.get("run_id")
                or not session.get("started_at")
                or session.get("observed_join") is not True
            ):
                continue
            sessions_by_owner[str(session["owner"])].append(session)

        inferred = 0
        for span in self.spans.values():
            if (
                span.get("run_binding") == "inferred"
                and span.get("inferred_run_id")
            ):
                inferred += 1
                continue
            if (
                span.get("kind") != "claim"
                or span.get("run_binding") != "unbound"
                or not span.get("started_at")
                or span.get("first_visible_event") != "claim-created"
            ):
                continue
            started_at = str(span["started_at"])
            ended_at = _text(span.get("ended_at"))
            if ended_at is None:
                continue
            candidates: list[dict[str, Any]] = []
            for session in sessions_by_owner.get(str(span["owner"]), []):
                session_started = str(session["started_at"])
                session_ended = _text(session.get("ended_at"))
                if started_at < session_started:
                    continue
                if session_ended is None or started_at > session_ended:
                    continue
                if ended_at > session_ended:
                    continue
                candidates.append(session)
            if len(candidates) != 1:
                continue
            session = candidates[0]
            span["run_binding"] = "inferred"
            span["inferred_run_id"] = session["run_id"]
            span["details"]["run_inference"] = "unique-owner-run-window"
            span["details"]["run_inference_authority"] = "presentation-only"
            inferred += 1
        return inferred

    def serialise(self, *, limit: int, source_events: int) -> dict[str, object]:
        inferred_run_bindings = self._infer_presentation_runs()
        all_items = [*self.spans.values(), *self.markers.values()]
        ordered = sorted(
            all_items,
            key=lambda item: (
                str(item.get("started_at") or item.get("at") or ""),
                str(item["id"]),
            ),
        )
        if len(ordered) > limit:
            important = [
                item for item in ordered if item.get("status") in IMPORTANT_STATUSES
            ]
            selected = {str(item["id"]) for item in important[-limit:]}
            for item in reversed(ordered):
                if len(selected) >= limit:
                    break
                selected.add(str(item["id"]))
        else:
            selected = {str(item["id"]) for item in ordered}

        spans = [item for item in self.spans.values() if item["id"] in selected]
        markers = [
            item for item in self.markers.values() if item["id"] in selected
        ]
        visible_relations: list[dict[str, Any]] = []
        for relation in self.relations.values():
            source_item = relation.get("source_item")
            target_item = relation.get("target_item")
            if source_item and source_item not in selected:
                continue
            if target_item and target_item not in selected:
                continue
            visible_relations.append(relation)

        owners = {
            str(span["owner"])
            for span in spans
            if span["owner"] not in {"__canonical__", "__system__"}
        }
        for marker in markers:
            owners.update(
                owner
                for owner in marker.get("owners", [])
                if owner not in {"__canonical__", "__system__"}
            )
            lane = str(marker.get("lane", ""))
            if lane not in {"__canonical__", "__system__"}:
                owners.add(lane)
        for relation in visible_relations:
            for field in ("source_owner", "target_owner"):
                owner = _text(relation.get(field))
                if owner and owner not in {"__canonical__", "__system__"}:
                    owners.add(owner)
            owners.update(
                owner
                for owner in relation.get("owners", [])
                if owner not in {"__canonical__", "__system__"}
            )

        canonical_labels = [
            str(marker["details"].get("canonical_branch"))
            for marker in markers
            if marker["lane"] == "__canonical__"
            and marker["details"].get("canonical_branch")
        ]
        canonical_label = canonical_labels[-1] if canonical_labels else "canonical"
        lanes: list[dict[str, object]] = [
            {
                "id": "__canonical__",
                "kind": "canonical",
                "label": canonical_label,
                "status": "observed",
                "item_count": sum(
                    marker["lane"] == "__canonical__" for marker in markers
                ),
            }
        ]
        for owner in sorted(
            owners,
            key=lambda value: (self.first_activity.get(value, "\uffff"), value),
        ):
            owner_spans = [span for span in spans if span["owner"] == owner]
            statuses = {str(span["status"]) for span in owner_spans}
            lanes.append(
                {
                    "id": owner,
                    "kind": "agent",
                    "label": owner,
                    "status": (
                        "stalled"
                        if "stalled" in statuses
                        else "active"
                        if statuses & {"active", "waiting", "diverted"}
                        else "observed"
                    ),
                    "item_count": len(owner_spans),
                    "started_at": self.first_activity.get(owner),
                }
            )
        if any(span["owner"] == "__system__" for span in spans):
            lanes.append(
                {
                    "id": "__system__",
                    "kind": "system",
                    "label": "legacy / unknown",
                    "status": "observed",
                    "item_count": sum(
                        span["owner"] == "__system__" for span in spans
                    ),
                }
            )

        native_total = self.trace_native + self.trace_derived + self.trace_unknown
        trace_coverage = (
            round(self.trace_native / native_total, 4) if native_total else 0.0
        )
        joined_runs, max_concurrent_runs = self._run_summary()
        focus = self._focus(visible_relations, spans)
        return {
            "schema": 2,
            "workspace_id": self.workspace_id,
            "workspace_root": self.workspace_root,
            "summary": {
                "actors": len(owners),
                "owner_labels_in_window": len(self.first_activity),
                "joined_runs": joined_runs,
                "max_concurrent_runs": max_concurrent_runs,
                "work_spans": len(spans),
                "markers": len(markers),
                "relations": len(visible_relations),
                "intersections": sum(
                    relation["kind"] == "contention"
                    for relation in visible_relations
                ),
                "transactions": len(
                    {
                        span["details"].get("transaction_id")
                        for span in spans
                        if span["kind"] == "transaction"
                    }
                ),
                "branch_forks": sum(
                    relation["kind"] == "fork" for relation in visible_relations
                ),
                "waits": sum(
                    span["kind"] in {"waiting", "diverted"} for span in spans
                ),
                "messages": sum(
                    relation["kind"] == "message"
                    for relation in visible_relations
                ),
                "handoffs": sum(
                    relation["kind"] in {"handoff", "reassigned"}
                    for relation in visible_relations
                ),
                "source_events": source_events,
                "trace_coverage": trace_coverage,
                "native_events": self.trace_native,
                "derived_events": self.trace_derived,
                "legacy_unknown_events": self.trace_unknown,
                "inferred_run_bindings": inferred_run_bindings,
                "total_items": len(all_items),
                "visible_items": len(spans) + len(markers),
                "truncated": len(all_items) > len(spans) + len(markers),
            },
            "focus": focus,
            "lanes": lanes,
            "spans": sorted(
                spans,
                key=lambda item: (str(item.get("started_at") or ""), item["id"]),
            ),
            "markers": sorted(
                markers,
                key=lambda item: (str(item.get("at") or ""), item["id"]),
            ),
            "relations": sorted(
                visible_relations,
                key=lambda item: (str(item.get("at") or ""), item["id"]),
            ),
        }
