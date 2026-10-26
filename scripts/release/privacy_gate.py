#!/usr/bin/env python3
"""Fail-closed source/history gate. Reports rules/locations, NEVER matched values.

Pattern scanning is not proof that arbitrary research data is safe to publish.
This gate covers identifiable paths, credential shapes and private-state files;
unpublished research artifacts and Git author identities still need human review.
"""
import argparse
import io
import json
import re
import subprocess
from pathlib import Path
import zipfile
if __package__:
    from .patterns import PATTERNS
    from .asset_metadata import PUBLIC_ASSETS, has_private_metadata
else:
    from patterns import PATTERNS
    from asset_metadata import PUBLIC_ASSETS, has_private_metadata

HOME_PATH = re.compile(rb"(?:/" + rb"Users/(?!\[|\*|<)[^/\s\"'<>]+(?:/|(?=[\s\"'<>]|$))|/" + rb"home/(?!\[|\*|<)[^/\s\"'<>]+/|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+[\\/])")
FIXTURE_PATHS = {
    "packaging/macos/test_privacy.py": (b"/Users/" + b"private-user",),
    "apps/desktop/src-tauri/src/chat.rs": (b"/Users/" + b"local",),
}
PRIVATE_NAMES = {"auth.json", "providers.json", "secrets.json", "credentials.json", "opencode.json", "opencode.jsonc",
                 "opencode.db", ".npmrc", ".pypirc", ".netrc", ".Rhistory", ".Renviron", ".Rprofile", ".openclaude-profile.json"}


def rules(data, name=""):
    found = {rule for rule, pattern in PATTERNS.items() if pattern.search(data)}
    fixtures = FIXTURE_PATHS.get(name, ())
    if any(not any(match.group().rstrip(b"/").startswith(prefix) and
                   match.group().rstrip(b"/")[len(prefix):len(prefix)+1] in {b"", b"/"}
                   for prefix in fixtures) for match in HOME_PATH.finditer(data)):
        found.add("personal-machine-path")
    return sorted(found)


def inspect(data, name, budget=None):
    budget = budget if budget is not None else [256 * 1024 * 1024]
    found = set(rules(data, name))
    if name in PUBLIC_ASSETS:
        try:
            if has_private_metadata(data): found.add("brand-image-metadata-requires-review")
        except (ValueError, TypeError):
            found.add("brand-image-metadata-cannot-be-audited")
    # Scan ZIP payloads without extracting files or executing code. Bounded to
    # fail closed on archive bombs and unsupported encrypted payloads.
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if sum(row.file_size for row in entries) > budget[0]:
                    found.add("archive-exceeds-audit-limit")
                else:
                    for row in entries:
                        budget[0] -= row.file_size
                        found.update(rules(row.filename.encode(), name))
                        found.update(inspect(archive.read(row), name, budget))
        except (ValueError, RuntimeError, OSError, RecursionError, zipfile.BadZipFile):
            found.add("archive-cannot-be-audited")
    return sorted(found)


def private_filename(name):
    path = Path(name)
    return (path.name in PRIVATE_NAMES or path.name.startswith(".env") and path.name != ".env.example"
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
            or any(part in {".aws", ".codex", ".docker", ".ssh", ".opencode", ".secrets", ".git", ".vscode", ".idea", ".venv", ".venv-web",
                           ".cache", "__pycache__", "node_modules"} for part in path.parts)
            or (".local." in path.name and not path.name.endswith(".example")) or name == "docs/opencode/opencode.json"
            or name.startswith(("var/", ".gleskit/", ".secrets/")))


def audit_repository(root):
    root = Path(root).resolve()
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    # A new source module may already be imported by a tracked file even before
    # it is staged. Cover non-ignored build inputs without scanning unrelated
    # private runtime/output directories or vendored experiments under tmp/.
    untracked = subprocess.check_output(["git", "ls-files", "-z", "--others", "--exclude-standard", "--",
        "src", "scripts", "tests", "apps/web", "config", "docker", ".github",
        "Dockerfile", ".dockerignore", "compose.yaml", "docs/opencode/prompts", "docs/opencode/skills",
        ".openclaude-profile.json"], cwd=root).decode().split("\0")
    names = sorted(set(names + untracked))
    findings, count = [], 0
    for name in filter(None, names):
        path = root / name
        if path.is_symlink():
            findings.append({"scope":"working-tree", "file":name,"rule":"tracked-symlink-requires-review"})
            continue
        if not path.is_file():
            continue
        count += 1
        detected = inspect(path.read_bytes(), name)
        if private_filename(name): detected.append("tracked-private-state")
        for rule in detected:
            findings.append({"scope":"working-tree","file":name,"rule":rule})
    rows = subprocess.check_output(["git", "rev-list", "--objects", "--all"], cwd=root).splitlines()
    blobs = 0
    with subprocess.Popen(["git", "cat-file", "--batch"], cwd=root,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        try:
            for row in rows:
                pieces = row.split(b" ", 1); oid = pieces[0]
                proc.stdin.write(oid + b"\n"); proc.stdin.flush()
                header = proc.stdout.readline().split()
                if len(header) != 3: raise RuntimeError("Git objects could not be audited")
                data = proc.stdout.read(int(header[2])); proc.stdout.read(1)
                if header[1] != b"blob": continue
                blobs += 1
                name = pieces[1].decode(errors="replace") if len(pieces) == 2 else "unknown-object"
                detected = inspect(data, name)
                if private_filename(name): detected.append("historical-private-state")
                for rule in detected:
                    findings.append({"scope":"git-history","file":name,"object":oid.decode(),"rule":rule})
        finally:
            proc.stdin.close()
    return {"ok":not findings,"current_source_files_checked":count,"history_blobs_checked":blobs,
            "findings":findings,"ready_for_public_release":False,
            "author_identity_and_research_artifact_review_required":True}


def require_repository_privacy(root):
    report = audit_repository(root)
    if not report["ok"]:
        raise RuntimeError(f"Packaging blocked by repository/history privacy gate ({len(report['findings'])} findings). Run privacy_gate.py; do not publish Git history.")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit_repository(args.root)
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.report.open("w") as stream:
        json.dump(report, stream, indent=2); stream.write("\n")
    args.report.chmod(0o600)
    print(json.dumps({"ok":report["ok"],"findings":len(report["findings"]),"ready_for_public_release":False}))
    raise SystemExit(0 if report["ok"] else 1)
