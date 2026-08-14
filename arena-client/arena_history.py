"""Safe bounded history for provider-health metadata only.

This module deliberately has no access to prompts, model responses, request
bodies, credentials, or transports. It stores only normalized health events.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


EVENT_TYPES = frozenset(
    {
        "health_checked",
        "provider_healthy",
        "provider_degraded",
        "provider_down",
        "authentication_failed",
        "model_unavailable",
        "rate_limited",
        "quota_exhausted",
        "circuit_opened",
        "circuit_closed",
        "circuit_half_open",
        "provider_not_configured",
        "live_request_blocked",
    }
)

EVENT_FILTERS: dict[str, frozenset[str] | None] = {
    "all": None,
    "errors": frozenset(
        {
            "provider_degraded",
            "provider_down",
            "authentication_failed",
            "model_unavailable",
            "quota_exhausted",
            "provider_not_configured",
            "live_request_blocked",
        }
    ),
    "rate_limits": frozenset({"rate_limited"}),
    "circuit_breaker": frozenset(
        {"circuit_opened", "circuit_closed", "circuit_half_open"}
    ),
    "health_checks": frozenset({"health_checked", "provider_healthy"}),
}


@dataclass(frozen=True)
class ProviderHealthEvent:
    timestamp: str
    provider: str
    event_type: str
    health_status: str | None = None
    status_code: int | None = None
    retry_after_seconds: float | None = None
    circuit_state: str | None = None
    message: str | None = None
    source: str = "arena"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderHealthHistory:
    """Bounded local event store; persistence is optional and network-free."""

    schema_version = 1

    def __init__(self, path: str | Path | None = None, *, max_events: int = 100):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.path = Path(path).expanduser().resolve() if path else None
        self.max_events = int(max_events)
        self._events: list[ProviderHealthEvent] = []
        self._load()

    def record(
        self,
        provider: str,
        event_type: str,
        *,
        health_status: str | None = None,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        circuit_state: str | None = None,
        message: str | None = None,
        source: str = "arena",
        timestamp: str | None = None,
    ) -> ProviderHealthEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unsupported provider health event: {event_type}")
        event = ProviderHealthEvent(
            timestamp=_safe_timestamp(timestamp),
            provider=_safe_identifier(provider, 80),
            event_type=event_type,
            health_status=_safe_identifier(health_status, 80) if health_status else None,
            status_code=_safe_status_code(status_code),
            retry_after_seconds=_safe_nonnegative_float(retry_after_seconds),
            circuit_state=_safe_identifier(circuit_state, 40) if circuit_state else None,
            message=_sanitize_message(message),
            source=_safe_identifier(source, 80),
        )
        self._events.append(event)
        self._events.sort(key=lambda item: item.timestamp)
        self._events = self._events[-self.max_events :]
        self._persist()
        return event

    def events(
        self,
        *,
        provider: str | None = None,
        event_filter: str = "all",
        limit: int | None = None,
    ) -> list[ProviderHealthEvent]:
        if event_filter not in EVENT_FILTERS:
            raise ValueError(f"Unsupported provider event filter: {event_filter}")
        allowed = EVENT_FILTERS[event_filter]
        result = [
            event
            for event in self._events
            if (provider is None or event.provider == provider)
            and (allowed is None or event.event_type in allowed)
        ]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must not be negative")
            result = result[-limit:] if limit else []
        return list(result)

    def export_data(
        self,
        *,
        provider: str | None = None,
        event_filter: str = "all",
    ) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "max_events": self.max_events,
            "events": [
                event.to_dict()
                for event in self.events(provider=provider, event_filter=event_filter)
            ],
        }

    def export_json(
        self,
        path: str | Path,
        *,
        provider: str | None = None,
        event_filter: str = "all",
    ) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            self.export_data(provider=provider, event_filter=event_filter),
            indent=2,
            ensure_ascii=False,
        )
        destination.write_text(data + "\n", encoding="utf-8", newline="\n")
        return destination

    def clear(self) -> None:
        self._events = []
        self._persist()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or raw.get("schema_version") != self.schema_version:
                return
            events = raw.get("events")
            if not isinstance(events, list):
                return
            loaded = [_event_from_dict(item) for item in events if isinstance(item, dict)]
            loaded.sort(key=lambda item: item.timestamp)
            self._events = loaded[-self.max_events :]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            # A corrupt local history must not prevent Arena diagnostics from
            # starting. It is discarded in memory; no provider is contacted.
            self._events = []

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.export_data(), indent=2, ensure_ascii=False) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix="provider-health-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _event_from_dict(raw: dict[str, Any]) -> ProviderHealthEvent:
    event_type = str(raw.get("event_type", "health_checked"))
    if event_type not in EVENT_TYPES:
        event_type = "health_checked"
    return ProviderHealthEvent(
        timestamp=_safe_timestamp(raw.get("timestamp")),
        provider=_safe_identifier(raw.get("provider", "unknown"), 80),
        event_type=event_type,
        health_status=_safe_identifier(raw.get("health_status"), 80)
        if raw.get("health_status")
        else None,
        status_code=_safe_status_code(raw.get("status_code")),
        retry_after_seconds=_safe_nonnegative_float(raw.get("retry_after_seconds")),
        circuit_state=_safe_identifier(raw.get("circuit_state"), 40)
        if raw.get("circuit_state")
        else None,
        message=_sanitize_message(raw.get("message")),
        source=_safe_identifier(raw.get("source", "arena"), 80),
    )


def _safe_timestamp(value: Any) -> str:
    if isinstance(value, str) and value.strip() and "\n" not in value and "\r" not in value:
        return value.strip()[:40]
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")


def _safe_identifier(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] or "unknown"


def _safe_status_code(value: Any) -> int | None:
    try:
        number = int(value)
        return number if 100 <= number <= 599 else None
    except (TypeError, ValueError):
        return None


def _safe_nonnegative_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return round(number, 3) if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _sanitize_message(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")[:300]
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)(key|token|secret|password|prompt|response|content)=[^\s&]+",
        "<redacted>",
        text,
    )
    text = re.sub(r"(?i)AIza[A-Za-z0-9_-]+", "<redacted-key>", text)
    text = re.sub(r"(?i)sk-[A-Za-z0-9_-]+", "<redacted-key>", text)
    return text


__all__ = ["EVENT_FILTERS", "EVENT_TYPES", "ProviderHealthEvent", "ProviderHealthHistory"]
