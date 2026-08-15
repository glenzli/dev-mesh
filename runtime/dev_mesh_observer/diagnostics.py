"""Bounded, diagnostic-only consistency projections over collected evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime


MAX_DIAGNOSTICS = 256

TERMINAL_EVENTS = {
    "run": {"agent-left"},
    "claim": {
        "claim-released",
        "claim-completed",
        "transaction-published",
        "transaction-aborted",
    },
    "handoff": {"handoff-accepted", "handoff-rejected", "handoff-withdrawn"},
    "contention": {"contention-completed", "contention-cancelled"},
    "transaction": {"transaction-published", "transaction-aborted"},
    "direct-commit": {"direct-commit-completed"},
    "cleanup": {"cleanup-completed"},
    "work": {"work-resumed"},
    "work-result": {"claim-completed"},
}

TERMINAL_STATUSES = {
    "run": {"closed"},
    "claim": {"released", "completed", "published", "aborted"},
    "handoff": {"accepted", "rejected", "withdrawn"},
    "contention": {"completed", "cancelled"},
    "transaction": {"published", "aborted"},
    "direct-commit": {"completed"},
    "cleanup": {"completed"},
    "work": {"resumed"},
    "work-result": {"recorded"},
}

OPEN_EVENTS = {
    "run": {"agent-joined"},
    "claim": {
        "claim-created",
        "claim-activated",
        "claim-baseline-required",
        "claim-baseline-accepted",
    },
    "handoff": {"handoff-offered"},
    "contention": {"contention-opened"},
    "transaction": {"transaction-created"},
    "direct-commit": {"direct-commit-started"},
    "cleanup": {"cleanup-authorized"},
    "work": {"work-suspended"},
}

CLOSE_EVENTS = {
    "run": {"agent-left"},
    "claim": TERMINAL_EVENTS["claim"],
    "handoff": TERMINAL_EVENTS["handoff"],
    "contention": TERMINAL_EVENTS["contention"],
    "transaction": TERMINAL_EVENTS["transaction"],
    "direct-commit": TERMINAL_EVENTS["direct-commit"],
    "cleanup": TERMINAL_EVENTS["cleanup"],
    "work": TERMINAL_EVENTS["work"],
}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _issue(
    item: dict[str, object], code: str, *, severity: str = "warning"
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "workspace_id": item["workspace_id"],
        "object_id": item["object_id"],
    }


def _event_object_id(kind: str, record: dict[str, object]) -> str | None:
    field = {
        "run": "run_id",
        "claim": "scope",
        "handoff": "handoff_id",
        "contention": "contention_id",
        "transaction": "transaction_id",
        "direct-commit": "direct_commit_id",
        "cleanup": "cleanup_id",
        "work": "work_state_id",
        "work-result": "result_id",
    }[kind]
    value = record.get(field)
    return value if isinstance(value, str) else None


def _lifecycle_object_id(kind: str, record: dict[str, object]) -> str | None:
    field = {
        "run": "run_id",
        "claim": "scope",
        "handoff": "handoff_id",
        "contention": "contention_id",
        "transaction": "transaction_id",
        "direct-commit": "direct_commit_id",
        "cleanup": "cleanup_id",
        "work": "work_state_id",
        "work-result": "result_id",
    }[kind]
    value = record.get(field)
    return value if isinstance(value, str) else None


def project_diagnostics(
    events: list[dict[str, object]],
    snapshots: list[dict[str, object]],
    integrity_findings: list[dict[str, object]],
    *,
    integrity_counts: dict[str, int],
    integrity_by_workspace: dict[str, int],
    stale_after_seconds: int,
    collection_errors: list[dict[str, object]] | None = None,
    not_observed_workspaces: list[dict[str, object]] | None = None,
    pending_acknowledgements: list[dict[str, object]] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
    """Return bounded findings, full counts, and a non-authoritative cutover view."""

    now_time = datetime.now(UTC)
    diagnostics: list[dict[str, object]] = []
    run_snapshots: dict[tuple[str, str, str], dict[str, object]] = {}
    event_names_by_run: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    last_activity_by_run: dict[tuple[str, str, str], datetime] = {}
    claim_run_keys: set[tuple[str, str, str]] = set()
    contention_snapshots: dict[tuple[str, str], dict[str, object]] = {}

    for item in snapshots:
        record = item["record"]
        workspace_id = str(item["workspace_id"])
        if item["kind"] == "run":
            owner = record.get("owner")
            run_id = record.get("run_id")
            if isinstance(owner, str) and isinstance(run_id, str):
                run_snapshots[(workspace_id, owner, run_id)] = item
        if item["kind"] == "claim" and item.get("lifecycle") == "current":
            owner = record.get("owner")
            run_id = record.get("run_id")
            if isinstance(owner, str) and isinstance(run_id, str):
                key = (workspace_id, owner, run_id)
                claim_run_keys.add(key)
                heartbeat = _parse_time(record.get("heartbeat_at"))
                if heartbeat is not None:
                    last_activity_by_run[key] = max(last_activity_by_run.get(key, heartbeat), heartbeat)
        if item["kind"] == "contention":
            contention_id = record.get("contention_id")
            if isinstance(contention_id, str):
                contention_snapshots[(workspace_id, contention_id)] = item

    active_runs = {
        key
        for key, item in run_snapshots.items()
        if item["record"].get("status") == "active"
    }

    terminal_events_by_id: dict[str, dict[tuple[str, str], Counter[str]]] = {
        kind: defaultdict(Counter) for kind in TERMINAL_EVENTS
    }
    open_events_by_id: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in events:
        record = item["record"]
        owner = record.get("owner")
        run_id = record.get("run_id")
        at = _parse_time(item.get("at"))
        if isinstance(owner, str) and isinstance(run_id, str):
            key = (str(item["workspace_id"]), owner, run_id)
            event_names_by_run[key].add(str(item["event"]))
            if at is not None:
                last_activity_by_run[key] = max(last_activity_by_run.get(key, at), at)
        for kind, names in TERMINAL_EVENTS.items():
            if item["event"] in names:
                object_id = _event_object_id(kind, record)
                if object_id is not None:
                    terminal_events_by_id[kind][
                        (str(item["workspace_id"]), object_id)
                    ][str(item["event"])] += 1
        event_name = str(item["event"])
        for kind, names in OPEN_EVENTS.items():
            if event_name not in names:
                continue
            object_id = _lifecycle_object_id(kind, record)
            if object_id is not None:
                open_events_by_id[(str(item["workspace_id"]), kind, object_id)] = item
        for kind, names in CLOSE_EVENTS.items():
            if event_name not in names:
                continue
            object_id = _lifecycle_object_id(kind, record)
            if object_id is not None:
                open_events_by_id.pop((str(item["workspace_id"]), kind, object_id), None)

    for item in snapshots:
        record = item["record"]
        workspace_id = str(item["workspace_id"])
        lifecycle = str(item.get("lifecycle") or "current")
        kind = str(item["kind"])
        status = str(item.get("status") or "unspecified")

        if kind == "contention" and lifecycle == "active":
            participants = [
                participant
                for participant in record.get("participants", [])
                if isinstance(participant, dict)
            ]
            participant_keys = {
                (workspace_id, str(participant.get("owner")), str(participant.get("run_id")))
                for participant in participants
                if isinstance(participant.get("owner"), str)
                and isinstance(participant.get("run_id"), str)
            }
            if not participant_keys or not participant_keys.issubset(active_runs):
                diagnostics.append(_issue(item, "contention.orphaned"))
            else:
                coordinator = record.get("coordinator")
                expires = (
                    _parse_time(coordinator.get("lease_expires_at"))
                    if isinstance(coordinator, dict)
                    else None
                )
                if expires is not None and expires < now_time:
                    diagnostics.append(_issue(item, "contention.live-stalled"))
            if status == "finalizing":
                diagnostics.append(_issue(item, "contention.finalization-pending"))

        if kind == "handoff" and status == "offered":
            source_owner = record.get("source_owner")
            source_run_id = record.get("source_run_id")
            source_key = (workspace_id, str(source_owner), str(source_run_id))
            if (
                not isinstance(source_owner, str)
                or not isinstance(source_run_id, str)
                or source_key not in active_runs
            ):
                diagnostics.append(_issue(item, "handoff.source-terminal"))

        if kind == "claim" and lifecycle == "current":
            exact_run = (
                workspace_id,
                str(record.get("owner")),
                str(record.get("run_id")),
            )
            if exact_run not in active_runs:
                diagnostics.append(_issue(item, "claim.owner-run-missing"))
            if (
                status == "pending-arbitration"
                and not isinstance(record.get("contention_id"), str)
            ):
                diagnostics.append(_issue(item, "claim.pending-without-contention"))
            elif status == "pending-arbitration":
                contention = contention_snapshots.get(
                    (workspace_id, str(record.get("contention_id")))
                )
                if contention is None:
                    diagnostics.append(_issue(item, "claim.pending-without-contention"))
                elif contention["record"].get("status") == "cancelled":
                    diagnostics.append(
                        _issue(item, "claim.pending-after-cancel", severity="info")
                    )
            heartbeat = _parse_time(record.get("heartbeat_at"))
            if heartbeat is not None:
                heartbeat_age = (now_time - heartbeat).total_seconds()
                if heartbeat_age > stale_after_seconds:
                    diagnostics.append(_issue(item, "claim.heartbeat-stale", severity="info"))
                elif heartbeat_age >= stale_after_seconds * 0.8:
                    diagnostics.append(_issue(item, "claim.heartbeat-aging", severity="info"))
            if status in {"released", "published", "aborted"}:
                diagnostics.append(_issue(item, "claim.finalization-pending"))
            elif status == "pending-baseline":
                diagnostics.append(
                    _issue(item, "claim.baseline-acknowledgement-pending", severity="info")
                )
            elif status == "completing":
                diagnostics.append(_issue(item, "claim.completion-pending"))

        if kind == "transaction" and lifecycle == "active":
            exact_run = (
                workspace_id,
                str(record.get("owner")),
                str(record.get("run_id")),
            )
            if exact_run not in active_runs:
                diagnostics.append(_issue(item, "transaction.owner-run-missing"))
            if status in {"initialization-needs-attention", "refresh-needs-attention"}:
                diagnostics.append(_issue(item, f"transaction.{status}"))

        if kind == "direct-commit" and lifecycle == "active":
            exact_run = (
                workspace_id,
                str(record.get("owner")),
                str(record.get("run_id")),
            )
            if exact_run not in active_runs:
                diagnostics.append(_issue(item, "direct-commit.owner-run-missing"))
            if status == "needs-attention":
                diagnostics.append(_issue(item, "direct-commit.needs-attention"))

        if kind == "cleanup" and lifecycle == "active" and status in {
            "needs-attention",
            "archive-pending",
        }:
            diagnostics.append(_issue(item, f"cleanup.{status}"))

        if kind == "work" and lifecycle == "active" and status == "finalizing":
            diagnostics.append(_issue(item, "work.finalization-pending"))

        if kind in TERMINAL_EVENTS:
            # A preflight-rejected direct commit is intentionally archived as
            # ``needs-attention``: it never acquired Git authority and is kept
            # only for audit.  It is neither an active commit nor a malformed
            # terminal archive.
            if kind == "direct-commit" and lifecycle == "archive" and status == "needs-attention":
                continue
            identity = (workspace_id, str(item["object_id"]))
            has_terminal_event = identity in terminal_events_by_id[kind]
            terminal_status = status in TERMINAL_STATUSES[kind]
            if lifecycle == "archive" and not terminal_status:
                diagnostics.append(_issue(item, f"{kind}.archive-nonterminal"))
            elif lifecycle == "active" and terminal_status and not has_terminal_event:
                diagnostics.append(_issue(item, f"{kind}.terminal-event-missing"))
            elif lifecycle == "active" and terminal_status and has_terminal_event:
                diagnostics.append(
                    _issue(item, f"{kind}.terminal-event-with-active-snapshot")
                )
            elif lifecycle in {"archive", "current"} and terminal_status and not has_terminal_event:
                diagnostics.append(_issue(item, f"{kind}.terminal-event-missing"))
            elif lifecycle in {"active", "current"} and not terminal_status and has_terminal_event:
                diagnostics.append(
                    _issue(item, f"{kind}.terminal-event-with-active-snapshot")
                )

    snapshot_identity_counts = Counter(
        (str(item["workspace_id"]), str(item["kind"]), str(item["object_id"]))
        for item in snapshots
    )
    terminal_snapshot_counts = Counter(
        (str(item["workspace_id"]), str(item["kind"]), str(item["object_id"]))
        for item in snapshots
        if str(item.get("status") or "unspecified")
        in TERMINAL_STATUSES.get(str(item["kind"]), set())
    )
    for kind, identities in terminal_events_by_id.items():
        for (workspace_id, object_id), event_counts in identities.items():
            snapshot_identity = (workspace_id, kind, object_id)
            if snapshot_identity not in snapshot_identity_counts:
                diagnostics.append(
                    {
                        "code": f"{kind}.terminal-event-without-snapshot",
                        "severity": "warning",
                        "workspace_id": workspace_id,
                        "object_id": object_id,
                    }
                )
            terminal_count = sum(event_counts.values())
            terminal_snapshot_count = terminal_snapshot_counts[snapshot_identity]
            if terminal_count <= terminal_snapshot_count:
                # A semantic id (notably a Claim scope) may be reused after its
                # previous instance is archived. One terminal event per durable
                # terminal snapshot is therefore expected, even if outcomes differ.
                continue
            if len(event_counts) > 1:
                diagnostics.append(
                    {
                        "code": f"{kind}.conflicting-terminal-events",
                        "severity": "warning",
                        "workspace_id": workspace_id,
                        "object_id": object_id,
                    }
                )
            elif terminal_count > 1:
                diagnostics.append(
                    {
                        "code": f"{kind}.multiple-terminal-events",
                        "severity": "warning",
                        "workspace_id": workspace_id,
                        "object_id": object_id,
                    }
                )

    for (workspace_id, kind, object_id), _event in sorted(open_events_by_id.items()):
        if (workspace_id, kind, object_id) in snapshot_identity_counts:
            continue
        diagnostics.append(
            {
                "code": f"{kind}.open-event-without-snapshot",
                "severity": "warning",
                "workspace_id": workspace_id,
                "object_id": object_id,
            }
        )

    for key in sorted(active_runs):
        item = run_snapshots[key]
        record = item["record"]
        has_claim = key in claim_run_keys
        activity = last_activity_by_run.get(key) or _parse_time(record.get("joined_at"))
        is_stale = (
            activity is not None
            and (now_time - activity).total_seconds() > stale_after_seconds
        )
        if not has_claim and is_stale:
            # No authority is being retained: this is usually a missed final leave,
            # not blocked collaboration. Keep the wording actionable and low severity.
            diagnostics.append(_issue(item, "run.unclosed", severity="info"))
            continue
        if not has_claim:
            diagnostics.append(_issue(item, "run.open-without-claim", severity="info"))
        if event_names_by_run.get(key) == {"agent-joined"}:
            diagnostics.append(_issue(item, "run.join-only", severity="info"))
        if is_stale:
            diagnostics.append(_issue(item, "run.stale", severity="info"))

    for pending in pending_acknowledgements or []:
        sent_at = _parse_time(pending.get("at"))
        if sent_at is None or (now_time - sent_at).total_seconds() <= stale_after_seconds:
            continue
        diagnostics.append(
            {
                "code": "message.ack-stale",
                "severity": "warning",
                "workspace_id": pending["workspace_id"],
                "object_id": pending["message_id"],
                "at": pending.get("at"),
                "source_owner": pending.get("source_owner"),
                "target_owner": pending.get("target_owner"),
                "topic": pending.get("topic"),
            }
        )

    derived_diagnostics = list(diagnostics)
    for finding in integrity_findings:
        diagnostics.append(
            {
                "code": str(finding["code"]),
                "severity": "critical",
                "workspace_id": finding["workspace_id"],
                "object_id": finding["object_id"],
            }
        )
    for issue in collection_errors or []:
        issue_record = (
            {
                "code": "observer.collection-incomplete",
                "severity": "critical",
                "workspace_id": issue["workspace_id"],
                "object_id": issue["workspace_id"],
            }
        )
        diagnostics.append(issue_record)
        derived_diagnostics.append(issue_record)
    for issue in not_observed_workspaces or []:
        issue_record = {
            "code": "observer.workspace-not-observed",
            "severity": "critical",
            "workspace_id": issue["workspace_id"],
            "object_id": issue["workspace_id"],
        }
        diagnostics.append(issue_record)
        derived_diagnostics.append(issue_record)

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    diagnostics.sort(
        key=lambda item: (
            severity_rank.get(str(item["severity"]), 3),
            str(item["workspace_id"]),
            str(item["code"]),
            str(item["object_id"]),
        )
    )
    counts = Counter(str(item["code"]) for item in derived_diagnostics)
    counts.update(integrity_counts)
    total = sum(counts.values())
    summary = {
        "total": total,
        "shown": min(total, MAX_DIAGNOSTICS),
        "truncated": total > MAX_DIAGNOSTICS,
        "counts": dict(sorted(counts.items())),
    }
    readiness = _cutover_readiness(
        snapshots,
        integrity_by_workspace,
        derived_diagnostics,
        collection_errors or [],
        not_observed_workspaces or [],
    )
    return diagnostics[:MAX_DIAGNOSTICS], summary, readiness


def _cutover_readiness(
    snapshots: list[dict[str, object]],
    integrity_by_workspace: dict[str, int],
    diagnostics: list[dict[str, object]],
    collection_errors: list[dict[str, object]],
    not_observed_workspaces: list[dict[str, object]],
) -> dict[str, object]:
    blocker_counts: Counter[str] = Counter()
    by_workspace: dict[str, Counter[str]] = defaultdict(Counter)
    for item in snapshots:
        kind = str(item["kind"])
        lifecycle = str(item.get("lifecycle") or "current")
        status = str(item.get("status") or "unspecified")
        blocker: str | None = None
        if kind == "run" and status == "active":
            blocker = "active_runs"
        elif kind == "claim" and lifecycle == "current" and status in {
            "active",
            "paused",
            "pending-arbitration",
            "pending-baseline",
            "completing",
            "transaction",
            "released",
            "published",
            "aborted",
        }:
            blocker = "active_claims"
        elif kind == "handoff" and status == "offered":
            blocker = "offered_handoffs"
        elif kind == "contention" and lifecycle == "active":
            blocker = "active_contentions"
        elif kind == "transaction" and lifecycle == "active":
            blocker = "active_transactions"
        elif kind == "direct-commit" and lifecycle == "active":
            blocker = "active_direct_commits"
        elif kind == "cleanup" and lifecycle == "active":
            blocker = "active_cleanups"
        elif kind == "work" and lifecycle == "active":
            blocker = "active_work_dispositions"
        if blocker is not None:
            blocker_counts[blocker] += 1
            by_workspace[str(item["workspace_id"])][blocker] += 1
    for workspace_id, count in integrity_by_workspace.items():
        blocker_counts["integrity_findings"] += count
        by_workspace[workspace_id]["integrity_findings"] += count
    for issue in collection_errors:
        blocker_counts["collection_errors"] += 1
        by_workspace[str(issue["workspace_id"])]["collection_errors"] += 1
    for issue in not_observed_workspaces:
        blocker_counts["workspaces_not_observed"] += 1
        by_workspace[str(issue["workspace_id"])]["workspaces_not_observed"] += 1
    audit_gap_suffixes = (
        ".archive-nonterminal",
        ".terminal-event-missing",
        ".terminal-event-with-active-snapshot",
        ".terminal-event-without-snapshot",
        ".open-event-without-snapshot",
        ".multiple-terminal-events",
        ".conflicting-terminal-events",
    )
    for diagnostic in diagnostics:
        if not str(diagnostic.get("code")).endswith(audit_gap_suffixes):
            continue
        blocker_counts["audit_gaps"] += 1
        by_workspace[str(diagnostic["workspace_id"])]["audit_gaps"] += 1
    return {
        "ready": not blocker_counts,
        "blockers": dict(sorted(blocker_counts.items())),
        "by_workspace": {
            workspace_id: dict(sorted(counts.items()))
            for workspace_id, counts in sorted(by_workspace.items())
        },
    }
