"""Phase-9G headless dashboard tests; no Tk window or network is opened."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from arena_app import ProviderDiagnosticsDashboard, diagnostics_table_rows, health_tag
from arena_transport import ExternalLiveRequestGate


class _Value:
    def __init__(self, value: str):
        self.value = value

    def get(self):
        return self.value


class _Control:
    def __init__(self):
        self.calls: list[tuple[object, object]] = []
        self.result = {
            "mode": "DRY_RUN",
            "live_gate": "DISABLED",
            "network": "NO",
            "network_requests": 0,
            "providers": [],
        }

    def provider_diagnostics(self, provider_ids, *, live_gate=None):
        self.calls.append((provider_ids, live_gate))
        return self.result

    def health_history_events(self, *, provider=None, event_filter="all", limit=None):
        return [{
            "timestamp": "2026-01-01T00:00:00+00:00",
            "provider": provider or "groq",
            "event_type": "rate_limited",
            "health_status": "rate_limited",
            "circuit_state": "open",
            "retry_after_seconds": 10,
            "message": "rate_limited",
        }]

    def alert_filter_options(self):
        return {"types": ["repeated_provider_down"]}

    def provider_health_snapshot(
        self,
        *,
        window="1h",
        provider=None,
        event_filter="all",
        alert_provider=None,
        alert_window=None,
        alert_severity=None,
        alert_type=None,
        alert_status=None,
        include_suppressed=False,
    ):
        return {
            "window": window,
            "network": "NO",
            "trends": [{
                "provider": provider or "groq",
                "window": window,
                "event_count": 3,
                "counts": {"successful_health_checks": 1, "provider_down": 2},
                "latest_status": "provider_down",
            }],
            "alerts": [{
                "severity": "warning",
                "provider": provider or "groq",
                "type": "repeated_provider_down",
                "count": 2,
                "window": window,
                "message": "provider_down occurred 2 times in 1h",
                "status": "ACTIVE",
                "alert_id": "fixture-alert",
            }],
        }

    def clear_health_history(self):
        self.history_cleared = True

    def bulk_acknowledge_alerts(self, alert_ids):
        self.bulk_action = ("acknowledge", alert_ids)

    def bulk_suppress_alerts(self, alert_ids, duration):
        self.bulk_action = ("suppress", alert_ids, duration)

    def bulk_resolve_alerts(self, alert_ids):
        self.bulk_action = ("resolve", alert_ids)

    def export_health_history(self, path, *, provider=None, event_filter="all"):
        self.exported = (path, provider, event_filter)


class _Tree:
    def __init__(self):
        self.rows = []
        self.selected = []

    def get_children(self):
        return list(range(len(self.rows)))

    def delete(self, _item_id):
        self.rows.pop(0)

    def insert(self, _parent, _position, values, **_kwargs):
        self.rows.append(values)
        return str(len(self.rows) - 1)

    def selection(self):
        return tuple(self.selected)


class ProviderDashboardTests(unittest.TestCase):
    def test_table_rows_use_sanitized_diagnostics_fields(self):
        payload = {
            "providers": [
                {
                    "provider": "groq",
                    "health": "rate_limited",
                    "credential_status": "available",
                    "adapter_available": True,
                    "models": ["fixture-model"],
                    "circuit_breaker": {"state": "open"},
                    "retry_after_seconds": 12.5,
                    "network": "NO",
                    "last_error": "rate_limited",
                },
                {
                    "provider": "sambanova",
                    "health": "not_configured",
                    "credential_status": "missing",
                    "adapter_available": False,
                    "models": [],
                    "circuit_breaker": {"state": "closed"},
                    "retry_after_seconds": None,
                    "network": "NO",
                    "last_error": None,
                },
            ]
        }
        rows = diagnostics_table_rows(payload)
        self.assertEqual(rows[0][0:4], ("groq", "rate_limited", "available", "available"))
        self.assertEqual(rows[0][5:8], ("open", "12.5", "NO"))
        self.assertEqual(rows[1][3], "unavailable")
        self.assertEqual(rows[1][4], "-")

    def test_health_tags_only_present_existing_states(self):
        self.assertEqual(health_tag("healthy"), "healthy")
        self.assertEqual(health_tag("rate_limited"), "warning")
        self.assertEqual(health_tag("provider_down"), "error")
        self.assertEqual(health_tag("privacy_blocked"), "blocked")
        self.assertEqual(health_tag("not_checked"), "not_checked")

    def test_dashboard_refresh_is_dry_run_and_does_not_open_live_gate(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.provider_var = _Value("groq")
        dashboard._render = Mock()

        dashboard.refresh_diagnostics()

        self.assertEqual(len(dashboard.control.calls), 1)
        provider_ids, gate = dashboard.control.calls[0]
        self.assertEqual(provider_ids, ["groq"])
        self.assertIsNone(gate)
        dashboard._render.assert_called_once()

    def test_live_check_requires_confirmation_and_uses_existing_gate(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.provider_var = _Value("groq")
        dashboard._render = Mock()

        with patch("arena_app.messagebox.askyesno", return_value=True):
            dashboard.live_check_selected()

        self.assertEqual(len(dashboard.control.calls), 1)
        provider_ids, gate = dashboard.control.calls[0]
        self.assertEqual(provider_ids, ["groq"])
        self.assertIsInstance(gate, ExternalLiveRequestGate)
        self.assertTrue(gate.enabled)
        self.assertIn("diagnostics UI", gate.reason)

    def test_live_check_all_provider_selection_is_rejected(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.provider_var = _Value("All")
        dashboard._render = Mock()

        with patch("arena_app.messagebox.showwarning") as warning:
            dashboard.live_check_selected()

        self.assertEqual(dashboard.control.calls, [])
        warning.assert_called_once()

    def test_history_refresh_uses_local_history_api(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.history_provider_var = _Value("All")
        dashboard.history_event_var = _Value("Rate Limits")
        dashboard.history_tree = _Tree()

        dashboard.refresh_history()

        self.assertEqual(len(dashboard.history_tree.rows), 1)
        self.assertEqual(dashboard.history_tree.rows[0][2], "rate_limited")
        self.assertEqual(dashboard.history_tree.rows[0][5], 10)

    def test_trends_refresh_uses_offline_snapshot(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.trend_window_var = _Value("1h")
        dashboard.history_provider_var = _Value("All")
        dashboard.history_event_var = _Value("All Events")
        dashboard.trend_tree = _Tree()
        dashboard.alert_tree = _Tree()

        dashboard.refresh_trends()

        self.assertEqual(dashboard.trend_tree.rows[0][0], "groq")
        self.assertEqual(dashboard.trend_tree.rows[0][4], "2")
        self.assertEqual(dashboard.alert_tree.rows[0][2], "repeated_provider_down")

    def test_bulk_resolve_requires_confirmation_and_cancel_is_safe(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.alert_tree = _Tree()
        dashboard.alert_tree.selected = ["row-1", "row-2"]
        dashboard._alert_ids = {"row-1": "alert-1", "row-2": "alert-2"}

        with patch("arena_app.messagebox.askyesno", return_value=False):
            dashboard.resolve_selected_alerts()

        self.assertFalse(hasattr(dashboard.control, "bulk_action"))

    def test_bulk_suppress_uses_selected_duration(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.alert_tree = _Tree()
        dashboard.alert_tree.selected = ["row-1"]
        dashboard._alert_ids = {"row-1": "alert-1"}
        dashboard.alert_duration_var = _Value("6h")
        dashboard.history_provider_var = _Value("All")
        dashboard.history_event_var = _Value("All Events")
        dashboard.trend_window_var = _Value("1h")
        dashboard.trend_tree = _Tree()
        dashboard.alert_type_var = _Value("All")
        dashboard.alert_type_combo = {"values": []}

        with patch("arena_app.messagebox.askyesno", return_value=True):
            dashboard.suppress_selected_alerts()

        self.assertEqual(dashboard.control.bulk_action, ("suppress", ["alert-1"], "6h"))

    def test_live_check_cancel_does_not_call_control(self):
        dashboard = ProviderDiagnosticsDashboard.__new__(ProviderDiagnosticsDashboard)
        dashboard.control = _Control()
        dashboard.provider_var = _Value("gemini")
        dashboard._render = Mock()

        with patch("arena_app.messagebox.askyesno", return_value=False):
            dashboard.live_check_selected()

        self.assertEqual(dashboard.control.calls, [])


if __name__ == "__main__":
    unittest.main()
