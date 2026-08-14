"""Phase-9B OpenAI-compatible transport tests.

All provider calls in this module are fixtures. No external request is made,
even if GROQ_API_KEY exists in the process environment.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from arena_api import ArenaControl
from arena_credentials import CredentialStore
from arena_router import RouteRequest, RoutingError
from arena_runtime import RuntimeConfig
from arena_transport import (
    ExternalLiveRequestGate,
    OpenAICompatibleTransport,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
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


def request_fixture(secret: str = "fixture-secret-never-logged"):
    calls: list[object] = []

    def urlopen(request, timeout):
        calls.append(request)
        assert request.full_url == "https://groq.test/openai/v1/chat/completions"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == f"Bearer {secret}"
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == "openai/test-model"
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["temperature"] == 0.1
        assert body["max_tokens"] == 12
        assert body["stream"] is False
        return FakeResponse(
            {
                "id": "fixture-response",
                "model": "openai/test-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "fixture-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        )

    return calls, urlopen


class OpenAICompatibleTransportTests(unittest.TestCase):
    def make_transport(self, *, environ=None, urlopen=None):
        return OpenAICompatibleTransport(
            provider_id="groq",
            base_url="https://groq.test/openai/v1",
            credential_env="GROQ_API_KEY",
            credential_store=CredentialStore(environ or {}),
            models=("openai/test-model",),
            urlopen=urlopen,
            live_gate=ExternalLiveRequestGate.explicit("fixture transport test"),
        )

    def request(self):
        return ProviderRequest.from_messages(
            "openai/test-model",
            [{"role": "user", "content": "hello"}],
            task_type="general",
            temperature=0.1,
            max_tokens=12,
        )

    def test_openai_compatible_request_and_response(self):
        calls, urlopen = request_fixture()
        transport = self.make_transport(
            environ={"GROQ_API_KEY": "fixture-secret-never-logged"}, urlopen=urlopen
        )
        response = transport.chat(self.request())
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(response, ProviderResponse)
        self.assertEqual(response.content, "fixture-ok")
        self.assertEqual(response.usage["total_tokens"], 3)
        self.assertNotIn("fixture-secret-never-logged", str(response))

    def test_missing_credential_is_not_configured_and_sends_no_request(self):
        calls: list[object] = []

        def urlopen(request, timeout):
            calls.append(request)
            raise AssertionError("fixture must not be called without credentials")

        transport = self.make_transport(environ={}, urlopen=urlopen)
        health = transport.health_check()
        self.assertFalse(health.healthy)
        self.assertEqual(health.detail, "not_configured")
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(self.request())
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(calls, [])

    def test_health_with_credential_is_local_readiness_only(self):
        calls: list[object] = []
        transport = self.make_transport(
            environ={"GROQ_API_KEY": "fixture-secret"},
            urlopen=lambda request, timeout: calls.append(request),
        )
        health = transport.health_check(live_gate=ExternalLiveRequestGate.disabled())
        self.assertFalse(health.healthy)
        self.assertEqual(health.detail, "not_checked")
        self.assertEqual(calls, [])

    def test_http_status_mapping_and_retry_after(self):
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
            headers = {"Retry-After": "60"} if status == 429 else {}

            def urlopen(_request, timeout, status=status, headers=headers):
                raise urllib.error.HTTPError(
                    "https://groq.test/openai/v1/chat/completions",
                    status,
                    "provider error body must not escape",
                    headers,
                    None,
                )

            transport = self.make_transport(
                environ={"GROQ_API_KEY": "fixture-secret"}, urlopen=urlopen
            )
            with self.assertRaises(ProviderTransportError) as caught:
                transport.chat(self.request())
            self.assertEqual(caught.exception.code, code, status)
            self.assertNotIn("provider error body", str(caught.exception))
            if status == 429:
                self.assertEqual(caught.exception.retry_after_seconds, 60.0)

    def test_network_and_timeout_errors_are_normalized(self):
        for error, expected in (
            (urllib.error.URLError("network details"), "connection_failed"),
            (TimeoutError("timeout details"), "timeout"),
        ):
            def urlopen(_request, timeout, error=error):
                raise error

            transport = self.make_transport(
                environ={"GROQ_API_KEY": "fixture-secret"}, urlopen=urlopen
            )
            with self.assertRaises(ProviderTransportError) as caught:
                transport.chat(self.request())
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("network details", str(caught.exception))
            self.assertNotIn("timeout details", str(caught.exception))


class GroqRouterIntegrationTests(unittest.TestCase):
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
        control.providers.set_health("ollama", "provider_down")
        groq = control.providers.get("groq")
        groq.configured = True
        groq.models = ["openai/test-model"]
        control.providers.set_health("groq", "healthy")
        return control

    def test_public_router_to_groq_fixture_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = self.make_control(root)
            calls, urlopen = request_fixture()
            control.transports["groq"] = OpenAICompatibleTransport(
                "groq",
                "https://groq.test/openai/v1",
                "GROQ_API_KEY",
                credential_store=CredentialStore({"GROQ_API_KEY": "fixture-secret-never-logged"}),
                models=("openai/test-model",),
                urlopen=urlopen,
            )
            response = control.live_request(
                "groq",
                RouteRequest(
                    "coding", privacy_class="PUBLIC", requested_model="openai/test-model"
                ),
                ProviderRequest.from_messages(
                    "openai/test-model",
                    [{"role": "user", "content": "hello"}],
                    temperature=0.1,
                    max_tokens=12,
                ),
                live_gate=ExternalLiveRequestGate.explicit("fixture integration test"),
            )
            self.assertEqual(response.provider, "groq")
            self.assertEqual(response.content, "fixture-ok")
            self.assertEqual(len(calls), 1)

    def test_secret_privacy_block_happens_before_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            calls: list[object] = []

            class MustNotBeCalled:
                def chat(self, request):
                    calls.append(request)
                    raise AssertionError("privacy block must precede transport")

            control.transports["groq"] = MustNotBeCalled()
            with self.assertRaises(RoutingError):
                control.live_request(
                    "groq",
                    RouteRequest(
                        "coding", privacy_class="SECRET", requested_model="openai/test-model"
                    ),
                    ProviderRequest.from_messages(
                        "openai/test-model", [{"role": "user", "content": "secret"}]
                    ),
                    live_gate=ExternalLiveRequestGate.explicit("privacy fixture test"),
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
