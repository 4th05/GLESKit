"""Persistent, profile-local dotenv credentials; never load them into os.environ."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading

from .common import WebError, private_dir


def validate_secret(secret):
    if secret is not None and (not isinstance(secret, str) or not 8 <= len(secret) <= 4096 or any(not 33 <= ord(c) <= 126 or c in '\\"' for c in secret)):
        raise WebError("The API key must be a single printable token of at least 8 characters.")


class Credentials:
    """Keys persist in a private .env; an existing OS vault is migration-only."""

    storage = "private-dotenv"
    pattern = re.compile(r"^(GLESKIT_PROVIDER_[A-F0-9]{24}_API_KEY)\s*=\s*(.*)$")
    named_keys = {"OPENAI_API_KEY": "openai", "OPENROUTER_API_KEY": "openrouter", "ANTHROPIC_API_KEY": "anthropic"}

    def __init__(self, profile):
        self.path = Path(profile.data) / ".env"
        project = getattr(profile, "project", None)
        # Isolated/custom profiles must never silently inherit developer keys.
        self.project_env = project / ".env" if project and profile.data == project / "var/local-web/profile" else None
        self.lock = threading.RLock()
        self.service = "com.gleskit.local-web." + hashlib.sha256(str(profile.data).encode()).hexdigest()[:20]
        self.backend = None
        self.checked_legacy = set()
        try:
            import keyring
            backend = keyring.get_keyring()
            if type(backend).__module__ in {"keyring.backends.macOS", "keyring.backends.SecretService", "keyring.backends.Windows"}:
                self.backend = backend
        except Exception:
            pass

    @staticmethod
    def variable(account):
        return "GLESKIT_PROVIDER_" + hashlib.sha256(account.encode()).hexdigest()[:24].upper() + "_API_KEY"

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        except FileNotFoundError:
            return [], {}
        except OSError:
            raise WebError("Cannot safely read the private .env credential file.") from None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                    raise ValueError()
                if stat.S_IMODE(info.st_mode) != 0o600:
                    os.fchmod(stream.fileno(), 0o600)
                lines = stream.read(1024 * 1024 + 1).splitlines()
            values = {}
            for line in lines:
                match = self.pattern.fullmatch(line)
                if not match:
                    if line.lstrip().startswith("GLESKIT_PROVIDER_"):
                        raise ValueError()
                    continue  # Preserve comments/unrelated variables; never execute or expand them.
                name, raw = match.groups()
                value = json.loads(raw)
                if not isinstance(value, str) or name in values:
                    raise ValueError()
                if value:
                    validate_secret(value)
                values[name] = value
            return lines, values
        except (OSError, ValueError, UnicodeError, WebError):
            raise WebError("The private .env credential file is unsafe or invalid. Its contents were not copied into the error.") from None

    def _write(self, account, secret):
        lines, _ = self._read()
        name = self.variable(account)
        replacement = name + "=" + json.dumps(secret)
        output, replaced = [], False
        for line in lines:
            match = self.pattern.fullmatch(line)
            if match and match.group(1) == name:
                output.append(replacement)
                replaced = True
            else:
                output.append(line)
        if not replaced:
            output.append(replacement)
        private_dir(self.path.parent)
        temporary = self.path.with_name(".env." + secrets.token_hex(8) + ".tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("\n".join(output) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        except OSError:
            raise WebError("Cannot save the API key in the private .env file. No key was included in this error.") from None
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, account):
        with self.lock:
            _, values = self._read()
            name = self.variable(account)
            if name in values:
                return values[name] or None
            if self.backend and account not in self.checked_legacy:
                try:
                    secret = self.backend.get_password(self.service, account)
                except Exception:
                    return None  # A locked legacy vault must not hide all saved connections.
                self.checked_legacy.add(account)
                if secret:
                    validate_secret(secret)
                    self._write(account, secret)
                    return secret
            return None

    def set(self, account, secret):
        validate_secret(secret)
        if not secret:
            raise WebError("Enter an API key to save.")
        with self.lock:
            self._write(account, secret)

    def delete(self, account):
        with self.lock:
            # Empty tombstones prevent an old vault copy from resurrecting a
            # deliberately deleted key, even if that vault is currently locked.
            self._write(account, "")
            if self.backend:
                try:
                    if self.backend.get_password(self.service, account):
                        self.backend.delete_password(self.service, account)
                except Exception:
                    raise WebError("The .env key was removed, but the legacy OS-vault copy could not be removed. Unlock the vault and retry.") from None

    def _named(self):
        result = {}
        for source, path in (("profile", self.path), ("project", self.project_env)):
            if path is None:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            except OSError:
                raise WebError("Cannot safely read the selected .env file.") from None
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                        raise ValueError()
                    lines = stream.read(1024 * 1024 + 1).splitlines()
                seen = set()
                for line in lines:
                    match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                    if not match or match[1] not in self.named_keys:
                        continue
                    name, value = match.groups()
                    if name in seen:
                        raise ValueError()
                    seen.add(name)
                    if value.startswith('"'):
                        value, end = json.JSONDecoder().raw_decode(value)
                        if match[2][end:].strip() and not match[2][end:].lstrip().startswith("#"):
                            raise ValueError()
                    elif value.startswith("'"):
                        end = value.find("'", 1)
                        if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                            raise ValueError()
                        value = value[1:end]
                    else:
                        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                    if value:
                        validate_secret(value)  # Literal strings only; never source, evaluate or expand.
                        result[source + ":" + name] = (name, self.named_keys[name], value)
            except (OSError, ValueError, UnicodeError, WebError):
                raise WebError("A supported API-key entry in .env is unsafe or invalid. No file contents were included in this error.") from None
        return result

    def sources(self):
        with self.lock:
            return [{"id": key, "name": name, "kind": kind, "source": key.split(":")[0]}
                    for key, (name, kind, _) in self._named().items()]

    def from_source(self, source, kind):
        with self.lock:
            row = self._named().get(source) if isinstance(source, str) else None
            if not row or row[1] != kind:
                raise WebError("Select an existing .env key matching this provider.")
            return row[2]
