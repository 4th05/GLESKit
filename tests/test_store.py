from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from gleskit_mcp.store import WorkspaceStore


class WorkspaceStoreTests(unittest.TestCase):
    def test_list_sessions_treats_all_as_no_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            active = store.create_session(
                title="Active session",
                description="Still active.",
                species="human",
            )
            archived = store.create_session(
                title="Archived session",
                description="Should still appear in all-sessions views.",
                species="human",
            )
            store.archive_session(archived.session_id)

            default_sessions = store.list_sessions()
            all_sessions = store.list_sessions(status="all")
            active_sessions = store.list_sessions(status="active")
            archived_sessions = store.list_sessions(status="archived")

            self.assertEqual(
                {session.session_id for session in default_sessions},
                {session.session_id for session in all_sessions},
            )
            self.assertEqual(
                {session.session_id for session in active_sessions},
                {active.session_id},
            )
            self.assertEqual(
                {session.session_id for session in archived_sessions},
                {archived.session_id},
            )

    def test_create_session_note_and_artifact_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            session = store.create_session(
                title="Test session",
                description="What is the role of TP53?",
                species="human",
            )
            created_note = store.add_note(
                session.session_id,
                title="Initial exploration",
                body_markdown="Initial exploration\n\n- TP53 may regulate apoptosis.",
                author="assistant",
            )
            artifact = store.create_artifact(
                session.session_id,
                kind="gene_resolution",
                title="Saved artifact",
                summary="Resolved TP53.",
                provider="local",
                payload={"symbol": "TP53"},
            )

            loaded_session = store.get_session(session.session_id)
            artifacts = store.list_artifacts(session.session_id)
            loaded_note = store.get_note(session.session_id, created_note.note_id)

            self.assertEqual(loaded_session.session_id, session.session_id)
            self.assertEqual(loaded_session.description, "What is the role of TP53?")
            self.assertEqual(len(loaded_session.notes), 1)
            self.assertEqual(loaded_note.title, "Initial exploration")
            self.assertIn("TP53 may regulate apoptosis", loaded_note.body_markdown)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0].artifact_id, artifact.artifact_id)

    def test_create_multiple_reports_for_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            session = store.create_session(
                title="Report session",
                description="Multiple authored reports should coexist.",
                species="human",
            )
            first = store.create_report(
                session.session_id,
                title="Mechanistic angle",
                body_markdown="# Mechanistic\n\nDDR and checkpoint framing.",
                focus="mechanistic",
                template_id="mechanism",
            )
            second = store.create_report(
                session.session_id,
                title="Biomarker angle",
                body_markdown="# Biomarkers\n\nMarker-oriented interpretation.",
                focus="biomarker",
                template_id="biomarker",
                custom_guidance="Keep it concise for translational review.",
            )

            reports = store.list_reports(session.session_id)
            loaded = store.get_report(session.session_id, first.report_id)

            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[0].report_id, second.report_id)
            self.assertEqual(loaded.focus, "mechanistic")
            self.assertEqual(loaded.template_id, "mechanism")
            self.assertIn("checkpoint framing", loaded.body_markdown)

    def test_update_session_title_and_description_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            session = store.create_session(
                title="Initial session",
                description="Initial description.",
                species="human",
            )

            updated = store.update_session(
                session.session_id,
                title="Refined session title",
                description="Refined session description.",
            )
            loaded = store.get_session(session.session_id)

            self.assertEqual(updated.title, "Refined session title")
            self.assertEqual(updated.description, "Refined session description.")
            self.assertEqual(loaded.title, "Refined session title")
            self.assertEqual(loaded.description, "Refined session description.")
            self.assertGreaterEqual(loaded.updated_at, session.updated_at)

    def test_update_and_delete_report_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            session = store.create_session(
                title="Editable report session",
                description="Reports should be editable objects.",
                species="human",
            )
            report = store.create_report(
                session.session_id,
                title="Initial report",
                body_markdown="# Initial\n\nFirst pass.",
                template_id="coverage_first",
            )

            updated = store.update_report(
                session.session_id,
                report.report_id,
                title="Updated report",
                body_markdown="# Updated\n\nSecond pass.",
                focus="coverage update",
                template_id="mechanism",
                custom_guidance="Focus on causal uncertainty.",
            )
            deleted_id = store.delete_report(session.session_id, report.report_id)

            self.assertEqual(updated.title, "Updated report")
            self.assertEqual(updated.template_id, "mechanism")
            self.assertEqual(updated.custom_guidance, "Focus on causal uncertainty.")
            self.assertEqual(deleted_id, report.report_id)
            self.assertEqual(store.list_reports(session.session_id), [])

    def test_delete_multiple_notes_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            session = store.create_session(
                title="Bulk docs session",
                description="Notes and reports should support bulk deletion.",
                species="human",
            )
            first_note = store.add_note(
                session.session_id,
                title="First note",
                body_markdown="Alpha",
                author="user",
            )
            second_note = store.add_note(
                session.session_id,
                title="Second note",
                body_markdown="Beta",
                author="assistant",
            )
            third_note = store.add_note(
                session.session_id,
                title="Third note",
                body_markdown="Gamma",
                author="assistant",
            )
            first_report = store.create_report(
                session.session_id,
                title="First report",
                body_markdown="# One",
                template_id="coverage_first",
            )
            second_report = store.create_report(
                session.session_id,
                title="Second report",
                body_markdown="# Two",
                template_id="coverage_first",
            )

            deleted_notes = store.delete_notes(session.session_id, [first_note.note_id, third_note.note_id])
            deleted_reports = store.delete_reports(session.session_id, [second_report.report_id])

            remaining_notes = store.list_notes(session.session_id)
            remaining_reports = store.list_reports(session.session_id)

            self.assertEqual(deleted_notes, [first_note.note_id, third_note.note_id])
            self.assertEqual(deleted_reports, [second_report.report_id])
            self.assertEqual([note.note_id for note in remaining_notes], [second_note.note_id])
            self.assertEqual([report.report_id for report in remaining_reports], [first_report.report_id])

    def test_delete_multiple_artifacts_from_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))

            session = store.create_session(
                title="Artifact delete session",
                description="Selected artifacts should be removable in bulk.",
                species="human",
            )
            first = store.create_artifact(
                session.session_id,
                kind="gene_resolution",
                title="First artifact",
                summary="Resolved one gene.",
                provider="local",
                payload={"symbol": "TP53"},
            )
            second = store.create_artifact(
                session.session_id,
                kind="enrichment",
                title="Second artifact",
                summary="Enriched the gene set.",
                provider="local",
                payload={"term": "DNA repair"},
            )
            third = store.create_artifact(
                session.session_id,
                kind="paper_search",
                title="Third artifact",
                summary="Found supporting papers.",
                provider="local",
                payload={"query": "TP53 DNA repair cancer"},
            )

            deleted = store.delete_artifacts(session.session_id, [first.artifact_id, third.artifact_id])
            remaining = store.list_artifacts(session.session_id)

            self.assertEqual(deleted, [first.artifact_id, third.artifact_id])
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].artifact_id, second.artifact_id)

    def test_create_upload_and_delete_session_cleans_copied_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root)

            session = store.create_session(
                title="Upload session",
                description="Uploads should live with the session.",
                species="human",
            )
            source_file = root / "counts.tsv"
            source_file.write_text("gene\tcount\nTP53\t10\n")
            copied_path = store._session_dir(session.session_id) / "uploads" / "upload_copy.tsv"
            copied_path.parent.mkdir(parents=True, exist_ok=True)
            copied_path.write_text(source_file.read_text())
            linked_file = root / "linked_counts.tsv"
            linked_file.write_text("gene\tcount\nBRCA1\t5\n")

            copied = store.create_upload(
                session.session_id,
                data_path=str(copied_path),
                source_mode="copied",
                file_name="upload_copy.tsv",
                stored_path=str(copied_path),
            )
            linked = store.create_upload(
                session.session_id,
                data_path=str(linked_file),
                source_mode="linked",
                file_name="linked_counts.tsv",
                source_path=str(linked_file),
            )

            uploads = store.list_artifacts(session.session_id, kind="upload")
            deleted_sessions = store.delete_sessions([session.session_id])

            self.assertEqual([item.artifact_id for item in uploads], [copied.artifact_id, linked.artifact_id])
            self.assertEqual(deleted_sessions, [session.session_id])
            self.assertFalse(copied_path.exists())
            self.assertTrue(linked_file.exists())
            self.assertEqual(copied.payload["summary"]["data_type"], "raw_counts")
            self.assertEqual(copied.payload["summary"]["sample_count"], 1)
            self.assertEqual(linked.payload["summary"]["data_type"], "raw_counts")
            self.assertEqual(linked.payload["summary"]["sample_count"], 1)

    def test_create_cohort_from_frames_accepts_integer_valued_float_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root)

            counts_frame = pd.DataFrame(
                {
                    "gene": ["TP53", "BRCA1"],
                    "s1": [10.0, 5.0],
                    "s2": [20.0, 7.0],
                }
            )
            metadata_frame = pd.DataFrame(
                {
                    "sample": ["s1", "s2"],
                    "condition": ["A", "B"],
                    "batch": ["B1", "B1"],
                }
            )

            cohort = store.create_cohort_from_frames(
                title="GTEx Brain",
                counts_frame=counts_frame,
                metadata_frame=metadata_frame,
                provider_id="gtex",
                source_kind="downloaded",
                species="human",
                min_count=1,
            )

            self.assertEqual(cohort.profile["sample_count"], 2)
            self.assertEqual(cohort.profile["row_count"], 2)
            self.assertEqual(cohort.profile["filtered_gene_count"], 2)
            self.assertEqual(cohort.inspection["counts"]["summary"]["sample_count"], 2)
            self.assertTrue(Path(cohort.counts_path).exists())
            self.assertTrue(Path(cohort.metadata_path).exists())

    def test_create_cohort_requires_valid_counts_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root)

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t7\n")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\tbatch\ns1\tA\tB1\ns2\tB\tB1\n")

            cohort = store.create_cohort(
                title="TCGA BRCA",
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                provider_id="tcga",
                source_kind="downloaded",
                species="human",
            )
            loaded = store.get_cohort(cohort.cohort_id)

            self.assertEqual(cohort.title, "TCGA BRCA")
            self.assertEqual(cohort.provider_id, "tcga")
            self.assertEqual(cohort.profile["recommended_group_column"], "condition")
            self.assertIn("condition", cohort.profile["recommended_design_formula"])
            self.assertEqual(cohort.inspection["counts"]["data_type"], "raw_counts")
            self.assertEqual(cohort.inspection["metadata"]["data_type"], "metadata")
            self.assertEqual(cohort.inspection["counts"]["summary"]["sample_count"], 2)
            self.assertTrue(Path(cohort.counts_path).exists())
            self.assertTrue(Path(cohort.metadata_path).exists())
            self.assertEqual(loaded.cohort_id, cohort.cohort_id)
            self.assertEqual(store.list_cohorts()[0].cohort_id, cohort.cohort_id)

    def test_create_session_from_cohort_links_counts_and_metadata_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root)

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t7\n")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns2\tB\n")
            cohort = store.create_cohort(
                title="Linked cohort",
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                provider_id="local_import",
                source_kind="imported",
                species="human",
            )

            session_payload = store.create_session_from_cohort(
                cohort_id=cohort.cohort_id,
                title="Session from cohort",
                description="Linked cohort uploads should be preserved as pointers.",
                species="human",
            )
            uploads = store.list_artifacts(session_payload["session_id"], kind="upload")

            self.assertEqual(session_payload["cohort_id"], cohort.cohort_id)
            self.assertEqual(session_payload["linked_upload_ids"], [item.artifact_id for item in uploads])
            self.assertEqual(len(uploads), 2)
            self.assertTrue(all(item.payload.get("source_mode") == "linked" for item in uploads))
            self.assertTrue(all(Path(item.payload.get("data_path") or "").exists() for item in uploads))
            self.assertEqual(store.list_artifacts(session_payload["session_id"]), [])
            self.assertEqual([item.artifact_id for item in store.list_artifacts(session_payload["session_id"], kind="upload")], [item.artifact_id for item in uploads])

            deleted_cohort_id = store.delete_cohort(cohort.cohort_id)
            self.assertEqual(deleted_cohort_id, cohort.cohort_id)
            self.assertEqual(store.list_artifacts(session_payload["session_id"], kind="upload"), [])

    def test_create_cohort_rejects_mismatched_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root)

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\n")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns3\tB\n")

            with self.assertRaises(ValueError):
                store.create_cohort(
                    title="Broken cohort",
                    counts_path=str(counts_path),
                    metadata_path=str(metadata_path),
                    provider_id="local_import",
                    source_kind="imported",
                    species="human",
                )


if __name__ == "__main__":
    unittest.main()
