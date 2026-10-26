#!/usr/bin/env python3
"""One developer/researcher entrypoint; never upgrades the existing lab venv."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(args, *, cwd=ROOT, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("GLESKit needs Python 3.11 or newer. Install it, then run this launcher again.")
    if os.name == "nt":
        raise SystemExit("Native Windows is not validated. Use macOS, or a separately tested Linux/WSL environment.")
    # Preserve the existing scientific environment. If it cannot host the UI,
    # create a separate environment instead of installing/upgrading inside it.
    candidates = [ROOT / ".venv/bin/python", ROOT / ".venv-web/bin/python", Path(sys.executable)]
    python = None
    for candidate in candidates:
        if candidate.is_file() and subprocess.run([str(candidate), "-c", "import importlib.util; assert all(importlib.util.find_spec(n) for n in ('starlette','uvicorn','httpx','mcp','pandas'))"],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            python = candidate
            break
    environment = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR") if name in os.environ}
    if python is None:
        folder = ROOT / ".venv-web"
        if not folder.exists():
            print("Preparing a separate web environment. Your existing .venv and results are unchanged.", flush=True)
            run([sys.executable, "-m", "venv", str(folder)], env=environment)
        python = folder / "bin/python"
        print("Installing the project's MCP/web dependencies in .venv-web (network access required).", flush=True)
        install_env = {**environment, "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        run([str(python), "-m", "pip", "install", "-e", ".[mcp,web]"], env=install_env)
    app = ROOT / "apps/web"
    sources = [*app.joinpath("src").rglob("*.js"), *app.joinpath("src").rglob("*.css"),
               *app.joinpath("src/assets").rglob("*"), app / "index.html", app / "vite.config.js",
               app / "package.json", app / "package-lock.json", app / "scripts/build-web.mjs"]
    built = app / "dist-web/index.html"
    if not built.is_file() or any(path.stat().st_mtime > built.stat().st_mtime for path in sources):
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("Node.js/npm is required to build this source checkout's UI once. Install Node.js, then rerun. No Rust/Tauri build is needed.")
        if not (app / "node_modules/vite/bin/vite.js").is_file():
            print("Installing frontend build dependencies (no package scripts or global installs).", flush=True)
            run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=app, env=environment)
        print("Building local browser assets. Subsequent launches reuse them until sources change.", flush=True)
        run([npm, "run", "build:web"], cwd=app, env=environment)
    environment["PYTHONPATH"] = str(ROOT / "src")
    os.execve(str(python), [str(python), "-m", "gleskit_mcp.web", "--project-root", str(ROOT), *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()
