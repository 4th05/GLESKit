from __future__ import annotations

from dataclasses import replace
import fcntl
import os
from pathlib import Path
import shutil
import sys

from gleskit_mcp.config import load_settings
from .common import WebError, private_dir, read_json, write_private


class WebProfile:
    """Private runtime state, with an explicit locally configured workspace."""

    def __init__(self, project: Path, data: Path, *, opencode: Path | None = None, workspace_source: str | None = None,
                 forward_resource_limits: bool = False):
        self.project = project.resolve()
        self.forward_resource_limits = forward_resource_limits
        if data.is_symlink():
            raise WebError("The web profile cannot be a symlink.")
        self.data = data.resolve()
        if self.data == self.project or self.project.is_relative_to(self.data):
            raise WebError("The project directory cannot be used as a web profile.")
        marker = self.data / "web-profile.json"
        if self.data.exists() and any(self.data.iterdir()) and not marker.is_file():
            raise WebError("Refusing to adopt an existing directory. Choose a fresh web profile.")
        private_dir(self.data)
        self._lock = (self.data / ".profile.lock").open("a+")
        os.chmod(self.data / ".profile.lock", 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise WebError("This web profile is already open. Use its browser window or stop it first.") from None
        if marker.exists() and read_json(marker).get("schema") != 1:
            self.close()
            raise WebError("Unsupported web profile version; existing files were not changed.")
        write_private(marker, {"schema": 1})
        self.separate_workspace = private_dir(self.data / "workspace")
        self.heartbeat = private_dir(self.data / "heartbeat") / "workspace.json"
        self.cache = private_dir(self.data / "cache")
        self.home = private_dir(self.data / "engine/home")
        self.engine_config = private_dir(self.data / "engine/config/opencode") / "opencode.json"
        self.instructions = private_dir(self.data / "instructions")
        self.settings_file = private_dir(self.data / "config") / "gleskit.local.json"
        self.staging = private_dir(self.data / "staging")
        source = self.project / "docs/opencode/skills/gleskit-research-assistant"
        target = private_dir(self.engine_config.parent / "skills") / "gleskit-research-assistant"
        if not target.exists():
            if not source.is_dir() or any(p.is_symlink() for p in source.rglob("*")):
                raise WebError("Authored GLESKit skill files are missing or contain symlinks.")
            shutil.copytree(source, target)
        prompt = self.instructions / "gleskit-agent-prompt.md"
        if not prompt.exists():
            prompt.write_bytes((self.project / "docs/opencode/prompts/agents/gleskit-agent-prompt.md").read_bytes())
            prompt.chmod(0o600)
        self.prompt = prompt
        self.skill = target
        # Runtime/config/cache remain private. Normal launches use the same
        # configured workspace as the desktop; custom profiles stay isolated.
        # Never write the source checkout's local config or scientific library.
        base = load_settings(self.project)
        # Child processes intentionally do not inherit the user's PATH. Resolve
        # the existing R executable now so Homebrew/framework installs still work.
        rscript = shutil.which(base.rscript_bin) or base.rscript_bin
        self.existing_workspace = base.workspace_default_root
        selection = read_json(self.data / "workspace-selection.json", {})
        default = "configured" if self.data == self.project / "var/local-web/profile" else "separate"
        self.workspace_source = workspace_source or selection.get("source") or default
        try:
            self.workspace = self.workspace_path(self.workspace_source)
        except WebError:
            self.close()
            raise
        self.settings = replace(base, workspace_default_root=self.workspace,
                                runtime_root=self.heartbeat.parent, runtime_manifest_path=self.heartbeat,
                                reference_root=self.data / "references",
                                reference_manifest_path=self.data / "references/manifest.json",
                                rscript_bin=rscript,
                                local_config_path=self.settings_file)
        if not self.settings_file.exists():
            write_private(self.settings_file, {"paths": {
                "workspace_root": str(self.workspace), "runtime_root": str(self.heartbeat.parent),
                "reference_root": str(self.settings.reference_root), "r_lib_root": str(base.r_lib_root)},
                "tools": {"rscript_bin": rscript}})
        candidate = opencode or (self.project / "var/macos-stage2-prerequisites/runtime/opencode")
        self.opencode = str(candidate.resolve()) if candidate.is_file() else shutil.which("opencode")
        if workspace_source is not None:
            self.select_workspace(workspace_source)

    def workspace_path(self, source):
        if source not in {"configured", "configured-read-only", "separate"}:
            raise WebError("Unknown workspace source. Select configured or separate.")
        root = self.separate_workspace if source == "separate" else self.existing_workspace
        if root.is_symlink():
            raise WebError("The workspace root cannot be a symlink.")
        if source == "configured-read-only" and not (root / "sessions").is_dir():
            raise WebError("No existing configured workspace was found.", 404)
        return root.resolve()

    def select_workspace(self, source):
        root = self.workspace_path(source)
        config = read_json(self.settings_file)
        config.setdefault("paths", {})["workspace_root"] = str(root)
        write_private(self.settings_file, config)
        # Store only a mode, never copy source sessions into the runtime profile.
        write_private(self.data / "workspace-selection.json", {"source": source})
        self.workspace, self.workspace_source = root, source
        self.settings = replace(self.settings, workspace_default_root=root)

    def environment(self) -> dict[str, str]:
        from .engine import PRIVACY_ENV
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
               "HOME": str(self.home), "PYTHONPATH": str(self.project / "src"),
               "XDG_CONFIG_HOME": str(self.data / "engine/config"),
               "XDG_DATA_HOME": str(self.data / "engine/data"),
               "XDG_STATE_HOME": str(self.data / "engine/state"),
               "XDG_CACHE_HOME": str(self.cache / "engine"),
               "OPENCODE_CONFIG": str(self.engine_config),
               "OPENCODE_CONFIG_DIR": str(self.engine_config.parent),
               "GLESKIT_DEFAULT_CONFIG_PATH": str(self.project / "config/gleskit.defaults.json"),
               "GLESKIT_LOCAL_CONFIG_PATH": str(self.settings_file),
               "GLESKIT_WORKSPACE_HOME": str(self.workspace),
               "GLESKIT_RUNTIME_HOME": str(self.heartbeat.parent),
               "GLESKIT_REFERENCE_HOME": str(self.settings.reference_root),
               "GLESKIT_R_LIB_HOME": str(self.settings.r_lib_root),
               "GLESKIT_RSCRIPT_BIN": self.settings.rscript_bin,
               "MPLCONFIGDIR": str(self.cache / "matplotlib")}
        if self.forward_resource_limits:
            env.update({"GLESKIT_LOCAL_MAX_WORKERS": str(self.settings.local_max_workers),
                        "GLESKIT_R_THREADS": str(self.settings.r_threads),
                        "GLESKIT_R_MAX_VSIZE": self.settings.r_max_vsize})
        if getattr(self, "figure_origin", None):
            env.update({"GLESKIT_WEB_PROFILE": str(self.data), "GLESKIT_WEB_ORIGIN": self.figure_origin})
        env.update(PRIVACY_ENV)
        return env

    def mcp_command(self) -> list[str]:
        return [sys.executable, "-m", "gleskit_mcp", "--project-root", str(self.project),
                "serve", "--transport", "stdio"]

    def close(self) -> None:
        if not self._lock.closed:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
