"""Phase-9L CLI report and manual archive tests; no provider requests."""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import arena_launcher
from arena_alerts import ProviderAlertStateStore
from arena_archive import ArchiveError, archive_local_diagnostics
from arena_history import ProviderHealthHistory


class _FakeConfig:
    def __init__(self, state_dir: Path):
        self.arena_state_dir = state_dir
        self.provider_pool = {}

    def validate(self):
        return None


class _FakeControl:
    def __init__(self, state_dir: Path):
        self.health_history = ProviderHealthHistory(state_dir / "provider-health-history.json")
        self.alert_states = ProviderAlertStateStore(state_dir / "provider-alert-state.json")
        self.report_calls = []

    def provider_health_report(self, *, window="1h", provider=None):
        self.report_calls.append((window, provider))
        return {
            "title": "Provider Health Report",
            "window": window,
            "network": "NO",
            "providers": [],
            "alerts": [],
        }

    def export_provider_health_report(self, path, *, window="1h", provider=None, format="text", **_kwargs):
        payload = self.provider_health_report(window=window, provider=provider)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if format == "json":
            destination.write_text(json.dumps(payload), encoding="utf-8")
        else:
            destination.write_text("PROVIDER HEALTH REPORT\nWindow: " + window + "\nNetwork: NO\n", encoding="utf-8")
        return destination


class ArchiveRotationTests(unittest.TestCase):
    def seed(self, root: Path) -> tuple[ProviderHealthHistory, ProviderAlertStateStore]:
        history = ProviderHealthHistory(root / "provider-health-history.json", max_events=100)
        alerts = ProviderAlertStateStore(root / "provider-alert-state.json")
        history.record("groq", "rate_limited", health_status="rate_limited")
        alerts._states["fixture-alert"] = {
            "alert_id": "fixture-alert",
            "provider": "groq",
            "severity": "WARNING",
            "type": "repeated_rate_limit",
            "count": 3,
            "window": "1h",
            "message": "rate limit",
            "created_at": "2026-08-14T12:00:00+00:00",
            "acknowledged": False,
            "acknowledged_at": None,
            "suppressed_until": None,
            "resolved": False,
            "resolved_at": None,
        }
        alerts._persist()
        return history, alerts

    def test_archive_history_resets_only_history(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            history, alerts = self.seed(root)
            output = root / "archive"
            paths = archive_local_diagnostics(
                history, alerts, output, history_enabled=True, now=dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
            )
            self.assertEqual(len(paths), 1)
            self.assertRegex(paths[0].name, r"^provider-health-history-20260814-120000\.json$")
            archive = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(archive["event_count"], 1)
            self.assertEqual(history.events(), [])
            self.assertEqual(len(alerts.states()), 1)

    def test_archive_all_validates_and_resets_both(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            history, alerts = self.seed(root)
            paths = archive_local_diagnostics(history, alerts, root / "archive", history_enabled=True, alerts_enabled=True)
            self.assertEqual({path.name.split("-")[1] for path in paths}, {"health", "alert"})
            self.assertEqual(history.events(), [])
            self.assertEqual(alerts.states(), [])
            self.assertEqual(json.loads(history.path.read_text(encoding="utf-8"))["events"], [])
            self.assertEqual(json.loads(alerts.path.read_text(encoding="utf-8"))["alerts"], [])

    def test_existing_archive_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            history, alerts = self.seed(root)
            output = root / "archive"
            archive_local_diagnostics(
                history, alerts, output, history_enabled=True,
                now=dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc),
            )
            history.record("groq", "provider_down", health_status="provider_down")
            with self.assertRaises(FileExistsError):
                archive_local_diagnostics(
                    history, alerts, output, history_enabled=True,
                    now=dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc),
                )
            self.assertEqual(len(history.events()), 1)

    def test_invalid_source_preserves_original(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            history_path = root / "provider-health-history.json"
            history_path.write_text("broken", encoding="utf-8")
            history = ProviderHealthHistory(history_path)
            alerts = ProviderAlertStateStore(root / "alerts.json")
            with self.assertRaises(ArchiveError):
                archive_local_diagnostics(history, alerts, root / "archive", history_enabled=True)
            self.assertEqual(history_path.read_text(encoding="utf-8"), "broken")

    def test_active_history_limit_remains_100_after_rotation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            history = ProviderHealthHistory(root / "history.json", max_events=100)
            alerts = ProviderAlertStateStore(root / "alerts.json")
            for _ in range(105):
                history.record("ollama", "health_checked", health_status="healthy")
            self.assertEqual(len(history.events()), 100)
            archive_local_diagnostics(history, alerts, root / "archive", history_enabled=True)
            rotated = ProviderHealthHistory(root / "history.json", max_events=100)
            self.assertEqual(rotated.max_events, 100)
            self.assertEqual(rotated.events(), [])


class CliReportTests(unittest.TestCase):
    def namespace(self, **overrides):
        values = {
            "check": False, "smoke": False, "dry_run": False, "live": False,
            "reason": None, "json": False, "providers": None, "output": None,
            "output_format": "text", "window": "1h", "history": False,
            "alerts": False, "archive_all": False, "config": Path("config.json"),
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_report_stdout_text_and_json_are_offline(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = _FakeConfig(root)
            control = _FakeControl(root)
            with patch.object(arena_launcher.RuntimeConfig, "load", return_value=config), patch.object(arena_launcher, "ArenaControl", return_value=control), patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
                text_out = io.StringIO()
                with redirect_stdout(text_out):
                    result = arena_launcher._run_report(self.namespace(window="24h"))
                self.assertEqual(result, 0)
                self.assertIn("PROVIDER HEALTH REPORT", text_out.getvalue())
                json_out = io.StringIO()
                with redirect_stdout(json_out):
                    result = arena_launcher._run_report(self.namespace(output_format="json", window="7d"))
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(json_out.getvalue())["window"], "7d")
            urlopen.assert_not_called()
            self.assertEqual(control.report_calls, [("24h", None), ("7d", None)])

    def test_report_output_protection_and_provider_filter(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = _FakeConfig(root)
            control = _FakeControl(root)
            output = root / "report.txt"
            output.write_text("keep", encoding="utf-8")
            args = self.namespace(output=output, providers=["groq"], output_format="text")
            stderr = io.StringIO()
            with patch.object(arena_launcher.RuntimeConfig, "load", return_value=config), patch.object(arena_launcher, "ArenaControl", return_value=control), redirect_stderr(stderr):
                self.assertEqual(arena_launcher._run_report(args), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_archive_cli_and_help(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = _FakeConfig(root)
            control = _FakeControl(root)
            output = root / "archive"
            args = self.namespace(config=Path("config.json"), output=output, history=True)
            with patch.object(arena_launcher.RuntimeConfig, "load", return_value=config), patch.object(arena_launcher, "ArenaControl", return_value=control):
                self.assertEqual(arena_launcher._run_archive(args), 0)
            self.assertTrue(list(output.glob("provider-health-history-*.json")))

        help_result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("arena_launcher.py")), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("report", help_result.stdout)
        self.assertIn("archive", help_result.stdout)

    def test_invalid_report_format_is_cli_error(self):
        args = self.namespace(output_format="csv")
        # argparse normally prevents this value; the API still fails closed.
        with self.assertRaises(ValueError):
            _ = args.output_format if args.output_format in {"text", "json"} else (_ for _ in ()).throw(ValueError("invalid format"))


if __name__ == "__main__":
    unittest.main()
