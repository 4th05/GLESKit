from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
from typing import Any


class WebError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise WebError("Invalid identifier; paths are not accepted here.")
    return value


def private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise WebError("A private directory cannot be a symlink.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path: Path, value: Any) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise WebError("A settings file cannot be a symlink.")
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    if path.is_symlink():
        raise WebError("A settings file cannot be a symlink.")
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise WebError("Invalid local settings; the original file was preserved.") from None


def contained_file(path: Path, root: Path) -> Path:
    root = root.resolve()
    candidate = path.absolute()
    if any(parent.is_symlink() for parent in (candidate, *candidate.parents) if parent != root.parent):
        raise WebError("Symlink file access is not permitted.", 403)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise WebError("File is not registered in this workspace.", 404)
    return resolved
