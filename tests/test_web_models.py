"""Provider catalog metadata only: synthetic HTTP, no model inference or keys."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    import httpx
except ImportError:
    raise unittest.SkipTest("Install web extras for model catalog tests")

from gleskit_mcp.web.providers import Providers
from gleskit_mcp.web.common import WebError
from tests.test_web_shell import MemoryCredentials


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-model-catalog-test-")
        self.providers = Providers(SimpleNamespace(data=Path(self.temp.name)), MemoryCredentials())

    def tearDown(self): self.temp.cleanup()

    def client(self, handler):
        real = httpx.Client
        return patch("gleskit_mcp.web.providers.httpx.Client", side_effect=lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))

    def test_unsaved_openrouter_catalog_needs_no_model_id_or_key_and_is_cached(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [{"id": "deepseek/fixture-flash", "name": "DeepSeek Fixture Flash"}]})
        with self.client(handler):
            first = self.providers.fetch_models(kind="openrouter")
            second = self.providers.fetch_models(kind="openrouter")
            self.assertEqual(len(seen), 1)
            self.providers.fetch_models(kind="openrouter", force=True)
        self.assertEqual(len(seen), 2)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["id"], "deepseek/fixture-flash")
        self.assertTrue(all(request.method == "GET" and str(request.url) == "https://openrouter.ai/api/v1/models" for request in seen))
        self.assertNotIn("authorization", seen[0].headers)
        self.assertFalse(self.providers.path.exists(), "Catalog browsing must not save/activate a connection")
        self.assertEqual(self.providers.credentials.rows, {})

    def test_unsaved_authenticated_catalog_does_not_store_or_echo_draft_key(self):
        key = "synthetic-model-list-secret"
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer " + key)
            return httpx.Response(200, json={"data": [{"id": "fixture", "name": key}]})
        with self.client(handler):
            result = self.providers.fetch_models(kind="openai", api_key=key)
        self.assertEqual(result, [{"id": "fixture", "label": "[redacted]"}])
        self.assertFalse(self.providers.path.exists())
        self.assertEqual(self.providers.credentials.rows, {})
        self.assertNotIn(key, json.dumps(result))

    def test_saved_key_is_not_forwarded_to_changed_kind_or_endpoint(self):
        self.providers.save(providerId="fixture", kind="openai", label="Fixture", endpoint="https://provider.example/v1",
                            apiKey="synthetic-saved-key", models=["fixture"], enabled=True)
        with self.client(lambda request: self.fail("Changed destinations must not receive the saved key")):
            for values in ({"endpoint": "https://other.example/v1"}, {"kind": "anthropic"}):
                with self.assertRaisesRegex(WebError, "original provider and endpoint"):
                    self.providers.fetch_models("fixture", **values)

    def test_anthropic_catalog_follows_all_pages_and_deduplicates(self):
        seen = []
        def handler(request):
            seen.append(request)
            self.assertEqual(request.headers["x-api-key"], "synthetic-anthropic-fixture")
            self.assertEqual(request.url.params["limit"], "1000")
            if "after_id" not in request.url.params:
                return httpx.Response(200, json={"data": [{"id": "claude-fixture-a", "display_name": "Claude A"}], "has_more": True, "last_id": "cursor-a"})
            self.assertEqual(request.url.params["after_id"], "cursor-a")
            return httpx.Response(200, json={"data": [{"id": "claude-fixture-a"}, {"id": "claude-fixture-b", "display_name": "Claude B"}], "has_more": False})
        with self.client(handler):
            result = self.providers.fetch_models(kind="anthropic", api_key="synthetic-anthropic-fixture")
        self.assertEqual([row["id"] for row in result], ["claude-fixture-a", "claude-fixture-b"])
        self.assertEqual(len(seen), 2)

    def test_invalid_pagination_does_not_accept_a_partial_catalog(self):
        with self.client(lambda request: httpx.Response(200, json={"data": [{"id": "fixture"}], "has_more": True, "last_id": "same"})):
            with self.assertRaisesRegex(WebError, "incomplete or invalid"):
                self.providers.fetch_models(kind="anthropic", api_key="synthetic-anthropic-fixture")
        self.assertEqual(self.providers.model_cache, {})

    def test_auth_errors_redirects_and_malformed_payloads_do_not_echo_keys(self):
        key = "synthetic-private-model-key"
        for status, body in ((401, {"error": key}), (302, {"error": key}), (200, {"invalid": key})):
            with self.subTest(status=status), self.client(lambda request: httpx.Response(status, json=body, headers={"Location": "https://other.example"})):
                with self.assertRaises(WebError) as error:
                    self.providers.fetch_models(kind="openai", api_key=key)
                self.assertNotIn(key, str(error.exception))

    def test_catalog_does_not_silently_drop_models_after_three_thousand(self):
        rows = [{"id": "fixture-" + str(index)} for index in range(3500)]
        with self.client(lambda request: httpx.Response(200, json={"data": rows})):
            result = self.providers.fetch_models(kind="openrouter")
        self.assertEqual(len(result), 3500)

    def test_cache_is_scoped_to_credential_and_endpoint(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})
        with self.client(handler):
            self.providers.fetch_models(kind="openai", endpoint="https://first.example/v1", api_key="synthetic-key-one")
            self.providers.fetch_models(kind="openai", endpoint="https://first.example/v1", api_key="synthetic-key-two")
            self.providers.fetch_models(kind="openai", endpoint="https://second.example/v1", api_key="synthetic-key-two")
        self.assertEqual(len(seen), 3)

    def test_openai_and_anthropic_public_catalogs_refresh_without_keys(self):
        seen = []
        payload = {
            "openai": {"models": {"gpt-fixture": {"id": "gpt-fixture", "name": "GPT Fixture"}, "retired": {"status": "retired"}}},
            "anthropic": {"models": {"claude-fixture": {"name": "Claude Fixture"}}},
        }
        def handler(request):
            seen.append(request)
            self.assertEqual(str(request.url), "https://models.dev/api.json")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("x-api-key", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, json=payload)
        with self.client(handler):
            first = self.providers.fetch_models(kind="openai", include_metadata=True)
            second = self.providers.fetch_models(kind="anthropic", include_metadata=True)
            self.assertEqual(len(seen), 1, "Switching public providers must reuse the catalog download")
            payload["openai"]["models"]["new-fixture"] = {"name": "New Fixture"}
            refreshed = self.providers.fetch_models(kind="openai", force=True, include_metadata=True)
        self.assertEqual(len(seen), 2)
        self.assertEqual(first["models"], [{"id": "gpt-fixture", "label": "GPT Fixture"}])
        self.assertEqual(second["models"], [{"id": "claude-fixture", "label": "Claude Fixture"}])
        self.assertEqual(len(refreshed["models"]), 2)
        self.assertTrue(first["requires_key_for_chat"])
        self.assertIn("not account-verified", first["source"])
        self.assertFalse(self.providers.path.exists())
        self.assertEqual(self.providers.credentials.rows, {})

    def test_every_provider_uses_its_own_authenticated_or_local_model_api(self):
        endpoints = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1",
                     "anthropic": "https://api.anthropic.com/v1", "openai-compatible": "https://custom.example/v1",
                     "local": "http://127.0.0.1:11434/v1"}
        for kind, endpoint in endpoints.items():
            with self.subTest(kind=kind):
                secret = None if kind == "local" else "synthetic-key-for-" + kind
                def handler(request):
                    self.assertEqual(str(request.url).split("?")[0], endpoint + "/models")
                    if secret:
                        self.assertEqual(request.headers["x-api-key"] if kind == "anthropic" else request.headers["authorization"],
                                         secret if kind == "anthropic" else "Bearer " + secret)
                    else:
                        self.assertNotIn("authorization", request.headers)
                    return httpx.Response(200, json={"data": [{"id": kind + "-fixture"}], "has_more": False})
                with self.client(handler):
                    result = self.providers.fetch_models(kind=kind, endpoint=endpoint, api_key=secret, include_metadata=True)
                self.assertEqual(result["models"][0]["id"], kind + "-fixture")
                self.assertNotIn("public", result["source"].lower())

    def test_keyless_custom_compatible_endpoint_is_queried_without_credentials(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://custom.example/v1/models")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json={"data": [{"id": "custom-fixture"}]})
        with self.client(handler):
            result = self.providers.fetch_models(kind="openai-compatible", endpoint="https://custom.example/v1")
        self.assertEqual(result, [{"id": "custom-fixture", "label": "custom-fixture"}])

    def test_invalid_public_catalog_reports_failure_without_inventing_models(self):
        with self.client(lambda request: httpx.Response(200, json={"wrong-schema": {}})):
            with self.assertRaisesRegex(WebError, "public model catalog"):
                self.providers.fetch_models(kind="openai")
        self.assertIsNone(self.providers.public_catalog)
