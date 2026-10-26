"""Explicit Docker entrypoint; the browser authentication boundary is unchanged."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys

PROJECT = Path("/app")
PROFILE = PROJECT / "var/local-web/profile"
PORT = 8123
ORIGIN = "http://127.0.0.1:8123"
INTERNAL_BIND = "0.0.0.0"


def ping(instance=None):
    """Lightweight and unauthenticated: no profile, engine, MCP or R creation."""
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
    try:
        connection.request("GET", "/api/ping", headers={"Host": "127.0.0.1:8123"})
        response = connection.getresponse()
        payload = json.loads(response.read(2048))
        return (response.status == 200 and payload.get("app") == "gleskit-local-web"
                and bool(re.fullmatch(r"[a-f0-9]{32}", payload.get("instance", "")))
                and (instance is None or payload["instance"] == instance))
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def launch_url():
    """Explicit private handoff for a local launcher, never a log/health endpoint."""
    from .common import WebError, read_json
    from .lifecycle import command_args, argument, matches, process_table, registry_path

    try:
        fd = os.open(PROFILE / "web-instance.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise ValueError()
            value = json.load(stream)
        if (value.get("schema") != 1 or value.get("origin") != ORIGIN
                or type(value.get("pid")) is not int
                or not re.fullmatch(r"[a-f0-9]{32}", value.get("instance", ""))
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value.get("nonce", ""))):
            raise ValueError()
        record = read_json(registry_path(PROJECT, PROFILE), {})
        host = record.get("host", {})
        if (record.get("schema") != 1 or record.get("project") != str(PROJECT)
                or record.get("profile") != str(PROFILE) or host.get("pid") != value["pid"]
                or not matches(host, process_table().get(value["pid"]))
                or argument(command_args(value["pid"]), "-m") != "gleskit_mcp.web.container"
                or not ping(value["instance"])):
            raise ValueError()
        return ORIGIN + "/#launch=" + value["nonce"]
    except (OSError, ValueError, TypeError, AttributeError, KeyError, WebError):
        raise WebError("No verified running GLESKit container instance is available.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="GLESKit private local-container host.")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--healthcheck", action="store_true", help="Check only the local ping endpoint.")
    operation.add_argument("--launch-info", action="store_true", help="Private launch URL for capture by a local launcher; do not log it.")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return 0 if ping() else 1
    if args.launch_info:
        from .common import WebError
        try:
            print(launch_url())
            return 0
        except WebError as exc:
            parser.exit(1, str(exc) + "\n")
    if not sys.platform.startswith("linux") or os.getuid() == 0:
        parser.exit(1, "Use the unprivileged Docker service; native launches use make ui.\n")
    state = PROJECT / "var"
    if state.is_symlink() or not state.is_dir() or state.resolve() != state or state.stat().st_uid != os.getuid():
        parser.exit(1, "The private Docker state volume is missing or has the wrong owner.\n")
    os.umask(0o077)
    from .__main__ import main as serve
    serve(_bind_host=INTERNAL_BIND, _forward_resource_limits=True, _argv=[
        "--project-root", str(PROJECT), "--profile", str(PROFILE),
        "--ui-root", "/app/apps/web/dist-web", "--opencode", "/opt/gleskit/bin/opencode",
        "--port", str(PORT), "--no-browser",
    ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
