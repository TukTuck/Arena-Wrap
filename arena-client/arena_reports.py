"""Offline provider-health report generation.

Reports consume already computed diagnostics, trends, and alert lifecycle data.
They do not access transports or credentials and never perform network I/O.
"""

from __future__ import annotations

import datetime as _datetime
from typing import Any, Mapping


_REPORT_COUNT_FIELDS = (
    "health_checked",
    "successful_health_checks",
    "provider_down",
    "rate_limited",
    "authentication_failed",
    "model_unavailable",
    "circuit_opened",
)


def build_provider_health_report(
    diagnostics: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    window: str,
) -> dict[str, Any]:
    """Build a deterministic, sanitized report from local diagnostic payloads."""
    trend_by_provider = {
        str(item.get("provider")): item
        for item in snapshot.get("trends", [])
        if isinstance(item, Mapping)
    }
    alerts_by_provider: dict[str, dict[str, int]] = {}
    for item in snapshot.get("alerts", []):
        if not isinstance(item, Mapping):
            continue
        provider = str(item.get("provider", "unknown"))
        status = str(item.get("status", "ACTIVE"))
        bucket = alerts_by_provider.setdefault(
            provider,
            {"active": 0, "acknowledged": 0, "suppressed": 0, "resolved": 0},
        )
        key = status.casefold()
        if key in bucket:
            bucket[key] += 1

    providers: list[dict[str, Any]] = []
    for item in diagnostics.get("providers", []):
        if not isinstance(item, Mapping):
            continue
        provider = str(item.get("provider", "unknown"))
        trend = trend_by_provider.get(provider, {})
        counts = trend.get("counts", {}) if isinstance(trend, Mapping) else {}
        alert_counts = alerts_by_provider.get(
            provider,
            {"active": 0, "acknowledged": 0, "suppressed": 0, "resolved": 0},
        )
        providers.append(
            {
                "provider": provider,
                "name": str(item.get("name", provider)),
                "health": str(item.get("health", "not_checked")),
                "adapter": "available" if item.get("adapter_available") else "unavailable",
                "credential_status": str(item.get("credential_status", "missing")),
                "counts": {
                    field: _safe_count(counts.get(field, 0))
                    for field in _REPORT_COUNT_FIELDS
                },
                "alerts": dict(alert_counts),
            }
        )

    return {
        "title": "Provider Health Report",
        "window": str(window),
        "generated_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "network": "NO",
        "providers": providers,
        "alerts": [
            {
                key: item.get(key)
                for key in (
                    "alert_id", "provider", "severity", "type", "count",
                    "window", "message", "created_at", "status",
                )
                if key in item
            }
            for item in snapshot.get("alerts", [])
            if isinstance(item, Mapping)
        ],
    }


def render_provider_health_report(report: Mapping[str, Any]) -> str:
    """Render a human-readable report without Python reprs or sensitive data."""
    lines = [
        "PROVIDER HEALTH REPORT",
        "======================",
        "",
        f"Window: {report.get('window', '-')}",
        "Network: NO",
        "",
    ]
    for provider in report.get("providers", []):
        if not isinstance(provider, Mapping):
            continue
        counts = provider.get("counts", {})
        alerts = provider.get("alerts", {})
        lines.extend(
            [
                str(provider.get("name", provider.get("provider", "unknown"))),
                "-" * max(4, len(str(provider.get("name", provider.get("provider", "unknown"))))),
                f"Status: {provider.get('health', 'not_checked')}",
                f"Adapter: {provider.get('adapter', 'unavailable')}",
                f"Credential: {provider.get('credential_status', 'missing')}",
                f"Health checks: {_safe_count(counts.get('health_checked', 0))}",
                f"Successful checks: {_safe_count(counts.get('successful_health_checks', 0))}",
                f"Provider failures: {_safe_count(counts.get('provider_down', 0))}",
                f"Rate limits: {_safe_count(counts.get('rate_limited', 0))}",
                f"Authentication errors: {_safe_count(counts.get('authentication_failed', 0))}",
                f"Model unavailable: {_safe_count(counts.get('model_unavailable', 0))}",
                f"Circuit breaker events: {_safe_count(counts.get('circuit_opened', 0))}",
                f"Active alerts: {_safe_count(alerts.get('active', 0))}",
                f"Acknowledged alerts: {_safe_count(alerts.get('acknowledged', 0))}",
                f"Suppressed alerts: {_safe_count(alerts.get('suppressed', 0))}",
                f"Resolved alerts: {_safe_count(alerts.get('resolved', 0))}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


__all__ = ["build_provider_health_report", "render_provider_health_report"]
