"""Phase-9M Doppler integration tests using synthetic environment values only."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena_api import ArenaControl
from arena_credentials import CredentialStore
from arena_runtime import RuntimeConfig
from arena_transport import ExternalLiveRequestGate
from arena_version import VERSION_TAG


class DopplerEnvironmentTests(unittest.TestCase):
    def test_doppler_style_environment_maps_to_existing_credentials(self):
        fixture_env = {
            "GROQ_API_KEY": "fixture-groq-secret",
            "GOOGLE_API_KEY": "fixture-google-secret",
        }
        store = CredentialStore(fixture_env)
        self.assertTrue(store.reference("GROQ_API_KEY").configured)
        self.assertTrue(store.reference("GOOGLE_API_KEY").configured)
        public = store.public_status("GROQ_API_KEY")
        self.assertEqual(public["env_var"], "GROQ_API_KEY")
        self.assertTrue(public["configured"])
        self.assertNotIn("fixture-groq-secret", json.dumps(public))

    def test_missing_doppler_style_environment_remains_not_configured(self):
        store = CredentialStore({})
        self.assertFalse(store.reference("GROQ_API_KEY").configured)
        self.assertFalse(store.reference("GOOGLE_API_KEY").configured)

    def test_dry_run_with_injected_fixture_has_zero_network_and_redaction(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
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
            with patch.dict(
                os.environ,
                {
                    "GROQ_API_KEY": "fixture-groq-secret",
                    "GOOGLE_API_KEY": "fixture-google-secret",
                },
                clear=False,
            ), patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
                control = ArenaControl(config)
                diagnostics = control.provider_diagnostics(["groq", "gemini"])
                report = control.provider_health_report(window="1h")
            urlopen.assert_not_called()
            self.assertEqual(diagnostics["network_requests"], 0)
            self.assertEqual(diagnostics["network"], "NO")
            self.assertEqual(diagnostics["live_gate"], "DISABLED")
            serialized = json.dumps({"diagnostics": diagnostics, "report": report})
            self.assertNotIn("fixture-groq-secret", serialized)
            self.assertNotIn("fixture-google-secret", serialized)

    def test_fixture_environment_does_not_enable_live_gate(self):
        gate = ExternalLiveRequestGate.disabled()
        self.assertFalse(gate.enabled)
        self.assertEqual(VERSION_TAG, "v0.9.0")


if __name__ == "__main__":
    unittest.main()
