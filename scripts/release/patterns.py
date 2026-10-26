"""Value-free source scan patterns shared by the release audit tools."""
import hashlib
import re

PATTERNS = {
    "credential": re.compile(rb"\b(?:sk-(?:proj-|ant-|or-v1-)[A-Za-z0-9_-]{24,}|sk-[A-Za-z0-9]{48}|gh[pousr]_[A-Za-z0-9]{36,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{35})\b"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n(?:[A-Za-z0-9+/=]{16,}\r?\n){2,}-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
}


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
