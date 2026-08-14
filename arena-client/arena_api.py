"""Arena product control layer above the isolated Hermes runtime."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
import time
from typing import Any

from arena_agents import ArenaAgents
from arena_alerts import ProviderAlertStateStore, SUPPRESSION_DURATIONS
from arena_reports import build_provider_health_report, render_provider_health_report
from arena_health import HealthSynchronizer
from arena_history import ProviderHealthHistory
from arena_trends import WINDOWS, ProviderHealthAnalyzer
from arena_credentials import CredentialStore
from arena_projects import ArenaProjects
from arena_providers import ProviderRegistry
from arena_router import ProviderRouter, RouteRequest, RoutingError
from arena_runtime import ArenaRuntimeError, ArenaRuntimeManager, RuntimeConfig
from arena_sessions import ArenaSessions
from arena_state import ArenaStateStore
from arena_transport import (
    ExternalLiveRequestGate,
    GeminiTransport,
    OllamaTransport,
    OpenAICompatibleTransport,
    ProviderRequest,
    ProviderResponse,
    ProviderHealth,
    ProviderTransportError,
)


_TRANSPORT_STATUS_CODES = {
    "authentication_failed": 401,
    "quota_exhausted": 402,
    "privacy_blocked": 403,
    "model_not_found": 404,
    "rate_limited": 429,
    "provider_error": 503,
    "provider_unavailable": 503,
    "connection_failed": 503,
    "timeout": 504,
    "not_configured": 503,
    "live_request_blocked": 403,
}


class ArenaStatus(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    ERROR = "ERROR"
    STOPPING = "STOPPING"


class ArenaControl:
    """Coordinates Arena metadata, runtime, providers, and routing policy."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.runtime = ArenaRuntimeManager(config)
        self.store = ArenaStateStore(config.arena_state_dir)
        self.projects = ArenaProjects(self.store)
        self.sessions = ArenaSessions(self.store)
        self.agents = ArenaAgents(self.store)
        self.providers = ProviderRegistry.from_config(config.provider_pool)
        self.router = ProviderRouter(
            self.providers,
            (config.provider_pool or {}).get("fallback_chains"),
        )
        ollama = self.providers.get("ollama")
        timeout = (config.provider_pool or {}).get("ollama_timeout", 30.0)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 30.0
        groq = self.providers.get("groq")
        gemini = self.providers.get("gemini")
        openai_timeout = (config.provider_pool or {}).get("openai_compatible_timeout", 30.0)
        try:
            openai_timeout = float(openai_timeout)
        except (TypeError, ValueError):
            openai_timeout = 30.0
        self.transports = {
            "ollama": OllamaTransport(ollama.endpoint, timeout=timeout),
            "groq": OpenAICompatibleTransport(
                provider_id=groq.id,
                base_url=groq.endpoint,
                credential_env=groq.credential_env or "GROQ_API_KEY",
                credential_store=CredentialStore(),
                timeout=openai_timeout,
                models=tuple(groq.models),
            ),
            "gemini": GeminiTransport(
                base_url=gemini.endpoint,
                credential_env=gemini.credential_env or "GOOGLE_API_KEY",
                credential_store=CredentialStore(),
                timeout=openai_timeout,
                models=tuple(gemini.models),
            ),
        }
        self.health_history = ProviderHealthHistory(
            self.config.arena_state_dir / "provider-health-history.json"
        )
        self.health_trends = ProviderHealthAnalyzer(self.health_history)
        self.alert_states = ProviderAlertStateStore(
            self.config.arena_state_dir / "provider-alert-state.json"
        )
        self.health = HealthSynchronizer(
            self.providers, self.transports, history=self.health_history
        )
        self._status = ArenaStatus.STOPPED
        self._last_error: str | None = None

    @property
    def status(self) -> ArenaStatus:
        return self._status

    def initialize_state(self) -> dict[str, Any]:
        self.store.initialize()
        return self.state_summary()

    def state_summary(self) -> dict[str, Any]:
        data = self.store.read()
        return {
            "state_file": str(self.store.path),
            "projects": len(data["projects"]),
            "sessions": len(data["sessions"]),
            "agents": len(data["agents"]),
        }

    def provider_summary(self) -> list[dict[str, Any]]:
        return self.providers.summary()

    def provider_diagnostics(
        self,
        provider_ids: tuple[str, ...] | list[str] | None = None,
        *,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> dict[str, Any]:
        """Return sanitized provider diagnostics without implicit network access.

        With no enabled live gate this is metadata-only. A supplied explicit
        gate checks only selected providers that have an Arena transport; it
        never invokes fallback providers or model-generation endpoints.
        """
        selected = list(provider_ids) if provider_ids else [
            provider.id for provider in self.providers.list()
        ]
        for provider_id in selected:
            self.providers.get(provider_id)  # fail closed for unknown IDs

        live_enabled = bool(live_gate and live_gate.enabled)
        checked: list[str] = []
        if live_enabled:
            checked = [
                provider_id
                for provider_id in selected
                if provider_id in self.transports
                and self.providers.get(provider_id).enabled
            ]
            if checked:
                self.health.synchronize(checked, live_gate=live_gate)

        diagnostics: list[dict[str, Any]] = []
        network_requests = 0
        for provider_id in selected:
            provider = self.providers.get(provider_id)
            adapter_available = provider_id in self.transports
            credential_status = (
                "local" if provider.credential_env is None
                else "available" if provider.configured else "missing"
            )
            breaker = provider.circuit_breaker
            retry_after = _remaining_retry_after(breaker.get("opened_until"))
            network = bool(
                live_enabled
                and provider_id in checked
                and (provider_id == "ollama" or credential_status == "available")
            )
            if network:
                network_requests += 1
            diagnostics.append(
                {
                    "provider": provider.id,
                    "name": provider.name,
                    "configured": provider.configured,
                    "health": provider.health_status,
                    "credential_status": credential_status,
                    "adapter_available": adapter_available,
                    "models": list(provider.models),
                    "circuit_breaker": {
                        "state": breaker.get("state", "closed"),
                        "failure_count": int(breaker.get("failure_count") or 0),
                    },
                    "last_error": _sanitize_diagnostic_text(breaker.get("last_error")),
                    "retry_after_seconds": retry_after,
                    "endpoint": provider.endpoint,
                    "network": "YES" if network else "NO",
                }
            )

        return {
            "mode": "LIVE" if live_enabled else "DRY_RUN",
            "live_gate": "ENABLED" if live_enabled else "DISABLED",
            "network": "YES" if network_requests else "NO",
            "network_requests": network_requests,
            "checked_providers": checked,
            "providers": diagnostics,
        }

    def health_history_events(
        self,
        *,
        provider: str | None = None,
        event_filter: str = "all",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return [
            event.to_dict()
            for event in self.health_history.events(
                provider=provider, event_filter=event_filter, limit=limit
            )
        ]

    def clear_health_history(self) -> None:
        self.health_history.clear()

    def provider_health_trends(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
    ) -> list[dict[str, Any]]:
        """Return offline trend aggregates from the existing health history."""
        return [
            item.to_dict()
            for item in self.health_trends.trends(
                window=window, provider=provider, event_filter=event_filter
            )
        ]

    def _current_alerts(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        alert_window: str | None = None,
    ) -> list[Any]:
        selected_window = alert_window or window
        windows = list(WINDOWS) if selected_window.casefold() == "all" else [selected_window]
        alerts: list[Any] = []
        seen: set[str] = set()
        for current_window in windows:
            for alert in self.health_trends.alerts(
                window=current_window, provider=provider
            ):
                if alert.alert_id not in seen:
                    alerts.append(alert)
                    seen.add(alert.alert_id)
        return alerts

    def provider_health_alerts(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        alert_window: str | None = None,
        severity: str | None = None,
        alert_type: str | None = None,
        status: str | None = None,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return locally filtered alerts; this method never probes providers."""
        alerts = self._current_alerts(
            window=window, provider=provider, alert_window=alert_window
        )
        states = self.alert_states.visible(alerts, include_suppressed=True)
        filtered = self.alert_states.filter_states(
            states,
            provider=provider,
            severity=severity,
            alert_type=alert_type,
            status=status,
            window=alert_window,
        )
        if include_suppressed or str(status or "").upper() == "SUPPRESSED":
            return filtered
        return [
            state
            for state in filtered
            if state.get("status") != "SUPPRESSED"
        ]

    def alert_filter_options(self, *, provider: str | None = None) -> dict[str, list[str]]:
        """Return filter values derived from current local alert data."""
        states = self.provider_health_alerts(
            provider=provider,
            alert_window="All",
            include_suppressed=True,
        )
        return {
            "providers": sorted({str(state.get("provider")) for state in states}),
            "severities": sorted({str(state.get("severity")) for state in states}),
            "types": sorted({str(state.get("type")) for state in states}),
            "statuses": sorted({str(state.get("status")) for state in states}),
            "windows": list(WINDOWS),
        }

    def provider_health_snapshot(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
        alert_provider: str | None = None,
        alert_window: str | None = None,
        alert_severity: str | None = None,
        alert_type: str | None = None,
        alert_status: str | None = None,
        include_suppressed: bool = False,
    ) -> dict[str, Any]:
        """Return trends and locally filtered alert state without network access."""
        snapshot = self.health_trends.snapshot(
            window=window, provider=provider, event_filter=event_filter
        )
        snapshot["alerts"] = self.provider_health_alerts(
            window=window,
            provider=alert_provider if alert_provider is not None else provider,
            alert_window=alert_window,
            severity=alert_severity,
            alert_type=alert_type,
            status=alert_status,
            include_suppressed=include_suppressed,
        )
        snapshot["alert_filter_options"] = self.alert_filter_options(provider=provider)
        return snapshot

    def acknowledge_alert(self, alert_id: str) -> dict[str, Any]:
        """Acknowledge one known alert locally; never probes a provider."""
        return self.alert_states.acknowledge(alert_id)

    def suppress_alert(self, alert_id: str, duration: str) -> dict[str, Any]:
        """Suppress one known alert locally for an allowed duration."""
        return self.alert_states.suppress(alert_id, duration)

    def resolve_alert(self, alert_id: str) -> dict[str, Any]:
        """Mark one known alert resolved locally, without automatic resolution."""
        return self.alert_states.resolve(alert_id)

    def clear_alert_state(self, alert_id: str | None = None) -> None:
        """Clear only local alert lifecycle state."""
        self.alert_states.clear(alert_id)

    @staticmethod
    def _validate_alert_ids(alert_ids: list[str] | tuple[str, ...]) -> list[str]:
        values = [str(alert_id) for alert_id in alert_ids]
        if not values:
            raise ValueError("at least one alert must be selected")
        if len(set(values)) != len(values):
            raise ValueError("duplicate alert IDs are not allowed")
        return values

    def bulk_acknowledge_alerts(self, alert_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """Acknowledge explicitly selected alerts after validating all IDs."""
        values = self._validate_alert_ids(alert_ids)
        for alert_id in values:
            self.alert_states.get(alert_id)
        return [self.acknowledge_alert(alert_id) for alert_id in values]

    def bulk_suppress_alerts(
        self,
        alert_ids: list[str] | tuple[str, ...],
        duration: str,
    ) -> list[dict[str, Any]]:
        """Suppress explicitly selected alerts for one supported duration."""
        values = self._validate_alert_ids(alert_ids)
        if duration not in SUPPRESSION_DURATIONS:
            raise ValueError(f"unsupported suppression duration: {duration}")
        for alert_id in values:
            self.alert_states.get(alert_id)
        return [self.suppress_alert(alert_id, duration) for alert_id in values]

    def bulk_resolve_alerts(self, alert_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """Resolve explicitly selected alerts; confirmation belongs to the UI."""
        values = self._validate_alert_ids(alert_ids)
        for alert_id in values:
            self.alert_states.get(alert_id)
        return [self.resolve_alert(alert_id) for alert_id in values]

    def export_provider_health_report(
        self,
        path: str | Path,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
        alert_provider: str | None = None,
        alert_window: str | None = None,
        alert_severity: str | None = None,
        alert_type: str | None = None,
        alert_status: str | None = None,
        format: str | None = None,
    ) -> Path:
        """Write a local JSON or human-readable TXT report."""
        import json

        destination = Path(path).expanduser().resolve()
        report = self.provider_health_report(
            window=window,
            provider=provider,
            event_filter=event_filter,
            alert_provider=alert_provider,
            alert_window=alert_window,
            alert_severity=alert_severity,
            alert_type=alert_type,
            alert_status=alert_status,
        )
        selected_format = (format or destination.suffix.lstrip(".") or "json").casefold()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if selected_format == "txt":
            destination.write_text(
                render_provider_health_report(report), encoding="utf-8", newline="\n"
            )
        elif selected_format == "json":
            destination.write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        else:
            raise ValueError("report format must be json or txt")
        return destination

    def provider_health_report(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
        alert_provider: str | None = None,
        alert_window: str | None = None,
        alert_severity: str | None = None,
        alert_type: str | None = None,
        alert_status: str | None = None,
    ) -> dict[str, Any]:
        """Build a sanitized report entirely from local diagnostics data."""
        diagnostics = self.provider_diagnostics([provider] if provider else None)
        snapshot = self.provider_health_snapshot(
            window=window,
            provider=provider,
            event_filter=event_filter,
            alert_provider=alert_provider,
            alert_window=alert_window or window,
            alert_severity=alert_severity,
            alert_type=alert_type,
            alert_status=alert_status,
            include_suppressed=True,
        )
        return build_provider_health_report(diagnostics, snapshot, window=window)

    def export_provider_diagnostics(
        self,
        path: str | Path,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
    ) -> Path:
        """Export sanitized diagnostics, trends, history, and alert state."""
        import json

        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "network": "NO",
            "provider_diagnostics": self.provider_diagnostics(
                [provider] if provider else None
            ),
            "history": self.health_history.export_data(
                provider=provider, event_filter=event_filter
            ),
            "trends": self.provider_health_snapshot(
                window=window,
                provider=provider,
                event_filter=event_filter,
                alert_window="All",
                include_suppressed=True,
            ),
            "report": self.provider_health_report(
                window=window, provider=provider, event_filter=event_filter
            ),
            "alert_state": self.alert_states.export_data(),
        }
        destination.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return destination

    def export_health_history(
        self,
        path: str | Path,
        *,
        provider: str | None = None,
        event_filter: str = "all",
    ) -> Path:
        return self.health_history.export_json(
            path, provider=provider, event_filter=event_filter
        )

    def _record_health_event(
        self,
        provider_id: str,
        event_type: str,
        *,
        health_status: str | None = None,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        message: str | None = None,
        source: str = "arena_control",
    ) -> None:
        self.health_history.record(
            provider_id,
            event_type,
            health_status=health_status,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            circuit_state=(
                str(self.providers.get(provider_id).circuit_breaker.get("state", "closed"))
                if provider_id in self.providers.providers
                else None
            ),
            message=message,
            source=source,
        )

    def synchronize_provider_health(
        self,
        provider_ids: tuple[str, ...] | list[str] | None = None,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> dict[str, ProviderHealth]:
        """Synchronize adapter health; external checks require an explicit gate."""
        return self.health.synchronize(
            provider_ids, timeout=timeout, live_gate=live_gate
        )

    def ollama_health(self, *, timeout: float | None = None) -> ProviderHealth:
        return self.health.synchronize_one("ollama", timeout=timeout)

    def groq_health(
        self,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderHealth:
        return self.health.synchronize_one(
            "groq", timeout=timeout, live_gate=live_gate
        )

    def gemini_health(
        self,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderHealth:
        return self.health.synchronize_one(
            "gemini", timeout=timeout, live_gate=live_gate
        )

    def _refresh_external_transport_readiness(self) -> None:
        """Synchronize credential readiness without probing external services."""
        for provider_id, transport in self.transports.items():
            if not isinstance(transport, (OpenAICompatibleTransport, GeminiTransport)):
                continue
            provider = self.providers.get(provider_id)
            if provider.health_status != "not_checked":
                continue
            health = transport.health_check()
            if health.detail == "not_configured":
                provider.configured = False
                provider.health_status = "not_configured"
            elif health.detail == "not_checked":
                # A configured external provider remains deliberately unchecked
                # until an explicit live gate is supplied.
                provider.health_status = "not_checked"

    def dry_run_provider_request(
        self,
        provider_id: str,
        route: RouteRequest,
        request: ProviderRequest,
    ) -> dict[str, Any]:
        """Describe one external request without making any network call."""
        self._refresh_external_transport_readiness()
        provider = self.providers.get(provider_id)
        decision = None
        decision_error = None
        excluded = set(self.providers.providers) - {provider_id}
        try:
            decision = self.router.select(
                route,
                exclude=excluded,
                allow_unchecked=True,
            )
        except Exception as exc:
            decision_error = type(exc).__name__
        credential = "available" if provider.configured else "missing"
        allowed = bool(
            decision is not None
            and decision.provider_id == provider_id
            and provider.configured
            and route.privacy_class.upper() in provider.allowed_privacy_classes
        )
        return {
            "provider": provider.id,
            "model": request.model,
            "privacy": route.privacy_class.upper(),
            "capability": route.task_type,
            "credential": credential,
            "health": provider.health_status,
            "endpoint": provider.endpoint,
            "request_allowed": "yes" if allowed else "no",
            "network_request": "NO",
            "live_gate": "DISABLED",
            "decision": decision.public_dict() if decision else None,
            "decision_error": decision_error,
        }

    def live_request(
        self,
        provider_id: str,
        route: RouteRequest,
        request: ProviderRequest,
        *,
        live_gate: ExternalLiveRequestGate,
    ) -> ProviderResponse:
        """Execute exactly one explicitly gated external provider request."""
        try:
            live_gate.require_enabled()
        except ProviderTransportError as exc:
            self._record_health_event(
                provider_id,
                "live_request_blocked",
                message="external live request blocked",
            )
            raise
        self._refresh_external_transport_readiness()
        if provider_id not in self.transports:
            raise ArenaRuntimeError(f"Kein Transport-Adapter für Provider: {provider_id}")
        excluded = set(self.providers.providers) - {provider_id}
        decision = self.router.select(
            route,
            exclude=excluded,
            allow_unchecked=True,
        )
        transport = self.transports[decision.provider_id]
        if not isinstance(transport, (OpenAICompatibleTransport, GeminiTransport)):
            raise ArenaRuntimeError(
                f"Provider ist kein unterstützter externer Transport: {provider_id}"
            )
        try:
            response = transport.chat(request, live_gate=live_gate)
        except ProviderTransportError as exc:
            if exc.code == "live_request_blocked":
                self._record_health_event(
                    decision.provider_id,
                    "live_request_blocked",
                    message="external live request blocked",
                )
            else:
                self.router.report_response(
                    decision.provider_id,
                    _TRANSPORT_STATUS_CODES.get(exc.code, 503),
                    retry_after_seconds=exc.retry_after_seconds,
                    detail=exc.code,
                )
                self._record_transport_event(decision.provider_id, exc)
            raise
        self.router.report_response(decision.provider_id, 200, latency_ms=response.latency_ms)
        return response

    def _record_transport_event(
        self, provider_id: str, error: ProviderTransportError
    ) -> None:
        event_type = {
            "authentication_failed": "authentication_failed",
            "model_not_found": "model_unavailable",
            "rate_limited": "rate_limited",
            "quota_exhausted": "quota_exhausted",
            "live_request_blocked": "live_request_blocked",
        }.get(error.code, "provider_down")
        self._record_health_event(
            provider_id,
            event_type,
            status_code=error.status_code,
            retry_after_seconds=error.retry_after_seconds,
            message=error.code,
            source="provider_transport",
        )

    def send_provider_request(
        self,
        route: RouteRequest,
        request: ProviderRequest,
    ) -> ProviderResponse:
        """Route one request and execute it through the selected adapter.

        Phase 9A's Ollama adapter and Phase 9B's Groq adapter are the only
        executable transports. Other selected providers fail closed instead of
        being contacted through ad-hoc HTTP.
        """
        self._refresh_external_transport_readiness()
        decision = self.router.select(route)
        transport = self.transports.get(decision.provider_id)
        if transport is None:
            raise ArenaRuntimeError(
                f"Kein Transport-Adapter für Provider: {decision.provider_id}"
            )
        try:
            response = transport.chat(request)
        except ProviderTransportError as exc:
            if exc.code == "live_request_blocked":
                self._record_health_event(
                    decision.provider_id,
                    "live_request_blocked",
                    message="external live request blocked",
                )
            else:
                status_code = _TRANSPORT_STATUS_CODES.get(exc.code, 503)
                self.router.report_response(
                    decision.provider_id,
                    status_code,
                    retry_after_seconds=exc.retry_after_seconds,
                    detail=exc.code,
                )
                self._record_transport_event(decision.provider_id, exc)
            raise
        self.router.report_response(decision.provider_id, 200, latency_ms=response.latency_ms)
        return response

    def chat_with_ollama(
        self,
        messages: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        task_type: str = "general",
        privacy_class: str = "PRIVATE",
        model: str | None = None,
        timeout: float | None = None,
    ) -> ProviderResponse:
        """Explicit local chat entry point; never falls back to a cloud provider."""
        health = self.ollama_health(timeout=timeout)
        if not health.healthy:
            raise ProviderTransportError(
                "provider_unavailable",
                "local Ollama is unavailable",
                provider="ollama",
            )
        available = set(health.models)
        selected_model = model or (health.models[0] if health.models else None)
        if not selected_model or selected_model not in available:
            raise ProviderTransportError(
                "model_not_found",
                "no requested local Ollama model is available",
                provider="ollama",
            )
        request = ProviderRequest.from_messages(
            selected_model,
            messages,
            task_type=task_type,
            timeout=timeout,
        )
        route = RouteRequest(
            task_type=task_type,
            privacy_class=privacy_class,
            requested_model=selected_model,
        )
        return self.send_provider_request(route, request)

    def route_preview(
        self,
        task_type: str,
        *,
        privacy_class: str = "PUBLIC",
        requested_model: str | None = None,
        required_capabilities: set[str] | None = None,
    ) -> dict[str, Any]:
        """Return a policy decision; does not call or probe a provider."""
        request = RouteRequest(
            task_type=task_type,
            privacy_class=privacy_class,
            requested_model=requested_model,
            required_capabilities=frozenset(required_capabilities or set()),
        )
        return self.router.plan(request).public_dict()

    def start(self, timeout: float = 45.0) -> dict[str, Any]:
        if self._status in {ArenaStatus.STARTING, ArenaStatus.READY}:
            return self.runtime_status()
        self._status = ArenaStatus.STARTING
        self._last_error = None
        try:
            pid = self.runtime.start()
            port = self.runtime.wait_until_ready(timeout)
            http = self.runtime.health_check()
            self._status = ArenaStatus.READY
            return self.runtime_status(pid=pid, http=http, backend_port=port)
        except Exception as exc:
            self._status = ArenaStatus.ERROR
            self._last_error = str(exc)
            self.runtime.stop()
            if isinstance(exc, ArenaRuntimeError):
                raise
            raise ArenaRuntimeError(str(exc)) from exc

    def stop(self) -> dict[str, Any]:
        if self._status == ArenaStatus.STOPPED:
            return self.runtime_status()
        self._status = ArenaStatus.STOPPING
        self.runtime.stop()
        self._status = ArenaStatus.STOPPED
        return self.runtime_status()

    def runtime_status(
        self,
        *,
        pid: int | None = None,
        http: dict[str, int] | None = None,
        backend_port: int | None = None,
    ) -> dict[str, Any]:
        return {
            "status": self._status.value,
            "pid": pid,
            "backend_port": backend_port if backend_port is not None else self.runtime.backend_port,
            "hermes_version": self.config.hermes_version,
            "runtime_isolated": True,
            "last_error": self._last_error,
            "http": http or {},
            "state": self.state_summary() if self.store.exists() else None,
            "runtime": self.config.as_public_dict(),
            "providers": self.provider_summary(),
        }

    def smoke(self, timeout: float = 45.0) -> dict[str, Any]:
        """Validate product metadata and provider registration without inference."""
        self.initialize_state()
        project = self.projects.create("Arena Smoke Test", self.config.config_file.parent)
        session = self.sessions.create(project["id"], "Connectivity Smoke Session")
        agents = self.agents.list()
        result = {
            "state": self.state_summary(),
            "project_crud": True,
            "session_metadata": session,
            "agent_count": len(agents),
            "provider_registry": {
                "count": len(self.providers.providers),
                "configured_count": sum(1 for item in self.providers.list() if item.configured),
                "not_configured": [item.id for item in self.providers.list() if item.health_status == "not_configured"],
            },
        }
        self.sessions.delete(session["session_id"])
        self.projects.delete(project["id"])
        result["state_after_cleanup"] = self.state_summary()
        return result


def _remaining_retry_after(opened_until: Any) -> float | None:
    try:
        if opened_until is None:
            return None
        remaining = float(opened_until) - time.time()
        return round(remaining, 2) if remaining > 0 else None
    except (TypeError, ValueError):
        return None


def _sanitize_diagnostic_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")[:300]
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(r"(?i)(key|token|secret|password)=[^\s&]+", r"\1=<redacted>", text)
    return text


def load_control(config_path: str | Path) -> ArenaControl:
    config = RuntimeConfig.load(config_path)
    config.validate()
    return ArenaControl(config)
