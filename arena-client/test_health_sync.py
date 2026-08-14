"""Phase-9E health synchronization tests; no external requests are made."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

from arena_credentials import CredentialStore
from arena_health import HealthSynchronizer
from arena_providers import (
    AUTHENTICATION_FAILED,
    HEALTHY,
    MODEL_UNAVAILABLE,
    NOT_CHECKED,
    NOT_CONFIGURED,
    PROVIDER_DOWN,
    RATE_LIMITED,
    ProviderRegistry,
)
from arena_router import ProviderRouter, RouteRequest, RoutingError
from arena_transport import (
    ExternalLiveRequestGate,
    GeminiTransport,
    OllamaTransport,
    OpenAICompatibleTransport,
    ProviderHealth,
)


class FakeResponse:
    def __init__(self, payload: dict):
        self.status = 200
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


def make_tags_response():
    return FakeResponse({"models": [{"name": "local-test-model"}]})


def make_groq_models_response():
    return FakeResponse({"data": [{"id": "groq-test-model"}]})


def make_gemini_models_response():
    return FakeResponse(
        {
            "models": [
                {
                    "name": "models/gemini-test-model",
                    "supportedGenerationMethods": ["generateContent"],
                }
            ]
        }
    )


def make_transports(calls: list[object], *, groq_error=None, gemini_error=None):
    def ollama_urlopen(request, timeout):
        calls.append(("ollama", request))
        if request.full_url.endswith("/api/chat"):
            raise AssertionError("health synchronization must not call /api/chat")
        return make_tags_response()

    def groq_urlopen(request, timeout):
        calls.append(("groq", request))
        if groq_error is not None:
            raise groq_error
        return make_groq_models_response()

    def gemini_urlopen(request, timeout):
        calls.append(("gemini", request))
        if gemini_error is not None:
            raise gemini_error
        return make_gemini_models_response()

    return {
        "ollama": OllamaTransport("http://ollama.test", urlopen=ollama_urlopen),
        "groq": OpenAICompatibleTransport(
            "groq",
            "https://groq.test/openai/v1",
            "GROQ_API_KEY",
            credential_store=CredentialStore({"GROQ_API_KEY": "fixture"}),
            models=("groq-configured-model",),
            urlopen=groq_urlopen,
        ),
        "gemini": GeminiTransport(
            "https://gemini.test",
            credential_store=CredentialStore({"GOOGLE_API_KEY": "fixture"}),
            models=("gemini-configured-model",),
            urlopen=gemini_urlopen,
        ),
    }


def fixture_transports(calls: list[object], *, groq_error=None, gemini_error=None):
    # Fixture credentials are local test values and never leave the injected
    # transport; no process environment is consulted.
    return make_transports(calls, groq_error=groq_error, gemini_error=gemini_error)


def error(url: str, status: int, headers=None):
    return urllib.error.HTTPError(url, status, "redacted provider body", headers or {}, None)


class HealthSynchronizerTests(unittest.TestCase):
    def test_default_sync_checks_local_ollama_but_no_external_provider(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        transports = fixture_transports(calls)
        results = HealthSynchronizer(registry, transports).synchronize()

        self.assertEqual(results["ollama"].detail, None)
        self.assertEqual(results["groq"].detail, "not_checked")
        self.assertEqual(results["gemini"].detail, "not_checked")
        self.assertEqual(registry.get("ollama").health_status, HEALTHY)
        self.assertEqual(registry.get("groq").health_status, NOT_CHECKED)
        self.assertEqual(registry.get("gemini").health_status, NOT_CHECKED)
        self.assertEqual([provider_id for provider_id, _ in calls], ["ollama"])

    def test_missing_external_credentials_are_not_configured_without_requests(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        transports = {
            "groq": OpenAICompatibleTransport(
                "groq",
                "https://groq.test/openai/v1",
                "GROQ_API_KEY",
                credential_store=CredentialStore({}),
                models=("test",),
                urlopen=lambda request, timeout: calls.append(request),
            ),
            "gemini": GeminiTransport(
                "https://gemini.test",
                credential_store=CredentialStore({}),
                models=("test",),
                urlopen=lambda request, timeout: calls.append(request),
            ),
        }
        results = HealthSynchronizer(registry, transports).synchronize(("groq", "gemini"))
        self.assertEqual(results["groq"].detail, "not_configured")
        self.assertEqual(results["gemini"].detail, "not_configured")
        self.assertEqual(registry.get("groq").health_status, NOT_CONFIGURED)
        self.assertEqual(registry.get("gemini").health_status, NOT_CONFIGURED)
        self.assertEqual(calls, [])

    def test_explicit_gate_updates_all_adapter_states_and_models(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        results = HealthSynchronizer(registry, fixture_transports(calls)).synchronize(
            live_gate=ExternalLiveRequestGate.explicit("health fixture")
        )
        self.assertTrue(all(result.healthy for result in results.values()))
        self.assertEqual(registry.get("ollama").models, ["local-test-model"])
        self.assertEqual(registry.get("groq").models, ["groq-test-model"])
        self.assertEqual(registry.get("gemini").models, ["gemini-test-model"])
        self.assertEqual(registry.get("groq").health_status, HEALTHY)
        self.assertEqual(registry.get("gemini").health_status, HEALTHY)
        self.assertEqual([provider_id for provider_id, _ in calls], ["ollama", "groq", "gemini"])
        self.assertTrue(all("/chat/completions" not in request.full_url for _, request in calls))
        self.assertTrue(all(":generateContent" not in request.full_url for _, request in calls))

    def test_authentication_failure_updates_registry(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        transports = fixture_transports(
            calls,
            groq_error=error("https://groq.test/models", 401),
        )
        health = HealthSynchronizer(registry, transports).synchronize_one(
            "groq", live_gate=ExternalLiveRequestGate.explicit("auth fixture")
        )
        self.assertEqual(health.detail, "authentication_failed")
        self.assertEqual(registry.get("groq").health_status, AUTHENTICATION_FAILED)
        self.assertNotIn("redacted provider body", str(registry.get("groq").public_dict()))

    def test_rate_limit_updates_circuit_and_router_excludes_provider(self):
        registry = ProviderRegistry.from_config(
            {
                "overrides": {
                    "groq": {"configured": True},
                    "sambanova": {"configured": True},
                }
            },
            environ={},
        )
        registry.set_health("ollama", PROVIDER_DOWN)
        registry.set_health("groq", HEALTHY)
        registry.set_health("sambanova", HEALTHY)
        calls: list[object] = []
        transports = fixture_transports(
            calls,
            groq_error=error("https://groq.test/models", 429, {"Retry-After": "60"}),
        )
        health = HealthSynchronizer(registry, transports).synchronize_one(
            "groq", live_gate=ExternalLiveRequestGate.explicit("rate fixture")
        )
        self.assertEqual(health.detail, "rate_limited")
        provider = registry.get("groq")
        self.assertEqual(provider.health_status, RATE_LIMITED)
        self.assertEqual(provider.circuit_breaker["state"], "open")
        self.assertGreater(provider.circuit_breaker["opened_until"], time.time())
        decision = ProviderRouter(registry).select(RouteRequest("coding", privacy_class="PUBLIC"))
        self.assertEqual(decision.provider_id, "sambanova")

    def test_timeout_maps_to_provider_down(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        transports = fixture_transports(calls, groq_error=TimeoutError("fixture timeout"))
        health = HealthSynchronizer(registry, transports).synchronize_one(
            "groq", live_gate=ExternalLiveRequestGate.explicit("timeout fixture")
        )
        self.assertEqual(health.detail, "timeout")
        self.assertEqual(registry.get("groq").health_status, PROVIDER_DOWN)

    def test_forbidden_authentication_failure_is_not_privacy_blocked(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        transports = fixture_transports(
            calls,
            gemini_error=error("https://gemini.test/v1beta/models", 403),
        )
        health = HealthSynchronizer(registry, transports).synchronize_one(
            "gemini", live_gate=ExternalLiveRequestGate.explicit("auth 403 fixture")
        )
        self.assertEqual(health.detail, "authentication_failed")
        self.assertEqual(registry.get("gemini").health_status, AUTHENTICATION_FAILED)

    def test_model_not_found_maps_to_model_unavailable(self):
        registry = ProviderRegistry.default(environ={})
        calls: list[object] = []
        transports = fixture_transports(
            calls,
            gemini_error=error("https://gemini.test/v1beta/models", 404),
        )
        health = HealthSynchronizer(registry, transports).synchronize_one(
            "gemini", live_gate=ExternalLiveRequestGate.explicit("model fixture")
        )
        self.assertEqual(health.detail, "model_not_found")
        self.assertEqual(registry.get("gemini").health_status, MODEL_UNAVAILABLE)

    def test_active_rate_limit_is_not_cleared_by_ungated_check(self):
        registry = ProviderRegistry.default(environ={})
        provider = registry.get("groq")
        provider.configured = True
        registry.record_response("groq", 429, retry_after_seconds=120)
        calls: list[object] = []
        transports = fixture_transports(calls)
        result = HealthSynchronizer(registry, transports).synchronize_one("groq")
        self.assertEqual(result.detail, "not_checked")
        self.assertEqual(provider.health_status, RATE_LIMITED)
        self.assertEqual(calls, [])

    def test_private_and_secret_routing_remain_blocked_after_external_health(self):
        registry = ProviderRegistry.from_config(
            {"overrides": {"groq": {"configured": True}, "gemini": {"configured": True}}},
            environ={},
        )
        registry.set_health("ollama", PROVIDER_DOWN)
        registry.set_health("groq", HEALTHY)
        registry.set_health("gemini", HEALTHY)
        router = ProviderRouter(registry)
        with self.assertRaises(RoutingError):
            router.select(RouteRequest("coding", privacy_class="PRIVATE"))
        with self.assertRaises(RoutingError):
            router.select(RouteRequest("coding", privacy_class="SECRET"))


if __name__ == "__main__":
    unittest.main()
