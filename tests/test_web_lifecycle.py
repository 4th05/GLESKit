"""Scoped shutdown tests. Real subprocess tests use disposable local profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gleskit_mcp.web import lifecycle as life
from gleskit_mcp.web.common import WebError, write_private

ROOT = Path(__file__).resolve().parents[1]


def process(pid, parent=1, group=None, started="Wed Oct 21 20:00:00 2026"):
    return {"pid": pid, "ppid": parent, "uid": os.getuid(), "pgid": group or pid, "started": started}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-lifecycle-unit-")
        self.project = Path(self.temp.name).resolve()
        self.profile = self.project / "var/local-web/profile"
        self.home = self.profile / "engine/home"
        self.home.mkdir(parents=True)
        write_private(self.profile / "web-profile.json", {"schema": 1})

    def tearDown(self):
        self.temp.cleanup()

    def test_private_profile_orphans_and_all_descendants_are_owned_not_other_r_python(self):
        table = {101: process(101), 102: process(102, 101), 103: process(103, 102), 900: process(900)}
        directories = {101: self.home, 102: self.home, 103: self.project / "temporary-r-work", 900: self.project}
        with patch.object(life, "process_table", return_value=table), patch.object(life, "working_directories", return_value=directories), patch.object(life, "command_args", return_value=["python", "-m", "ipykernel_launcher"]):
            self.assertEqual(set(life.owned_processes(self.project)), {101, 102, 103})

    def test_registered_hosts_are_scoped_to_project_and_selected_profile(self):
        other = self.project / "custom-profile"
        (other / "engine/home").mkdir(parents=True)
        write_private(other / "web-profile.json", {"schema": 1})
        table = {101: process(101), 201: process(201)}
        for profile, pid in ((self.profile, 101), (other, 201)):
            write_private(life.registry_path(self.project, profile), {"schema": 1, "project": str(self.project), "profile": str(profile), "host": life.identity(table[pid])})
        def args(pid):
            return [sys.executable, "-m", "gleskit_mcp.web", "--project-root", str(self.project)] + (["--profile", str(other)] if pid == 201 else [])
        with patch.object(life, "process_table", return_value=table), patch.object(life, "working_directories", return_value={pid: self.project for pid in table}), patch.object(life, "command_args", side_effect=args):
            self.assertEqual(set(life.owned_processes(self.project)), {101, 201})
            self.assertEqual(set(life.owned_processes(self.project, other)), {201})

    def test_reused_pid_is_not_killed_and_signals_are_identity_checked(self):
        old = process(101)
        reused = process(101, started="Wed Oct 21 21:00:00 2026")
        with patch.object(life, "process_table", side_effect=[{101: old}, {101: reused}, {101: reused}]), patch.object(life.os, "kill") as kill, patch.object(life.time, "sleep"):
            life.stop_processes({101: old})
        kill.assert_not_called()

    def test_host_modules_are_exact_and_container_helpers_are_not_hosts(self):
        for module, expected in (("gleskit_mcp.web", True), ("gleskit_mcp.web.container", True),
                                 ("gleskit_mcp.web.container.extra", False), ("ipykernel_launcher", False)):
            args = [sys.executable, "-m", module, "--project-root", str(self.project)]
            with self.subTest(module=module), patch.object(life, "command_args", return_value=args):
                self.assertEqual(life.is_host(101, self.project, self.profile, self.project), expected)
        for flag in ("--healthcheck", "--launch-info"):
            with self.subTest(flag=flag), patch.object(life, "command_args", return_value=[
                    sys.executable, "-m", "gleskit_mcp.web.container", flag]):
                self.assertFalse(life.is_host(101, self.project, self.profile, self.project))

    def test_unregistered_profile_from_another_checkout_is_not_a_target(self):
        other = self.project / "other-project/profile"
        (other / "engine/home").mkdir(parents=True)
        write_private(other / "web-profile.json", {"schema": 1})
        with patch.object(life, "process_table", return_value={}), patch.object(life, "working_directories", return_value={}):
            with self.assertRaisesRegex(WebError, "not registered"):
                life.owned_processes(self.project, other)

    def test_registered_identity_does_not_depend_on_flattened_process_arguments(self):
        row = process(101)
        write_private(life.registry_path(self.project, self.profile), {"schema": 1, "project": str(self.project),
                      "profile": str(self.profile), "host": life.identity(row)})
        with patch.object(life, "process_table", return_value={101: row}), patch.object(life, "working_directories", return_value={101: self.project}), patch.object(life, "command_args", return_value=[]):
            self.assertEqual(set(life.owned_processes(self.project)), {101})

    def test_term_then_kill_only_verified_owned_processes(self):
        table = {101: process(101), 900: process(900)}
        clock, signals = [0], []
        def kill(pid, sig):
            signals.append((pid, sig))
            if sig == signal.SIGKILL: table.pop(pid)
        with patch.object(life, "process_table", side_effect=lambda: table.copy()), patch.object(life.os, "kill", side_effect=kill), patch.object(life.time, "monotonic", side_effect=lambda: clock[0]), patch.object(life.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            life.stop_processes({101: table[101]}, grace=0.05)
        self.assertEqual(signals, [(101, signal.SIGTERM), (101, signal.SIGKILL)])
        self.assertIn(900, table)

    def test_unavailable_process_metadata_fails_closed(self):
        with patch.object(life.subprocess, "check_output", side_effect=OSError()), patch.object(life.os, "kill") as kill:
            with self.assertRaisesRegex(WebError, "ownership"):
                life.close_ui(self.project)
        kill.assert_not_called()

    def test_make_targets_use_scoped_shutdown_not_global_process_names(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn("ui web_app:", makefile)
        self.assertIn("close_ui:\n\t@python3 scripts/close_local_web.py", makefile)
        self.assertIn("kill_opencode: close_ui", makefile)
        self.assertNotIn("pkill -f", makefile)


@unittest.skipUnless(os.environ.get("GLESKIT_RUN_WEB_LIFECYCLE_TESTS") == "1", "explicit disposable subprocess lifecycle test")
class RealLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-lifecycle-real-")
        self.project = Path(self.temp.name).resolve() / "project"
        self.project.mkdir()
        (self.project / "src").symlink_to(ROOT / "src", target_is_directory=True)
        shutil.copytree(ROOT / "docs/opencode", self.project / "docs/opencode", ignore=shutil.ignore_patterns("opencode.json"))
        self.ui = self.project / "ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text("<!doctype html><title>Disposable local test</title>")
        self.profile = self.project / "var/local-web/profile"
        self.children = []
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": str(ROOT / "src"), "LC_ALL": "C"}

    def tearDown(self):
        try:
            life.close_ui(self.project)
        finally:
            for child in self.children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                if child.stdout: child.stdout.close()
            self.temp.cleanup()

    def spawn(self, args, cwd, **kwargs):
        child = subprocess.Popen(args, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True, **kwargs)
        self.children.append(child)
        return child

    def start_host(self):
        host = self.spawn([sys.executable, "-m", "gleskit_mcp.web", "--project-root", str(self.project),
                           "--ui-root", str(self.ui), "--no-browser"], self.project)
        deadline = time.monotonic() + 15
        handoff = self.profile / "web-instance.json"
        while time.monotonic() < deadline:
            if host.poll() is not None: self.fail("Disposable host did not start")
            if handoff.is_file():
                info = json.loads(handoff.read_text())
                return host, info
            time.sleep(0.1)
        self.fail("Disposable host startup timed out")

    def workers(self):
        home = self.profile / "engine/home"
        # A nested worker starts a new session, just like the R analysis runner.
        code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)'],start_new_session=True); print(p.pid,flush=True); time.sleep(600)"
        parent = self.spawn([sys.executable, "-c", code], home)
        worker = int(parent.stdout.readline())
        return parent, worker

    def wait_gone(self, pids):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not set(pids) & set(life.process_table()): return
            time.sleep(0.1)
        self.fail("Owned process survived shutdown")

    def test_close_command_is_idempotent_preserves_data_and_unrelated_kernel(self):
        host, info = self.start_host()
        parent, worker = self.workers()
        unrelated = self.spawn([sys.executable, "-c", "import time; time.sleep(600)"], self.project.parent)
        marker = self.profile / "workspace/keep-research-result.txt"
        marker.write_text("unchanged")
        for _ in range(2):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/close_local_web.py"), "--project-root", str(self.project)],
                                    env=self.env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_gone([host.pid, parent.pid, worker])
        self.assertIsNone(unrelated.poll())
        self.assertEqual(marker.read_text(), "unchanged")
        self.assertFalse(life.owned_processes(self.project))

    def test_abrupt_host_death_triggers_watchdog_for_detached_workers(self):
        host, info = self.start_host()
        parent, worker = self.workers()
        watchdogs = [pid for pid, row in life.process_table().items() if row["ppid"] == host.pid]
        self.assertEqual(len(watchdogs), 1, "Exactly one socket-free watchdog")
        host.kill()
        host.wait(timeout=5)
        self.wait_gone([parent.pid, worker, *watchdogs])
        self.assertFalse(life.owned_processes(self.project))

    def test_ctrl_c_and_real_r_worker_cleanup(self):
        rscript = shutil.which("Rscript")
        if not rscript: self.skipTest("Rscript is not installed")
        host, info = self.start_host()
        worker = self.spawn([rscript, "--vanilla", "-e", "Sys.sleep(600)"], self.profile / "engine/home")
        host.send_signal(signal.SIGINT)
        host.wait(timeout=15)
        self.wait_gone([worker.pid])
        self.assertFalse(life.owned_processes(self.project))
