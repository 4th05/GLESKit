"""Repo/profile-scoped POSIX process ownership. No ports, keys or global pkill.

Process metadata is inspected only during startup/shutdown. The parent watchdog
blocks on a pipe during normal use; it does not poll processes or serve HTTP.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import shlex
import select
import signal
import subprocess
import sys
import time

from .common import WebError, read_json, write_private


def process_table():
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,uid=,pgid=,lstart=,stat="], text=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise WebError("Cannot inspect process ownership. No unverified processes were stopped.") from None
    rows = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 10:
            continue
        pid, ppid, uid, pgid = map(int, fields[:4])
        if uid == os.getuid() and not fields[9].startswith("Z"):
            rows[pid] = {"pid": pid, "ppid": ppid, "uid": uid, "pgid": pgid, "started": " ".join(fields[4:9])}
    return rows


def identity(row):
    return {key: row[key] for key in ("pid", "uid", "started")}


def matches(expected, actual):
    return bool(actual and identity(actual) == identity(expected))


def descendants(table, seeds):
    owned = {pid: row for pid, row in seeds.items() if matches(row, table.get(pid))}
    while True:
        added = {pid: row for pid, row in table.items() if row["ppid"] in owned and pid not in owned}
        if not added:
            return owned
        owned.update(added)


def working_directories():
    if sys.platform.startswith("linux"):
        result = {}
        for folder in Path("/proc").iterdir():
            if folder.name.isdigit():
                try:
                    if folder.stat().st_uid == os.getuid():
                        result[int(folder.name)] = (folder / "cwd").resolve(strict=True)
                except (OSError, RuntimeError):
                    pass
        return result
    try:
        result = subprocess.run(["lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fn"],
                                text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise WebError("Cannot inspect private-profile ownership. Install lsof; no name-based kill was attempted.") from None
    if result.returncode not in (0, 1):
        raise WebError("Cannot inspect private-profile ownership; no name-based kill was attempted.")
    rows, pid = {}, None
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            rows[pid] = Path(line[1:])
    return rows


def command_args(pid):
    try:
        return shlex.split(subprocess.check_output(["ps", "-p", str(pid), "-o", "args="],
                                                  text=True, stderr=subprocess.DEVNULL, timeout=3).strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def argument(args, name):
    if name in args:
        values = args[args.index(name) + 1:args.index(name) + 2]
        return values[0] if values else None
    return next((arg[len(name) + 1:] for arg in args if arg.startswith(name + "=")), None)


def is_host(pid, project, profile, cwd):
    args = command_args(pid)
    module = argument(args, "-m")
    if module not in {"gleskit_mcp.web", "gleskit_mcp.web.container"}:
        return False
    if module == "gleskit_mcp.web.container" and any(flag in args for flag in ("--healthcheck", "--launch-info")):
        return False
    root = argument(args, "--project-root")
    if root and (cwd / root).resolve() != project:
        return False
    selected = argument(args, "--profile")
    if selected:
        return (cwd / selected).resolve() == profile
    return profile == project / "var/local-web/profile"


def registry_path(project, profile):
    name = hashlib.sha256(str(profile).encode()).hexdigest()
    return project / "var/local-web/process-owners" / (name + ".json")


def register_host(project, profile):
    row = process_table().get(os.getpid())
    if not row:
        raise WebError("Cannot verify the local host process identity.")
    write_private(registry_path(project, profile), {"schema": 1, "project": str(project), "profile": str(profile),
                                                   "host": identity(row)})
    return identity(row)


def profiles(project):
    result = {project / "var/local-web/profile": None}
    folder = project / "var/local-web/process-owners"
    if folder.is_symlink():
        raise WebError("Unsafe process ownership registry; no cleanup was attempted.")
    if folder.is_dir():
        for path in folder.glob("*.json"):
            value = read_json(path, {})
            if value.get("schema") == 1 and value.get("project") == str(project):
                profile = Path(value.get("profile", ""))
                if profile.is_absolute() and profile == profile.resolve() and path == registry_path(project, profile):
                    result[profile] = value.get("host")
    return result


def owned_processes(project, selected=None, *, include_hosts=True):
    project = project.resolve()
    table, directories = process_table(), working_directories()
    known = profiles(project)
    if selected is not None:
        if selected not in known:
            raise WebError("This profile is not registered to this checkout; refusing to stop another app's processes.")
        known = {selected: known[selected]}
    seeds = {}
    for profile, host in known.items():
        if profile.is_symlink() or not profile.is_dir():
            continue
        if read_json(profile / "web-profile.json", {}).get("schema") != 1:
            continue
        home = profile / "engine/home"
        if home.is_symlink() or home.resolve() != home:
            raise WebError("Unsafe private engine directory; no cleanup was attempted.")
        for pid, cwd in directories.items():
            # This is an app-private runtime directory, never the project,
            # research workspace or a general R/Python working directory.
            if pid in table and cwd.is_relative_to(home):
                seeds[pid] = table[pid]
        if include_hosts:
            # Registered identities are authoritative and do not depend on
            # parsing ps's flattened argv (which loses quoting in paths).
            if host and matches(host, table.get(host.get("pid"))):
                seeds[host["pid"]] = table[host["pid"]]
            # Upgrade path: old hosts have no ownership record. Require the
            # exact module AND exact project argument, not a substring match.
            for pid in table:
                if directories.get(pid) != project:
                    continue
                args = command_args(pid)
                if argument(args, "--project-root") and is_host(pid, project, profile, directories[pid]):
                    seeds[pid] = table[pid]
    owned = descendants(table, seeds)
    # Never signal the cleanup command, its shell, terminal or other ancestors.
    pid = os.getpid()
    while pid in table:
        owned.pop(pid, None)
        pid = table[pid]["ppid"]
    return owned


def stop_processes(seeds, *, grace=5):
    """Recheck PID/start identity before each signal, including forced cleanup."""
    known, signalled = dict(seeds), set()
    deadline = time.monotonic() + grace
    forced = False
    while True:
        table = process_table()
        live = descendants(table, known)
        known.update(live)
        if not live:
            return
        if time.monotonic() >= deadline:
            if forced:
                raise WebError(f"{len(live)} owned process(es) did not exit; cleanup is incomplete.")
            forced, deadline = True, time.monotonic() + 2
            signalled.clear()
        for pid, row in live.items():
            fingerprint = (pid, row["started"])
            if fingerprint not in signalled:
                try:
                    if matches(row, process_table().get(pid)):
                        os.kill(pid, signal.SIGKILL if forced else signal.SIGTERM)
                except ProcessLookupError:
                    pass
                signalled.add(fingerprint)
        time.sleep(0.1)


def stop_child(child):
    if child and child.poll() is None:
        table = process_table()
        if child.pid in table:
            stop_processes(descendants(table, {child.pid: table[child.pid]}), grace=3)
        child.wait(timeout=2)


def close_ui(project, selected=None):
    owned = owned_processes(project, selected)
    count = len(owned)
    stop_processes(owned)
    remaining = owned_processes(project, selected)
    if remaining:
        stop_processes(remaining)
    if owned_processes(project, selected):
        raise WebError("Owned processes remain; shutdown is incomplete.")
    return count


def start_watchdog(project, profile):
    # No provider credentials, passwords, launch nonces or user environment are
    # copied to this process. EOF on stdin is the parent-death notification.
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": str(project / "src"), "LC_ALL": "C"}
    child = subprocess.Popen([sys.executable, "-m", "gleskit_mcp.web.lifecycle", "--project-root", str(project),
                              "--profile", str(profile), "--watch-parent"], cwd=project, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    if not select.select([child.stdout], [], [], 5)[0] or child.stdout.read(1) != b"R":
        child.stdin.close()
        stop_child(child)
        raise WebError("Cannot start the private process-cleanup watchdog; UI startup was cancelled.")
    child.stdout.close()
    return child


def watch_parent(project, profile):
    sys.stdout.buffer.write(b"R")
    sys.stdout.buffer.flush()
    sys.stdin.buffer.read()  # Blocks without polling or a listening socket.
    # Do not race a new host that has already acquired the same profile lock.
    # New hosts clean stale private-profile workers before accepting requests.
    with (profile / ".profile.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stop_processes(owned_processes(project, profile, include_hosts=False))


def main():
    parser = argparse.ArgumentParser(description="Stop only this checkout's owned GLESKit web processes; retain all saved data.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--watch-parent", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    project = args.project_root.resolve()
    profile = args.profile.resolve() if args.profile else None
    try:
        if args.watch_parent:
            watch_parent(project, profile)
        else:
            count = close_ui(project, profile)
            print(f"GLESKit UI stopped ({count} owned process(es); saved data retained)." if count else "GLESKit UI is already stopped; no owned processes found.")
    except (WebError, PermissionError) as exc:
        print(str(exc) if isinstance(exc, WebError) else "Permission denied while stopping an owned process; cleanup is incomplete.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
