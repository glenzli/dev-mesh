"""Thin JSON CLI for the 20260823.1 producer, transaction, and cutover tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from . import canonical_git, contention, cross_project, interactions, lifecycle, transactions, work, work_results
from .cli_output import project
from .control_plane import initialize
from .cutover import apply as apply_cutover
from .cutover import build_plan, verify as verify_cutover, write_plan
from .errors import error_json
from .version_cutover import apply as apply_version_cutover
from .version_cutover import build_plan as build_version_cutover_plan
from .version_cutover import verify as verify_version_cutover


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _root(arguments: argparse.Namespace) -> Path:
    return Path(arguments.root)


def _common_identity(parser: argparse.ArgumentParser, *, scope: bool = False) -> None:
    if scope:
        parser.add_argument("--scope", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--run-id", required=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="dev-mesh-coord")
    result.add_argument("--root", default=".", help="Git workspace root")
    result.add_argument(
        "--verbose",
        action="store_true",
        help="Return complete materialized facts; compact action-oriented JSON is the default",
    )
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("init")
    status = commands.add_parser("status")
    status.add_argument("--owner")
    status.add_argument("--run-id")
    status.add_argument("--scope", action="append", default=[])

    join = commands.add_parser("join")
    _common_identity(join)
    join.add_argument("--task", required=True)
    join.add_argument("--parent-owner")

    claim = commands.add_parser("claim")
    _common_identity(claim, scope=True)
    claim.add_argument("--task", required=True)
    claim.add_argument("--path", action="append", required=True)
    claim.add_argument("--intent", default="local-edit")
    claim.add_argument(
        "--projection-mode",
        default="git-tree",
        choices=("git-tree", "workspace-bytes"),
        help="workspace-bytes is an explicit non-Git baseline for bounded ignored files",
    )
    claim.add_argument("--semantic-write", action="append", default=[])
    claim.add_argument("--sensitive-to", action="append", default=[])
    claim.add_argument("--validation", default="")
    claim.add_argument("--first-release", default="")
    claim.add_argument("--allow-overlap", action="store_true", help=argparse.SUPPRESS)

    update = commands.add_parser("claim-update")
    _common_identity(update, scope=True)
    update.add_argument("--task")
    update.add_argument("--path", action="append")
    update.add_argument("--semantic-write", action="append")
    update.add_argument("--sensitive-to", action="append")

    heartbeat = commands.add_parser("heartbeat")
    _common_identity(heartbeat, scope=True)

    activate = commands.add_parser("claim-activate")
    _common_identity(activate, scope=True)
    activate.add_argument("--evidence", default="")

    accept_baseline = commands.add_parser("claim-baseline-accept")
    _common_identity(accept_baseline, scope=True)
    accept_baseline.add_argument("--baseline-sha256", required=True)

    complete_claim = commands.add_parser("claim-complete")
    _common_identity(complete_claim, scope=True)
    complete_claim.add_argument("--result-id", required=True)
    complete_claim.add_argument("--summary", required=True)
    complete_claim.add_argument("--validation-evidence", required=True)

    finish_claim = commands.add_parser("claim-finish")
    _common_identity(finish_claim, scope=True)
    finish_claim.add_argument("--result-id", required=True)
    finish_claim.add_argument("--summary", required=True)
    finish_claim.add_argument("--validation-evidence", required=True)

    pause = commands.add_parser("claim-pause")
    _common_identity(pause, scope=True)
    pause.add_argument("--blocker-kind", required=True, choices=sorted(lifecycle.PAUSE_BLOCKER_KINDS))
    pause.add_argument("--checkpoint", required=True)
    pause.add_argument("--resume-condition", required=True)
    pause.add_argument("--operation")
    pause.add_argument("--resource", action="append", default=[])
    pause.add_argument("--error-kind")
    pause.add_argument("--retain-paths-reason")

    resume = commands.add_parser("claim-resume")
    _common_identity(resume, scope=True)
    resume.add_argument("--evidence", required=True)

    correction = commands.add_parser("claim-audit-correction")
    _common_identity(correction, scope=True)
    correction.add_argument("--supersedes-event-id", required=True)
    correction.add_argument("--observation", required=True)
    correction.add_argument("--resource", action="append", default=[])

    release = commands.add_parser("claim-release")
    _common_identity(release, scope=True)
    release.add_argument("--summary", required=True)

    leave = commands.add_parser("leave")
    _common_identity(leave)
    leave.add_argument("--outcome", required=True, choices=sorted(lifecycle.RUN_OUTCOMES))
    leave.add_argument("--summary", required=True)
    leave.add_argument("--force-terminal", action="store_true")
    leave.add_argument("--reason-code")

    reviewed_preview = commands.add_parser("run-close-preview")
    reviewed_preview.add_argument("--run-id", required=True)

    reviewed_close = commands.add_parser("run-close-reviewed")
    reviewed_close.add_argument("--run-id", required=True)
    reviewed_close.add_argument("--review-token", required=True)
    reviewed_close.add_argument("--reviewer", required=True)
    reviewed_close.add_argument("--outcome", required=True, choices=sorted(lifecycle.RUN_OUTCOMES))
    reviewed_close.add_argument("--reason-code", required=True)
    reviewed_close.add_argument("--evidence", required=True)

    recover_run = commands.add_parser("run-recover-authority")
    recover_run.add_argument("--closed-run-id", required=True)
    recover_run.add_argument("--owner", required=True)
    recover_run.add_argument("--recovery-run-id", required=True)
    recover_run.add_argument("--evidence", required=True)

    send = commands.add_parser("send")
    send.add_argument("--source-owner", required=True)
    send.add_argument("--target-owner", required=True)
    send.add_argument("--subject", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--kind", required=True, choices=sorted(interactions.INTERACTION_KINDS))
    send.add_argument("--topic", default="general", choices=sorted(interactions.INTERACTION_TOPICS))
    send.add_argument("--requires-ack", action="store_true")
    send.add_argument("--source-run-id", required=True)
    send.add_argument("--handoff-id")

    cross_open = commands.add_parser("cross-project-open")
    cross_open.add_argument("--collaboration-id", required=True)
    cross_open.add_argument("--source-owner", required=True)
    cross_open.add_argument("--source-run-id", required=True)
    cross_open.add_argument("--target-task-id", required=True)
    cross_open.add_argument("--target-workspace-id")
    cross_open.add_argument("--target-owner")
    cross_open.add_argument(
        "--kind", required=True, choices=sorted(cross_project.COLLABORATION_KINDS)
    )

    cross_bind = commands.add_parser("cross-project-bind")
    cross_bind.add_argument("--collaboration-id", required=True)
    cross_bind.add_argument("--source-workspace-id", required=True)
    cross_bind.add_argument("--source-owner", required=True)
    cross_bind.add_argument("--source-run-id", required=True)
    cross_bind.add_argument("--target-owner", required=True)
    cross_bind.add_argument("--target-run-id", required=True)
    cross_bind.add_argument("--target-task-id", required=True)
    cross_bind.add_argument(
        "--kind", required=True, choices=sorted(cross_project.COLLABORATION_KINDS)
    )

    cross_close = commands.add_parser("cross-project-close")
    cross_close.add_argument("--collaboration-id", required=True)
    cross_close.add_argument("--actor-role", required=True, choices=("source", "target"))
    cross_close.add_argument("--owner", required=True)
    cross_close.add_argument("--run-id", required=True)
    cross_close.add_argument("--source-workspace-id", required=True)
    cross_close.add_argument("--source-owner", required=True)
    cross_close.add_argument("--source-run-id", required=True)
    cross_close.add_argument("--target-workspace-id", required=True)
    cross_close.add_argument("--target-owner", required=True)
    cross_close.add_argument("--target-run-id", required=True)
    cross_close.add_argument("--target-task-id", required=True)
    cross_close.add_argument(
        "--kind", required=True, choices=sorted(cross_project.COLLABORATION_KINDS)
    )
    cross_close.add_argument(
        "--outcome", required=True, choices=sorted(cross_project.COLLABORATION_OUTCOMES)
    )

    cross_reconcile_close = commands.add_parser("cross-project-reconcile-close")
    cross_reconcile_close.add_argument("--collaboration-id", required=True)
    cross_reconcile_close.add_argument("--owner", required=True)
    cross_reconcile_close.add_argument("--run-id", required=True)
    cross_reconcile_close.add_argument(
        "--outcome", required=True, choices=sorted(cross_project.COLLABORATION_OUTCOMES)
    )

    ack = commands.add_parser("ack")
    ack.add_argument("--message-id", required=True)
    ack.add_argument("--target-owner", required=True)
    ack.add_argument("--target-run-id", required=True)
    ack.add_argument("--note", default="Acknowledged")

    for name in ("handoff-reject", "handoff-withdraw"):
        terminal = commands.add_parser(name)
        terminal.add_argument("--handoff-id", required=True)
        terminal.add_argument("--owner", required=True)
        terminal.add_argument("--run-id", required=True)
        terminal.add_argument("--reason-code", required=True)
        terminal.add_argument("--reason", required=True)

    opening = commands.add_parser("contention-open")
    opening.add_argument("--scope", required=True)
    opening.add_argument("--lease-seconds", type=int, default=contention.DEFAULT_LEASE_SECONDS)

    renew = commands.add_parser("contention-renew")
    renew.add_argument("--contention-id", required=True)
    renew.add_argument("--owner", required=True)
    renew.add_argument("--run-id", required=True)
    renew.add_argument("--epoch", required=True, type=int)
    renew.add_argument("--lease-seconds", type=int, default=contention.DEFAULT_LEASE_SECONDS)

    acquire = commands.add_parser("contention-acquire")
    acquire.add_argument("--contention-id", required=True)
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--run-id", required=True)
    acquire.add_argument("--expected-epoch", required=True, type=int)
    acquire.add_argument("--lease-seconds", type=int, default=contention.DEFAULT_LEASE_SECONDS)

    propose = commands.add_parser("contention-propose")
    propose.add_argument("--contention-id", required=True)
    propose.add_argument("--owner", required=True)
    propose.add_argument("--run-id", required=True)
    propose.add_argument("--epoch", required=True, type=int)
    propose.add_argument(
        "--decision",
        required=True,
        choices=sorted(contention.CONTENTION_DECISIONS),
        help="shared decision; parallel-tx offloads only the pending Claim to a temporary branch",
    )
    propose.add_argument("--reason", required=True)

    wait = commands.add_parser("contention-wait")
    _common_identity(wait, scope=True)
    wait.add_argument("--contention-id", required=True)
    wait.add_argument("--reason", required=True)

    respond = commands.add_parser("contention-respond")
    respond.add_argument("--contention-id", required=True)
    respond.add_argument("--scope", required=True)
    respond.add_argument("--owner", required=True)
    respond.add_argument("--run-id", required=True)
    respond.add_argument("--revision", required=True, type=int)
    response = respond.add_mutually_exclusive_group(required=True)
    response.add_argument("--accept", action="store_true")
    response.add_argument("--reject", action="store_true")
    respond.add_argument("--reason", default="")

    enact = commands.add_parser("contention-enact")
    enact.add_argument("--contention-id", required=True)
    enact.add_argument("--owner", required=True)
    enact.add_argument("--run-id", required=True)
    enact.add_argument("--epoch", required=True, type=int)

    cancel = commands.add_parser("contention-cancel")
    _common_identity(cancel, scope=True)
    cancel.add_argument("--contention-id", required=True)
    cancel.add_argument("--reason-code", required=True)
    cancel.add_argument("--reason", required=True)

    commands.add_parser("contention-reconcile")

    suspend = commands.add_parser("work-suspend")
    _common_identity(suspend, scope=True)
    suspend.add_argument("--disposition", required=True, choices=("waiting", "diverted"))
    suspend.add_argument("--reason", required=True)
    suspend.add_argument("--contention-id")
    suspend.add_argument("--blocked-by-owner")
    suspend.add_argument("--alternate-scope")

    work_resume = commands.add_parser("work-resume")
    work_resume.add_argument("--work-state-id", required=True)
    work_resume.add_argument("--owner", required=True)
    work_resume.add_argument("--run-id", required=True)
    work_resume.add_argument("--evidence", required=True)

    tx_begin = commands.add_parser("tx-begin")
    _common_identity(tx_begin, scope=True)
    tx_begin.add_argument("--contention-id", required=True)
    tx_begin.add_argument("--reason", required=True)

    tx_prepare = commands.add_parser("tx-prepare")
    tx_prepare.add_argument("--transaction-id", required=True)
    tx_prepare.add_argument("--owner", required=True)
    tx_prepare.add_argument("--owner-run-id", required=True)
    tx_prepare.add_argument("--summary", required=True)

    tx_validate = commands.add_parser("tx-validate")
    tx_validate.add_argument("--transaction-id", required=True)
    tx_validate.add_argument("--owner", required=True)
    tx_validate.add_argument("--owner-run-id", required=True)
    tx_validate.add_argument("--evidence", required=True)

    tx_publish = commands.add_parser("tx-publish")
    tx_publish.add_argument("--transaction-id", required=True)
    tx_publish.add_argument("--steward", required=True)
    tx_publish.add_argument("--steward-run-id", required=True)

    tx_handoff = commands.add_parser("tx-handoff")
    tx_handoff.add_argument("--transaction-id", required=True)
    tx_handoff.add_argument("--owner", required=True)
    tx_handoff.add_argument("--owner-run-id", required=True)
    tx_handoff.add_argument("--next-owner", required=True)
    tx_handoff.add_argument("--next-run-id", required=True)
    tx_handoff.add_argument("--checkpoint", required=True)

    tx_abort = commands.add_parser("tx-abort")
    tx_abort.add_argument("--transaction-id", required=True)
    tx_abort.add_argument("--owner", required=True)
    tx_abort.add_argument("--owner-run-id", required=True)
    tx_abort.add_argument("--reason-code", required=True)
    tx_abort.add_argument("--reason", required=True)
    tx_abort.add_argument("--discard", action="store_true")

    cleanup_authorize = commands.add_parser("tx-cleanup-authorize")
    cleanup_authorize.add_argument("--transaction-id", required=True)
    cleanup_authorize.add_argument("--owner", required=True)
    cleanup_authorize.add_argument("--owner-run-id", required=True)
    cleanup_authorize.add_argument("--reason", required=True)

    tx_reconcile = commands.add_parser("tx-reconcile")
    tx_reconcile.add_argument("--steward", required=True)
    tx_reconcile.add_argument("--steward-run-id", required=True)

    commands.add_parser("tx-doctor")

    direct_commit = commands.add_parser("direct-commit")
    _common_identity(direct_commit, scope=True)
    direct_commit.add_argument("--summary", required=True)
    direct_commit.add_argument("--validation-evidence", required=True)

    publish_results = commands.add_parser("publish-results")
    publish_results.add_argument("--result-id", action="append", required=True)
    publish_results.add_argument("--owner", required=True)
    publish_results.add_argument("--run-id", required=True)
    publish_results.add_argument("--summary", required=True)
    publish_results.add_argument("--validation-evidence", required=True)

    direct_reconcile = commands.add_parser("direct-commit-reconcile")
    direct_reconcile.add_argument("--steward", required=True)
    direct_reconcile.add_argument("--steward-run-id", required=True)

    commands.add_parser("direct-commit-doctor")

    cutover_plan = commands.add_parser("cutover-plan")
    cutover_plan.add_argument("--archive-root", required=True)
    cutover_plan.add_argument("--journal", required=True)

    cutover_apply = commands.add_parser("cutover-apply")
    cutover_apply.add_argument("--journal", required=True)
    cutover_apply.add_argument("--plan-digest", required=True)
    cutover_apply.add_argument("--confirm-agents-stopped", action="store_true")
    cutover_apply.add_argument("--confirm-no-legacy-writers", action="store_true")
    cutover_apply.add_argument("--confirm-retire-active-authority", action="store_true")

    cutover_verify = commands.add_parser("cutover-verify")
    cutover_verify.add_argument("--journal", required=True)
    cutover_verify.add_argument("--plan-digest", required=True)

    version_plan = commands.add_parser("version-cutover-plan")
    version_plan.add_argument("--cutover-id", required=True)

    version_apply = commands.add_parser("version-cutover-apply")
    version_apply.add_argument("--cutover-id", required=True)
    version_apply.add_argument("--plan-digest", required=True)
    version_apply.add_argument("--confirm-agents-stopped", action="store_true")
    version_apply.add_argument("--confirm-discard-old-state", action="store_true")

    version_verify = commands.add_parser("version-cutover-verify")
    version_verify.add_argument("--cutover-id", required=True)
    version_verify.add_argument("--plan-digest", required=True)
    return result


def dispatch(arguments: argparse.Namespace) -> object:
    root = _root(arguments)
    command = arguments.command
    if command == "init":
        plane = initialize(root)
        return {"protocol": plane.version, "state_root": str(plane.state_root)}
    if command == "status":
        return lifecycle.status(root)
    if command == "join":
        return lifecycle.join_run(root, run_id=arguments.run_id, owner=arguments.owner, task=arguments.task, parent_owner=arguments.parent_owner)
    if command == "claim":
        return lifecycle.create_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, task=arguments.task, paths=arguments.path, intent=arguments.intent, projection_mode=arguments.projection_mode, semantic_writes=arguments.semantic_write, sensitive_to=arguments.sensitive_to, validation=arguments.validation, first_release=arguments.first_release, allow_overlap=arguments.allow_overlap)
    if command == "claim-update":
        return lifecycle.update_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, task=arguments.task, paths=arguments.path, semantic_writes=arguments.semantic_write, sensitive_to=arguments.sensitive_to)
    if command == "heartbeat":
        return lifecycle.heartbeat_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id)
    if command == "claim-activate":
        return lifecycle.activate_pending_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, evidence=arguments.evidence)
    if command == "claim-baseline-accept":
        return work_results.accept_baseline(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, baseline_sha256=arguments.baseline_sha256)
    if command == "claim-complete":
        return work_results.complete_claim(root, result_id=arguments.result_id, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, summary=arguments.summary, validation_evidence=arguments.validation_evidence)
    if command == "claim-finish":
        return work_results.complete_claim(
            root,
            result_id=arguments.result_id,
            scope=arguments.scope,
            owner=arguments.owner,
            run_id=arguments.run_id,
            summary=arguments.summary,
            validation_evidence=arguments.validation_evidence,
            release_if_unchanged=True,
        )
    if command == "claim-pause":
        return lifecycle.pause_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, blocker_kind=arguments.blocker_kind, checkpoint=arguments.checkpoint, resume_condition=arguments.resume_condition, operation_name=arguments.operation, resources=arguments.resource, error_kind=arguments.error_kind, retain_paths_reason=arguments.retain_paths_reason)
    if command == "claim-resume":
        return lifecycle.resume_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, evidence=arguments.evidence)
    if command == "claim-audit-correction":
        return lifecycle.append_audit_correction(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, supersedes_event_id=arguments.supersedes_event_id, observation=arguments.observation, resources=arguments.resource)
    if command == "claim-release":
        return lifecycle.release_claim(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, summary=arguments.summary)
    if command == "leave":
        return lifecycle.leave_run(root, run_id=arguments.run_id, owner=arguments.owner, outcome=arguments.outcome, summary=arguments.summary, force_terminal=arguments.force_terminal, reason_code=arguments.reason_code)
    if command == "run-close-preview":
        return lifecycle.preview_reviewed_run_close(root, run_id=arguments.run_id)
    if command == "run-close-reviewed":
        return lifecycle.close_run_after_review(root, run_id=arguments.run_id, review_token=arguments.review_token, reviewer=arguments.reviewer, outcome=arguments.outcome, reason_code=arguments.reason_code, evidence=arguments.evidence)
    if command == "run-recover-authority":
        return lifecycle.recover_run_authority(root, closed_run_id=arguments.closed_run_id, owner=arguments.owner, recovery_run_id=arguments.recovery_run_id, evidence=arguments.evidence)
    if command == "send":
        return interactions.send(root, source_owner=arguments.source_owner, target_owner=arguments.target_owner, subject=arguments.subject, body=arguments.body, interaction_kind=arguments.kind, topic=arguments.topic, requires_ack=arguments.requires_ack, source_run_id=arguments.source_run_id, handoff_id=arguments.handoff_id)
    if command == "cross-project-open":
        return cross_project.open_collaboration(root, collaboration_id=arguments.collaboration_id, source_owner=arguments.source_owner, source_run_id=arguments.source_run_id, target_task_id=arguments.target_task_id, kind=arguments.kind, target_workspace_id=arguments.target_workspace_id, target_owner=arguments.target_owner)
    if command == "cross-project-bind":
        return cross_project.bind_collaboration(root, collaboration_id=arguments.collaboration_id, source_workspace_id=arguments.source_workspace_id, source_owner=arguments.source_owner, source_run_id=arguments.source_run_id, target_owner=arguments.target_owner, target_run_id=arguments.target_run_id, target_task_id=arguments.target_task_id, kind=arguments.kind)
    if command == "cross-project-close":
        return cross_project.close_collaboration(root, collaboration_id=arguments.collaboration_id, actor_role=arguments.actor_role, owner=arguments.owner, run_id=arguments.run_id, source_workspace_id=arguments.source_workspace_id, source_owner=arguments.source_owner, source_run_id=arguments.source_run_id, target_workspace_id=arguments.target_workspace_id, target_owner=arguments.target_owner, target_run_id=arguments.target_run_id, target_task_id=arguments.target_task_id, kind=arguments.kind, outcome=arguments.outcome)
    if command == "cross-project-reconcile-close":
        return cross_project.reconcile_closed_collaboration(root, collaboration_id=arguments.collaboration_id, owner=arguments.owner, run_id=arguments.run_id, outcome=arguments.outcome)
    if command == "ack":
        return interactions.acknowledge(root, message_id=arguments.message_id, target_owner=arguments.target_owner, target_run_id=arguments.target_run_id, note=arguments.note)
    if command == "handoff-reject":
        return interactions.reject(root, handoff_id=arguments.handoff_id, target_owner=arguments.owner, target_run_id=arguments.run_id, reason_code=arguments.reason_code, reason=arguments.reason)
    if command == "handoff-withdraw":
        return interactions.withdraw(root, handoff_id=arguments.handoff_id, source_owner=arguments.owner, source_run_id=arguments.run_id, reason_code=arguments.reason_code, reason=arguments.reason)
    if command == "contention-open":
        return contention.open_for_claim(root, scope=arguments.scope, lease_seconds=arguments.lease_seconds)
    if command == "contention-renew":
        return contention.renew(root, contention_id=arguments.contention_id, owner=arguments.owner, run_id=arguments.run_id, epoch=arguments.epoch, lease_seconds=arguments.lease_seconds)
    if command == "contention-acquire":
        return contention.acquire(root, contention_id=arguments.contention_id, owner=arguments.owner, run_id=arguments.run_id, expected_epoch=arguments.expected_epoch, lease_seconds=arguments.lease_seconds)
    if command == "contention-propose":
        return contention.propose(root, contention_id=arguments.contention_id, owner=arguments.owner, run_id=arguments.run_id, epoch=arguments.epoch, decision=arguments.decision, reason=arguments.reason)
    if command == "contention-wait":
        return contention.select_wait(root, contention_id=arguments.contention_id, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, reason=arguments.reason)
    if command == "contention-respond":
        return contention.respond(root, contention_id=arguments.contention_id, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, revision=arguments.revision, accept=arguments.accept, reason=arguments.reason)
    if command == "contention-enact":
        return contention.enact(root, contention_id=arguments.contention_id, owner=arguments.owner, run_id=arguments.run_id, epoch=arguments.epoch)
    if command == "contention-cancel":
        return contention.cancel(root, contention_id=arguments.contention_id, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, reason_code=arguments.reason_code, reason=arguments.reason)
    if command == "contention-reconcile":
        return contention.reconcile(root)
    if command == "work-suspend":
        return work.suspend(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, disposition=arguments.disposition, reason=arguments.reason, contention_id=arguments.contention_id, blocked_by_owner=arguments.blocked_by_owner, alternate_scope=arguments.alternate_scope)
    if command == "work-resume":
        return work.resume(root, work_state_id=arguments.work_state_id, owner=arguments.owner, run_id=arguments.run_id, evidence=arguments.evidence)
    if command == "tx-begin":
        return transactions.begin(root, scope=arguments.scope, owner=arguments.owner, run_id=arguments.run_id, contention_id=arguments.contention_id, reason=arguments.reason)
    if command == "tx-prepare":
        return transactions.prepare(root, transaction_id=arguments.transaction_id, owner=arguments.owner, owner_run_id=arguments.owner_run_id, summary=arguments.summary)
    if command == "tx-validate":
        return transactions.validate(root, transaction_id=arguments.transaction_id, owner=arguments.owner, owner_run_id=arguments.owner_run_id, evidence=arguments.evidence)
    if command == "tx-publish":
        return transactions.publish(root, transaction_id=arguments.transaction_id, steward=arguments.steward, steward_run_id=arguments.steward_run_id)
    if command == "tx-handoff":
        return transactions.handoff(root, transaction_id=arguments.transaction_id, owner=arguments.owner, owner_run_id=arguments.owner_run_id, next_owner=arguments.next_owner, next_run_id=arguments.next_run_id, checkpoint=arguments.checkpoint)
    if command == "tx-abort":
        return transactions.abort(root, transaction_id=arguments.transaction_id, owner=arguments.owner, owner_run_id=arguments.owner_run_id, reason_code=arguments.reason_code, reason=arguments.reason, discard=arguments.discard)
    if command == "tx-cleanup-authorize":
        return transactions.authorize_cleanup(root, transaction_id=arguments.transaction_id, owner=arguments.owner, owner_run_id=arguments.owner_run_id, reason=arguments.reason)
    if command == "tx-reconcile":
        return transactions.reconcile(root, steward=arguments.steward, steward_run_id=arguments.steward_run_id)
    if command == "tx-doctor":
        return transactions.doctor(root)
    if command == "direct-commit":
        return canonical_git.commit(
            root,
            scope=arguments.scope,
            owner=arguments.owner,
            run_id=arguments.run_id,
            summary=arguments.summary,
            validation_evidence=arguments.validation_evidence,
        )
    if command == "publish-results":
        return canonical_git.commit_results(
            root,
            result_ids=arguments.result_id,
            owner=arguments.owner,
            run_id=arguments.run_id,
            summary=arguments.summary,
            validation_evidence=arguments.validation_evidence,
        )
    if command == "direct-commit-reconcile":
        return canonical_git.reconcile(
            root,
            steward=arguments.steward,
            steward_run_id=arguments.steward_run_id,
        )
    if command == "direct-commit-doctor":
        return canonical_git.doctor(root)
    if command == "cutover-plan":
        plan = build_plan(root, archive_root=Path(arguments.archive_root))
        write_plan(Path(arguments.journal), plan)
        return plan
    if command == "cutover-apply":
        return apply_cutover(Path(arguments.journal), expected_plan_digest=arguments.plan_digest, confirm_agents_stopped=arguments.confirm_agents_stopped, confirm_no_legacy_writers=arguments.confirm_no_legacy_writers, confirm_retire_active_authority=arguments.confirm_retire_active_authority)
    if command == "cutover-verify":
        return verify_cutover(Path(arguments.journal), expected_plan_digest=arguments.plan_digest)
    if command == "version-cutover-plan":
        return build_version_cutover_plan(root, cutover_id=arguments.cutover_id)
    if command == "version-cutover-apply":
        return apply_version_cutover(
            root,
            cutover_id=arguments.cutover_id,
            expected_plan_digest=arguments.plan_digest,
            confirm_agents_stopped=arguments.confirm_agents_stopped,
            confirm_discard_old_state=arguments.confirm_discard_old_state,
        )
    if command == "version-cutover-verify":
        return verify_version_cutover(
            root,
            cutover_id=arguments.cutover_id,
            expected_plan_digest=arguments.plan_digest,
        )
    raise AssertionError(command)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        value = dispatch(arguments)
        _json(
            project(
                arguments.command,
                value,
                verbose=arguments.verbose,
                owner=getattr(arguments, "owner", None),
                run_id=getattr(arguments, "run_id", None),
                scopes=getattr(arguments, "scope", None)
                if isinstance(getattr(arguments, "scope", None), list)
                else None,
            )
        )
        return 0
    except Exception as error:
        print(error_json(error), file=sys.stderr)
        return 1
