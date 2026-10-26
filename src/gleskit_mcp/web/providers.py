from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx

from .common import WebError, identifier, read_json, write_private
from .redaction import SecretFilter
from .credentials import Credentials, validate_secret

CATALOG = {
    "openrouter": {"label": "OpenRouter", "endpoint": "https://openrouter.ai/api/v1", "protocol": "chat-completions"},
    "openai": {"label": "OpenAI", "endpoint": "https://api.openai.com/v1", "protocol": "responses"},
    "anthropic": {"label": "Anthropic", "endpoint": "https://api.anthropic.com/v1", "protocol": "messages"},
    "openai-compatible": {"label": "Other OpenAI-compatible", "endpoint": "", "protocol": "chat-completions"},
    "local": {"label": "Local OpenAI-compatible (no key required)", "endpoint": "http://127.0.0.1:11434/v1", "protocol": "chat-completions"},
}
PUBLIC_CATALOG_URL = "https://models.dev/api.json"


def catalog_result(models, source, source_url, requires_key, include_metadata, updated_at=None):
    rows = [row.copy() for row in models]
    return {"models": rows, "source": source, "source_url": source_url,
            "requires_key_for_chat": requires_key,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat()} if include_metadata else rows


def connection_endpoint(kind, endpoint):
    if kind not in CATALOG:
        raise WebError("Select a supported provider type.")
    endpoint = endpoint or CATALOG[kind]["endpoint"]
    if not isinstance(endpoint, str) or not endpoint:
        raise WebError("A compatible provider requires an explicit endpoint.")
    try:
        parsed = urlsplit(endpoint)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if ((parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise WebError("Use HTTPS, or HTTP on localhost, without credentials/query/fragment.")
        parsed.port
    except ValueError:
        raise WebError("The provider endpoint is invalid.") from None
    if kind == "local" and not local:
        raise WebError("A keyless local connection must use a loopback endpoint.")
    if any(ord(c) <= 32 or ord(c) == 127 for c in endpoint):
        raise WebError("The endpoint contains invalid characters.")
    return endpoint.rstrip("/")


class Providers:
    def __init__(self, profile, credentials=None):
        self.path = profile.data / "providers.json"
        self.credentials = credentials or Credentials(profile)
        self.lock = threading.RLock()
        self.model_cache = {}
        self.public_catalog = None
        self.public_catalog_lock = threading.Lock()
        self.rows = read_json(self.path, {})
        if not isinstance(self.rows, dict) or any("api_key" in row for row in self.rows.values()):
            raise WebError("Invalid provider metadata; plaintext keys are not accepted.")

    def list(self):
        with self.lock:
            return [{**row, "connected": row["kind"] == "local" or bool(self.credentials.get(key)),
                     "credential_storage": self.credentials.storage} for key, row in self.rows.items()]

    def active(self):
        with self.lock:
            return next((row.copy() for row in self.rows.values() if row["enabled"]), None)

    def credential_sources(self):
        return self.credentials.sources() if hasattr(self.credentials, "sources") else []

    def source_secret(self, source, kind, endpoint):
        if kind not in {"openai", "openrouter", "anthropic"} or endpoint != CATALOG[kind]["endpoint"]:
            raise WebError("Existing .env keys can only be imported for their matching official provider endpoint. Enter a separate key for a custom destination.")
        return self.credentials.from_source(source, kind)

    def save(self, **values):
        provider_id = identifier(values.get("providerId") or "provider_" + uuid.uuid4().hex)
        kind = values.get("kind")
        if kind not in CATALOG:
            raise WebError("Select a supported provider type.")
        models = values.get("models")
        if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m.strip() or len(m) > 200 or any(ord(c) < 32 for c in m) for m in models):
            raise WebError("Enter at least one exact model ID.")
        endpoint = connection_endpoint(kind, values.get("endpoint"))
        secret = values.get("apiKey")
        if values.get("keySource"):
            if secret:
                raise WebError("Choose an existing .env key or enter a new key, not both.")
            secret = self.source_secret(values["keySource"], kind, endpoint)
        validate_secret(secret)
        label = values.get("label") or kind
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise WebError("Enter a provider label of at most 120 characters.")
        model = values.get("defaultModel") or models[0]
        if model not in models:
            raise WebError("The default model must be in the model list.")
        with self.lock:
            previous = self.rows.get(provider_id)
            if previous and not secret and (kind != previous["kind"] or endpoint != previous["endpoint"]) and self.credentials.get(provider_id):
                raise WebError("Enter a new key when changing the provider or endpoint. The saved key was not sent to a changed destination.")
            rows = {key: row.copy() for key, row in self.rows.items()}
            previous_secret = self.credentials.get(provider_id) if secret else None
            if secret:
                self.credentials.set(provider_id, secret)
            if values.get("enabled", True):
                for row in rows.values():
                    row["enabled"] = False
            rows[provider_id] = {"provider_id": provider_id, "kind": kind, "label": label,
                                     "endpoint": endpoint, "models": models, "model_labels": models,
                                     "default_model": model, "enabled": bool(values.get("enabled", True))}
            try:
                write_private(self.path, rows)
            except (OSError, WebError):
                if secret:
                    if previous_secret:
                        self.credentials.set(provider_id, previous_secret)
                    else:
                        self.credentials.delete(provider_id)
                raise WebError("Cannot save the model connection. The previous settings were kept; no credential was included in this error.") from None
            self.rows = rows
            return next(row for row in self.list() if row["provider_id"] == provider_id)

    def delete(self, provider_id):
        with self.lock:
            self.credentials.delete(identifier(provider_id))
            self.rows.pop(provider_id, None)
            write_private(self.path, self.rows)
        return {"deleted": provider_id}

    def connected(self, row):
        return bool(row and (row["kind"] == "local" or self.credentials.get(row["provider_id"])))

    def catalog(self):
        sources = self.credential_sources()
        return [{"kind": key, **row, "key_sources": [source for source in sources if source["kind"] == key]}
                for key, row in CATALOG.items()]

    def _public_models(self, kind, force):
        # Discovery only: a fixed public metadata URL receives no credentials,
        # account IDs, configured endpoints, prompts or workspace information.
        with self.public_catalog_lock:
            if not force and self.public_catalog and self.public_catalog[1].get(kind) and time.monotonic() - self.public_catalog[0] < 300:
                return self.public_catalog[1].get(kind, []), self.public_catalog[2]
            try:
                data = bytearray()
                deadline = time.monotonic() + 30
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                    with client.stream("GET", PUBLIC_CATALOG_URL, headers={"Accept": "application/json"}) as response:
                        if not response.is_success:
                            raise ValueError()
                        for chunk in response.iter_bytes():
                            if time.monotonic() > deadline or len(data) + len(chunk) > 16 * 1024 * 1024:
                                raise ValueError()
                            data.extend(chunk)
                payload = json.loads(data)
                catalogs = {}
                for provider in ("openai", "anthropic"):
                    entries = payload.get(provider, {}).get("models", {})
                    if not isinstance(entries, dict) or len(entries) > 10000:
                        raise ValueError()
                    rows = []
                    for key, item in entries.items():
                        if not isinstance(item, dict) or item.get("status") in ("deprecated", "retired"):
                            continue
                        model = item.get("id", key)
                        if isinstance(model, str) and 0 < len(model) <= 200 and not any(ord(c) < 32 for c in model):
                            rows.append({"id": model, "label": str(item.get("name") or model)[:200]})
                    catalogs[provider] = rows
                if not catalogs.get(kind):
                    raise ValueError()
            except (httpx.HTTPError, ValueError, AttributeError):
                raise WebError("Cannot refresh the public model catalog. Retry, or enter an API key to query the provider directly. No account availability was verified.") from None
            updated = datetime.now(timezone.utc).isoformat()
            self.public_catalog = (time.monotonic(), catalogs, updated)
            return catalogs[kind], updated

    def fetch_models(self, provider_id=None, *, kind=None, endpoint=None, api_key=None, key_source=None, force=False, include_metadata=False):
        """List metadata without saving a connection, activating or starting inference."""
        if type(force) is not bool:
            raise WebError("Refresh must be true or false.")
        if type(include_metadata) is not bool:
            raise WebError("Catalog metadata must be true or false.")
        if provider_id is not None:
            identifier(provider_id)
        with self.lock:
            row = self.rows.get(provider_id, {}).copy()
        if provider_id and not row:
            raise WebError("This model connection does not exist.", 404)
        kind = kind or row.get("kind")
        endpoint = connection_endpoint(kind, endpoint or row.get("endpoint"))
        validate_secret(api_key)
        secret = api_key
        if key_source:
            if secret:
                raise WebError("Choose an existing .env key or enter a new key, not both.")
            secret = self.source_secret(key_source, kind, endpoint)
        # Never silently send a saved credential to an edited destination.
        if secret is None and row:
            saved = self.credentials.get(provider_id)
            if kind == row["kind"] and endpoint == connection_endpoint(row["kind"], row.get("endpoint")):
                secret = saved
            elif saved:
                raise WebError("Enter an API key for the changed destination. A saved key can only be reused with its original provider and endpoint.")
        if not secret and kind in {"openai", "anthropic"} and endpoint == CATALOG[kind]["endpoint"]:
            rows, updated = self._public_models(kind, force)
            return catalog_result(rows, "Public model catalog — not account-verified", PUBLIC_CATALOG_URL, True, include_metadata, updated)
        source = "Local server model catalog" if kind == "local" else "Provider API model catalog"
        requires_key = kind != "local" and not secret
        cache_key = hashlib.sha256(json.dumps([kind, endpoint, secret]).encode()).hexdigest()
        with self.lock:
            cached = self.model_cache.get(cache_key)
            if not force and cached and time.monotonic() - cached[0] < 300:
                return catalog_result(cached[1], source, endpoint + "/models", requires_key, include_metadata, cached[2])
        headers = {"Accept": "application/json"}
        if secret:
            headers["x-api-key" if kind == "anthropic" else "Authorization"] = secret if kind == "anthropic" else "Bearer " + secret
        if kind == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        models, cursors, total = {}, set(), 0
        params = {"limit": 1000} if kind == "anthropic" else {}
        deadline = time.monotonic() + 30
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                for _ in range(50):
                    if time.monotonic() > deadline:
                        raise WebError("Model catalog took too long to load. Retry explicitly or enter a model ID manually.")
                    with client.stream("GET", endpoint + "/models", headers=headers, params=params) as response:
                        if not response.is_success:
                            raise WebError(f"Provider model listing returned HTTP {response.status_code}. " +
                                ("This server requires a valid API key; enter it and reload models." if response.status_code in {401, 403} else "Check that the selected server is running and supports /models, then retry."))
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if time.monotonic() > deadline:
                                raise WebError("Model catalog took too long to load. Retry explicitly or enter a model ID manually.")
                            total += len(chunk)
                            if total > 16 * 1024 * 1024:
                                raise WebError("Provider model listing exceeds the local size limit; no partial catalog was accepted.")
                            data.extend(chunk)
                    payload = SecretFilter((secret,)).value(json.loads(data))
                    rows = payload.get("data")
                    if not isinstance(rows, list):
                        raise ValueError()
                    for item in rows:
                        model = item.get("id") if isinstance(item, dict) else None
                        if isinstance(model, str) and 0 < len(model) <= 200 and not any(ord(c) < 32 for c in model) and "[redacted]" not in model:
                            models[model] = {"id": model, "label": str(item.get("name") or item.get("display_name") or model)[:200]}
                    if len(models) > 10000:
                        raise WebError("Provider catalog exceeds 10,000 models; no partial catalog was accepted.")
                    if not payload.get("has_more"):
                        break
                    cursor = payload.get("last_id")
                    if kind != "anthropic" or not isinstance(cursor, str) or not 0 < len(cursor) <= 200 or cursor in cursors:
                        raise WebError("The provider returned an incomplete or invalid paginated catalog. Enter a model ID manually or retry.")
                    cursors.add(cursor)
                    params["after_id"] = cursor
                else:
                    raise WebError("Provider catalog exceeds the pagination limit; no partial catalog was accepted.")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise WebError("Cannot read the provider model list. You can still enter its exact model ID manually.") from None
        result = list(models.values())
        updated = datetime.now(timezone.utc).isoformat()
        with self.lock:
            if len(self.model_cache) >= 32:
                self.model_cache.pop(next(iter(self.model_cache)))
            self.model_cache[cache_key] = (time.monotonic(), result, updated)
        return catalog_result(result, source, endpoint + "/models", requires_key, include_metadata, updated)
