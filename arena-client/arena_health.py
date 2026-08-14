"""Central provider health synchronization for Arena.

The synchronizer coordinates existing transport health checks and writes their
normalized state into the existing ProviderRegistry. It never retries and never
initiates an external check without an explicit live-request gate.
"""

from __future__ import annotations

import time
from typing import Mapping

from arena_history import ProviderHealthHistory
from arena_providers import (
    NOT_CHECKED,
    NOT_CONFIGURED,
    PROVIDER_DOWN,
    RATE_LIMITED,
    Provider,
    ProviderRegistry,
)
from arena_transport import ExternalLiveRequestGate, ProviderHealth, ProviderTransport


_ERROR_STATUS_CODES = {
    "authentication_failed": 401,
    "invalid_request": 400,
    "model_not_found": 404,
    "model_unavailable": 404,
    "rate_limited": 429,
    "quota_exhausted": 402,
    "privacy_blocked": 403,
    "provider_unavailable": 503,
    "provider_down": 503,
    "connection_failed": 503,
    "timeout": 504,
    "provider_error": 503,
    "degraded": 500,
}


class HealthSynchronizer:
    """Synchronize adapter health into one existing provider registry."""

    def __init__(
        self,
        registry: ProviderRegistry,
        transports: Mapping[str, ProviderTransport],
        history: ProviderHealthHistory | None = None,
    ) -> None:
        self.registry = registry
        self.transports = transports
        self.history = history

    def synchronize(
        self,
        provider_ids: tuple[str, ...] | list[str] | None = None,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> dict[str, ProviderHealth]:
        """Run one health check per selected provider, without retries.

        Ollama is local and uses its existing health endpoint. External adapters
        receive the gate and therefore remain network-free unless the caller
        explicitly supplies ``ExternalLiveRequestGate.explicit(...)``.
        """
        selected = tuple(provider_ids or ("ollama", "groq", "gemini"))
        gate = live_gate or ExternalLiveRequestGate.disabled()
        results: dict[str, ProviderHealth] = {}
        for provider_id in selected:
            provider = self.registry.get(provider_id)
            transport = self.transports.get(provider_id)
            previous_status = provider.health_status
            previous_circuit = str(provider.circuit_breaker.get("state", "closed"))
            if transport is None:
                health = ProviderHealth(
                    provider=provider_id,
                    healthy=False,
                    models=tuple(provider.models),
                    detail="provider_down",
                )
            elif provider_id == "ollama":
                health = transport.health_check(timeout=timeout)
            else:
                health = transport.health_check(timeout=timeout, live_gate=gate)  # type: ignore[call-arg]
            self._apply(provider, health)
            self._record_events(
                provider,
                health,
                previous_status=previous_status,
                previous_circuit=previous_circuit,
            )
            results[provider_id] = health
        return results

    def synchronize_one(
        self,
        provider_id: str,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderHealth:
        """Synchronize one provider while retaining the same gate semantics."""
        return self.synchronize(
            (provider_id,), timeout=timeout, live_gate=live_gate
        )[provider_id]

    def _apply(self, provider: Provider, health: ProviderHealth) -> None:
        provider.latency_ms = health.latency_ms
        if health.models:
            provider.models = list(health.models)

        if health.detail == "not_configured":
            provider.configured = False
            self.registry.set_health(provider.id, NOT_CONFIGURED, latency_ms=health.latency_ms)
            return

        if health.detail == "not_checked":
            # A check without an external gate is deliberately inconclusive.
            # Preserve an active rate-limit lock instead of clearing it.
            if not self._active_rate_limit(provider):
                self.registry.set_health(provider.id, NOT_CHECKED, latency_ms=health.latency_ms)
            return

        if health.healthy:
            provider.configured = True
            # A successful /models probe does not prove that a rate-limit lock
            # has expired. Keep that temporary lock until its Retry-After time.
            if self._active_rate_limit(provider):
                return
            self.registry.record_response(
                provider.id, 200, latency_ms=health.latency_ms
            )
            return

        # Prefer the normalized transport code over the raw HTTP status. In
        # particular, an adapter's 403 authentication failure must not become
        # the registry's policy-only privacy_blocked state.
        status_code = _ERROR_STATUS_CODES.get(
            health.detail or "provider_down",
            health.status_code or 503,
        )
        self.registry.record_response(
            provider.id,
            status_code,
            retry_after_seconds=health.retry_after_seconds,
            latency_ms=health.latency_ms,
            detail=health.detail or "provider_down",
        )

    def _record_events(
        self,
        provider: Provider,
        health: ProviderHealth,
        *,
        previous_status: str,
        previous_circuit: str,
    ) -> None:
        if self.history is None:
            return
        current_status = provider.health_status
        circuit_state = str(provider.circuit_breaker.get("state", "closed"))
        self.history.record(
            provider.id,
            "health_checked",
            health_status=current_status,
            status_code=health.status_code,
            retry_after_seconds=health.retry_after_seconds,
            circuit_state=circuit_state,
            message=health.detail or "health check completed",
            source="health_synchronizer",
        )
        status_events = {
            "healthy": "provider_healthy",
            "degraded": "provider_degraded",
            "provider_down": "provider_down",
            "authentication_failed": "authentication_failed",
            "model_unavailable": "model_unavailable",
            "rate_limited": "rate_limited",
            "quota_exhausted": "quota_exhausted",
            "not_configured": "provider_not_configured",
        }
        event_type = status_events.get(current_status)
        if event_type and current_status != previous_status:
            self.history.record(
                provider.id,
                event_type,
                health_status=current_status,
                status_code=health.status_code,
                retry_after_seconds=health.retry_after_seconds,
                circuit_state=circuit_state,
                message=health.detail or current_status,
                source="health_synchronizer",
            )
        if circuit_state != previous_circuit:
            circuit_event = {
                "open": "circuit_opened",
                "closed": "circuit_closed",
                "half_open": "circuit_half_open",
            }.get(circuit_state)
            if circuit_event:
                self.history.record(
                    provider.id,
                    circuit_event,
                    health_status=current_status,
                    retry_after_seconds=health.retry_after_seconds,
                    circuit_state=circuit_state,
                    message=f"circuit {circuit_state}",
                    source="health_synchronizer",
                )

    @staticmethod
    def _active_rate_limit(provider: Provider) -> bool:
        if provider.health_status != RATE_LIMITED:
            return False
        opened_until = provider.circuit_breaker.get("opened_until")
        try:
            return opened_until is not None and float(opened_until) > time.time()
        except (TypeError, ValueError):
            return False


__all__ = ["HealthSynchronizer"]
