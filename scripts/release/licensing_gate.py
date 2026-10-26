#!/usr/bin/env python3
"""Require explicit, exact-byte licensing review before source export.

This verifies a review record, not legal compatibility or copyright ownership.
It never approves a record, contacts a service, or prints credential contents.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

LICENSE_ID = "CC-BY-NC-SA-4.0"
# Exact official https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.txt
STANDARD_LICENSE_SHA256 = "e66c269d4819aaab34b49ef5220c4ddab6756f21bb5180761a4eb8561f2b7bbd"
REVIEW_FILE = "docs/licensing-review.json"
LICENSING_INPUTS = (
    "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "pyproject.toml",
    "apps/web/package.json", "apps/web/package-lock.json",
    "src/gleskit_mcp/analysis/r_runner.py", "docs/licensing.md",
    "apps/web/src/assets/GESKit_icon.svg", "apps/web/src/assets/GESKit_icon_no_bg.png",
)
R_SOURCE_PREFIX = "src/gleskit_mcp/r/"
CONTAINER_INPUTS = ("Dockerfile", ".dockerignore", "compose.yaml", "scripts/docker_ui.sh",
                    "docker/runtime.json", "docker/python.lock", "docker/r-packages.lock.json",
                    "docker/install-r.R", "docker/third-party-inventory.json")


def review_inputs(files):
    return set(LICENSING_INPUTS) | (set(CONTAINER_INPUTS) if set(CONTAINER_INPUTS).intersection(files) else set()) | {
        name for name in files if name.startswith(R_SOURCE_PREFIX) and name.endswith(".R")
    }


def require_licensing_review(files, *, distribution="source"):
    """Validate the selected bytes, not a different checkout or revision."""
    if distribution not in {"source", "image"}:
        raise RuntimeError("Licensing blocked: unsupported distribution boundary")
    required = review_inputs(files)
    if not required.union({REVIEW_FILE}).issubset(files):
        raise RuntimeError("Licensing blocked: required license/review inputs are missing")
    try:
        review = json.loads(files[REVIEW_FILE])
        project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]
        frontend = json.loads(files["apps/web/package.json"])
        lock = json.loads(files["apps/web/package-lock.json"])
        notice_text = files["NOTICE"].decode("utf-8")
        metadata_valid = (
            isinstance(review, dict) and review.get("schema") == 1
            and review.get("license_id") == LICENSE_ID
            and project.get("license") == LICENSE_ID
            and {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"}.issubset(project.get("license-files", []))
            and frontend.get("license") == LICENSE_ID
            and lock["packages"][""].get("license") == frontend["license"]
            and "SPDX-License-Identifier: " + LICENSE_ID in notice_text
        )
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        raise RuntimeError("Licensing blocked: invalid licensing metadata") from None
    if not metadata_valid:
        raise RuntimeError("Licensing blocked: inconsistent licensing metadata")
    if hashlib.sha256(files["LICENSE"]).hexdigest() != STANDARD_LICENSE_SHA256:
        raise RuntimeError("Licensing blocked: standard license text changed; use unmodified CC terms")
    holder = review.get("copyright_holder")
    if not isinstance(holder, str) or not holder.strip() or holder not in notice_text:
        raise RuntimeError("Licensing blocked: copyright notice requires review")
    if review.get("source_license_review_complete") is not True:
        raise RuntimeError("Licensing blocked: source-license review is incomplete")
    if review.get("third_party_review_complete") is not True:
        raise RuntimeError("Licensing blocked: third-party compatibility review is incomplete")
    if distribution == "image" and review.get("container_review_complete") is not True:
        raise RuntimeError("Licensing blocked: container dependency/redistribution review is incomplete")
    hashes = review.get("reviewed_sha256")
    if not isinstance(hashes, dict) or set(hashes) != required:
        raise RuntimeError("Licensing blocked: complete exact-hash review is required")
    if any(hashes[name] != hashlib.sha256(files[name]).hexdigest() for name in required):
        raise RuntimeError("Licensing blocked: reviewed license/dependency inputs changed")
    return review


def working_tree_inputs(root):
    root = Path(root).resolve()
    names = set(LICENSING_INPUTS) | {REVIEW_FILE}
    if any((root / name).exists() for name in CONTAINER_INPUTS):
        names.update(CONTAINER_INPUTS)
    names.update(path.relative_to(root).as_posix() for path in (root / R_SOURCE_PREFIX).rglob("*.R"))
    files = {}
    for name in sorted(names):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeError("Licensing blocked: non-regular or external review input")
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--distribution", choices=("source", "image"), default="source")
    args = parser.parse_args()
    try:
        require_licensing_review(working_tree_inputs(args.root), distribution=args.distribution)
    except (RuntimeError, OSError) as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else "Licensing blocked: cannot read review inputs", file=sys.stderr)
        return 1
    print("Licensing review: PASS (" + args.distribution + "; technical review, not legal certification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
