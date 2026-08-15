"""Reviewed mutation boundary for loopback Console recovery actions."""

from __future__ import annotations

from pathlib import Path

from dev_mesh_coord.constants import PROTOCOL_VERSION
from dev_mesh_coord.lifecycle import close_run_after_review, preview_reviewed_run_close
from dev_mesh_observer.catalog import Catalog, workspace_id as observed_workspace_id

from .registry import RootRegistry


class ReviewedRecovery:
    """Resolve diagnostic identities back to exact materialized workspace state."""

    def __init__(self, *, database: Path, registry: RootRegistry):
        self.database = database
        self.registry = registry

    def _workspace_root(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or len(identifier) != 24:
            raise ValueError("workspace id must be a 24-character observer identity")
        with Catalog(self.database) as catalog:
            rows = catalog.connection.execute(
                """
                SELECT root
                FROM workspaces
                WHERE workspace_id = ? AND protocol_version = ?
                ORDER BY last_collected_at DESC
                LIMIT 2
                """,
                (identifier, PROTOCOL_VERSION),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("workspace must have one current observed root")
        root = Path(str(rows[0]["root"])).expanduser().resolve()
        allowed = False
        for registered in self.registry.roots():
            registered = registered.resolve()
            if root == registered:
                allowed = True
                break
            try:
                root.relative_to(registered)
            except ValueError:
                continue
            allowed = True
            break
        if not allowed:
            raise ValueError("workspace is outside registered Console roots")
        if observed_workspace_id(root) != identifier:
            raise ValueError("workspace identity changed after collection")
        return root

    def preview_run_close(self, *, workspace_id: str, run_id: str) -> dict[str, object]:
        root = self._workspace_root(workspace_id)
        return {
            "workspace_id": workspace_id,
            "workspace_root": str(root),
            **preview_reviewed_run_close(root, run_id=run_id),
        }

    def close_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        review_token: str,
        reviewer: str,
        outcome: str,
        reason_code: str,
        evidence: str,
    ) -> dict[str, object]:
        root = self._workspace_root(workspace_id)
        record = close_run_after_review(
            root,
            run_id=run_id,
            review_token=review_token,
            reviewer=reviewer,
            outcome=outcome,
            reason_code=reason_code,
            evidence=evidence,
        )
        return {
            "workspace_id": workspace_id,
            "workspace_root": str(root),
            "run": record,
        }
