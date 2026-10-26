#!/usr/bin/env python3
"""Hash-bound publication inventory and clean export; never copy Git history.

Inventories are private review inputs, NOT publication approval. Every included
or replacement file requires an explicit review decision for its exact bytes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
if __package__:
    from .privacy_gate import inspect, private_filename
    from .licensing_gate import require_licensing_review
else:
    from privacy_gate import inspect, private_filename
    from licensing_gate import require_licensing_review

BOUNDARY = "publication-boundary.json"

ROOT_FILES = {".gitignore", "AGENTS.md", "README.md", "Makefile", "pyproject.toml", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
              "Dockerfile", ".dockerignore", "compose.yaml"}
DOCKER_FILES = {"docker/runtime.json", "docker/python.lock", "docker/r-packages.lock.json", "docker/install-r.R",
                "docker/third-party-inventory.json"}
CI_FILES = {".github/workflows/docker-validation.yml"}
SOURCE_ROOTS = ("src/", "tests/", "scripts/", "apps/web/")
DOCS = {"docs/mcp_contract.md", "docs/tools_providers.md", "docs/opencode/README.md",
        "docs/bixbench_evaluation.md", "docs/repobench_evaluation.md", "docs/evaluation_results.md", "docs/privacy.md", "docs/local-web.md",
        "docs/licensing.md", "docs/licensing-review.json", "docs/release-privacy-status.md", "docs/runtime-baseline.md",
        "docs/assets/gleskit-architecture.png"}
NEVER_EXPORT = ("var/", ".gleskit/", "notebooks/", "tmp/", ".agents/", ".venv/", "node_modules/")
TEXT_SUFFIXES = {".py", ".R", ".rs", ".js", ".mjs", ".css", ".html", ".json", ".md",
                 ".toml", ".lock", ".sh", ".spec", ".xml", ".svg"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in name:
        raise RuntimeError("Unsafe publication path")
    return name


def classification(name):
    if private_filename(name) or name.startswith(NEVER_EXPORT) or name == ".openclaude-profile.json":
        return "EXCLUDE", "private or unapproved research/developer artifact"
    if name.startswith("docs/") and name not in DOCS and not name.startswith("docs/opencode/"):
        return "EXCLUDE", "unapproved manuscript/design/research material"
    if name in ROOT_FILES | DOCKER_FILES | CI_FILES or name in DOCS or name.startswith("config/") or name.startswith("docs/opencode/"):
        return "INCLUDE", "product configuration, contract or authored prompt/skill; content review required"
    if name.startswith(SOURCE_ROOTS) and (Path(name).suffix in TEXT_SUFFIXES or name.endswith("/Cargo.lock")):
        return "INCLUDE", "product source/build/test input; content review required"
    if name.startswith("apps/web/src/assets/") and Path(name).suffix in {".svg", ".png"}:
        return "INCLUDE", "brand asset; visual and metadata review required"
    if name == "data/demo_deseq_results.tsv":
        return "INCLUDE", "ten-row gene-only illustrative fixture; no subject identifiers"
    return "EXCLUDE", "not explicitly eligible for the product source snapshot"


def inventory(root, revision="HEAD"):
    revision = git(root, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    files = []
    for entry in git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not entry: continue
        head, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = head.decode().split()
        name = safe_name(raw_name.decode())
        data = git(root, "cat-file", "blob", oid) if kind == "blob" else b""
        decision, reason = classification(name)
        findings = inspect(data, name)
        if mode not in {"100644", "100755"}:
            decision, reason = "EXCLUDE", "symlink/submodule/special input is not exportable"
        if findings and decision == "INCLUDE":
            decision, reason = "SANITIZE", "detected privacy findings require remediation"
        files.append({"file":name, "sha256":digest(data), "mode":mode, "decision":decision,
                      "reason":reason, "rules":findings, "reviewed":decision == "EXCLUDE"})
    return {"schema":1, "source_commit":revision, "files":files,
            "review_complete":False, "privacy_ready_for_stage3":False, "ready_for_public_release":False}


def validate(root, manifest):
    if git(root, "rev-parse", "HEAD").decode().strip() != manifest["source_commit"]:
        raise RuntimeError("Publication approval is stale for this source revision")
    current = inventory(root, manifest["source_commit"])
    rows = manifest["files"]
    if len({row["file"] for row in rows}) != len(rows): raise RuntimeError("Duplicate publication entries")
    actual = {row["file"]:row for row in current["files"]}
    if set(actual) != {row["file"] for row in rows}: raise RuntimeError("Publication inventory is incomplete")
    if not manifest.get("review_complete"): raise RuntimeError("Publication review is incomplete")
    for row in rows:
        name = safe_name(row["file"])
        if row["sha256"] != actual[name]["sha256"] or row["mode"] != actual[name]["mode"]:
            raise RuntimeError("Publication source hash/mode changed")
        if row["decision"] not in {"INCLUDE", "SANITIZE", "EXCLUDE"} or row.get("reviewed") is not True:
            raise RuntimeError("An explicit hash-bound review decision is required")
        if row["decision"] == "EXCLUDE": continue
        if private_filename(name) or name.startswith(NEVER_EXPORT) or name == ".openclaude-profile.json" or row["mode"] not in {"100644", "100755"}:
            raise RuntimeError("Private state or non-regular input cannot be exported")
        if row["decision"] == "INCLUDE" and actual[name]["rules"]:
            raise RuntimeError("Included source has privacy findings")
        if row["decision"] == "SANITIZE":
            replacement = safe_name(row.get("replacement", ""))
            if private_filename(replacement) or replacement.startswith(NEVER_EXPORT):
                raise RuntimeError("Private state cannot be used as a publication replacement")
            if replacement not in actual or row.get("replacement_sha256") != actual[replacement]["sha256"]:
                raise RuntimeError("Replacement must be reviewed committed bytes with an exact hash")
            if actual[replacement]["rules"] or actual[replacement]["mode"] not in {"100644", "100755"}:
                raise RuntimeError("Replacement failed privacy checks")
    return actual


def export(root, manifest, target):
    validate(root, manifest)
    if target.exists(): raise RuntimeError("Clean export requires a new directory")
    selected = {}
    for row in manifest["files"]:
        if row["decision"] == "EXCLUDE": continue
        name = row["file"]
        source = row.get("replacement") if row["decision"] == "SANITIZE" else name
        data = git(root, "show", manifest["source_commit"] + ":" + source)
        expected = row.get("replacement_sha256") if row["decision"] == "SANITIZE" else row["sha256"]
        if digest(data) != expected: raise RuntimeError("Export content identity changed")
        selected[name] = data
    require_licensing_review(selected)
    target.mkdir(parents=True, mode=0o700)
    exported = {}
    for row in manifest["files"]:
        if row["decision"] == "EXCLUDE": continue
        name = row["file"]
        data = selected[name]
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700 if row["mode"] == "100755" else 0o600)
        with os.fdopen(fd, "wb") as stream: stream.write(data)
        exported[name] = digest(data)
    boundary = {"schema":1, "review_complete":True, "reviewed_source_commit":manifest["source_commit"],
                "review_manifest_sha256":digest(json.dumps(manifest, sort_keys=True).encode()),
                "files":exported, "distribution_scope":"source-only", "git_history_copied":False,
                "image_distribution_approved":False, "privacy_ready_for_stage3":False, "ready_for_public_release":False}
    fd = os.open(target / BOUNDARY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream: json.dump(boundary, stream, indent=2); stream.write("\n")
    return exported


def require_publication_boundary(root):
    root = Path(root).resolve()
    path = root / BOUNDARY
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Packaging requires a separately reviewed publication boundary")
    boundary = json.loads(path.read_text())
    if boundary.get("schema") != 1 or boundary.get("review_complete") is not True:
        raise RuntimeError("Publication boundary has not passed content review")
    names = {n for n in git(root, "ls-files", "-z").decode().split("\0") if n}
    files = boundary.get("files", {})
    if not isinstance(files, dict) or names != set(files) | {BOUNDARY}:
        raise RuntimeError("Publication boundary file set changed; renewed review is required")
    selected = {}
    for name, expected in files.items():
        safe_name(name)
        target = root / name
        if private_filename(name) or name.startswith(NEVER_EXPORT) or target.is_symlink() or not target.is_file():
            raise RuntimeError("Publication boundary includes a forbidden input")
        data = target.read_bytes()
        if not target.resolve().is_relative_to(root) or digest(data) != expected:
            raise RuntimeError("Publication content changed; renewed review is required")
        selected[name] = data
    require_licensing_review(selected)
    untracked = git(root, "ls-files", "-z", "--others", "--exclude-standard", "--",
                    "src", "scripts", "tests", "apps/web", "config", "docker", ".github",
                    "Dockerfile", ".dockerignore", "compose.yaml")
    if untracked.strip(b"\0"):
        raise RuntimeError("Unreviewed source/build input is present")
    return boundary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--manifest", type=Path, help="Explicit exact-hash reviewed inventory for clean export")
    parser.add_argument("--export", type=Path, help="New source-only target; never copies Git history")
    parser.add_argument("--verify-boundary", action="store_true")
    args = parser.parse_args()
    if args.export or args.manifest:
        if not args.export or not args.manifest or args.report or args.verify_boundary:
            parser.error("clean export requires --manifest and --export without other operations")
        selected = export(args.root, json.loads(args.manifest.read_text()), args.export)
        print(json.dumps({"exported_source_files": len(selected), "git_history_copied": False, "image_distribution_approved": False}))
        raise SystemExit(0)
    if args.verify_boundary:
        require_publication_boundary(args.root)
        print("Publication boundary: PASS (publication not authorized)")
        raise SystemExit(0)
    if args.report is None: parser.error("inventory requires --report")
    result = inventory(args.root, args.revision)
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream: json.dump(result, stream, indent=2); stream.write("\n")
    print(json.dumps({"files":len(result["files"]),"review_complete":False,"ready_for_public_release":False}))
