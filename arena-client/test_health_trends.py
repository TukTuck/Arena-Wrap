"""Phase-9I offline provider trend and alert tests."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena_api import ArenaControl
from arena_app import alert_table_rows, trend_table_rows
from arena_history import ProviderHealthHistory
from arena_runtime import RuntimeConfig
from arena_trends import ProviderHealthAnalyzer


NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)


def stamp(seconds_ago: int) -> str:
    return (NOW - dt.timedelta(seconds=seconds_ago)).isoformat()


class ProviderHealthTrendTests(unittest.TestCase):
    def make_history(self) -> ProviderHealthHistory:
        return ProviderHealthHistory(max_events=100)

    def test_trends_cover_failures_rate_limits_circuit_and_success(self):
        history = self.make_history()
        history.record("groq", "health_checked", health_status="healthy", timestamp=stamp(60))
        history.record("groq", "provider_healthy", health_status="healthy", timestamp=stamp(59))
        history.record("groq", "provider_down", health_status="provider_down", timestamp=stamp(120))
        history.record("groq", "rate_limited", health_status="rate_limited", timestamp=stamp(180))
        history.record("groq", "authentication_failed", health_status="authentication_failed", timestamp=stamp(240))
        history.record("groq", "model_unavailable", health_status="model_unavailable", timestamp=stamp(300))
        history.record("groq", "circuit_opened", health_status="provider_down", timestamp=stamp(360))

        trend = ProviderHealthAnalyzer(history).trends(window="1h", now=NOW)[0]
        self.assertEqual(trend.provider, "groq")
        self.assertEqual(trend.counts["provider_down"], 1)
        self.assertEqual(trend.counts["rate_limited"], 1)
        self.assertEqual(trend.counts["authentication_failed"], 1)
        self.assertEqual(trend.counts["model_unavailable"], 1)
        self.assertEqual(trend.counts["circuit_opened"], 1)
        self.assertEqual(trend.counts["successful_health_checks"], 1)

    def test_time_windows_and_provider_comparison(self):
        history = self.make_history()
        history.record("groq", "provider_down", health_status="provider_down", timestamp=stamp(60 * 60 + 1))
        history.record("gemini", "provider_healthy", health_status="healthy", timestamp=stamp(60 * 60 * 25))
        history.record("ollama", "provider_healthy", health_status="healthy", timestamp=stamp(60))
        analyzer = ProviderHealthAnalyzer(history)
        one_hour = analyzer.trends(window="1h", now=NOW)
        self.assertEqual([item.provider for item in one_hour], ["ollama"])
        one_day = analyzer.trends(window="24h", now=NOW)
        self.assertEqual([item.provider for item in one_day], ["groq", "ollama"])
        self.assertEqual(len(analyzer.trends(window="1h", provider="gemini", now=NOW)), 1)
        self.assertEqual(analyzer.trends(window="1h", provider="gemini", now=NOW)[0].event_count, 0)

    def test_event_filter_is_applied(self):
        history = self.make_history()
        history.record("groq", "provider_down", health_status="provider_down", timestamp=stamp(10))
        history.record("groq", "rate_limited", health_status="rate_limited", timestamp=stamp(20))
        analyzer = ProviderHealthAnalyzer(history)
        trends = analyzer.trends(window="1h", event_filter="rate_limits", now=NOW)
        self.assertEqual(trends[0].event_count, 1)
        self.assertEqual(trends[0].counts["rate_limited"], 1)

    def test_repeated_errors_create_alerts_but_single_error_does_not(self):
        history = self.make_history()
        history.record("groq", "provider_down", health_status="provider_down", timestamp=stamp(10))
        self.assertEqual(ProviderHealthAnalyzer(history).alerts(window="1h", now=NOW), [])
        history.record("groq", "provider_down", health_status="provider_down", timestamp=stamp(20))
        alerts = ProviderHealthAnalyzer(history).alerts(window="1h", now=NOW)
        self.assertEqual(alerts[0].alert_type, "repeated_provider_down")
        self.assertEqual(alerts[0].count, 2)

    def test_repeated_rate_limits_auth_and_circuit_alerts(self):
        history = self.make_history()
        for index in range(3):
            history.record("groq", "rate_limited", health_status="rate_limited", timestamp=stamp(10 + index))
        for index in range(2):
            history.record("gemini", "authentication_failed", health_status="authentication_failed", timestamp=stamp(20 + index))
            history.record("ollama", "circuit_opened", health_status="provider_down", timestamp=stamp(30 + index))
        alerts = ProviderHealthAnalyzer(history).alerts(window="1h", now=NOW)
        by_type = {alert.alert_type: alert for alert in alerts}
        self.assertEqual(by_type["repeated_rate_limit"].count, 3)
        self.assertEqual(by_type["repeated_authentication_failure"].provider, "gemini")
        self.assertEqual(by_type["repeated_circuit_opening"].provider, "ollama")

    def test_thresholds_are_configurable_and_empty_history_is_safe(self):
        history = self.make_history()
        history.record("groq", "rate_limited", health_status="rate_limited", timestamp=stamp(1))
        analyzer = ProviderHealthAnalyzer(history, thresholds={"rate_limited": 1})
        self.assertEqual(len(analyzer.alerts(window="1h", now=NOW)), 1)
        empty = ProviderHealthAnalyzer(ProviderHealthHistory()).snapshot(window="7d", now=NOW)
        self.assertEqual(empty["trends"], [])
        self.assertEqual(empty["alerts"], [])
        self.assertEqual(empty["network"], "NO")

    def test_corrupt_history_is_local_and_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "history.json"
            path.write_text("not-json", encoding="utf-8")
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                history = ProviderHealthHistory(path)
                snapshot = ProviderHealthAnalyzer(history).snapshot(now=NOW)
            self.assertEqual(snapshot["trends"], [])
            self.assertEqual(snapshot["alerts"], [])

    def test_trend_and_alert_outputs_are_sanitized_metadata(self):
        history = self.make_history()
        history.record(
            "groq",
            "provider_down",
            health_status="provider_down",
            message="Bear" + "er secret-value prompt=private",
            timestamp=stamp(1),
        )
        history.record("groq", "provider_down", health_status="provider_down", timestamp=stamp(2))
        payload = ProviderHealthAnalyzer(history).snapshot(now=NOW)
        serialized = json.dumps(payload).lower()
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("prompt", serialized)


class ArenaTrendIntegrationTests(unittest.TestCase):
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

    def test_control_exposes_offline_snapshot_and_dashboard_rows(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            control.health_history.record("groq", "rate_limited", health_status="rate_limited")
            snapshot = control.provider_health_snapshot(window="1h")
            self.assertEqual(snapshot["network"], "NO")
            self.assertEqual(trend_table_rows(snapshot)[0][0], "groq")
            self.assertEqual(alert_table_rows(snapshot), [])
            self.assertEqual(control.provider_health_alerts(window="1h"), [])


if __name__ == "__main__":
    unittest.main()
