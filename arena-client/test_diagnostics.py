"""Phase-9F diagnostics tests; all provider checks use local fixtures."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import arena_launcher
from arena_api import ArenaControl
from arena_runtime import RuntimeConfig
from arena_transport import ExternalLiveRequestGate, ProviderHealth


class FixtureHealthTransport:
    def __init__(self, provider: str, calls: list[str], health: ProviderHealth | None = None):
        self.provider_id = provider
        self.calls = calls
        self.health = health or ProviderHealth(
            provider=provider,
            healthy=True,
            models=(f"{provider}-fixture-model",),
            detail="live_probe_ok",
        )

    def health_check(self, *, timeout=None, live_gate=None):
        if self.provider_id != "ollama" and not (live_gate and live_gate.enabled):
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                detail="not_checked",
            )
        self.calls.append(self.provider_id)
        return self.health


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
    return ArenaControl(config)


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_dry_run_is_network_free_and_reports_all_registry_providers(self):
        with tempfile.TemporaryDirectory() as raw:
            control = make_control(Path(raw))
            calls: list[str] = []
            control.transports["ollama"] = FixtureHealthTransport("ollama", calls)
            control.transports["groq"] = FixtureHealthTransport("groq", calls)
            control.transports["gemini"] = FixtureHealthTransport("gemini", calls)
            result = control.provider_diagnostics()

            self.assertEqual(result["mode"], "DRY_RUN")
            self.assertEqual(result["live_gate"], "DISABLED")
            self.assertEqual(result["network"], "NO")
            self.assertEqual(result["network_requests"], 0)
            self.assertEqual(calls, [])
            self.assertEqual(len(result["providers"]), len(control.providers.providers))
            self.assertEqual(
                {item["provider"] for item in result["providers"]},
                set(control.providers.providers),
            )

    def test_selected_provider_dry_run_never_checks_health(self):
        with tempfile.TemporaryDirectory() as raw:
            control = make_control(Path(raw))
            calls: list[str] = []
            control.transports["groq"] = FixtureHealthTransport("groq", calls)
            result = control.provider_diagnostics(["groq"])
            self.assertEqual(result["providers"][0]["provider"], "groq")
            self.assertEqual(result["providers"][0]["network"], "NO")
            self.assertEqual(calls, [])

    def test_explicit_live_gate_checks_only_selected_adapter(self):
        with tempfile.TemporaryDirectory() as raw:
            control = make_control(Path(raw))
            calls: list[str] = []
            control.transports["groq"] = FixtureHealthTransport("groq", calls)
            control.transports["gemini"] = FixtureHealthTransport("gemini", calls)
            result = control.provider_diagnostics(
                ["groq"],
                live_gate=ExternalLiveRequestGate.explicit("diagnostic fixture"),
            )
            self.assertEqual(result["mode"], "LIVE")
            self.assertEqual(result["live_gate"], "ENABLED")
            self.assertEqual(result["checked_providers"], ["groq"])
            self.assertEqual(calls, ["groq"])
            self.assertEqual(result["network_requests"], 1)
            self.assertEqual(result["providers"][0]["health"], "healthy")
            self.assertEqual(result["providers"][0]["network"], "YES")

    def test_live_gate_health_error_and_retry_after_are_sanitized(self):
        with tempfile.TemporaryDirectory() as raw:
            control = make_control(Path(raw))
            calls: list[str] = []
            control.providers.get("groq").configured = True
            control.transports["groq"] = FixtureHealthTransport(
                "groq",
                calls,
                ProviderHealth(
                    provider="groq",
                    healthy=False,
                    detail="rate_limited",
                    status_code=429,
                    retry_after_seconds=60,
                ),
            )
            result = control.provider_diagnostics(
                ["groq"],
                live_gate=ExternalLiveRequestGate.explicit("rate fixture"),
            )
            item = result["providers"][0]
            self.assertEqual(item["health"], "rate_limited")
            self.assertEqual(item["circuit_breaker"]["state"], "open")
            self.assertGreater(item["retry_after_seconds"], 0)
            self.assertEqual(item["network"], "YES")

    def test_diagnostic_last_error_never_exposes_secret(self):
        with tempfile.TemporaryDirectory() as raw:
            control = make_control(Path(raw))
            provider = control.providers.get("groq")
            provider.circuit_breaker["last_error"] = (
                "Bear" + "er " + "super-secret-value key=another-secret"
            )
            result = control.provider_diagnostics(["groq"])
            serialized = json.dumps(result)
            self.assertNotIn("super-secret-value", serialized)
            self.assertNotIn("another-secret", serialized)
            self.assertIn("redacted", serialized)

    def test_unknown_provider_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            control = make_control(Path(raw))
            with self.assertRaises(KeyError):
                control.provider_diagnostics(["does-not-exist"])


class DiagnosticsCliTests(unittest.TestCase):
    def test_cli_diagnostics_json_uses_dry_run_by_default(self):
        result = {
            "mode": "DRY_RUN",
            "live_gate": "DISABLED",
            "network": "NO",
            "network_requests": 0,
            "checked_providers": [],
            "providers": [],
        }

        class FakeConfig:
            def validate(self):
                return None

        class FakeControl:
            def provider_diagnostics(self, provider_ids, *, live_gate):
                self.provider_ids = provider_ids
                self.live_gate = live_gate
                return result

        fake_control = FakeControl()
        with patch.object(sys, "argv", ["arena_launcher.py", "diagnostics", "--json"]), \
             patch.object(arena_launcher.RuntimeConfig, "load", return_value=FakeConfig()), \
             patch.object(arena_launcher, "ArenaControl", return_value=fake_control), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = arena_launcher.main()

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        self.assertIsNone(fake_control.live_gate)

    def test_cli_live_without_reason_returns_gate_error(self):
        with patch.object(sys, "argv", ["arena_launcher.py", "diagnostics", "--live"]), \
             contextlib.redirect_stderr(io.StringIO()) as error_output:
            code = arena_launcher.main()
        self.assertEqual(code, 3)
        self.assertIn("--reason", error_output.getvalue())

    def test_cli_live_passes_explicit_gate_to_control(self):
        result = {
            "mode": "LIVE",
            "live_gate": "ENABLED",
            "network": "NO",
            "network_requests": 0,
            "checked_providers": ["groq"],
            "providers": [{"provider": "groq", "health": "healthy"}],
        }

        class FakeConfig:
            def validate(self):
                return None

        class FakeControl:
            def provider_diagnostics(self, provider_ids, *, live_gate):
                self.live_gate = live_gate
                return result

        fake_control = FakeControl()
        with patch.object(sys, "argv", [
            "arena_launcher.py", "diagnostics", "--live", "--reason", "manual fixture", "--provider", "groq", "--json",
        ]), patch.object(arena_launcher.RuntimeConfig, "load", return_value=FakeConfig()), \
             patch.object(arena_launcher, "ArenaControl", return_value=fake_control), \
             contextlib.redirect_stdout(io.StringIO()):
            code = arena_launcher.main()

        self.assertEqual(code, 0)
        self.assertTrue(fake_control.live_gate.enabled)
        self.assertEqual(fake_control.live_gate.reason, "manual fixture")

    def test_cli_rejects_live_and_dry_run_together(self):
        with patch.object(sys, "argv", ["arena_launcher.py", "diagnostics", "--live", "--dry-run"]), \
             contextlib.redirect_stderr(io.StringIO()) as error_output:
            code = arena_launcher.main()
        self.assertEqual(code, 2)
        self.assertIn("exklusiv", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
