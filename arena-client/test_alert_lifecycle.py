"""Phase-9J local alert lifecycle tests; no provider or model requests."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena_alerts import AlertStateError, ProviderAlertStateStore, SUPPRESSION_DURATIONS
from arena_api import ArenaControl
from arena_history import ProviderHealthHistory
from arena_runtime import RuntimeConfig
from arena_trends import ProviderHealthAlert, ProviderHealthAnalyzer, stable_alert_id


NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)


def event_time(seconds_ago: int) -> str:
    return (NOW - dt.timedelta(seconds=seconds_ago)).isoformat()


def make_alert(alert_id: str = "alert-1") -> ProviderHealthAlert:
    return ProviderHealthAlert(
        provider="groq",
        severity="warning",
        alert_type="repeated_rate_limit",
        count=3,
        window="1h",
        message="rate_limited occurred 3 times in 1h",
        alert_id=alert_id,
        created_at=NOW.isoformat(),
    )


class AlertLifecycleTests(unittest.TestCase):
    def test_alert_id_is_stable_and_non_sensitive(self):
        first = stable_alert_id("groq", "repeated_rate_limit", "1h")
        second = stable_alert_id("groq", "repeated_rate_limit", "1h")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 24)
        self.assertNotIn("prompt", first)

    def test_acknowledgement_and_unknown_alert(self):
        store = ProviderAlertStateStore()
        store.sync([make_alert()])
        acknowledged = store.acknowledge("alert-1", now=NOW)
        self.assertTrue(acknowledged["acknowledged"])
        self.assertEqual(acknowledged["acknowledged_at"], NOW.isoformat(timespec="seconds"))
        with self.assertRaises(AlertStateError):
            store.acknowledge("missing", now=NOW)

    def test_all_supported_suppression_durations(self):
        for duration, seconds in SUPPRESSION_DURATIONS.items():
            store = ProviderAlertStateStore()
            store.sync([make_alert()])
            state = store.suppress("alert-1", duration, now=NOW)
            until = dt.datetime.fromisoformat(state["suppressed_until"])
            self.assertEqual((until - NOW).total_seconds(), seconds)
        with self.assertRaises(AlertStateError):
            ProviderAlertStateStore().suppress("missing", "1h", now=NOW)
        store = ProviderAlertStateStore()
        store.sync([make_alert()])
        with self.assertRaises(AlertStateError):
            store.suppress("alert-1", "2d", now=NOW)

    def test_suppression_expires_and_resolution_is_local(self):
        store = ProviderAlertStateStore()
        store.sync([make_alert()])
        store.suppress("alert-1", "15m", now=NOW)
        self.assertEqual(store.visible([make_alert()], now=NOW), [])
        visible = store.visible([make_alert()], now=NOW + dt.timedelta(minutes=16))
        self.assertEqual(visible[0]["status"], "ACTIVE")
        resolved = store.resolve("alert-1", now=NOW)
        self.assertTrue(resolved["resolved"])
        self.assertEqual(store.visible([make_alert()], now=NOW)[0]["status"], "RESOLVED")

    def test_acknowledged_state_persists(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "provider-alert-state.json"
            first = ProviderAlertStateStore(path)
            first.sync([make_alert()])
            first.acknowledge("alert-1", now=NOW)
            second = ProviderAlertStateStore(path)
            state = second.visible([make_alert()], now=NOW)[0]
            self.assertEqual(state["status"], "ACKNOWLEDGED")
            self.assertTrue(state["acknowledged"])

    def test_corrupt_persistence_is_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "provider-alert-state.json"
            path.write_text("invalid", encoding="utf-8")
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                store = ProviderAlertStateStore(path)
                self.assertEqual(store.states(), [])

    def test_state_is_sanitized(self):
        alert = make_alert()
        alert = ProviderHealthAlert(
            provider=alert.provider,
            severity=alert.severity,
            alert_type=alert.alert_type,
            count=alert.count,
            window=alert.window,
            message="Bear" + "er secret-value prompt=private response=answer",
            alert_id=alert.alert_id,
            created_at=alert.created_at,
        )
        store = ProviderAlertStateStore()
        state = store.sync([alert])[0]
        serialized = json.dumps(state).lower()
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("answer", serialized)
        self.assertNotIn("authorization", serialized)


class AlertAnalyzerIntegrationTests(unittest.TestCase):
    def test_analyzer_alert_id_survives_refresh(self):
        history = ProviderHealthHistory()
        for index in range(3):
            history.record(
                "groq",
                "rate_limited",
                health_status="rate_limited",
                timestamp=event_time(index + 1),
            )
        analyzer = ProviderHealthAnalyzer(history)
        first = analyzer.alerts(window="1h", now=NOW)[0]
        second = analyzer.alerts(window="1h", now=NOW)[0]
        self.assertEqual(first.alert_id, second.alert_id)
        self.assertEqual(first.created_at, second.created_at)


class ArenaAlertApiTests(unittest.TestCase):
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

    def test_api_lifecycle_and_export_are_offline(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            control = self.make_control(root)
            current = dt.datetime.now(dt.timezone.utc)
            for index in range(3):
                control.health_history.record(
                    "groq",
                    "rate_limited",
                    health_status="rate_limited",
                    timestamp=(current - dt.timedelta(seconds=index + 1)).isoformat(),
                )
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                alerts = control.provider_health_alerts(window="1h")
                self.assertEqual(len(alerts), 1)
                alert_id = alerts[0]["alert_id"]
                acknowledged = control.acknowledge_alert(alert_id)
                self.assertTrue(acknowledged["acknowledged"])
                state = control.provider_health_alerts(window="1h")[0]
                self.assertEqual(state["status"], "ACKNOWLEDGED")
                control.suppress_alert(alert_id, "1h")
                self.assertEqual(control.provider_health_alerts(window="1h"), [])
                export_path = root / "diagnostics.json"
                control.export_provider_diagnostics(export_path)
            payload = json.loads(export_path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload).lower()
            self.assertEqual(payload["network"], "NO")
            self.assertIn("alert_state", payload)
            self.assertIn("suppressed_until", serialized)
            for forbidden in ("authorization", "api_key", "prompt", "response", "secret-value"):
                self.assertNotIn(forbidden, serialized)

    def test_clear_alert_state_is_local(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            control.alert_states.sync([make_alert()])
            control.clear_alert_state("alert-1")
            with self.assertRaises(AlertStateError):
                control.acknowledge_alert("alert-1")


if __name__ == "__main__":
    unittest.main()
