"""Release protections survive retirement of the native installer."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from scripts.release.privacy_gate import audit_repository, inspect, private_filename, rules
from scripts.release.publication import classification

ROOT = Path(__file__).resolve().parents[1]


class RepositoryPrivacyTests(unittest.TestCase):
    def test_openrouter_keys_and_nested_developer_profiles_are_not_publishable(self):
        token = b"sk-or-v1-" + b"a" * 64
        self.assertIn("credential", rules(token))
        self.assertNotIn(token.decode(), str(rules(token)))
        for name in ("src/.vscode/settings.json", "src/.idea/workspace.xml", "src/.venv/config.json",
                     "scripts/.cache/private.json", "apps/web/node_modules/package.json", "src/.docker/config.json",
                     "scripts/.ssh/config", "config/credentials.json", "src/.npmrc", "opencode.json"):
            self.assertTrue(private_filename(name), name)
            self.assertEqual(classification(name)[0], "EXCLUDE", name)

    def test_archive_and_nested_archive_values_are_never_echoed(self):
        secret = b"sk-proj-" + b"x" * 48
        inner, outer = io.BytesIO(), io.BytesIO()
        with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("private.txt", secret)
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("nested.zip", inner.getvalue())
        for data in (inner.getvalue(), outer.getvalue()):
            report = inspect(data, "archive.zip")
            self.assertIn("credential", report)
            self.assertNotIn(secret.decode(), json.dumps(report))

    def test_audit_source_does_not_flag_its_own_pattern_literals(self):
        for name in ("privacy_gate", "patterns"):
            path = ROOT / f"scripts/release/{name}.py"
            self.assertEqual(rules(path.read_bytes(), path.relative_to(ROOT).as_posix()), [])

    def test_historical_fixture_exemption_is_exact_and_never_hides_keys(self):
        fake_home = b"/Users/" + b"private-user"
        fixture = "packaging/macos/test_privacy.py"
        self.assertNotIn("personal-machine-path", rules(fake_home + b"/test", fixture))
        self.assertIn("personal-machine-path", rules(fake_home + b"-other/test", fixture))
        self.assertIn("credential", rules(b"sk-proj-" + b"x" * 48, fixture))

    def test_private_state_and_developer_accounts_are_excluded(self):
        for name in ("docs/opencode/opencode.json", ".gleskit/workspace.json", "var/data.csv",
                     ".env.production", "auth.json", ".openclaude-profile.json"):
            self.assertTrue(private_filename(name), name)
            self.assertEqual(classification(name)[0], "EXCLUDE", name)
        self.assertFalse(private_filename("docs/opencode/opencode.json.example"))

    def test_removal_does_not_silently_clear_a_history_privacy_finding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
            git("init", "-q")
            git("config", "user.name", "Synthetic Fixture")
            git("config", "user.email", "fixture@example.invalid")
            personal = "/Users/" + "synthetic-privacy-fixture/research"
            (root / "private.txt").write_text(personal)
            git("add", "private.txt")
            git("commit", "-qm", "fixture")
            git("rm", "private.txt")
            git("commit", "-qm", "remove fixture")
            report = audit_repository(root)
            self.assertFalse(report["ok"])
            self.assertTrue(all(row["scope"] == "git-history" for row in report["findings"]))
            self.assertNotIn(personal, json.dumps(report))

    def test_untracked_web_build_input_is_checked_before_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "apps/web/src").mkdir(parents=True)
            (root / "apps/web/src/new.js").write_text("sk-proj-" + "x" * 48)
            report = audit_repository(root)
            self.assertFalse(report["ok"])
            self.assertTrue(any(row["file"] == "apps/web/src/new.js" for row in report["findings"]))

    @unittest.skipUnless(shutil.which("node") and (ROOT / "apps/web/node_modules/vite").is_dir(), "Node/Vite not installed")
    def test_isolated_vite_modes_exclude_private_env_and_public_files(self):
        app = ROOT / "apps/web"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            canary = "synthetic-private-build-canary"
            (fixture / ".env.production").write_text("VITE_PRIVATE_KEY=" + canary)
            (fixture / "public").mkdir()
            (fixture / "public/private.txt").write_text(canary)
            (fixture / "index.html").write_text('<script type="module" src="/main.js"></script>')
            (fixture / "main.js").write_text('document.body.textContent = import.meta.env.VITE_PRIVATE_KEY || "not injected";')
            isolated = root / "isolated"
            isolated.mkdir()
            env = {"PATH": os.environ["PATH"], "GLESKIT_ISOLATED_BUILD_DIR": str(isolated)}
            code = ('import {build} from "vite"; import definition from "./vite.config.js"; '
                    'const mode=process.argv[2]; const config=definition({mode,command:"build"}); '
                    'await build({...config, mode, root:process.argv[1], configFile:false, logLevel:"silent"});')
            for mode in ("production", "local-web"):
                subprocess.run([shutil.which("node"), "--input-type=module", "-e", code, str(fixture), mode],
                               cwd=app, env=env, check=True, capture_output=True, timeout=30)
                output = fixture / "dist-web"
                self.assertFalse((output / "private.txt").exists())
                for path in output.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(canary.encode(), path.read_bytes())
            # Neither a legacy production mode nor a new mode can bypass isolation.
            code = 'import config from "./vite.config.js"; config({mode:process.argv[1]});'
            for mode in ("production", "local-web"):
                result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code, mode],
                                        cwd=app, env={"PATH": os.environ["PATH"]}, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"isolated inputs", result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node not installed")
    def test_build_wrapper_passes_only_an_allowlisted_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder)
            (app / "scripts").mkdir()
            (app / "node_modules/vite/bin").mkdir(parents=True)
            shutil.copy2(ROOT / "apps/web/scripts/build-web.mjs", app / "scripts/build-web.mjs")
            (app / "node_modules/vite/bin/vite.js").write_text('process.stdout.write(JSON.stringify(process.env))')
            env = {**os.environ, "VITE_PRIVATE_KEY": "synthetic-build-canary", "OPENAI_API_KEY": "synthetic-build-canary",
                   "HTTPS_PROXY": "synthetic-build-canary"}
            result = subprocess.run([shutil.which("node"), str(app / "scripts/build-web.mjs")], env=env,
                                    capture_output=True, check=True, timeout=10)
            child = json.loads(result.stdout)
            self.assertNotIn("synthetic-build-canary", json.dumps(child))
            # macOS can inject this locale variable while starting Node itself.
            self.assertEqual(set(child) - {"__CF_USER_TEXT_ENCODING"},
                             {"PATH", "LANG", "LC_ALL", "GLESKIT_ISOLATED_BUILD_DIR"})

    def test_setup_generates_private_local_paths_without_overwriting_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = root / "checkout"
            for directory in ("scripts", "docs/opencode", ".venv/bin"):
                (fixture / directory).mkdir(parents=True)
            shutil.copy2(ROOT / "scripts/opencode_setup.sh", fixture / "scripts/opencode_setup.sh")
            shutil.copy2(ROOT / "docs/opencode/opencode.json.example", fixture / "docs/opencode/opencode.json.example")
            import sys
            (fixture / ".venv/bin/python").symlink_to(sys.executable)
            env = {"PATH": os.environ["PATH"], "HOME": str(root / "synthetic-home")}
            subprocess.run(["bash", str(fixture / "scripts/opencode_setup.sh")], env=env, check=True, capture_output=True)
            config = fixture / "docs/opencode/opencode.json"
            self.assertNotIn("@@", config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            original = config.read_bytes()
            subprocess.run(["bash", str(fixture / "scripts/opencode_setup.sh")], env=env, check=True, capture_output=True)
            self.assertEqual(config.read_bytes(), original)
