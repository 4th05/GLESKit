#!/usr/bin/env python3
"""Require actual runtime privacy evidence before ANY app resource copy/build."""
import argparse
import hashlib
import json
from pathlib import Path
if __package__:
    from .patterns import sha
else:
    from patterns import sha

POLICY_FILES = ["scripts/release/" + name + ".py" for name in
                ("patterns", "asset_metadata", "privacy_gate", "publication", "licensing_gate", "runtime_privacy_gate")]
POLICY_FILES += ["src/gleskit_mcp/web/" + name + ".py" for name in
                ("__init__", "__main__", "container", "app", "chat", "commands", "common", "credentials", "engine", "files", "instructions", "jobs", "lifecycle", "profile", "providers", "redaction", "relay", "runtime")]
POLICY_FILES += ["Dockerfile", ".dockerignore", "compose.yaml", "docker/runtime.json",
                 "docker/python.lock", "docker/r-packages.lock.json", "docker/install-r.R", "docker/third-party-inventory.json", "scripts/docker_ui.sh",
                 "src/gleskit_mcp/mcp_server.py", "scripts/release/docker_smoke.py", ".github/workflows/docker-validation.yml"]
POLICY_FILES += ["apps/web/src/" + name for name in
                ("main.js", "transport.web.js", "web-controls.js", "model-picker.js", "settings-dialog.js",
                 "notifications.js", "markdown.js", "cohort-download-clock.js", "styles.css", "web.css")]
POLICY_FILES += ["apps/web/vite.config.js", "apps/web/scripts/build-web.mjs", "apps/web/package.json",
                 "apps/web/package-lock.json", "apps/web/index.html", "scripts/run_local_web.py", "scripts/close_local_web.py"]
POLICY_FILES += ["apps/web/src/assets/GESKit_icon.svg", "apps/web/src/assets/GESKit_icon_no_bg.png"]
REQUIRED_CASES = {"success-a", "success-b", "unauthorized", "rate-limit", "server-error", "malformed", "timeout"}


def policy_identity(root):
    digest = hashlib.sha256()
    for name in sorted(POLICY_FILES):
        digest.update(name.encode() + b"\0" + bytes.fromhex(sha(Path(root) / name)))
    return digest.hexdigest()


def require_runtime_privacy(root, resources, report_path):
    if report_path is None or not Path(report_path).is_file():
        raise RuntimeError("Packaging blocked: a complete, passing runtime privacy report is required")
    report = json.loads(Path(report_path).read_text())
    if (report.get("ok") is not True or type(report.get("profile_credential_canary_count")) is not int
        or report["profile_credential_canary_count"] != 0):
        raise RuntimeError("Packaging blocked: engine credential-persistence gate failed or is incomplete")
    if report.get("network_observation_gate_passed") is not True:
        raise RuntimeError("Packaging blocked: full process-network observation has not passed")
    if set(report.get("cases_observed", [])) != REQUIRED_CASES or report.get("authorization_route_and_namespaced_model_correct") is not True:
        raise RuntimeError("Packaging blocked: provider failure/switching coverage is incomplete")
    ready = json.loads((resources / "ready.json").read_text())
    if not isinstance(ready.get("integration_files_sha256"), str) or not ready["integration_files_sha256"]:
        raise RuntimeError("Packaging blocked: runtime inventory identity is missing")
    if (report.get("engine_sha256") != sha(resources / "runtime/opencode")
        or report.get("runtime_inventory_sha256") != ready.get("integration_files_sha256")
        or report.get("policy_identity") != policy_identity(root)):
        raise RuntimeError("Packaging blocked: runtime privacy evidence is stale")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    require_runtime_privacy(args.root, args.resources, args.report)
    print("Runtime privacy evidence: PASS (public release not authorized)")
