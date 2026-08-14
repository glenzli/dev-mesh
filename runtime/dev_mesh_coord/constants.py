"""Protocol constants and exhaustive event semantics."""

from __future__ import annotations

PROTOCOL = "dev-mesh.coordination"
PROTOCOL_VERSION = "20260814.1"
EVENT_SCHEMA = 2
DEV_MESH_DIRECTORY = ".dev-mesh"
LEGACY_DIRECTORY = ".agent-coordination"
TOMBSTONE_NAME = "TOMBSTONE.json"

STATE_DIRECTORIES = (
    "events",
    "runs",
    "claims",
    "messages",
    "acks",
    "handoffs",
    "contentions/active",
    "contentions/archive",
    "work/active",
    "work/archive",
    "transactions/active",
    "transactions/archive",
    "direct-commits/active",
    "direct-commits/archive",
    "work-results",
    "cleanups/active",
    "cleanups/archive",
    "checkouts",
    "archive/claims",
    "locks",
)

AUTHORITY_EFFECTS = {
    "agent-joined": "none",
    "agent-left": "terminal",
    "run-authority-recovered": "transfer",
    "claim-created": "grant",
    "claim-requested": "none",
    "claim-activated": "grant",
    "claim-baseline-required": "none",
    "claim-baseline-accepted": "grant",
    "claim-updated": "retain",
    "claim-paused": "retain",
    "claim-resumed": "retain",
    "claim-released": "release",
    "claim-completed": "release",
    "message-sent": "none",
    "message-acknowledged": "none",
    "handoff-offered": "none",
    "handoff-accepted": "terminal",
    "handoff-rejected": "terminal",
    "handoff-withdrawn": "terminal",
    "contention-opened": "grant",
    "contention-coordinator-renewed": "retain",
    "contention-coordinator-acquired": "transfer",
    "contention-decision-proposed": "none",
    "contention-decision-responded": "none",
    "contention-completed": "terminal",
    "contention-cancelled": "terminal",
    "work-suspended": "none",
    "work-resumed": "terminal",
    "transaction-created": "grant",
    "transaction-prepared": "retain",
    "transaction-validated": "retain",
    "transaction-refreshed": "retain",
    "transaction-conflicted": "retain",
    "transaction-handed-off": "transfer",
    "transaction-published": "terminal",
    "transaction-aborted": "terminal",
    "direct-commit-started": "retain",
    "direct-commit-completed": "terminal",
    "cleanup-authorized": "retain",
    "cleanup-completed": "terminal",
    "cleanup-needs-attention": "retain",
    "audit-correction": "none",
}

INTERACTION_KINDS = {"notice", "request", "handoff"}
INTERACTION_TOPICS = {"general", "conflict", "decision", "takeover", "validation"}
CONTENTION_DECISIONS = {"handoff", "parallel-tx", "exclusive"}
RUN_OUTCOMES = {"completed", "failed", "abandoned"}
CLAIM_INTENTS = {"read", "local-edit", "semantic-edit", "exclusive-refactor"}
CLAIM_PROJECTION_MODES = {"git-tree", "workspace-bytes"}
PAUSE_BLOCKER_KINDS = {"authorization", "environment", "dependency", "external-resource", "other"}
EVIDENCE_REQUIRED_PAUSE_BLOCKERS = {"authorization", "environment", "external-resource"}

MAX_CLAIM_PATHS = 128
MAX_SEMANTIC_RESOURCES = 64
MAX_CONTENTION_PARTICIPANTS = 64
MAX_TRANSACTION_CHANGED_PATHS = 128
MAX_WORK_RESULTS_PER_COMMIT = 64
MAX_WORKSPACE_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
