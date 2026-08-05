from __future__ import annotations

import json

from tests.integration_support import TransactionRepositoryCase


class DirectClaimPauseIntegrationTest(TransactionRepositoryCase):
    def checkpoint(self) -> str:
        return json.dumps(
            {
                "base_revision": self.run_git("rev-parse", "HEAD").stdout.strip(),
                "owned_paths": ["src/router.txt"],
                "validation": "candidate validation passed",
                "known_failure": "sandbox denied the external promotion lock",
                "next_safe_owner": "agent-a after authorization and resource recheck",
            }
        )

    def test_authorization_pause_is_auditable_and_requires_resume_evidence(self) -> None:
        self.claim("health", "agent-a", "release:canonical-debug", intent="generated")

        self.run_coord(
            "pause",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--checkpoint",
            self.checkpoint(),
            "--resume-condition",
            "User authorizes promotion and canonical state is rechecked",
            "--retain-paths-reason",
            "The paused claim protects the canonical debug release boundary",
            "--blocker-kind",
            "authorization",
            "--operation",
            "promote canonical debug build",
            "--resources",
            "release:canonical-debug",
            "path:../.shadow-local-build/current-debug",
            "--error-kind",
            "sandbox-write-denied",
        )

        claim_path = self.repo / ".agent-coordination" / "claims" / "health.json"
        paused = json.loads(claim_path.read_text(encoding="utf-8"))
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["pause"]["blocker_kind"], "authorization")
        self.assertEqual(paused["pause"]["error_kind"], "sandbox-write-denied")
        self.assertEqual(
            paused["pause"]["resources"],
            [
                "release:canonical-debug",
                "path:../.shadow-local-build/current-debug",
            ],
        )

        audit = json.loads(self.run_tx("log", "--scope", "health").stdout)
        pause_event = next(
            event for event in audit["events"] if event["event"] == "claim-paused"
        )
        self.assertEqual(pause_event["blocker_kind"], "authorization")
        self.assertEqual(pause_event["operation"], "promote canonical debug build")
        self.assertEqual(pause_event["error_kind"], "sandbox-write-denied")

        blocked = self.run_coord(
            "resume",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            expected=1,
        )
        self.assertIn("requires --evidence", blocked.stderr)
        self.assertEqual(
            json.loads(claim_path.read_text(encoding="utf-8"))["status"],
            "paused",
        )

        self.run_coord(
            "resume",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--evidence",
            "User authorized the exact promotion; lock is absent and canonical link is unchanged",
        )
        resumed = json.loads(claim_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["status"], "active")
        self.assertNotIn("pause", resumed)
        self.assertEqual(resumed["last_pause"]["blocker_kind"], "authorization")
        self.assertIn("canonical link is unchanged", resumed["last_pause"]["resume_evidence"])

        audit = json.loads(self.run_tx("log", "--scope", "health").stdout)
        resume_event = next(
            event for event in audit["events"] if event["event"] == "claim-resumed"
        )
        self.assertIn("canonical link is unchanged", resume_event["resume_evidence"])

        self.run_coord(
            "audit-note",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--kind",
            "correction",
            "--message",
            "The prior evidence used a stale display label; the canonical digest is unchanged",
            "--resources",
            "release:canonical-debug",
            "--supersedes-event",
            resume_event["event_file"],
        )
        audit = json.loads(self.run_tx("log", "--scope", "health").stdout)
        correction = next(
            event for event in audit["events"] if event["event"] == "audit-correction"
        )
        superseded = next(
            event for event in audit["events"] if event["event_file"] == resume_event["event_file"]
        )
        self.assertEqual(correction["supersedes_event"], resume_event["event_file"])
        self.assertEqual(correction["supersedes_event_type"], "claim-resumed")
        self.assertEqual(superseded["superseded_by"], [correction["event_file"]])

    def test_audit_correction_cannot_target_another_owner(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        created = json.loads(self.run_tx("log", "--scope", "health").stdout)["events"][0]
        self.run_coord(
            "release",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--summary",
            "First owner completed",
        )
        self.claim("metrics", "agent-b", "route:/metrics")

        rejected = self.run_coord(
            "audit-note",
            "--scope",
            "metrics",
            "--owner",
            "agent-b",
            "--kind",
            "correction",
            "--message",
            "Attempt to rewrite another owner's evidence",
            "--supersedes-event",
            created["event_file"],
            expected=1,
        )

        self.assertIn("same scope and owner", rejected.stderr)

    def test_paused_claim_retains_conflict_authority(self) -> None:
        self.claim("health", "agent-a", "release:canonical-debug", intent="generated")
        self.run_coord(
            "pause",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--checkpoint",
            self.checkpoint(),
            "--resume-condition",
            "Dependency becomes available",
            "--blocker-kind",
            "dependency",
        )

        conflict = self.run_coord(
            "claim",
            "--scope",
            "metrics",
            "--owner",
            "agent-b",
            "--task",
            "Attempt the same canonical promotion",
            "--paths",
            "src/router.txt",
            "--first-release",
            "Promotion completes",
            "--intent",
            "generated",
            "--semantic-writes",
            "release:canonical-debug",
            expected=2,
        )

        self.assertIn("claim health", conflict.stderr)

    def test_legacy_pause_can_resume_without_evidence(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.run_coord(
            "pause",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--checkpoint",
            self.checkpoint(),
            "--resume-condition",
            "Continue the focused task",
        )

        self.run_coord(
            "resume",
            "--scope",
            "health",
            "--owner",
            "agent-a",
        )

        claim_path = self.repo / ".agent-coordination" / "claims" / "health.json"
        resumed = json.loads(claim_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["last_pause"]["blocker_kind"], "other")
