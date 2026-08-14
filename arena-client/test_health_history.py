"""Phase-9H health-history tests; no provider or model requests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arena_api import ArenaControl
from arena_history import ProviderHealthHistory
from arena_router import RouteRequest
from arena_runtime import RuntimeConfig
from arena_transport import ExternalLiveRequestGate, ProviderRequest, ProviderTransportError


class HealthHistoryTests(unittest.TestCase):
    def test_event_creation_and_sanitization(self):
        history = ProviderHealthHistory(max_events=10)
        event = history.record(
            "groq",
            "authentication_failed",
            health_status="authentication_failed",
            status_code=401,
            message="Bear" + "er " + "super-secret key=another-secret",
            source="fixture",
        )
        self.assertEqual(event.provider, "groq")
        self.assertEqual(event.event_type, "authentication_failed")
        self.assertEqual(event.status_code, 401)
        self.assertNotIn("super-secret", json.dumps(event.to_dict()))
        self.assertNotIn("another-secret", json.dumps(event.to_dict()))
        self.assertIn("redacted", event.message or "")

    def test_sensitive_payload_categories_are_not_part_of_event_model(self):
        history = ProviderHealthHistory()
        event = history.record("gemini", "health_checked", message="health check completed")
        serialized = json.dumps(event.to_dict())
        for forbidden in ("api_key", "authorization", "request_body", "prompt", "response", "cookie"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_history_limit_and_chronological_order(self):
        history = ProviderHealthHistory(max_events=3)
        for index in (3, 1, 2, 4):
            history.record(
                "ollama",
                "health_checked",
                timestamp=f"2026-01-01T00:00:0{index}+00:00",
                message=f"check {index}",
            )
        events = history.events()
        self.assertEqual(len(events), 3)
        self.assertEqual([event.message for event in events], ["check 2", "check 3", "check 4"])

    def test_provider_and_event_filters(self):
        history = ProviderHealthHistory()
        history.record("groq", "health_checked")
        history.record("groq", "rate_limited", health_status="rate_limited")
        history.record("gemini", "provider_down", health_status="provider_down")
        self.assertEqual(len(history.events(provider="groq")), 2)
        self.assertEqual(len(history.events(event_filter="rate_limits")), 1)
        self.assertEqual(len(history.events(event_filter="errors")), 1)
        self.assertEqual(len(history.events(event_filter="health_checks")), 1)

    def test_persistence_load_is_local_only(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "provider-health-history.json"
            first = ProviderHealthHistory(path, max_events=10)
            first.record("ollama", "provider_healthy", health_status="healthy")
            second = ProviderHealthHistory(path, max_events=10)
            self.assertEqual(len(second.events()), 1)
            self.assertEqual(second.events()[0].provider, "ollama")
            self.assertTrue(path.is_file())

    def test_clear_history_only_clears_local_events(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "provider-health-history.json"
            history = ProviderHealthHistory(path)
            history.record("groq", "provider_down")
            history.clear()
            self.assertEqual(history.events(), [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["events"], [])

    def test_export_is_sanitized_json(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            history = ProviderHealthHistory(root / "history.json")
            history.record("groq", "rate_limited", retry_after_seconds=12)
            destination = history.export_json(root / "export.json")
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["events"][0]["event_type"], "rate_limited")
            self.assertNotIn("prompt", json.dumps(payload).lower())
            self.assertNotIn("authorization", json.dumps(payload).lower())

    def test_unknown_event_type_is_rejected(self):
        with self.assertRaises(ValueError):
            ProviderHealthHistory().record("groq", "chat_message")


class ArenaHistoryIntegrationTests(unittest.TestCase):
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
        return ArenaControl(config)

    def test_health_synchronization_records_safe_events(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            calls: list[object] = []

            class FixtureOllama:
                def health_check(self, *, timeout=None):
                    calls.append("ollama")
                    from arena_transport import ProviderHealth
                    return ProviderHealth(
                        provider="ollama", healthy=True, models=("local",), detail=None
                    )

            control.transports["ollama"] = FixtureOllama()
            control.ollama_health()
            events = control.health_history_events(provider="ollama")
            self.assertEqual(calls, ["ollama"])
            self.assertEqual(events[0]["event_type"], "health_checked")
            self.assertIn("provider_healthy", [event["event_type"] for event in events])
            serialized = json.dumps(events).lower()
            self.assertNotIn("prompt", serialized)
            self.assertNotIn("response", serialized)

    def test_live_request_blocked_is_recorded_without_network(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            with self.assertRaises(ProviderTransportError) as caught:
                control.live_request(
                    "groq",
                    # Route and request are intentionally never reached by the gate.
                    RouteRequest("general"),
                    ProviderRequest.from_messages(
                        "fixture", [{"role": "user", "content": "not sent"}]
                    ),
                    live_gate=ExternalLiveRequestGate.disabled(),
                )
            self.assertEqual(caught.exception.code, "live_request_blocked")
            events = control.health_history_events(provider="groq")
            self.assertEqual(events[-1]["event_type"], "live_request_blocked")
            self.assertNotIn("not sent", json.dumps(events))

    def test_clear_and_export_api_are_local(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            control = self.make_control(root)
            control.health_history.record("gemini", "provider_not_configured")
            self.assertEqual(len(control.health_history_events()), 1)
            destination = root / "diagnostics.json"
            control.export_health_history(destination)
            self.assertTrue(destination.is_file())
            control.clear_health_history()
            self.assertEqual(control.health_history_events(), [])


if __name__ == "__main__":
    unittest.main()
