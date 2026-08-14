"""Phase-9D Gemini transport tests; no external network requests are made."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from arena_api import ArenaControl
from arena_credentials import CredentialStore
from arena_providers import ProviderRegistry
from arena_router import RouteRequest, RoutingError
from arena_runtime import RuntimeConfig
from arena_transport import (
    ExternalLiveRequestGate,
    GeminiTransport,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


def make_request() -> ProviderRequest:
    return ProviderRequest.from_messages(
        "gemini-test-model",
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hello"},
        ],
        task_type="general",
        temperature=0.2,
        max_tokens=24,
    )


def make_transport(environ: dict[str, str], calls: list[object], payload: dict | None = None):
    def urlopen(request, timeout):
        calls.append(request)
        if request.full_url.split("?", 1)[0].endswith("/v1beta/models"):
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
        return FakeResponse(
            payload
            or {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "gemini-fixture-ok"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
            }
        )

    return GeminiTransport(
        base_url="https://generativelanguage.googleapis.com",
        credential_store=CredentialStore(environ),
        models=("gemini-test-model",),
        urlopen=urlopen,
    )


class GeminiTransportTests(unittest.TestCase):
    def test_registry_uses_google_api_key_reference(self):
        registry = ProviderRegistry.default(environ={"GOOGLE_API_KEY": "fixture-secret"})
        provider = registry.get("gemini")
        self.assertEqual(provider.credential_env, "GOOGLE_API_KEY")
        self.assertTrue(provider.configured)
        self.assertNotIn("fixture-secret", str(provider.public_dict()))

    def test_missing_credential_is_local_and_sends_no_request(self):
        calls: list[object] = []
        transport = make_transport({}, calls)
        self.assertEqual(transport.health_check().detail, "not_configured")
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(make_request(), live_gate=ExternalLiveRequestGate.explicit("fixture"))
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(calls, [])

    def test_disabled_gate_blocks_without_request(self):
        calls: list[object] = []
        transport = make_transport({"GOOGLE_API_KEY": "fixture-secret"}, calls)
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(make_request())
        self.assertEqual(caught.exception.code, "live_request_blocked")
        self.assertEqual(calls, [])

    def test_request_construction_and_response_parsing(self):
        calls: list[object] = []
        secret = "fixture-secret-never-logged"
        transport = make_transport({"GOOGLE_API_KEY": secret}, calls)
        response = transport.chat(
            make_request(),
            live_gate=ExternalLiveRequestGate.explicit("Gemini fixture request"),
        )
        self.assertIsInstance(response, ProviderResponse)
        self.assertEqual(response.provider, "gemini")
        self.assertEqual(response.content, "gemini-fixture-ok")
        self.assertEqual(response.usage["promptTokenCount"], 2)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        parsed_url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(parsed_url.path, "/v1beta/models/gemini-test-model:generateContent")
        self.assertIn("key", urllib.parse.parse_qs(parsed_url.query))
        self.assertNotIn(secret, str(response))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["contents"][0]["role"], "user")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "hello")
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "Be concise.")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 24)

    def test_gated_health_discovers_only_generation_models(self):
        calls: list[object] = []
        transport = make_transport({"GOOGLE_API_KEY": "fixture-secret"}, calls)
        unchecked = transport.health_check()
        self.assertEqual(unchecked.detail, "not_checked")
        self.assertEqual(calls, [])
        checked = transport.health_check(
            live_gate=ExternalLiveRequestGate.explicit("Gemini health fixture")
        )
        self.assertTrue(checked.healthy)
        self.assertEqual(checked.detail, "live_probe_ok")
        self.assertEqual(checked.models, ("gemini-test-model",))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].full_url.split("?", 1)[0].endswith("/v1beta/models"))

    def test_malformed_response_is_provider_error(self):
        calls: list[object] = []
        transport = make_transport(
            {"GOOGLE_API_KEY": "fixture-secret"}, calls, payload={"candidates": []}
        )
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(
                make_request(),
                live_gate=ExternalLiveRequestGate.explicit("malformed fixture"),
            )
        self.assertEqual(caught.exception.code, "provider_error")

    def test_http_status_mapping_and_secret_redaction(self):
        expected = {
            400: "invalid_request",
            401: "authentication_failed",
            403: "authentication_failed",
            404: "model_not_found",
            408: "timeout",
            429: "rate_limited",
            500: "provider_error",
            502: "provider_unavailable",
            503: "provider_unavailable",
            504: "timeout",
        }
        for status, code in expected.items():
            headers = {"Retry-After": "30"} if status == 429 else {}

            def urlopen(_request, timeout, status=status, headers=headers):
                raise urllib.error.HTTPError(
                    "https://gemini.test/v1beta/models/test:generateContent",
                    status,
                    "secret provider body",
                    headers,
                    None,
                )

            transport = GeminiTransport(
                base_url="https://gemini.test",
                credential_store=CredentialStore({"GOOGLE_API_KEY": "fixture-secret"}),
                models=("gemini-test-model",),
                urlopen=urlopen,
            )
            with self.assertRaises(ProviderTransportError) as caught:
                transport.chat(
                    make_request(),
                    live_gate=ExternalLiveRequestGate.explicit("status fixture"),
                )
            self.assertEqual(caught.exception.code, code, status)
            self.assertNotIn("secret provider body", str(caught.exception))
            if status == 429:
                self.assertEqual(caught.exception.retry_after_seconds, 30.0)

    def test_network_and_timeout_errors_are_normalized(self):
        for error, expected in (
            (urllib.error.URLError("private network detail"), "connection_failed"),
            (TimeoutError("private timeout detail"), "timeout"),
        ):
            def urlopen(_request, timeout, error=error):
                raise error

            transport = GeminiTransport(
                credential_store=CredentialStore({"GOOGLE_API_KEY": "fixture-secret"}),
                models=("gemini-test-model",),
                urlopen=urlopen,
            )
            with self.assertRaises(ProviderTransportError) as caught:
                transport.chat(
                    make_request(),
                    live_gate=ExternalLiveRequestGate.explicit("network fixture"),
                )
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("private network detail", str(caught.exception))
            self.assertNotIn("private timeout detail", str(caught.exception))


class GeminiArenaIntegrationTests(unittest.TestCase):
    def make_control(self, root: Path) -> ArenaControl:
        config = RuntimeConfig(
            config_file=root / "config.json",
            arena_version="test",
            hermes_version="test",
            runtime_mode="development",
            hermes_root=root / "runtime",
            hermes_home=root / "hermes-home",
            desktop_user_data_dir=root / "desktop-data",
            arena_state_dir=root / "arena-state",
            python_executable=root / "runtime" / "venv" / "python.exe",
            desktop_executable=root / "desktop" / "Hermes.exe",
        )
        control = ArenaControl(config)
        for provider_id in ("ollama", "groq"):
            control.providers.set_health(provider_id, "provider_down")
        return control

    def test_public_router_to_gemini_fixture_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            provider = control.providers.get("gemini")
            provider.configured = True
            provider.models = ["gemini-test-model"]
            control.providers.set_health("gemini", "healthy")
            calls: list[object] = []
            control.transports["gemini"] = make_transport(
                {"GOOGLE_API_KEY": "fixture-secret"}, calls
            )
            response = control.live_request(
                "gemini",
                RouteRequest("general", privacy_class="PUBLIC", requested_model="gemini-test-model"),
                ProviderRequest.from_messages(
                    "gemini-test-model", [{"role": "user", "content": "hello"}]
                ),
                live_gate=ExternalLiveRequestGate.explicit("Gemini integration fixture"),
            )
            self.assertEqual(response.content, "gemini-fixture-ok")
            self.assertEqual(len(calls), 1)

    def test_secret_privacy_blocks_before_gemini_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            provider = control.providers.get("gemini")
            provider.configured = True
            provider.models = ["gemini-test-model"]
            control.providers.set_health("gemini", "healthy")
            calls: list[object] = []
            control.transports["gemini"] = make_transport(
                {"GOOGLE_API_KEY": "fixture-secret"}, calls
            )
            with self.assertRaises(RoutingError):
                control.live_request(
                    "gemini",
                    RouteRequest("general", privacy_class="SECRET", requested_model="gemini-test-model"),
                    ProviderRequest.from_messages(
                        "gemini-test-model", [{"role": "user", "content": "secret"}]
                    ),
                    live_gate=ExternalLiveRequestGate.explicit("Gemini privacy fixture"),
                )
            self.assertEqual(calls, [])

    def test_gemini_health_without_credential_is_not_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            health = control.gemini_health()
            self.assertEqual(health.detail, "not_configured")

    def test_gemini_dry_run_never_contacts_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            provider = control.providers.get("gemini")
            provider.configured = True
            provider.models = ["gemini-test-model"]
            control.providers.set_health("gemini", "healthy")
            calls: list[object] = []
            control.transports["gemini"] = make_transport(
                {"GOOGLE_API_KEY": "fixture-secret"}, calls
            )
            result = control.dry_run_provider_request(
                "gemini",
                RouteRequest("general", privacy_class="PUBLIC", requested_model="gemini-test-model"),
                ProviderRequest.from_messages(
                    "gemini-test-model", [{"role": "user", "content": "hello"}]
                ),
            )
            self.assertEqual(result["network_request"], "NO")
            self.assertEqual(result["live_gate"], "DISABLED")
            self.assertEqual(result["request_allowed"], "yes")
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
