from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dev_mesh_coord import contention
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import (
    activate_pending_claim,
    create_claim,
    join_run,
    leave_run,
    release_claim,
)
from dev_mesh_observer.catalog import Catalog, workspace_id

from helpers import GitWorkspaceTest


NEXT = Path(__file__).parents[1]


def _cli(root: Path, *arguments: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(NEXT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        (sys.executable, "-m", "dev_mesh_coord", "--root", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        check=False,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise AssertionError("coordination CLI did not return a JSON object")
    return value


class BoundedMultiAgentStressTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)

    def test_concurrent_short_read_runs_converge_without_heartbeat_event_flood(self) -> None:
        agent_count = 8
        heartbeat_count = 8

        def session(index: int) -> None:
            owner = f"reader-{index}"
            run_id = f"reader-run-{index}"
            scope = f"reader-scope-{index}"
            _cli(self.root, "join", "--owner", owner, "--run-id", run_id, "--task", "bounded read")
            _cli(
                self.root,
                "claim",
                "--scope",
                scope,
                "--owner",
                owner,
                "--run-id",
                run_id,
                "--task",
                "inspect shared file",
                "--path",
                "app.txt",
                "--intent",
                "read",
            )
            for _ in range(heartbeat_count):
                _cli(
                    self.root,
                    "heartbeat",
                    "--scope",
                    scope,
                    "--owner",
                    owner,
                    "--run-id",
                    run_id,
                )
            _cli(
                self.root,
                "claim-release",
                "--scope",
                scope,
                "--owner",
                owner,
                "--run-id",
                run_id,
                "--summary",
                "read complete",
            )
            _cli(
                self.root,
                "leave",
                "--owner",
                owner,
                "--run-id",
                run_id,
                "--outcome",
                "completed",
                "--summary",
                "bounded read complete",
            )

        with ThreadPoolExecutor(max_workers=agent_count) as executor:
            list(executor.map(session, range(agent_count)))

        plane = resolve(self.root)
        self.assertEqual(list((plane.state_root / "claims").glob("*.json")), [])
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (plane.state_root / "events").glob("*.json")
        ]
        counts = {
            name: sum(event.get("event") == name for event in events)
            for name in ("agent-joined", "claim-created", "claim-released", "agent-left")
        }
        self.assertEqual(counts, {name: agent_count for name in counts})
        self.assertEqual(len(events), agent_count * 4)

    def test_ten_way_hotspot_materializes_only_one_bounded_contention(self) -> None:
        agent_count = 10
        identities = [
            (f"writer-{index}", f"writer-run-{index}", f"writer-scope-{index}")
            for index in range(agent_count)
        ]
        for owner, run_id, _scope in identities:
            join_run(self.root, run_id=run_id, owner=owner, task="bounded hotspot")

        def attempt(identity: tuple[str, str, str]) -> dict[str, object] | str:
            owner, run_id, scope = identity
            try:
                return create_claim(
                    self.root,
                    scope=scope,
                    owner=owner,
                    run_id=run_id,
                    task="bounded hotspot",
                    paths=["app.txt"],
                    semantic_writes=["hotspot"],
                    allow_overlap=True,
                )
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=agent_count) as executor:
            results = list(executor.map(attempt, identities))
        successes = [item for item in results if isinstance(item, dict)]
        failures = [item for item in results if isinstance(item, str)]
        self.assertEqual(len(successes), 2)
        self.assertEqual(len(failures), agent_count - 2)
        active = next(item for item in successes if item["status"] == "active")
        pending = next(item for item in successes if item["status"] == "pending-arbitration")
        contention_id = str(pending["contention_id"])
        contention.cancel(
            self.root,
            contention_id=contention_id,
            scope=str(pending["scope"]),
            owner=str(pending["owner"]),
            run_id=str(pending["run_id"]),
            reason_code="stress-cleanup",
            reason="bounded hotspot observation complete",
        )
        for claim in (active, pending):
            release_claim(
                self.root,
                scope=str(claim["scope"]),
                owner=str(claim["owner"]),
                run_id=str(claim["run_id"]),
                summary="bounded hotspot released",
            )
        for owner, run_id, _scope in identities:
            leave_run(
                self.root,
                run_id=run_id,
                owner=owner,
                outcome="completed",
                summary="bounded hotspot complete",
            )

        database = Path(self.temporary.name) / "hotspot-observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(report["contention"]["opened"], 1)
        self.assertEqual(report["contention"]["cancelled"], 1)
        self.assertEqual(report["diagnostic_summary"]["total"], 0)
        self.assertTrue(report["cutover_readiness"]["ready"])

    def test_repeated_two_agent_contentions_converge_and_observer_is_clean(self) -> None:
        cycle_count = 3
        for index in range(cycle_count):
            primary_owner = f"primary-{index}"
            waiting_owner = f"waiting-{index}"
            primary_run = f"primary-run-{index}"
            waiting_run = f"waiting-run-{index}"
            primary_scope = f"primary-scope-{index}"
            waiting_scope = f"waiting-scope-{index}"
            join_run(self.root, run_id=primary_run, owner=primary_owner, task="short writer")
            join_run(self.root, run_id=waiting_run, owner=waiting_owner, task="short contender")
            create_claim(
                self.root,
                scope=primary_scope,
                owner=primary_owner,
                run_id=primary_run,
                task="short writer",
                paths=["app.txt"],
                semantic_writes=["shared-surface"],
            )
            pending = create_claim(
                self.root,
                scope=waiting_scope,
                owner=waiting_owner,
                run_id=waiting_run,
                task="short contender",
                paths=["app.txt"],
                semantic_writes=["shared-surface"],
                allow_overlap=True,
            )
            contention_id = str(pending["contention_id"])
            contention.select_wait(
                self.root,
                contention_id=contention_id,
                scope=waiting_scope,
                owner=waiting_owner,
                run_id=waiting_run,
                reason="bounded overlapping edit is shorter than a branch transaction",
            )
            release_claim(
                self.root,
                scope=primary_scope,
                owner=primary_owner,
                run_id=primary_run,
                summary="short writer released",
            )
            activate_pending_claim(
                self.root,
                scope=waiting_scope,
                owner=waiting_owner,
                run_id=waiting_run,
                evidence="original writer released and overlap was rechecked",
            )
            release_claim(
                self.root,
                scope=waiting_scope,
                owner=waiting_owner,
                run_id=waiting_run,
                summary="waiting writer released",
            )
            leave_run(
                self.root,
                run_id=primary_run,
                owner=primary_owner,
                outcome="completed",
                summary="cycle complete",
            )
            leave_run(
                self.root,
                run_id=waiting_run,
                owner=waiting_owner,
                outcome="completed",
                summary="cycle complete",
            )

        self.assertEqual(contention.reconcile(self.root), [])
        database = Path(self.temporary.name) / "stress-observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(report["contention"]["opened"], cycle_count)
        self.assertEqual(report["contention"]["completed"], cycle_count)
        self.assertEqual(report["diagnostic_summary"]["total"], 0)
        self.assertTrue(report["cutover_readiness"]["ready"])
