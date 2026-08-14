"""Arena shell with a thin, network-safe provider diagnostics dashboard."""

from __future__ import annotations

import argparse
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Any, Mapping

from arena_api import ArenaControl
from arena_reports import render_provider_health_report
from arena_runtime import ArenaRuntimeError, RuntimeConfig
from arena_transport import ExternalLiveRequestGate


_HEALTH_TAGS = {
    "healthy": "healthy",
    "degraded": "warning",
    "rate_limited": "warning",
    "quota_exhausted": "error",
    "provider_down": "error",
    "model_unavailable": "error",
    "authentication_failed": "error",
    "privacy_blocked": "blocked",
    "not_configured": "not_configured",
    "not_checked": "not_checked",
    "disabled": "disabled",
}


def diagnostics_table_rows(payload: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Convert sanitized diagnostics into display rows without new policy logic."""
    rows: list[tuple[str, ...]] = []
    for item in payload.get("providers", []):
        breaker = item.get("circuit_breaker", {})
        rows.append(
            (
                str(item.get("provider", "")),
                str(item.get("health", "")),
                str(item.get("credential_status", "")),
                "available" if item.get("adapter_available") else "unavailable",
                ", ".join(str(model) for model in item.get("models", [])) or "-",
                str(breaker.get("state", "closed")),
                str(item.get("retry_after_seconds") or "-"),
                str(item.get("network", "NO")),
                str(item.get("last_error") or "-"),
            )
        )
    return rows


def health_tag(health: str) -> str:
    """Map an existing registry health state to a presentation tag only."""
    return _HEALTH_TAGS.get(str(health), "warning")


def trend_table_rows(payload: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Convert offline trend data into compact display rows."""
    rows: list[tuple[str, ...]] = []
    for item in payload.get("trends", []):
        counts = item.get("counts", {})
        rows.append(
            (
                str(item.get("provider", "")),
                str(item.get("window", "")),
                str(item.get("event_count", 0)),
                str(counts.get("successful_health_checks", 0)),
                str(counts.get("provider_down", 0)),
                str(counts.get("rate_limited", 0)),
                str(counts.get("authentication_failed", 0)),
                str(counts.get("model_unavailable", 0)),
                str(counts.get("circuit_opened", 0)),
                str(item.get("latest_status") or "-"),
            )
        )
    return rows


def alert_table_rows(payload: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Convert sanitized local alerts into display rows."""
    return [
        (
            str(item.get("severity", "")),
            str(item.get("provider", "")),
            str(item.get("type", "")),
            str(item.get("count", 0)),
            str(item.get("window", "")),
            str(item.get("message", "")),
            str(item.get("status", "ACTIVE")),
            str(item.get("alert_id", "")),
        )
        for item in payload.get("alerts", [])
    ]


class ProviderDiagnosticsDashboard(ttk.Frame):
    """Read-only diagnostics view with explicit dry-run/live actions."""

    def __init__(self, parent: tk.Misc, control: ArenaControl):
        super().__init__(parent, padding=12)
        self.control = control
        self.provider_var = tk.StringVar(value="All")
        self.mode_var = tk.StringVar(value="DRY RUN")
        self.network_var = tk.StringVar(value="Network Requests: 0")
        self.privacy_var = tk.StringVar(
            value="Privacy: PUBLIC / INTERNAL / PRIVATE / SECRET · PRIVATE/SECRET bleiben extern blockiert"
        )
        self.history_provider_var = tk.StringVar(value="All")
        self.history_event_var = tk.StringVar(value="All Events")
        self.trend_window_var = tk.StringVar(value="1h")
        self.alert_provider_var = tk.StringVar(value="All")
        self.alert_status_var = tk.StringVar(value="All")
        self.alert_severity_var = tk.StringVar(value="All")
        self.alert_type_var = tk.StringVar(value="All")
        self.alert_window_var = tk.StringVar(value="All")
        self.alert_duration_var = tk.StringVar(value="1h")
        self._build()
        self.refresh_diagnostics()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="Provider Diagnostics", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(toolbar, textvariable=self.mode_var).pack(side="left", padx=(18, 4))
        ttk.Label(toolbar, textvariable=self.network_var).pack(side="left", padx=4)

        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Provider:").pack(side="left")
        provider_ids = [provider.id for provider in self.control.providers.list()]
        self.provider_combo = ttk.Combobox(
            controls,
            textvariable=self.provider_var,
            values=["All", *provider_ids],
            state="readonly",
            width=18,
        )
        self.provider_combo.pack(side="left", padx=(6, 12))
        ttk.Button(controls, text="Refresh Diagnostics (Dry Run)", command=self.refresh_diagnostics).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(controls, text="Live Check Selected", command=self.live_check_selected).pack(side="left")

        ttk.Label(self, textvariable=self.privacy_var, foreground="#6b4f00").pack(anchor="w", pady=(0, 8))

        columns = (
            "provider",
            "health",
            "credential",
            "adapter",
            "models",
            "breaker",
            "retry",
            "network",
            "error",
        )
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=16)
        headings = {
            "provider": "Provider",
            "health": "Status",
            "credential": "Credential",
            "adapter": "Adapter",
            "models": "Models",
            "breaker": "Circuit Breaker",
            "retry": "Retry-After",
            "network": "Network",
            "error": "Last Error",
        }
        widths = {
            "provider": 100,
            "health": 130,
            "credential": 90,
            "adapter": 95,
            "models": 250,
            "breaker": 105,
            "retry": 85,
            "network": 75,
            "error": 220,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("healthy", foreground="#17803d")
        self.tree.tag_configure("warning", foreground="#9a6700")
        self.tree.tag_configure("error", foreground="#b42318")
        self.tree.tag_configure("blocked", foreground="#7a3e00")
        self.tree.tag_configure("not_configured", foreground="#666666")
        self.tree.tag_configure("not_checked", foreground="#666666")
        self.tree.tag_configure("disabled", foreground="#666666")

        history_controls = ttk.Frame(self)
        history_controls.pack(fill="x", pady=(10, 4))
        ttk.Label(history_controls, text="Provider Health History", font=("Segoe UI", 11, "bold")).pack(side="left")
        provider_ids = [provider.id for provider in self.control.providers.list()]
        ttk.Label(history_controls, text="Provider:").pack(side="left", padx=(16, 4))
        ttk.Combobox(
            history_controls,
            textvariable=self.history_provider_var,
            values=["All", *provider_ids],
            state="readonly",
            width=14,
        ).pack(side="left")
        ttk.Label(history_controls, text="Event:").pack(side="left", padx=(10, 4))
        ttk.Combobox(
            history_controls,
            textvariable=self.history_event_var,
            values=["All Events", "Errors", "Rate Limits", "Circuit Breaker", "Health Checks"],
            state="readonly",
            width=16,
        ).pack(side="left")
        ttk.Button(history_controls, text="Refresh History", command=self.refresh_history).pack(side="left", padx=(8, 4))
        ttk.Button(history_controls, text="Clear History", command=self.clear_history).pack(side="left", padx=4)
        ttk.Button(history_controls, text="Export Diagnostics", command=self.export_diagnostics).pack(side="left", padx=4)

        history_columns = ("time", "provider", "event", "status", "circuit", "retry", "message")
        self.history_tree = ttk.Treeview(self, columns=history_columns, show="headings", height=7)
        history_headings = {
            "time": "Time",
            "provider": "Provider",
            "event": "Event",
            "status": "Status",
            "circuit": "Circuit",
            "retry": "Retry-After",
            "message": "Message",
        }
        history_widths = {"time": 160, "provider": 90, "event": 150, "status": 130, "circuit": 90, "retry": 90, "message": 300}
        for column in history_columns:
            self.history_tree.heading(column, text=history_headings[column])
            self.history_tree.column(column, width=history_widths[column], anchor="w")
        self.history_tree.pack(fill="x", expand=False)

        trend_controls = ttk.Frame(self)
        trend_controls.pack(fill="x", pady=(10, 4))
        ttk.Label(trend_controls, text="Health Trends / Alerts", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Label(trend_controls, text="Window:").pack(side="left", padx=(16, 4))
        ttk.Combobox(
            trend_controls,
            textvariable=self.trend_window_var,
            values=["1h", "6h", "24h", "7d"],
            state="readonly",
            width=8,
        ).pack(side="left")
        ttk.Button(trend_controls, text="Refresh Trends", command=self.refresh_trends).pack(side="left", padx=8)

        trend_columns = ("provider", "window", "events", "healthy", "down", "rate", "auth", "model", "circuit", "latest")
        self.trend_tree = ttk.Treeview(self, columns=trend_columns, show="headings", height=4)
        trend_headings = {
            "provider": "Provider", "window": "Window", "events": "Events", "healthy": "Healthy",
            "down": "Down", "rate": "429", "auth": "Auth", "model": "Model", "circuit": "Circuit", "latest": "Latest",
        }
        for column in trend_columns:
            self.trend_tree.heading(column, text=trend_headings[column])
            self.trend_tree.column(column, width=78 if column != "provider" else 100, anchor="w")
        self.trend_tree.pack(fill="x", expand=False)

        ttk.Label(self, text="Local informational alerts only · no automatic provider action", foreground="#6b4f00").pack(anchor="w", pady=(4, 2))
        alert_filters = ttk.Frame(self)
        alert_filters.pack(fill="x", pady=(2, 4))
        ttk.Label(alert_filters, text="Alerts:").pack(side="left")
        provider_ids = [provider.id for provider in self.control.providers.list()]
        self.alert_provider_combo = ttk.Combobox(
            alert_filters, textvariable=self.alert_provider_var,
            values=["All", *provider_ids], state="readonly", width=12
        )
        self.alert_provider_combo.pack(side="left", padx=(5, 5))
        self.alert_status_combo = ttk.Combobox(
            alert_filters, textvariable=self.alert_status_var,
            values=["All", "ACTIVE", "ACKNOWLEDGED", "SUPPRESSED", "RESOLVED"],
            state="readonly", width=14
        )
        self.alert_status_combo.pack(side="left", padx=5)
        self.alert_severity_combo = ttk.Combobox(
            alert_filters, textvariable=self.alert_severity_var,
            values=["All", "INFO", "WARNING", "ERROR"],
            state="readonly", width=10
        )
        self.alert_severity_combo.pack(side="left", padx=5)
        self.alert_type_combo = ttk.Combobox(
            alert_filters, textvariable=self.alert_type_var,
            values=["All"], state="readonly", width=26
        )
        self.alert_type_combo.pack(side="left", padx=5)
        self.alert_window_combo = ttk.Combobox(
            alert_filters, textvariable=self.alert_window_var,
            values=["All", "1h", "6h", "24h", "7d"], state="readonly", width=8
        )
        self.alert_window_combo.pack(side="left", padx=5)
        ttk.Label(alert_filters, text="Suppress:").pack(side="left", padx=(8, 2))
        ttk.Combobox(
            alert_filters, textvariable=self.alert_duration_var,
            values=["15m", "1h", "6h", "24h"], state="readonly", width=6
        ).pack(side="left", padx=2)
        ttk.Button(alert_filters, text="Apply Filters", command=self.refresh_trends).pack(side="left", padx=5)

        alert_columns = ("severity", "provider", "type", "count", "window", "message", "status")
        self.alert_tree = ttk.Treeview(
            self, columns=alert_columns, show="headings", height=3, selectmode="extended"
        )
        alert_headings = {"severity": "Severity", "provider": "Provider", "type": "Alert", "count": "Count", "window": "Window", "message": "Message", "status": "Status"}
        for column in alert_columns:
            self.alert_tree.heading(column, text=alert_headings[column])
            self.alert_tree.column(column, width=90 if column not in {"message", "status"} else 350, anchor="w")
        self.alert_tree.pack(fill="x", expand=False)
        self._alert_ids: dict[str, str] = {}
        alert_actions = ttk.Frame(self)
        alert_actions.pack(fill="x", pady=(4, 0))
        ttk.Button(alert_actions, text="Acknowledge Selected", command=self.acknowledge_selected_alerts).pack(side="left", padx=(0, 4))
        ttk.Button(alert_actions, text="Suppress Selected", command=self.suppress_selected_alerts).pack(side="left", padx=4)
        ttk.Button(alert_actions, text="Resolve Selected", command=self.resolve_selected_alerts).pack(side="left", padx=4)
        ttk.Button(alert_actions, text="Generate Report", command=self.generate_report).pack(side="left", padx=12)
        ttk.Button(alert_actions, text="Export JSON", command=lambda: self.export_report("json")).pack(side="left", padx=4)
        ttk.Button(alert_actions, text="Export TXT", command=lambda: self.export_report("txt")).pack(side="left", padx=4)
        self.report_text = tk.Text(self, height=9, wrap="word", state="disabled")
        self.report_text.pack(fill="x", expand=False, pady=(4, 0))

    def _selected_ids(self) -> list[str] | None:
        selected = self.provider_var.get().strip()
        return None if not selected or selected == "All" else [selected]

    def refresh_diagnostics(self) -> None:
        """Refresh metadata only; this action never opens a network connection."""
        try:
            result = self.control.provider_diagnostics(self._selected_ids())
            self._render(result)
        except (ArenaRuntimeError, KeyError) as exc:
            messagebox.showerror("Provider Diagnostics", str(exc))

    def live_check_selected(self) -> None:
        """Run one explicitly confirmed gated check for one selected provider."""
        selected = self.provider_var.get().strip()
        if not selected or selected == "All":
            messagebox.showwarning(
                "Provider Diagnostics",
                "Für einen Live-Check muss genau ein Provider ausgewählt werden.",
            )
            return
        confirmed = messagebox.askyesno(
            "LIVE / NETWORK",
            f"Provider '{selected}' wird jetzt explizit über das Netzwerk geprüft. Fortfahren?",
        )
        if not confirmed:
            return
        gate = ExternalLiveRequestGate.explicit("manually confirmed Arena diagnostics UI check")
        try:
            result = self.control.provider_diagnostics([selected], live_gate=gate)
            self._render(result)
        except (ArenaRuntimeError, KeyError) as exc:
            messagebox.showerror("Provider Diagnostics", str(exc))

    def _render(self, payload: Mapping[str, Any]) -> None:
        self.mode_var.set(str(payload.get("mode", "DRY_RUN")))
        self.network_var.set(f"Network Requests: {payload.get('network_requests', 0)}")
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for row in diagnostics_table_rows(payload):
            health = row[1]
            self.tree.insert("", "end", values=row, tags=(health_tag(health),))
        self.refresh_history()
        self.refresh_trends()

    def _history_filter(self) -> tuple[str | None, str]:
        provider = self.history_provider_var.get().strip()
        provider_id = None if not provider or provider == "All" else provider
        event_filter = {
            "All Events": "all",
            "Errors": "errors",
            "Rate Limits": "rate_limits",
            "Circuit Breaker": "circuit_breaker",
            "Health Checks": "health_checks",
        }.get(self.history_event_var.get(), "all")
        return provider_id, event_filter

    def refresh_history(self) -> None:
        provider, event_filter = self._history_filter()
        events = self.control.health_history_events(
            provider=provider, event_filter=event_filter, limit=100
        )
        for item_id in self.history_tree.get_children():
            self.history_tree.delete(item_id)
        for event in events:
            self.history_tree.insert(
                "",
                "end",
                values=(
                    event.get("timestamp", ""),
                    event.get("provider", ""),
                    event.get("event_type", ""),
                    event.get("health_status") or "-",
                    event.get("circuit_state") or "-",
                    event.get("retry_after_seconds")
                    if event.get("retry_after_seconds") is not None
                    else "-",
                    event.get("message") or "-",
                ),
            )

    def refresh_trends(self) -> None:
        provider, event_filter = self._history_filter()
        options = self.control.alert_filter_options()
        types = ["All", *options.get("types", [])]
        alert_type_combo = getattr(self, "alert_type_combo", None)
        if alert_type_combo is not None:
            alert_type_combo["values"] = types
        alert_type_var = getattr(self, "alert_type_var", None)
        if alert_type_var is not None and alert_type_var.get() not in types:
            alert_type_var.set("All")
        payload = self.control.provider_health_snapshot(
            window=self.trend_window_var.get(),
            provider=provider,
            event_filter=event_filter,
            alert_provider=self._alert_filter_value(getattr(self, "alert_provider_var", None)),
            alert_window=self._alert_filter_value(getattr(self, "alert_window_var", None)),
            alert_severity=self._alert_filter_value(getattr(self, "alert_severity_var", None)),
            alert_type=self._alert_filter_value(getattr(self, "alert_type_var", None)),
            alert_status=self._alert_filter_value(getattr(self, "alert_status_var", None)),
            include_suppressed=True,
        )
        for item_id in self.trend_tree.get_children():
            self.trend_tree.delete(item_id)
        for row in trend_table_rows(payload):
            self.trend_tree.insert("", "end", values=row)
        for item_id in self.alert_tree.get_children():
            self.alert_tree.delete(item_id)
        self._alert_ids = {}
        for row in alert_table_rows(payload):
            item_id = self.alert_tree.insert("", "end", values=row[:-1])
            self._alert_ids[str(item_id)] = row[-1]

    @staticmethod
    def _alert_filter_value(variable: tk.StringVar | None) -> str | None:
        if variable is None:
            return None
        value = variable.get().strip()
        return None if not value or value == "All" else value

    def _selected_alert_ids(self) -> list[str]:
        selection = self.alert_tree.selection()
        if not selection:
            messagebox.showwarning("Provider Alerts", "Bitte mindestens einen Alert auswählen.")
            return []
        alert_ids = [self._alert_ids.get(str(item_id), "") for item_id in selection]
        alert_ids = [alert_id for alert_id in alert_ids if alert_id]
        if not alert_ids:
            messagebox.showerror("Provider Alerts", "Die Auswahl enthält keine gültigen Alert-IDs.")
        return alert_ids

    def _confirm_bulk_action(self, action: str, alert_ids: list[str], *, resolve: bool = False) -> bool:
        if not alert_ids:
            return False
        message = f"{len(alert_ids)} alerts selected\nAction: {action}"
        if resolve:
            message += "\n\nDiese Aktion markiert die Auswahl lokal als RESOLVED."
        return messagebox.askyesno("Provider Alerts", message)

    def acknowledge_selected_alerts(self) -> None:
        alert_ids = self._selected_alert_ids()
        if not self._confirm_bulk_action("ACKNOWLEDGE", alert_ids):
            return
        try:
            self.control.bulk_acknowledge_alerts(alert_ids)
            self.refresh_trends()
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Provider Alerts", str(exc))

    def suppress_selected_alerts(self) -> None:
        alert_ids = self._selected_alert_ids()
        duration = self.alert_duration_var.get()
        if not self._confirm_bulk_action(f"SUPPRESS ({duration})", alert_ids):
            return
        try:
            self.control.bulk_suppress_alerts(alert_ids, duration)
            self.refresh_trends()
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Provider Alerts", str(exc))

    def resolve_selected_alerts(self) -> None:
        alert_ids = self._selected_alert_ids()
        if not self._confirm_bulk_action("RESOLVE", alert_ids, resolve=True):
            return
        try:
            self.control.bulk_resolve_alerts(alert_ids)
            self.refresh_trends()
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Provider Alerts", str(exc))

    def generate_report(self) -> None:
        provider, event_filter = self._history_filter()
        report = self.control.provider_health_report(
            window=self.trend_window_var.get(),
            provider=provider,
            event_filter=event_filter,
            alert_provider=self._alert_filter_value(self.alert_provider_var),
            alert_window=self._alert_filter_value(self.alert_window_var),
            alert_severity=self._alert_filter_value(self.alert_severity_var),
            alert_type=self._alert_filter_value(self.alert_type_var),
            alert_status=self._alert_filter_value(self.alert_status_var),
        )
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", render_provider_health_report(report))
        self.report_text.configure(state="disabled")

    def export_report(self, report_format: str) -> None:
        provider, event_filter = self._history_filter()
        extension = ".txt" if report_format == "txt" else ".json"
        destination = filedialog.asksaveasfilename(
            title=f"Export Provider Health Report ({report_format.upper()})",
            defaultextension=extension,
            filetypes=[(report_format.upper(), f"*{extension}"), ("All files", "*.*")],
        )
        if not destination:
            return
        try:
            self.control.export_provider_health_report(
                destination,
                window=self.trend_window_var.get(),
                provider=provider,
                event_filter=event_filter,
                alert_provider=self._alert_filter_value(self.alert_provider_var),
                alert_window=self._alert_filter_value(self.alert_window_var),
                alert_severity=self._alert_filter_value(self.alert_severity_var),
                alert_type=self._alert_filter_value(self.alert_type_var),
                alert_status=self._alert_filter_value(self.alert_status_var),
                format=report_format,
            )
            messagebox.showinfo("Provider Health Report", "Lokaler Report gespeichert.")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Provider Health Report", str(exc))

    def _selected_alert_id(self) -> str | None:
        selection = self.alert_tree.selection()
        if not selection:
            messagebox.showwarning("Provider Alerts", "Bitte zuerst einen Alert auswählen.")
            return None
        alert_id = self._alert_ids.get(str(selection[0]))
        if not alert_id:
            messagebox.showerror("Provider Alerts", "Der ausgewählte Alert besitzt keine gültige ID.")
            return None
        return alert_id

    def acknowledge_selected_alert(self) -> None:
        alert_id = self._selected_alert_id()
        if not alert_id:
            return
        try:
            self.control.acknowledge_alert(alert_id)
            self.refresh_trends()
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Provider Alerts", str(exc))

    def suppress_selected_alert(self) -> None:
        alert_id = self._selected_alert_id()
        if not alert_id:
            return
        try:
            self.control.suppress_alert(alert_id, "1h")
            self.refresh_trends()
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Provider Alerts", str(exc))

    def resolve_selected_alert(self) -> None:
        alert_id = self._selected_alert_id()
        if not alert_id:
            return
        try:
            self.control.resolve_alert(alert_id)
            self.refresh_trends()
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Provider Alerts", str(exc))

    def clear_history(self) -> None:
        if not messagebox.askyesno(
            "Provider Health History",
            "Nur lokale Provider-Health-Events löschen?",
        ):
            return
        self.control.clear_health_history()
        self.refresh_history()

    def export_diagnostics(self) -> None:
        provider, event_filter = self._history_filter()
        destination = filedialog.asksaveasfilename(
            title="Export Diagnostics",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not destination:
            return
        try:
            self.control.export_provider_diagnostics(
                destination,
                provider=provider,
                event_filter=event_filter,
                window=self.trend_window_var.get(),
            )
            messagebox.showinfo("Provider Diagnostics", "Sanitizierter Export gespeichert.")
        except OSError as exc:
            messagebox.showerror("Provider Diagnostics", str(exc))


class ArenaShell:
    def __init__(self, root: tk.Tk, control: ArenaControl):
        self.root = root
        self.control = control
        self.root.title("Arena")
        self.root.geometry("1100x680")
        self.status_var = tk.StringVar(value="STOPPED")
        self.project_var = tk.StringVar(value="Keine Projekte")
        self.session_var = tk.StringVar(value="Keine Sessions")
        self.agent_var = tk.StringVar(value="Keine Agents")
        self._build()
        self._refresh_metadata()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="ARENA", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Eigenständiger Client-Kern · Hermes als kontrollierte Runtime").pack(
            anchor="w", pady=(0, 12)
        )

        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        overview = ttk.Frame(notebook, padding=16)
        notebook.add(overview, text="Arena")
        self.dashboard = ProviderDiagnosticsDashboard(notebook, self.control)
        notebook.add(self.dashboard, text="Provider Diagnostics")
        self._build_overview(overview)

    def _build_overview(self, frame: ttk.Frame) -> None:
        status = ttk.Frame(frame)
        status.pack(fill="x", pady=(0, 18))
        ttk.Label(status, text="Runtime:").pack(side="left")
        ttk.Label(status, textvariable=self.status_var, foreground="#17803d").pack(side="left", padx=8)

        self._row(frame, "Project", self.project_var)
        self._row(frame, "Session", self.session_var)
        self._row(frame, "Agent", self.agent_var)

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=22)
        ttk.Button(buttons, text="Start", command=self._start).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Stop", command=self._stop).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Refresh", command=self._refresh_metadata).pack(side="left")

        ttk.Label(
            frame,
            text="Phase 9G: Provider Diagnostics ist standardmäßig Dry-Run und netzwerkfrei.",
            foreground="#666666",
        ).pack(anchor="w", side="bottom")

    @staticmethod
    def _row(parent: ttk.Frame, label: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=f"{label}:", width=12).pack(side="left")
        ttk.Label(row, textvariable=variable).pack(side="left")

    def _refresh_metadata(self) -> None:
        self.status_var.set(self.control.status.value)
        projects = self.control.projects.list() if self.control.store.exists() else []
        sessions = self.control.sessions.list() if self.control.store.exists() else []
        agents = self.control.agents.list() if self.control.store.exists() else []
        self.project_var.set(", ".join(item["name"] for item in projects) or "Keine Projekte")
        self.session_var.set(", ".join(item["title"] for item in sessions) or "Keine Sessions")
        self.agent_var.set(", ".join(item["name"] for item in agents) or "Keine Agents")

    def _start(self) -> None:
        try:
            self.control.start()
            self.status_var.set(self.control.status.value)
        except ArenaRuntimeError as exc:
            self.status_var.set("ERROR")
            messagebox.showerror("Arena Runtime", str(exc))

    def _stop(self) -> None:
        self.control.stop()
        self.status_var.set(self.control.status.value)

    def _close(self) -> None:
        self.control.stop()
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Schlanke Arena-Control-Shell")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = RuntimeConfig.load(args.config)
    config.validate()
    control = ArenaControl(config)
    root = tk.Tk()
    ArenaShell(root, control)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
