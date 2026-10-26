"""Standard non-commercial/share-alike terms and publication protections."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from scripts.release.licensing_gate import (
    LICENSE_ID, LICENSING_INPUTS, CONTAINER_INPUTS, REVIEW_FILE, STANDARD_LICENSE_SHA256, require_licensing_review,
    review_inputs, working_tree_inputs,
)
from scripts.release.publication import classification

ROOT = Path(__file__).resolve().parents[1]


def approved_licensing_fixture():
    """Synthetic approval is only for isolated test repositories."""
    frontend = {"license": LICENSE_ID}
    files = {
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
        "NOTICE": ("SPDX-License-Identifier: " + LICENSE_ID + "\nCopyright Synthetic Holder\n").encode(),
        "THIRD_PARTY_NOTICES.md": b"Synthetic fixture; no bundled third-party code.\n",
        "pyproject.toml": ('[project]\nlicense = "' + LICENSE_ID + '"\n'
                           'license-files = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]\n').encode(),
        "apps/web/package.json": json.dumps(frontend).encode(),
        "apps/web/package-lock.json": json.dumps({"packages": {"": frontend}}).encode(),
        "src/gleskit_mcp/analysis/r_runner.py": b"# Synthetic R subprocess fixture\n",
        "src/gleskit_mcp/r/synthetic.R": b"# Synthetic R integration\n",
        "docs/licensing.md": b"Synthetic source-only licensing guide.\n",
        "apps/web/src/assets/GESKit_icon.svg": b"<svg/>\n",
        "apps/web/src/assets/GESKit_icon_no_bg.png": b"synthetic brand fixture\n",
    }
    review = {
        "schema": 1, "license_id": LICENSE_ID, "copyright_holder": "Synthetic Holder",
        "source_license_review_complete": True, "third_party_review_complete": True,
        "reviewed_sha256": {name: hashlib.sha256(files[name]).hexdigest() for name in review_inputs(files)},
    }
    files[REVIEW_FILE] = json.dumps(review).encode()
    return files


def write_licensing_fixture(root):
    files = approved_licensing_fixture()
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return files


class LicensingGateTests(unittest.TestCase):
    def test_container_requires_complete_separate_exact_byte_review(self):
        files = approved_licensing_fixture()
        files.update({name: b"synthetic container input\n" for name in CONTAINER_INPUTS})
        review = json.loads(files[REVIEW_FILE])
        review["reviewed_sha256"] = {name: hashlib.sha256(files[name]).hexdigest() for name in review_inputs(files)}
        files[REVIEW_FILE] = json.dumps(review).encode()
        require_licensing_review(files)  # Source-only instructions do not distribute an installed runtime.
        with self.assertRaisesRegex(RuntimeError, "container dependency"):
            require_licensing_review(files, distribution="image")
        review["container_review_complete"] = True
        files[REVIEW_FILE] = json.dumps(review).encode()
        require_licensing_review(files, distribution="image")
        for name in CONTAINER_INPUTS:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "changed"):
                require_licensing_review({**files, name: files[name] + b"changed"}, distribution="image")
        del files["docker/r-packages.lock.json"]
        with self.assertRaisesRegex(RuntimeError, "missing"):
            require_licensing_review(files)

    def test_unknown_distribution_scope_cannot_bypass_review(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported distribution"):
            require_licensing_review(approved_licensing_fixture(), distribution="unreviewed")

    def test_exact_reviewed_inputs_pass_without_authorizing_publication(self):
        files = approved_licensing_fixture()
        result = require_licensing_review(files)
        self.assertTrue(result["third_party_review_complete"])
        self.assertNotIn("ready_for_public_release", result)

    def test_both_reviews_require_explicit_true_not_truthy_values(self):
        files = approved_licensing_fixture()
        for name in ("source_license_review_complete", "third_party_review_complete"):
            for value in (False, None, "true", 1):
                with self.subTest(name=name, value=value):
                    review = json.loads(files[REVIEW_FILE]); review[name] = value
                    candidate = {**files, REVIEW_FILE: json.dumps(review).encode()}
                    with self.assertRaisesRegex(RuntimeError, "review is incomplete"):
                        require_licensing_review(candidate)

    def test_every_required_input_and_review_record_must_be_present(self):
        files = approved_licensing_fixture()
        for name in (*LICENSING_INPUTS, REVIEW_FILE):
            candidate = {key: data for key, data in files.items() if key != name}
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "missing"):
                require_licensing_review(candidate)

    def test_changes_to_license_notices_metadata_or_r_invalidate_review(self):
        files = approved_licensing_fixture()
        for name in review_inputs(files):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "changed"):
                require_licensing_review({**files, name: files[name] + b"\n"})
        with self.assertRaisesRegex(RuntimeError, "complete exact-hash"):
            require_licensing_review({**files, "src/gleskit_mcp/r/new.R": b"# new dependency\n"})

    def test_incomplete_or_extra_hash_entries_are_rejected(self):
        files = approved_licensing_fixture()
        review = json.loads(files[REVIEW_FILE])
        for hashes in ({}, {**review["reviewed_sha256"], "unreviewed": "unused"}):
            candidate = {**files, REVIEW_FILE: json.dumps({**review, "reviewed_sha256": hashes}).encode()}
            with self.assertRaisesRegex(RuntimeError, "complete exact-hash"):
                require_licensing_review(candidate)

    def test_invalid_json_toml_or_conflicting_metadata_is_rejected(self):
        files = approved_licensing_fixture()
        cases = [
            (REVIEW_FILE, b"[]"), (REVIEW_FILE, b"{invalid"),
            ("pyproject.toml", b"invalid TOML"),
            ("pyproject.toml", b'[project]\nlicense="MIT"\n'),
            ("apps/web/package.json", b'{"license":"MIT"}'),
            ("apps/web/package-lock.json", b'{"packages":{}}'),
            ("NOTICE", b"No SPDX identifier"),
        ]
        for name, data in cases:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "metadata"):
                require_licensing_review({**files, name: data})

    def test_standard_license_cannot_be_modified_even_with_an_updated_review_hash(self):
        files = approved_licensing_fixture()
        files["LICENSE"] += b"Additional restriction\n"
        review = json.loads(files[REVIEW_FILE])
        review["reviewed_sha256"]["LICENSE"] = hashlib.sha256(files["LICENSE"]).hexdigest()
        files[REVIEW_FILE] = json.dumps(review).encode()
        with self.assertRaisesRegex(RuntimeError, "standard license text changed"):
            require_licensing_review(files)

    def test_holder_must_match_notice_without_printing_their_identity(self):
        files = approved_licensing_fixture()
        review = json.loads(files[REVIEW_FILE]); review["copyright_holder"] = "unverified-private-holder"
        with self.assertRaisesRegex(RuntimeError, "copyright notice") as caught:
            require_licensing_review({**files, REVIEW_FILE: json.dumps(review).encode()})
        self.assertNotIn(review["copyright_holder"], str(caught.exception))

    def test_local_reader_rejects_symlink_and_external_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir()
            files = write_licensing_fixture(root)
            require_licensing_review(working_tree_inputs(root))
            license_path = root / "LICENSE"; license_path.unlink()
            outside = Path(folder) / "license.txt"; outside.write_bytes(files["LICENSE"])
            license_path.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "external review input"):
                working_tree_inputs(root)
            license_path.unlink(); license_path.write_bytes(files["LICENSE"])
            (root / REVIEW_FILE).unlink(); (root / "docs/licensing.md").unlink(); (root / "docs").rmdir()
            external_docs = Path(folder) / "external-docs"; external_docs.mkdir()
            (external_docs / "licensing-review.json").write_bytes(files[REVIEW_FILE])
            (root / "docs").symlink_to(external_docs, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "external review input"):
                working_tree_inputs(root)

    def test_cli_blocks_pending_review_without_changing_any_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = write_licensing_fixture(root)
            review = json.loads(files[REVIEW_FILE]); review["third_party_review_complete"] = False
            (root / REVIEW_FILE).write_text(json.dumps(review))
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result = subprocess.run([sys.executable, str(ROOT / "scripts/release/licensing_gate.py"),
                                     "--root", str(root)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"third-party compatibility review is incomplete", result.stderr)
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_licensing_material_is_eligible_for_reviewed_source_export(self):
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "docs/licensing.md", REVIEW_FILE):
            self.assertEqual(classification(name)[0], "INCLUDE", name)

    def test_actual_project_metadata_and_license_are_consistent(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        package = json.loads((ROOT / "apps/web/package.json").read_text())
        lock = json.loads((ROOT / "apps/web/package-lock.json").read_text())
        self.assertEqual(project["license"], LICENSE_ID)
        self.assertEqual(set(project["license-files"]), {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"})
        self.assertEqual(package["license"], LICENSE_ID)
        self.assertEqual(lock["packages"][""]["license"], package["license"])
        self.assertEqual(hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(), STANDARD_LICENSE_SHA256)
        notice = (ROOT / "NOTICE").read_text()
        self.assertIn("SPDX-License-Identifier: " + LICENSE_ID, notice)
        self.assertIn("Copyright (c) 2026 the GLESKit author", notice)

