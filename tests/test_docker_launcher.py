"""Exercise the POSIX launcher with fake commands; never contact a Docker engine."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NONCE = "n" * 43

# The executable fixture records arguments, not command output/capabilities.
FAKE_COMMAND = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
state_path = root / "state.json"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
if name == "open":
    if state.get("browser_error"):
        print("unsafe browser diagnostic: " + args[0], file=sys.stderr)
        sys.exit(1)
    (root / "browser.json").write_text(json.dumps(args))
    sys.exit(0)
if name == "uname":
    print(state.get("host_os", "Darwin") if args == ["-s"] else "arm64")
    sys.exit(0)
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if args == ["context", "show"]:
    print("desktop-linux"); sys.exit(0)
if args[:2] == ["context", "inspect"]:
    print(state.get("endpoint", "unix:///var/run/docker.sock")); sys.exit(0)
assert args[:2] == ["--context", "desktop-linux"], args
args = args[2:]
if args[0] == "info":
    if state.get("daemon_down"): sys.exit(1)
    print(state.get("runtime", "29.0.0|linux|aarch64"))
elif args[:2] == ["volume", "ls"]:
    if state.get("volume"): print("gleskit-local_state")
elif args[:2] == ["volume", "inspect"]:
    print(state.get("volume_owner", "gleskit-local|state"))
elif args[0] == "ps":
    if state.get("container"):
        print("c" * 64)
        if state.get("multiple"): print("d" * 64)
elif args[:2] == ["image", "ls"]:
    if state.get("image", True): print("a" * 12)
elif args[:2] == ["image", "inspect"]:
    print(state.get("image_metadata", "linux|arm64|local-web"))
elif args[:2] == ["buildx", "inspect"]:
    assert args == ["buildx", "inspect", "desktop-linux"], args
    print(state.get("builder", "Name: desktop-linux\nDriver: docker\nNodes:\nName: desktop-linux\nEndpoint: desktop-linux\nStatus: running"))
elif args[0] == "inspect":
    template = args[args.index("--format") + 1]
    if "project.working_dir" in template:
        owner = "local-web|gleskit-local|gleskit|False|" + str(root) + "|" + str(root / "compose.yaml")
        if state.get("override"): owner += "," + str(root / "compose.override.yaml")
        print(state.get("owner", owner))
    elif template == "{{.Image}}": print("sha256:" + "a" * 64)
    elif "ReadonlyRootfs" in template:
        print(state.get("boundary", "10001:10001|true|false|8123/tcp=127.0.0.1:8123,;"))
    elif "State.Status" in template: print(state.get("health", "running|healthy"))
    else: raise AssertionError(template)
elif args[0] == "compose":
    assert args[1:9] == ["--ansi", "never", "--env-file", "/dev/null", "--project-name", "gleskit-local", "--project-directory", str(root)], args
    assert args[9:11] == ["-f", str(root / "compose.yaml")], args
    assert os.environ.get("COMPOSE_FILE") is None
    assert os.environ.get("COMPOSE_ENV_FILES") is None
    args = args[11:]
    if args[:2] == ["version", "--short"]:
        if state.get("no_compose"): sys.exit(1)
    elif args[0] == "build":
        assert args[1:] == ["--builder", "desktop-linux", "--provenance=false", "gleskit"], args
        if state.get("build_error"): sys.exit(1)
        state["image"] = True
    elif args[0] == "up":
        assert args == ["up", "-d", "--wait", "--wait-timeout", "60", "--no-build", "--no-recreate", "--pull", "never", "--no-deps", "gleskit"], args
        if state.get("up_error"): sys.exit(1)
        state.update(container=True, volume=True, health="running|healthy")
    elif args[0] == "down":
        assert args == ["down", "--timeout", "20"], args
        if state.get("down_error"): sys.exit(1)
        state["container"] = False
    elif args[0] == "exec":
        assert args == ["exec", "-T", "gleskit", "python", "-m", "gleskit_mcp.web.container", "--launch-info"], args
        if state.get("handoff_error"):
            print("unsafe helper diagnostic " + "n" * 43, file=sys.stderr); sys.exit(1)
        print(state.get("url", "http://127.0.0.1:8123/#launch=" + "n" * 43))
    else: raise AssertionError(args)
else: raise AssertionError(args)
state_path.write_text(json.dumps(state))
'''


class DockerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "checkout with spaces"
        (self.root / "scripts").mkdir(parents=True)
        self.script = self.root / "scripts/docker_ui.sh"
        shutil.copyfile(ROOT / "scripts/docker_ui.sh", self.script)
        (self.root / "compose.yaml").write_text("synthetic fixture\n")
        (self.root / ".env").write_text("MUST_NOT_BE_SOURCED=synthetic-private-canary\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "open", "uname"):
            executable = self.bin / name
            executable.write_text("#!" + sys.executable + "\n" + FAKE_COMMAND)
            executable.chmod(0o700)
        self.state = {"image": True, "container": False}
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("DOCKER_", "COMPOSE_", "BUILDX_"))}
        self.env.update(FIXTURE_ROOT=str(self.root), PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        COMPOSE_FILE="/unwanted/config.yaml", COMPOSE_ENV_FILES="/unwanted/.env")

    def run_launcher(self, action="open", *, trace=False):
        (self.root / "state.json").write_text(json.dumps(self.state))
        result = subprocess.run(["/bin/sh", *(["-x"] if trace else []), str(self.script), action],
                                env=self.env, cwd="/", text=True, capture_output=True, timeout=20)
        self.state = json.loads((self.root / "state.json").read_text())
        self.assertNotIn(NONCE, result.stdout + result.stderr)
        self.assertNotIn("synthetic-private-canary", result.stdout + result.stderr)
        return result

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def mutations(self):
        return [call for call in self.calls() if "compose" in call and
                any(value in call for value in ("up", "down", "build", "exec"))]

    def test_open_starts_once_and_reuses_the_healthy_instance_without_building(self):
        self.assertEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.run_launcher().returncode, 0)
        calls = self.calls()
        self.assertEqual(sum("up" in call for call in calls), 1)
        self.assertFalse(any("build" in call for call in calls))
        self.assertEqual(json.loads((self.root / "browser.json").read_text()),
                         ["http://127.0.0.1:8123/#launch=" + NONCE])

    def test_missing_image_builds_once_with_the_verified_local_builder(self):
        self.state["image"] = False
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("20 minutes", result.stdout)
        self.assertEqual(sum("build" in call for call in self.calls()), 1)
        self.assertEqual(self.run_launcher().returncode, 0)
        self.assertEqual(sum("build" in call for call in self.calls()), 1)

    def test_status_is_read_only_and_stop_is_idempotent_preserving_state(self):
        self.state.update(container=True, volume=True)
        result = self.run_launcher("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("running (healthy)", result.stdout)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.run_launcher("stop").returncode, 0)
        self.assertEqual(self.run_launcher("stop").returncode, 0)
        self.assertTrue(self.state["volume"])
        self.assertEqual(sum("down" in call for call in self.calls()), 1)
        self.assertFalse((self.root / "browser.json").exists())

    def test_remote_context_is_rejected_before_contacting_a_daemon(self):
        for endpoint in ("ssh://remote.invalid", "tcp://remote.invalid:2376"):
            with self.subTest(endpoint=endpoint):
                self.state["endpoint"] = endpoint
                result = self.run_launcher()
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("info" in call for call in self.calls()))
        self.assertEqual(self.mutations(), [])

    def test_endpoint_override_is_rejected_without_docker_calls(self):
        self.env["DOCKER_HOST"] = "tcp://remote.invalid:2376"
        self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_unsupported_engine_architecture_version_and_missing_daemon_are_read_only(self):
        for changes in ({"runtime": "27.5.1|linux|aarch64"}, {"runtime": "29.0.0|linux|x86_64"},
                        {"runtime": "unverified|linux|aarch64"}, {"daemon_down": True}, {"no_compose": True}):
            with self.subTest(changes=changes):
                self.state = {"image": True, "container": False, **changes}
                self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_foreign_checkout_multiple_containers_and_unowned_volume_are_untouched(self):
        for changes in ({"owner": "another-checkout"}, {"multiple": True},
                        {"volume": True, "volume_owner": "other|state"}):
            with self.subTest(changes=changes):
                self.state = {"image": True, "container": True, **changes}
                self.assertNotEqual(self.run_launcher("stop").returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_wrong_image_or_unsafe_port_binding_never_opens_browser(self):
        for changes in ({"image_metadata": "linux|arm64|other-app"},
                        {"container": True, "boundary": "10001:10001|true|false|8123/tcp=0.0.0.0:8123,;"}):
            with self.subTest(changes=changes):
                self.state = {"image": True, "container": False, **changes}
                self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertFalse((self.root / "browser.json").exists())
        self.assertEqual(self.mutations(), [])

    def test_missing_image_with_remote_selected_builder_fails_without_building(self):
        self.state.update(image=False, builder="Driver: docker-container\nEndpoint: ssh://remote.invalid")
        self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_explicit_input_override_can_be_stopped_but_is_not_silently_reconfigured(self):
        self.state.update(container=True, volume=True, override=True)
        self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.run_launcher("stop").returncode, 0)

    def test_handoff_and_browser_errors_and_shell_trace_cannot_print_capability(self):
        for changes in ({"handoff_error": True}, {"browser_error": True},
                        {"url": "https://remote.invalid/#launch=" + NONCE},
                        {"url": "http://127.0.0.1:8123/#launch=" + NONCE + "\nextra"}):
            with self.subTest(changes=changes):
                self.state = {"image": True, "container": True, **changes}
                self.assertNotEqual(self.run_launcher(trace=True).returncode, 0)

    def test_successful_handoff_disables_inherited_shell_trace(self):
        self.state["container"] = True
        result = self.run_launcher(trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_build_failure_keeps_state_and_does_not_start_service(self):
        self.state.update(image=False, build_error=True, volume=True)
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state["container"])
        self.assertTrue(self.state["volume"])
        self.assertFalse(any("up" in call or "down" in call for call in self.calls()))

    def test_new_container_boundary_is_verified_before_browser_handoff(self):
        self.state["boundary"] = "10001:10001|true|false|8123/tcp=0.0.0.0:8123,;"
        self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertFalse((self.root / "browser.json").exists())
        self.assertFalse(any("exec" in call for call in self.calls()))

    def test_start_failure_does_not_kill_any_process_or_open_browser(self):
        self.state["up_error"] = True
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Port 8123", result.stderr)
        self.assertFalse((self.root / "browser.json").exists())
        self.assertEqual(len(self.mutations()), 1)

    def test_shutdown_failure_is_not_reported_as_success(self):
        self.state.update(container=True, volume=True, down_error=True)
        result = self.run_launcher("stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.state["container"])
        self.assertTrue(self.state["volume"])

    def test_help_and_invalid_actions_need_no_docker(self):
        self.assertEqual(self.run_launcher("--help").returncode, 0)
        self.assertEqual(self.run_launcher("delete-data").returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_make_aliases_have_no_native_runtime_prerequisites(self):
        makefile = (ROOT / "Makefile").read_text()
        for target, action in (("ui_docker", "open"), ("close_ui_docker", "stop"), ("status_ui_docker", "status")):
            self.assertIn(target + ":\n\t@sh scripts/docker_ui.sh " + action, makefile)
        self.assertIn('ui web_app:\n\t@python3 scripts/run_local_web.py $(WEB_ARGS)', makefile)
