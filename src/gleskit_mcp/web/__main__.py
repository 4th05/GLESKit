"""Run the local browser host; native launches remain loopback-only."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import socket
import sys
import webbrowser


def main(*, _argv=None, _bind_host="127.0.0.1", _forward_resource_limits=False):
    parser = argparse.ArgumentParser(description="GLESKit local browser interface (not a public web server).")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--profile", type=Path, help="Private runtime directory; custom profiles default to separate workspaces")
    parser.add_argument("--workspace-source", choices=("configured", "separate"),
                        help="Use the source-configured workspace or a separate profile workspace; remembers the choice")
    parser.add_argument("--ui-root", type=Path)
    parser.add_argument("--opencode", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(_argv)
    root = args.project_root.resolve()
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535.")
    try:
        import uvicorn
        from .app import create_app
        from .common import WebError, read_json
        from .profile import WebProfile
        from .lifecycle import register_host, owned_processes, stop_processes, start_watchdog
    except ImportError:
        parser.exit(1, 'Missing local-web dependencies. Install the web extra in your environment: python -m pip install -e ".[mcp,web]"\n')
    data = (args.profile or root / "var/local-web/profile").resolve()
    ui = (args.ui_root or root / "apps/web/dist-web").resolve()
    if not (ui / "index.html").is_file():
        parser.exit(1, "Browser assets are missing. Run make web_app to build and launch them.\n")
    try:
        profile = WebProfile(root, data, opencode=args.opencode, workspace_source=args.workspace_source,
                             forward_resource_limits=_forward_resource_limits)
    except WebError as exc:
        # A second launcher can only attach to the exact owned instance.
        import httpx
        existing = read_json(data / "web-instance.json", {})
        origin = existing.get("origin", "")
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port and parsed.path in {"", "/"} and not parsed.username and not parsed.password:
            try:
                result = httpx.get(origin + "/api/ping", trust_env=False, timeout=1).json()
                if result.get("app") == "gleskit-local-web" and result.get("instance") == existing.get("instance"):
                    if args.workspace_source:
                        parser.exit(1, "GLESKit is already running. Change workspace in Settings, or stop the launcher before using --workspace-source.\n")
                    print("GLESKit is already running at " + origin, flush=True)
                    if not args.no_browser:
                        webbrowser.open(origin + "/#launch=" + existing["nonce"])
                    return
            except (httpx.HTTPError, ValueError):
                pass
        parser.exit(1, str(exc) + "\n")
    watchdog = None
    try:
        # The profile lock proves there is no other live host using this tree.
        # Clean old detached workers before accepting new UI/model requests.
        register_host(root, data)
        stop_processes(owned_processes(root, data, include_hosts=False))
        watchdog = start_watchdog(root, data)
    except WebError as exc:
        profile.close()
        parser.exit(1, str(exc) + "\n")
    listener = socket.socket()
    try:
        listener.bind((_bind_host, args.port))
    except OSError:
        listener.close()
        profile.close()
        watchdog.stdin.close()
        watchdog.wait(timeout=10)
        parser.exit(1, "Cannot bind the local port. Check network permissions or choose another --port.\n")
    listener.listen(128)
    listener.setblocking(False)
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    try:
        app = create_app(profile, ui, origin)
        server = uvicorn.Server(uvicorn.Config(app, host=_bind_host, port=listener.getsockname()[1],
                                              access_log=False, log_level="critical", timeout_graceful_shutdown=3))
        async def serve():
            task = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started and not task.done():
                await asyncio.sleep(0.05)
            if server.started:
                stop_hint = ("use Docker Compose to stop the container app" if _bind_host == "0.0.0.0"
                             else "Ctrl+C or make close_ui to stop all owned processes")
                print("GLESKit Local: " + origin + " (" + stop_hint + ")", flush=True)
                if not args.no_browser:
                    webbrowser.open(origin + "/#launch=" + app.state.host.nonce)
            await task
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            print("\nGLESKit Local stopped.", flush=True)
    finally:
        listener.close()
        if not profile._lock.closed:
            profile.close()
        watchdog.stdin.close()
        watchdog.wait(timeout=10)


if __name__ == "__main__":
    main()
