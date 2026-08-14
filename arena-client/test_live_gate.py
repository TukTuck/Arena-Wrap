"""Phase-9C tests for the explicit external live-request gate."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arena_api import ArenaControl
from arena_credentials import CredentialStore
from arena_router import RouteRequest, RoutingError
from arena_runtime import RuntimeConfig
from arena_transport import (
    ExternalLiveRequestGate,
    OpenAICompatibleTransport,
    ProviderRequest,
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


def chat_payload() -> dict:
    return {
        "model": "openai/test-model",
        "choices": [{"message": {"content": "fixture-live-ok"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 1},
    }


def make_request() -> ProviderRequest:
    return ProviderRequest.from_messages(
        "openai/test-model", [{"role": "user", "content": "fixture"}]
    )


def make_transport(environ: dict[str, str], calls: list[object], payload: dict | None = None):
    def urlopen(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/models"):
            return FakeResponse({"data": [{"id": "openai/test-model"}]})
        return FakeResponse(payload or chat_payload())

    return OpenAICompatibleTransport(
        "groq",
        "https://groq.test/openai/v1",
        "GROQ_API_KEY",
        credential_store=CredentialStore(environ),
        models=("openai/test-model",),
        urlopen=urlopen,
    )


def make_control(root: Path) -> ArenaControl:
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
    return control


class LiveGateTransportTests(unittest.TestCase):
    def test_gate_requires_explicit_reason(self):
        with self.assertRaises(ValueError):
            ExternalLiveRequestGate(enabled=True)

    def test_disabled_gate_blocks_with_zero_requests(self):
        calls: list[object] = []
        transport = make_transport({"GROQ_API_KEY": "fixture"}, calls)
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(make_request())
        self.assertEqual(caught.exception.code, "live_request_blocked")
        self.assertEqual(calls, [])

    def test_enabled_gate_without_credential_still_sends_zero_requests(self):
        calls: list[object] = []
        transport = make_transport({}, calls)
        with self.assertRaises(ProviderTransportError) as caught:
            transport.chat(
                make_request(),
                live_gate=ExternalLiveRequestGate.explicit("missing-key fixture"),
            )
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(calls, [])

    def test_dry_run_has_zero_requests_and_no_secret_output(self):
        with tempfile.TemporaryDirectory() as temp:
            control = make_control(Path(temp))
            provider = control.providers.get("groq")
            provider.configured = False
            provider.health_status = "not_configured"
            calls: list[object] = []
            control.transports["groq"] = make_transport({}, calls)
            result = control.dry_run_provider_request(
                "groq",
                RouteRequest(
                    "coding", privacy_class="PUBLIC", requested_model="openai/test-model"
                ),
                make_request(),
            )
            self.assertEqual(result["network_request"], "NO")
            self.assertEqual(result["live_gate"], "DISABLED")
            self.assertEqual(result["request_allowed"], "no")
            self.assertEqual(result["credential"], "missing")
            self.assertNotIn("fixture", str(result))
            self.assertEqual(calls, [])

    def test_health_is_not_checked_without_gate_and_probed_only_with_gate(self):
        calls: list[object] = []
        transport = make_transport({"GROQ_API_KEY": "fixture"}, calls)
        unchecked = transport.health_check()
        self.assertFalse(unchecked.healthy)
        self.assertEqual(unchecked.detail, "not_checked")
        self.assertEqual(calls, [])

        checked = transport.health_check(
            live_gate=ExternalLiveRequestGate.explicit("health fixture")
        )
        self.assertTrue(checked.healthy)
        self.assertEqual(checked.detail, "live_probe_ok")
        self.assertEqual(checked.models, ("openai/test-model",))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].full_url.endswith("/models"))


class LiveGateArenaIntegrationTests(unittest.TestCase):
    def test_controlled_groq_health_updates_registry_only_when_gated(self):
        with tempfile.TemporaryDirectory() as temp:
            control = make_control(Path(temp))
            provider = control.providers.get("groq")
            provider.configured = True
            provider.health_status = "not_checked"
            calls: list[object] = []
            control.transports["groq"] = make_transport(
                {"GROQ_API_KEY": "fixture"}, calls
            )
            unchecked = control.groq_health()
            self.assertEqual(unchecked.detail, "not_checked")
            self.assertEqual(calls, [])
            checked = control.groq_health(
                live_gate=ExternalLiveRequestGate.explicit("health integration fixture")
            )
            self.assertEqual(checked.detail, "live_probe_ok")
            self.assertEqual(provider.health_status, "healthy")
            self.assertEqual(len(calls), 1)

    def test_explicit_live_request_executes_exactly_one_fixture_request(self):
        with tempfile.TemporaryDirectory() as temp:
            control = make_control(Path(temp))
            provider = control.providers.get("groq")
            provider.configured = True
            provider.health_status = "not_checked"
            provider.models = ["openai/test-model"]
            calls: list[object] = []
            control.transports["groq"] = make_transport(
                {"GROQ_API_KEY": "fixture"}, calls
            )
            response = control.live_request(
                "groq",
                RouteRequest(
                    "coding", privacy_class="PUBLIC", requested_model="openai/test-model"
                ),
                make_request(),
                live_gate=ExternalLiveRequestGate.explicit("phase 9C fixture"),
            )
            self.assertEqual(response.content, "fixture-live-ok")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].full_url.endswith("/chat/completions"))

    def test_normal_request_path_cannot_contact_groq(self):
        with tempfile.TemporaryDirectory() as temp:
            control = make_control(Path(temp))
            provider = control.providers.get("groq")
            provider.configured = True
            provider.health_status = "healthy"
            provider.models = ["openai/test-model"]
            calls: list[object] = []
            control.transports["groq"] = make_transport(
                {"GROQ_API_KEY": "fixture"}, calls
            )
            with self.assertRaises(ProviderTransportError) as caught:
                control.send_provider_request(
                    RouteRequest(
                        "coding", privacy_class="PUBLIC", requested_model="openai/test-model"
                    ),
                    make_request(),
                )
            self.assertEqual(caught.exception.code, "live_request_blocked")
            self.assertEqual(calls, [])

    def test_secret_policy_blocks_before_transport_even_with_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            control = make_control(Path(temp))
            provider = control.providers.get("groq")
            provider.configured = True
            provider.health_status = "healthy"
            provider.models = ["openai/test-model"]
            calls: list[object] = []
            control.transports["groq"] = make_transport(
                {"GROQ_API_KEY": "fixture"}, calls
            )
            with self.assertRaises(RoutingError):
                control.live_request(
                    "groq",
                    RouteRequest(
                        "coding", privacy_class="SECRET", requested_model="openai/test-model"
                    ),
                    make_request(),
                    live_gate=ExternalLiveRequestGate.explicit("secret fixture"),
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
