from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from gleskit_mcp.cli import main
from gleskit_mcp.config import load_settings
from gleskit_mcp.workspace import WorkspaceService, WorkspaceStore


class SessionSnapshotCommandTests(unittest.TestCase):
    """`session-snapshot` collapses three workspace reads into one CLI call.

    The desktop app relies on this to avoid spawning three Python processes per
    session selection, so the contract under test is: exactly one JSON document
    on stdout, carrying the same nested values the individual commands return.
    """

    def _seed_workspace(self, project_root: Path) -> tuple[WorkspaceService, str]:
        settings = load_settings(project_root)
        workspace_root = project_root / "var" / "gleskit_mcp"
        workspace_root.mkdir(parents=True, exist_ok=True)
        service = WorkspaceService(WorkspaceStore(workspace_root), settings=settings)

        session = service.create_session(
            title="Circadian clock x cancer",
            description="Mechanistic study linking circadian genes and DNA damage.",
            species="human",
        )
        session_id = session["session_id"]

        service.capture_artifact(
            session_id,
            kind="analysis",
            title="Differential expression",
            summary="DESeq2 results for the circadian contrast.",
            payload={"gene_count": 3, "genes": ["ARNTL", "PER2", "TP53"]},
            provider="deseq2",
        )
        service.create_upload(
            session_id,
            data_path="data/demo_deseq_results.tsv",
            source_mode="linked",
            title="Demo DESeq results",
            file_name="demo_deseq_results.tsv",
            mime_type="text/tab-separated-values",
        )
        return service, session_id

    def _run_cli(self, project_root: Path, workspace_root: Path, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--project-root",
                    str(project_root),
                    "--workspace-root",
                    str(workspace_root),
                    *argv,
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_session_snapshot_returns_summary_artifacts_and_uploads_in_one_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            service, session_id = self._seed_workspace(project_root)
            workspace_root = project_root / "var" / "gleskit_mcp"

            exit_code, stdout, stderr = self._run_cli(
                project_root, workspace_root, "session-snapshot", "--session-id", session_id
            )

            self.assertEqual(exit_code, 0, stderr)
            payload = json.loads(stdout)

            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(payload["summary"]["session_id"], session_id)
            self.assertEqual(payload["summary"]["title"], "Circadian clock x cancer")

            artifacts = payload["artifacts"]["artifacts"]
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["title"], "Differential expression")
            self.assertEqual(artifacts[0]["kind"], "analysis")

            uploads = payload["uploads"]["uploads"]
            self.assertEqual(len(uploads), 1)
            self.assertEqual(uploads[0]["file_name"], "demo_deseq_results.tsv")

            # The nested values must match the single-purpose commands exactly,
            # otherwise the desktop app would render different data depending on
            # which path it took.
            _, summary_stdout, _ = self._run_cli(
                project_root, workspace_root, "session-summary", "--session-id", session_id
            )
            _, artifacts_stdout, _ = self._run_cli(
                project_root, workspace_root, "artifacts", "--session-id", session_id
            )
            _, uploads_stdout, _ = self._run_cli(
                project_root, workspace_root, "uploads", "--session-id", session_id
            )

            self.assertEqual(payload["summary"], json.loads(summary_stdout))
            self.assertEqual(payload["artifacts"], json.loads(artifacts_stdout))
            self.assertEqual(payload["uploads"], json.loads(uploads_stdout))

    def test_session_snapshot_emits_a_single_json_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            _, session_id = self._seed_workspace(project_root)
            workspace_root = project_root / "var" / "gleskit_mcp"

            exit_code, stdout, stderr = self._run_cli(
                project_root, workspace_root, "session-snapshot", "--session-id", session_id
            )

            self.assertEqual(exit_code, 0, stderr)
            # The desktop app parses stdout as JSON, so incidental logging on
            # stdout would break the contract.
            self.assertEqual(stdout.strip(), json.dumps(json.loads(stdout), indent=2, sort_keys=True))

    def test_session_snapshot_requires_a_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            workspace_root = project_root / "var" / "gleskit_mcp"
            workspace_root.mkdir(parents=True, exist_ok=True)

            with self.assertRaises(SystemExit):
                with redirect_stderr(io.StringIO()):
                    main(
                        [
                            "--project-root",
                            str(project_root),
                            "--workspace-root",
                            str(workspace_root),
                            "session-snapshot",
                        ]
                    )


if __name__ == "__main__":
    unittest.main()
