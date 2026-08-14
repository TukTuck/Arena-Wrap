"""Offline provider-health trends and informative alert policy.

This module consumes only ``ProviderHealthHistory`` events.  It has no access
 to transports, credentials, prompts, responses, or network clients by design.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from arena_history import EVENT_FILTERS, ProviderHealthEvent, ProviderHealthHistory


WINDOWS: dict[str, int] = {
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}

_TREND_EVENT_TYPES = (
    "provider_down",
    "rate_limited",
    "authentication_failed",
    "model_unavailable",
    "circuit_opened",
    "provider_healthy",
    "provider_degraded",
    "quota_exhausted",
)

_ALERT_RULES = (
    ("provider_down", "repeated_provider_down", 2, "warning"),
    ("rate_limited", "repeated_rate_limit", 3, "warning"),
    ("authentication_failed", "repeated_authentication_failure", 2, "error"),
    ("circuit_opened", "repeated_circuit_opening", 2, "error"),
)


@dataclass(frozen=True)
class ProviderHealthTrend:
    provider: str
    window: str
    event_count: int
    counts: dict[str, int]
    latest_status: str | None
    latest_timestamp: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "window": self.window,
            "event_count": self.event_count,
            "counts": dict(self.counts),
            "latest_status": self.latest_status,
            "latest_timestamp": self.latest_timestamp,
        }


@dataclass(frozen=True)
class ProviderHealthAlert:
    provider: str
    severity: str
    alert_type: str
    count: int
    window: str
    message: str
    alert_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "provider": self.provider,
            "severity": self.severity,
            "type": self.alert_type,
            "count": self.count,
            "window": self.window,
            "message": self.message,
            "created_at": self.created_at,
        }


class ProviderHealthAnalyzer:
    """Compute bounded local trends and alerts without contacting providers."""

    def __init__(
        self,
        history: ProviderHealthHistory,
        *,
        thresholds: Mapping[str, int] | None = None,
    ) -> None:
        self.history = history
        self.thresholds = {
            event_type: threshold
            for event_type, _alert_type, threshold, _severity in _ALERT_RULES
        }
        if thresholds:
            for event_type, threshold in thresholds.items():
                if event_type in self.thresholds and int(threshold) > 0:
                    self.thresholds[event_type] = int(threshold)

    def trends(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
        now: _datetime.datetime | None = None,
    ) -> list[ProviderHealthTrend]:
        seconds = _window_seconds(window)
        current = _utc_now(now)
        events = self._window_events(
            seconds=seconds, current=current, provider=provider, event_filter=event_filter
        )
        providers = sorted({event.provider for event in events})
        if provider and not providers:
            providers = [provider]
        return [
            self._trend_for_provider(item, window, events)
            for item in providers
        ]

    def alerts(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        now: _datetime.datetime | None = None,
    ) -> list[ProviderHealthAlert]:
        seconds = _window_seconds(window)
        current = _utc_now(now)
        events = self._window_events(seconds=seconds, current=current, provider=provider)
        alerts: list[ProviderHealthAlert] = []
        for provider_id in sorted({event.provider for event in events}):
            provider_events = [event for event in events if event.provider == provider_id]
            for event_type, alert_type, default_threshold, severity in _ALERT_RULES:
                count = self._occurrence_count(provider_events, event_type)
                threshold = self.thresholds.get(event_type, default_threshold)
                if count >= threshold:
                    relevant_events = [
                        event
                        for event in provider_events
                        if event.event_type == event_type
                        or (
                            event.event_type == "health_checked"
                            and event.health_status == event_type
                        )
                    ]
                    created_at = (
                        relevant_events[-1].timestamp
                        if relevant_events
                        else _utc_now(now).isoformat(timespec="seconds")
                    )
                    alerts.append(
                        ProviderHealthAlert(
                            provider=provider_id,
                            severity=severity,
                            alert_type=alert_type,
                            count=count,
                            window=window,
                            message=f"{event_type} occurred {count} times in {window}",
                            alert_id=stable_alert_id(provider_id, alert_type, window),
                            created_at=created_at,
                        )
                    )
        return alerts

    def snapshot(
        self,
        *,
        window: str = "1h",
        provider: str | None = None,
        event_filter: str = "all",
        now: _datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Return a safe machine-readable trend/alert snapshot."""
        return {
            "window": window,
            "network": "NO",
            "trends": [
                item.to_dict()
                for item in self.trends(
                    window=window,
                    provider=provider,
                    event_filter=event_filter,
                    now=now,
                )
            ],
            "alerts": [
                item.to_dict()
                for item in self.alerts(window=window, provider=provider, now=now)
            ],
        }

    def _window_events(
        self,
        *,
        seconds: int,
        current: _datetime.datetime,
        provider: str | None,
        event_filter: str = "all",
    ) -> list[ProviderHealthEvent]:
        if event_filter not in EVENT_FILTERS:
            raise ValueError(f"Unsupported provider event filter: {event_filter}")
        cutoff = current.timestamp() - seconds
        result: list[ProviderHealthEvent] = []
        for event in self.history.events(provider=provider, event_filter=event_filter):
            timestamp = _parse_timestamp(event.timestamp)
            if timestamp is not None and cutoff <= timestamp <= current.timestamp():
                result.append(event)
        return result

    def _trend_for_provider(
        self,
        provider: str,
        window: str,
        events: list[ProviderHealthEvent],
    ) -> ProviderHealthTrend:
        selected = [event for event in events if event.provider == provider]
        counts = {event_type: 0 for event_type in _TREND_EVENT_TYPES}
        counts["successful_health_checks"] = 0
        counts["health_checked"] = 0
        for event in selected:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
            if event.event_type == "health_checked":
                counts["health_checked"] += 1
                if event.health_status == "healthy":
                    counts["successful_health_checks"] += 1
        return ProviderHealthTrend(
            provider=provider,
            window=window,
            event_count=len(selected),
            counts=counts,
            latest_status=selected[-1].health_status if selected else None,
            latest_timestamp=selected[-1].timestamp if selected else None,
        )

    @staticmethod
    def _occurrence_count(events: list[ProviderHealthEvent], event_type: str) -> int:
        """Count explicit events plus repeated health checks without double count."""
        explicit = [event for event in events if event.event_type == event_type]
        count = len(explicit)
        explicit_keys = {
            (event.timestamp, event.provider, event.health_status)
            for event in explicit
        }
        status = event_type
        for event in events:
            if (
                event.event_type == "health_checked"
                and event.health_status == status
                and (event.timestamp, event.provider, event.health_status)
                not in explicit_keys
            ):
                count += 1
        return count


def stable_alert_id(provider: str, alert_type: str, window: str) -> str:
    """Create an opaque deterministic ID from non-sensitive alert dimensions."""
    value = f"{provider}|{alert_type}|{window}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _window_seconds(window: str) -> int:
    try:
        return WINDOWS[window]
    except KeyError as exc:
        raise ValueError(f"Unsupported provider trend window: {window}") from exc


def _utc_now(value: _datetime.datetime | None) -> _datetime.datetime:
    current = value or _datetime.datetime.now(_datetime.timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=_datetime.timezone.utc)
    return current.astimezone(_datetime.timezone.utc)


def _parse_timestamp(value: str) -> float | None:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = _datetime.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


__all__ = [
    "ProviderHealthAlert",
    "ProviderHealthAnalyzer",
    "ProviderHealthTrend",
    "WINDOWS",
    "stable_alert_id",
]
