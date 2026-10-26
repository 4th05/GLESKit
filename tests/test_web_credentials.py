"""Persistent credential tests use fake tokens and isolated temporary profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gleskit_mcp.web.credentials import Credentials
from gleskit_mcp.web.providers import Providers
from gleskit_mcp.web.common import WebError


class WebCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-env-test-")
        self.profile = SimpleNamespace(data=Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def credentials(self):
        with patch.dict(sys.modules, {"keyring": None}):
            return Credentials(self.profile)

    def providers(self):
        return Providers(self.profile, self.credentials())

    def save(self, providers, **values):
        return providers.save(**{"providerId": "fixture", "kind": "openrouter", "label": "Fixture",
                                 "models": ["vendor/first", "vendor/second"], "defaultModel": "vendor/second",
                                 "apiKey": "synthetic-persistent-token", **values})

    def test_key_models_default_and_activation_survive_restart_without_keyring(self):
        first = self.providers()
        self.save(first)
        second = self.providers()
        self.assertTrue(second.list()[0]["connected"])
        self.assertEqual(second.active()["models"], ["vendor/first", "vendor/second"])
        self.assertEqual(second.active()["default_model"], "vendor/second")
        self.assertEqual(second.credentials.get("fixture"), "synthetic-persistent-token")
        self.assertEqual(second.list()[0]["credential_storage"], "private-dotenv")
        self.assertNotIn("synthetic-persistent-token", json.dumps(second.list()))
        self.assertNotIn("synthetic-persistent-token", second.path.read_text())
        self.assertIn("synthetic-persistent-token", (self.profile.data / ".env").read_text())

    def test_existing_dotenv_keys_are_discovered_by_name_and_explicitly_imported(self):
        project = self.profile.data / "project"
        profile = SimpleNamespace(project=project, data=project / "var/local-web/profile")
        profile.data.mkdir(parents=True)
        (project / ".env").write_text("export OPENAI_API_KEY='synthetic-openai-token'\nOPENROUTER_API_KEY=synthetic-router-token # local\nUNRELATED_SECRET=do-not-discover\n")
        providers = Providers(profile)
        catalog = providers.catalog()
        self.assertNotIn("synthetic-", json.dumps(catalog))
        self.assertNotIn("UNRELATED_SECRET", json.dumps(catalog))
        self.assertEqual(next(row for row in catalog if row["kind"] == "openai")["key_sources"][0]["id"], "project:OPENAI_API_KEY")
        self.assertEqual(providers.list(), [], "Discovery must not activate or create a connection")
        self.save(providers, apiKey=None, keySource="project:OPENROUTER_API_KEY")
        self.assertTrue(providers.list()[0]["connected"])
        self.assertEqual(providers.credentials.get("fixture"), "synthetic-router-token")
        reloaded = Providers(profile)
        self.assertTrue(reloaded.list()[0]["connected"])
        self.assertNotIn("synthetic-router-token", json.dumps(reloaded.list()))
        with self.assertRaisesRegex(WebError, "official provider endpoint"):
            self.save(providers, apiKey=None, keySource="project:OPENROUTER_API_KEY", endpoint="https://other.example/v1")
        with self.assertRaisesRegex(WebError, "matching this provider"):
            self.save(providers, apiKey=None, keySource="project:OPENAI_API_KEY")
        with self.assertRaisesRegex(WebError, "official provider endpoint"):
            providers.fetch_models(kind="openrouter", endpoint="https://other.example/v1", key_source="project:OPENROUTER_API_KEY")

    def test_isolated_profiles_do_not_discover_project_keys_and_named_values_are_literal(self):
        project = self.profile.data / "project"
        project.mkdir()
        (project / ".env").write_text("OPENAI_API_KEY=synthetic-project-token\n")
        self.profile.project = project
        credentials = self.credentials()
        self.assertEqual(credentials.sources(), [])
        credentials.path.write_text('OPENAI_API_KEY="synthetic-${UNCHANGED}-token" # comment\n')
        self.assertEqual(credentials.sources()[0]["id"], "profile:OPENAI_API_KEY")
        self.assertEqual(credentials.from_source("profile:OPENAI_API_KEY", "openai"), "synthetic-${UNCHANGED}-token")
        credentials.path.write_text('OPENAI_API_KEY=synthetic-first\nOPENAI_API_KEY=synthetic-second\n')
        with self.assertRaises(WebError) as error:
            credentials.sources()
        self.assertNotIn("synthetic-", str(error.exception))
        credentials.path.unlink()
        credentials.path.symlink_to(project / ".env")
        with self.assertRaises(WebError):
            credentials.sources()

    def test_key_replacement_and_blank_key_model_edit_do_not_create_duplicates(self):
        providers = self.providers()
        self.save(providers)
        self.save(providers, apiKey="synthetic-replacement-token")
        self.save(providers, apiKey=None, models=["vendor/new"], defaultModel="vendor/new")
        reloaded = self.providers()
        self.assertEqual(len(reloaded.list()), 1)
        self.assertEqual(reloaded.active()["models"], ["vendor/new"])
        self.assertEqual(reloaded.credentials.get("fixture"), "synthetic-replacement-token")
        text = reloaded.credentials.path.read_text()
        self.assertNotIn("synthetic-persistent-token", text)
        self.assertEqual(text.count("synthetic-replacement-token"), 1)

    def test_distinct_connections_and_removal_keep_other_keys_and_models(self):
        providers = self.providers()
        self.save(providers)
        self.save(providers, providerId="second", apiKey="synthetic-second-token")
        providers.delete("fixture")
        reloaded = self.providers()
        self.assertEqual([row["provider_id"] for row in reloaded.list()], ["second"])
        self.assertEqual(reloaded.credentials.get("second"), "synthetic-second-token")
        self.assertIsNone(reloaded.credentials.get("fixture"))
        self.assertNotIn("synthetic-persistent-token", reloaded.credentials.path.read_text())

    def test_changed_destination_cannot_reuse_saved_key(self):
        providers = self.providers()
        self.save(providers)
        with self.assertRaisesRegex(WebError, "new key"):
            self.save(providers, endpoint="https://different.example/v1", apiKey=None)
        self.assertEqual(providers.active()["endpoint"], "https://openrouter.ai/api/v1")

    def test_metadata_write_failure_restores_the_previous_key_and_models(self):
        providers = self.providers()
        self.save(providers)
        with patch("gleskit_mcp.web.providers.write_private", side_effect=OSError("Synthetic metadata failure")):
            with self.assertRaisesRegex(WebError, "previous settings"):
                self.save(providers, endpoint="https://different.example/v1", apiKey="synthetic-replacement-token")
        reloaded = self.providers()
        self.assertEqual(reloaded.active()["endpoint"], "https://openrouter.ai/api/v1")
        self.assertEqual(reloaded.credentials.get("fixture"), "synthetic-persistent-token")
        self.assertNotIn("synthetic-replacement-token", reloaded.credentials.path.read_text())

    def test_dotenv_preserves_comments_and_does_not_expand_or_export_tokens(self):
        credentials = self.credentials()
        credentials.path.write_text("# My settings\nUNRELATED=preserve-me\n")
        token = "synthetic-$HOME-${UNCHANGED}-#-token"
        credentials.set("fixture", token)
        self.assertEqual(credentials.get("fixture"), token)
        self.assertIn("UNRELATED=preserve-me", credentials.path.read_text())
        self.assertNotIn(credentials.variable("fixture"), os.environ)
        self.assertNotEqual(credentials.variable("fixture"), credentials.variable("Fixture"))
        self.assertEqual(stat.S_IMODE(credentials.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(credentials.path.parent.stat().st_mode), 0o700)
        self.assertFalse(list(credentials.path.parent.glob(".env.*.tmp")))

    def test_symlinks_hardlinks_and_malformed_files_fail_without_disclosing_keys(self):
        credentials = self.credentials()
        target = self.profile.data / "untouched.txt"
        target.write_text("synthetic-do-not-disclose-token")
        credentials.path.symlink_to(target)
        with self.assertRaises(WebError): credentials.set("fixture", "synthetic-other-token")
        self.assertEqual(target.read_text(), "synthetic-do-not-disclose-token")
        credentials.path.unlink()
        os.link(target, credentials.path)
        with self.assertRaises(WebError): credentials.get("fixture")
        credentials.path.unlink()
        credentials.path.write_text(credentials.variable("fixture") + "=synthetic-do-not-disclose-token\n")
        with self.assertRaises(WebError) as raised: credentials.get("fixture")
        self.assertNotIn("synthetic-do-not-disclose-token", str(raised.exception))

    def test_legacy_vault_key_migrates_but_deleted_key_cannot_reappear(self):
        class Vault:
            def get_password(self, service, account): return "synthetic-legacy-vault-token"
            def delete_password(self, service, account): raise RuntimeError("Locked synthetic vault")
        credentials = self.credentials()
        credentials.backend = Vault()
        self.assertEqual(credentials.get("fixture"), "synthetic-legacy-vault-token")
        self.assertEqual(self.credentials().get("fixture"), "synthetic-legacy-vault-token")
        with self.assertRaisesRegex(WebError, "legacy OS-vault"):
            credentials.delete("fixture")
        self.assertIsNone(credentials.get("fixture"))
        self.assertNotIn("synthetic-legacy-vault-token", credentials.path.read_text())

    def test_saved_connection_survives_a_fresh_python_process(self):
        providers = self.providers()
        self.save(providers)
        code = """import sys,json
from pathlib import Path
from types import SimpleNamespace
sys.modules['keyring']=None
from gleskit_mcp.web.providers import Providers
p=Providers(SimpleNamespace(data=Path(sys.argv[1])))
print(json.dumps(p.list()))
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.profile.data)], capture_output=True, text=True,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}, timeout=10)
        self.assertEqual(result.returncode, 0)
        row = json.loads(result.stdout)[0]
        self.assertTrue(row["connected"])
        self.assertEqual(row["models"], ["vendor/first", "vendor/second"])
        self.assertNotIn("synthetic-persistent-token", result.stdout + result.stderr)
