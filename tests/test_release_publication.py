import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.release.publication import inventory, validate, export, require_publication_boundary, classification, BOUNDARY
from tests.test_release_licensing import approved_licensing_fixture, write_licensing_fixture


class PublicationTests(unittest.TestCase):
    def test_only_named_architecture_export_is_eligible(self):
        self.assertEqual(classification("docs/assets/gleskit-architecture.png")[0], "INCLUDE")
        for name in ("docs/assets/fig1_architecture.pdf", "docs/assets/draft.png", "docs/assets/private.json"):
            self.assertEqual(classification(name)[0], "EXCLUDE", name)

    def test_private_source_cannot_be_relabelled_as_a_sanitized_public_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); git = self.fixture(root)
            (root / "var").mkdir(); (root / "var/private.txt").write_text("private runtime, even without a known credential shape")
            git("add", "-f", "var/private.txt"); git("commit", "-qm", "private fixture")
            manifest = self.reviewed(root)
            source = next(row for row in manifest["files"] if row["file"] == "var/private.txt")
            public = next(row for row in manifest["files"] if row["file"] == "src/product.py")
            public.update(decision="SANITIZE", replacement=source["file"], replacement_sha256=source["sha256"])
            with self.assertRaisesRegex(RuntimeError, "Private state cannot"):
                validate(root, manifest)

    def test_only_explicit_docker_and_ci_inputs_are_eligible(self):
        for name in ("Dockerfile", ".dockerignore", "compose.yaml", "docker/runtime.json", "docker/python.lock",
                     "docker/r-packages.lock.json", "docker/install-r.R", "docker/third-party-inventory.json",
                     ".github/workflows/docker-validation.yml"):
            self.assertEqual(classification(name)[0], "INCLUDE", name)
        for name in ("docker/private-inventory.json", "docker/.env", "compose.override.yaml",
                     ".github/workflows/unreviewed.yml", "src/secrets.json", "config/secret.key",
                     "src/.aws/credentials", "src/.codex/config.toml", "apps/web/node_modules/a/package.json"):
            self.assertEqual(classification(name)[0], "EXCLUDE", name)

    def fixture(self, root):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
        git("init", "-q"); git("config", "user.name", "Synthetic Fixture"); git("config", "user.email", "fixture@example.invalid")
        (root / "src").mkdir(); (root / "src/product.py").write_text("answer = 42\n")
        (root / "notebooks").mkdir(); (root / "notebooks/private.txt").write_text("private research\n")
        write_licensing_fixture(root)
        git("add", "."); git("commit", "-qm", "fixture")
        return git

    def reviewed(self, root):
        manifest = inventory(root)
        for row in manifest["files"]: row["reviewed"] = True
        manifest["review_complete"] = True
        return manifest

    def test_new_inventory_is_not_publication_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root)
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                validate(root, inventory(root))

    def test_export_only_contains_reviewed_committed_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir(); self.fixture(root)
            (root / "auth.json").write_text("private ignored input")
            target = Path(folder) / "export"
            files = export(root, self.reviewed(root), target)
            self.assertEqual(set(files), {"src/product.py"} | set(approved_licensing_fixture()))
            self.assertFalse((target / ".git").exists())
            self.assertFalse((target / "auth.json").exists())
            self.assertFalse((target / "notebooks").exists())

    def test_new_commit_invalidates_older_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); git = self.fixture(root); manifest = self.reviewed(root)
            (root / "src/new.py").write_text("new = True\n")
            git("add", "."); git("commit", "-qm", "new fixture input")
            with self.assertRaisesRegex(RuntimeError, "stale"):
                validate(root, manifest)

    def test_hash_tampering_and_forced_private_inclusion_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root); manifest = self.reviewed(root)
            tampered = copy.deepcopy(manifest); tampered["files"][0]["sha256"] = "not-reviewed"
            with self.assertRaisesRegex(RuntimeError, "hash"):
                validate(root, tampered)
            for row in manifest["files"]:
                if row["file"].startswith("notebooks/"): row["decision"] = "INCLUDE"
            with self.assertRaisesRegex(RuntimeError, "Private state"):
                validate(root, manifest)

    def test_missing_or_duplicate_entry_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root); manifest = self.reviewed(root)
            manifest["files"].append(manifest["files"][0])
            with self.assertRaisesRegex(RuntimeError, "Duplicate"):
                validate(root, manifest)
            manifest["files"] = manifest["files"][:1]
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                validate(root, manifest)

    def test_boundary_blocks_changed_and_new_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir(); self.fixture(root)
            target = Path(folder) / "export"; export(root, self.reviewed(root), target)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=target, stderr=subprocess.DEVNULL)
            git("init", "-q"); git("add", ".")
            require_publication_boundary(target)
            (target / "src/product.py").write_text("changed = True\n")
            with self.assertRaisesRegex(RuntimeError, "content changed"):
                require_publication_boundary(target)
            (target / "src/product.py").write_text("answer = 42\n")
            (target / "src/new.py").write_text("new = True\n")
            with self.assertRaisesRegex(RuntimeError, "Unreviewed"):
                require_publication_boundary(target)

    def test_export_cannot_omit_licensing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir(); self.fixture(root)
            manifest = self.reviewed(root)
            for row in manifest["files"]:
                if row["file"] == "LICENSE": row["decision"] = "EXCLUDE"
            target = Path(folder) / "export"
            with self.assertRaisesRegex(RuntimeError, "Licensing blocked"):
                export(root, manifest, target)
            self.assertFalse(target.exists())

    def test_working_tree_approval_cannot_authorize_unapproved_committed_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir(); git = self.fixture(root)
            path = root / "docs/licensing-review.json"
            approved = path.read_bytes()
            review = json.loads(approved); review["third_party_review_complete"] = False
            path.write_text(json.dumps(review)); git("add", "."); git("commit", "-qm", "pending review fixture")
            path.write_bytes(approved)
            target = Path(folder) / "export"
            with self.assertRaisesRegex(RuntimeError, "third-party compatibility review is incomplete"):
                export(root, self.reviewed(root), target)
            self.assertFalse(target.exists())

    def test_sanitized_license_must_have_its_own_matching_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir(); git = self.fixture(root)
            (root / "replacement.txt").write_bytes((root / "LICENSE").read_bytes() + b"Different terms\n")
            git("add", "."); git("commit", "-qm", "replacement fixture")
            manifest = self.reviewed(root)
            replacement = next(row for row in manifest["files"] if row["file"] == "replacement.txt")
            license_row = next(row for row in manifest["files"] if row["file"] == "LICENSE")
            license_row.update(decision="SANITIZE", replacement="replacement.txt", replacement_sha256=replacement["sha256"])
            target = Path(folder) / "export"
            with self.assertRaisesRegex(RuntimeError, "changed"):
                export(root, manifest, target)
            self.assertFalse(target.exists())
