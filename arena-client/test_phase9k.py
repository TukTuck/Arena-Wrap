"""Phase-9K local filtering, bulk action, and report tests."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena_api import ArenaControl
from arena_runtime import RuntimeConfig
from arena_reports import render_provider_health_report


def recent_timestamp(seconds_ago: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat()


class Phase9KControlTests(unittest.TestCase):
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

    def seed_alerts(self, control: ArenaControl) -> None:
        for index in range(3):
            control.health_history.record(
                "groq", "rate_limited", health_status="rate_limited", timestamp=recent_timestamp(index + 1)
            )
        for index in range(2):
            control.health_history.record(
                "gemini", "authentication_failed", health_status="authentication_failed", timestamp=recent_timestamp(index + 4)
            )
            control.health_history.record(
                "ollama", "provider_down", health_status="provider_down", timestamp=recent_timestamp(index + 7)
            )

    def test_provider_status_severity_type_and_combined_filters(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            self.seed_alerts(control)
            all_alerts = control.provider_health_alerts(alert_window="1h", include_suppressed=True)
            self.assertEqual(len(all_alerts), 3)
            self.assertEqual(len(control.provider_health_alerts(provider="groq", alert_window="1h", include_suppressed=True)), 1)
            self.assertEqual(len(control.provider_health_alerts(severity="ERROR", alert_window="1h", include_suppressed=True)), 1)
            self.assertEqual(len(control.provider_health_alerts(alert_type="repeated_rate_limit", alert_window="1h", include_suppressed=True)), 1)
            self.assertEqual(len(control.provider_health_alerts(provider="groq", severity="WARNING", alert_type="repeated_rate_limit", status="ACTIVE", alert_window="1h", include_suppressed=True)), 1)
            control.suppress_alert(all_alerts[0]["alert_id"], "1h")
            self.assertEqual(len(control.provider_health_alerts(status="SUPPRESSED", alert_window="1h")), 1)
            self.assertEqual(control.provider_health_alerts(provider="missing", alert_window="1h", include_suppressed=True), [])

    def test_filtering_is_local_and_has_no_network(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            self.seed_alerts(control)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
                control.alert_filter_options()
                control.provider_health_alerts(status="ACTIVE", alert_window="All", include_suppressed=True)
                control.provider_health_snapshot(alert_window="All", include_suppressed=True)
            urlopen.assert_not_called()

    def test_bulk_acknowledge_suppress_all_durations_and_resolve(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            self.seed_alerts(control)
            alerts = control.provider_health_alerts(alert_window="All", include_suppressed=True)
            ids = [item["alert_id"] for item in alerts]
            acknowledged = control.bulk_acknowledge_alerts(ids[:2])
            self.assertEqual(len(acknowledged), 2)
            self.assertTrue(all(item["acknowledged"] for item in acknowledged))
            for duration in ("15m", "1h", "6h", "24h"):
                control.bulk_suppress_alerts([ids[0]], duration)
                control.alert_states.clear(ids[0])
                control.alert_states.sync(control._current_alerts(alert_window="All"))
            resolved = control.bulk_resolve_alerts([ids[1]])
            self.assertTrue(resolved[0]["resolved"])
            with self.assertRaises(ValueError):
                control.bulk_acknowledge_alerts([])
            with self.assertRaises(ValueError):
                control.bulk_acknowledge_alerts([ids[0], "unknown"])

    def test_cancelled_or_unselected_bulk_action_changes_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            self.seed_alerts(control)
            before = control.alert_states.states()
            with self.assertRaises(ValueError):
                control.bulk_resolve_alerts([])
            self.assertEqual(control.alert_states.states(), before)

    def test_report_and_json_txt_exports_are_sanitized_and_offline(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            control = self.make_control(root)
            self.seed_alerts(control)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
                report = control.provider_health_report(window="24h")
                text = render_provider_health_report(report)
                json_path = control.export_provider_health_report(root / "report.json", format="json")
                txt_path = control.export_provider_health_report(root / "report.txt", format="txt")
                diagnostics_path = control.export_provider_diagnostics(root / "diagnostics.json")
            urlopen.assert_not_called()
            self.assertEqual(report["network"], "NO")
            self.assertIn("PROVIDER HEALTH REPORT", text)
            self.assertIn("Rate limits:", text)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
            self.assertIn("providers", payload)
            self.assertIn("report", diagnostics)
            self.assertIn("Provider failures:", txt_path.read_text(encoding="utf-8"))
            serialized = json.dumps(diagnostics).lower() + txt_path.read_text(encoding="utf-8").lower()
            for forbidden in ("api_key", "authorization", "prompt", "model response", "request body", "bearer"):
                self.assertNotIn(forbidden, serialized)

    def test_invalid_report_format_is_rejected_without_network(self):
        with tempfile.TemporaryDirectory() as raw:
            control = self.make_control(Path(raw))
            with self.assertRaises(ValueError):
                control.export_provider_health_report(Path(raw) / "report.csv", format="csv")


if __name__ == "__main__":
    unittest.main()
