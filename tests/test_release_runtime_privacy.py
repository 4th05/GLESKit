import copy
import json
from pathlib import Path
import tempfile
import unittest
from scripts.release.runtime_privacy_gate import POLICY_FILES, REQUIRED_CASES, policy_identity, require_runtime_privacy
from scripts.release.patterns import sha


class RuntimePrivacyGateTests(unittest.TestCase):
    def test_policy_tracks_existing_web_and_security_inputs(self):
        root = Path(__file__).resolve().parents[1]
        for name in POLICY_FILES:
            self.assertTrue((root / name).is_file(), name)
        self.assertEqual(len(policy_identity(root)), 64)

    def fixture(self, root):
        for name in POLICY_FILES:
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("synthetic fixture\n")
        resources = root / "resources"; (resources / "runtime").mkdir(parents=True)
        (resources / "runtime/opencode").write_bytes(b"synthetic engine")
        (resources / "ready.json").write_text(json.dumps({"integration_files_sha256":"synthetic-inventory"}))
        report = {"ok":True, "profile_credential_canary_count":0, "network_observation_gate_passed":True,
                  "cases_observed":sorted(REQUIRED_CASES),"authorization_route_and_namespaced_model_correct":True,
                  "engine_sha256":sha(resources / "runtime/opencode"),"runtime_inventory_sha256":"synthetic-inventory",
                  "policy_identity":policy_identity(root)}
        path = root / "report.json"; path.write_text(json.dumps(report))
        return resources, path, report

    def test_credential_network_and_coverage_failures_are_release_blockers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); resources, path, valid = self.fixture(root)
            require_runtime_privacy(root, resources, path)
            for key, value in [("ok",False),("profile_credential_canary_count",2),("profile_credential_canary_count",False),
                               ("network_observation_gate_passed",False),("cases_observed",["success-a"])]:
                report = copy.deepcopy(valid); report[key] = value; path.write_text(json.dumps(report))
                with self.assertRaisesRegex(RuntimeError, "blocked"):
                    require_runtime_privacy(root, resources, path)

    def test_stale_source_engine_or_inventory_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); resources, path, valid = self.fixture(root)
            for key in ("engine_sha256", "runtime_inventory_sha256", "policy_identity"):
                report = copy.deepcopy(valid); report[key] = "stale"; path.write_text(json.dumps(report))
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    require_runtime_privacy(root, resources, path)

    def test_no_report_cannot_be_implicitly_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); resources, _, _ = self.fixture(root)
            with self.assertRaisesRegex(RuntimeError, "required"):
                require_runtime_privacy(root, resources, None)
