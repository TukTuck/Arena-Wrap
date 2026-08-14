"""Phase-8 provider and routing tests; no external provider or model requests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from arena_credentials import CredentialStore
from arena_providers import (
    HEALTHY,
    NOT_CONFIGURED,
    PROVIDER_DOWN,
    RATE_LIMITED,
    SECRET,
    PUBLIC,
    ProviderRegistry,
)
from arena_router import ProviderRouter, RouteRequest, RoutingError


class ProviderRoutingTests(unittest.TestCase):
    def test_registry_loads_without_credentials(self) -> None:
        registry = ProviderRegistry.default(environ={})
        self.assertGreaterEqual(len(registry.providers), 14)
        self.assertEqual(registry.get("groq").health_status, NOT_CONFIGURED)
        self.assertFalse(registry.get("groq").configured)
        self.assertTrue(registry.get("ollama").configured)
        self.assertNotIn("super-secret", str(registry.summary()))

    def test_credential_status_never_contains_secret_value(self) -> None:
        store = CredentialStore({"GROQ_API_KEY": "super-secret-test-value"})
        status = store.public_status("GROQ_API_KEY")
        self.assertEqual(status, {"env_var": "GROQ_API_KEY", "configured": True})
        self.assertNotIn("super-secret-test-value", str(status))

    def test_private_coding_selects_healthy_ollama(self) -> None:
        registry = ProviderRegistry.default(environ={})
        registry.set_health("ollama", HEALTHY)
        router = ProviderRouter(registry)
        decision = router.select(RouteRequest("coding", privacy_class="PRIVATE"))
        self.assertEqual(decision.provider_id, "ollama")
        self.assertEqual(decision.privacy_class, "PRIVATE")

    def test_public_coding_selects_configured_groq(self) -> None:
        registry = ProviderRegistry.from_config(
            {"overrides": {"groq": {"configured": True}}}, environ={}
        )
        registry.set_health("groq", HEALTHY)
        router = ProviderRouter(registry)
        decision = router.select(RouteRequest("coding", privacy_class=PUBLIC))
        self.assertEqual(decision.provider_id, "groq")
        self.assertEqual(decision.model, "openai/gpt-oss-120b")

    def test_research_is_search_then_synthesis(self) -> None:
        registry = ProviderRegistry.from_config(
            {
                "overrides": {
                    "jina": {"configured": True},
                    "gemini": {"configured": True},
                }
            },
            environ={},
        )
        registry.set_health("jina", HEALTHY)
        registry.set_health("gemini", HEALTHY)
        router = ProviderRouter(registry)
        plan = router.plan(RouteRequest("research", privacy_class=PUBLIC))
        self.assertEqual(plan.reason, "search-provider+llm-synthesis")
        self.assertEqual([stage["stage"] for stage in plan.stages], ["search", "synthesis"])
        self.assertEqual(plan.stages[0]["selected_provider"], "jina")
        self.assertEqual(plan.stages[1]["selected_provider"], "gemini")

    def test_429_temporarily_removes_provider_and_falls_back(self) -> None:
        registry = ProviderRegistry.from_config(
            {
                "overrides": {
                    "groq": {"configured": True},
                    "sambanova": {"configured": True},
                }
            },
            environ={},
        )
        registry.set_health("groq", HEALTHY)
        registry.set_health("sambanova", HEALTHY)
        router = ProviderRouter(registry)
        first = router.select(RouteRequest("coding"))
        self.assertEqual(first.provider_id, "groq")
        router.report_response("groq", 429, retry_after_seconds=60)
        self.assertEqual(registry.get("groq").health_status, RATE_LIMITED)
        second = router.select(RouteRequest("coding"))
        self.assertEqual(second.provider_id, "sambanova")
        self.assertTrue(second.fallback)

    def test_secret_never_routes_to_external_free_provider(self) -> None:
        registry = ProviderRegistry.from_config(
            {
                "overrides": {
                    "groq": {"configured": True},
                    "gemini": {"configured": True},
                }
            },
            environ={},
        )
        registry.set_health("groq", HEALTHY)
        registry.set_health("gemini", HEALTHY)
        registry.set_health("ollama", PROVIDER_DOWN)
        router = ProviderRouter(registry)
        with self.assertRaises(RoutingError):
            router.select(RouteRequest("coding", privacy_class=SECRET))

    def test_ollama_probe_is_read_only_fixture(self) -> None:
        registry = ProviderRegistry.default(environ={})
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=response) as opener:
            provider = registry.probe_ollama()
        opener.assert_called_once()
        self.assertEqual(provider.health_status, HEALTHY)
        self.assertEqual(provider.id, "ollama")

    def test_http_status_mapping(self) -> None:
        registry = ProviderRegistry.from_config(
            {"overrides": {"groq": {"configured": True}}}, environ={}
        )
        registry.record_response("groq", 401)
        self.assertEqual(registry.get("groq").health_status, "authentication_failed")
        registry.record_response("groq", 402)
        self.assertEqual(registry.get("groq").health_status, "quota_exhausted")
        registry.record_response("groq", 404)
        self.assertEqual(registry.get("groq").health_status, "model_unavailable")


if __name__ == "__main__":
    unittest.main()
