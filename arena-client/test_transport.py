"""Phase-9A transport tests; fixtures never contact an external provider."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from arena_api import ArenaControl
from arena_router import RouteRequest
from arena_runtime import RuntimeConfig
from arena_transport import (
    OllamaTransport,
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


class OllamaTransportTests(unittest.TestCase):
    def test_health_and_model_discovery(self):
        def urlopen(request, timeout):
            self.assertEqual(request.full_url, "http://ollama.test/api/tags")
            self.assertEqual(timeout, 2.0)
            return FakeResponse({"models": [{"name": "local-coder:latest"}]})

        transport = OllamaTransport("http://ollama.test", timeout=2, urlopen=urlopen)
        health = transport.health_check()
        self.assertTrue(health.healthy)
        self.assertEqual(health.models, ("local-coder:latest",))

    def test_model_availability_is_read_only(self):
        def urlopen(request, timeout):
            return FakeResponse({"models": [{"name": "local-coder:latest"}]})

        transport = OllamaTransport("http://ollama.test", urlopen=urlopen)
        self.assertTrue(transport.model_available("local-coder:latest"))
        self.assertFalse(transport.model_available("missing"))

    def test_chat_maps_sanitized_response(self):
        def urlopen(request, timeout):
            self.assertEqual(request.full_url, "http://ollama.test/api/chat")
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body["model"], "local-coder:latest")
            self.assertFalse(body["stream"])
            return FakeResponse(
                {
                    "model": "local-coder:latest",
                    "message": {"role": "assistant", "content": "ARENA_TRANSPORT_OK"},
                    "done": True,
                    "eval_count": 3,
                }
            )

        transport = OllamaTransport("http://ollama.test", urlopen=urlopen)
        response = transport.chat(
            ProviderRequest.from_messages(
                "local-coder:latest",
                [{"role": "user", "content": "Return exactly: ARENA_TRANSPORT_OK"}],
            )
        )
        self.assertIsInstance(response, ProviderResponse)
        self.assertEqual(response.content, "ARENA_TRANSPORT_OK")
        self.assertEqual(response.usage["eval_count"], 3)

    def test_http_404_maps_to_model_not_found_without_body(self):
        def urlopen(_request, timeout):
            raise urllib.error.HTTPError(
                "http://ollama.test/api/chat", 404, "missing", {}, None
            )

        transport = OllamaTransport("http://ollama.test", urlopen=urlopen)
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(
                ProviderRequest.from_messages("missing", [{"role": "user", "content": "x"}])
            )
        self.assertEqual(caught.exception.code, "model_not_found")
        self.assertNotIn("missing", str(caught.exception))

    def test_connection_and_timeout_errors_are_normalized(self):
        for error, expected in ((urllib.error.URLError("offline"), "connection_failed"),
                                (TimeoutError("slow"), "timeout")):
            def urlopen(_request, timeout, error=error):
                raise error

            transport = OllamaTransport("http://ollama.test", urlopen=urlopen)
            with self.assertRaises(ProviderTransportError) as caught:
                transport.discover_models()
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("offline", str(caught.exception))
            self.assertNotIn("slow", str(caught.exception))


class ArenaTransportIntegrationTests(unittest.TestCase):
    def test_private_route_executes_only_registered_ollama_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
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
            control.providers.set_health("ollama", "healthy")
            control.providers.get("ollama").models = ["local-coder"]

            class FixtureTransport:
                def chat(self, request):
                    return ProviderResponse(
                        provider="ollama",
                        model=request.model,
                        content="fixture-ok",
                        latency_ms=1.0,
                    )

            control.transports["ollama"] = FixtureTransport()
            response = control.send_provider_request(
                RouteRequest("coding", privacy_class="PRIVATE", requested_model="local-coder"),
                ProviderRequest.from_messages(
                    "local-coder", [{"role": "user", "content": "test"}]
                ),
            )
            self.assertEqual(response.content, "fixture-ok")
            self.assertEqual(response.provider, "ollama")

    def test_no_adapter_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
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
            control.providers.get("groq").configured = True
            control.providers.set_health("groq", "healthy")
            control.transports.pop("groq")
            with self.assertRaises(Exception) as caught:
                control.send_provider_request(
                    RouteRequest("coding", privacy_class="PUBLIC"),
                    ProviderRequest.from_messages(
                        "openai/gpt-oss-120b", [{"role": "user", "content": "test"}]
                    ),
                )
            self.assertIn("Transport-Adapter", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
