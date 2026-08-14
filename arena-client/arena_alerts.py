"""Local lifecycle state for health alerts.

This module manages only acknowledgement/suppression metadata for alerts
created by ``ProviderHealthAnalyzer``. It never talks to transports and never
stores prompts, responses, credentials, or request data.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from arena_trends import ProviderHealthAlert


ALERT_STATUSES = frozenset({"ACTIVE", "ACKNOWLEDGED", "SUPPRESSED", "RESOLVED"})


SUPPRESSION_DURATIONS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
}


class AlertStateError(ValueError):
    """Raised for unknown alerts or unsupported local state operations."""


class ProviderAlertStateStore:
    """Bounded, atomic, local-only persistence for alert lifecycle state."""

    schema_version = 1

    def __init__(self, path: str | Path | None = None, *, max_alerts: int = 100):
        if max_alerts < 1:
            raise ValueError("max_alerts must be positive")
        self.path = Path(path).expanduser().resolve() if path else None
        self.max_alerts = int(max_alerts)
        self._states: dict[str, dict[str, Any]] = {}
        self._load()

    def sync(
        self,
        alerts: Iterable[ProviderHealthAlert],
        *,
        now: _datetime.datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Merge current analyzer alerts while retaining local user state."""
        current_time = _utc_now(now).isoformat(timespec="seconds")
        for alert in alerts:
            alert_id = _safe_text(alert.alert_id, 80)
            if not alert_id:
                raise AlertStateError("alert has no stable alert_id")
            previous = self._states.get(alert_id, {})
            self._states[alert_id] = {
                "alert_id": alert_id,
                "provider": _safe_text(alert.provider, 80),
                "severity": _safe_text(alert.severity, 30),
                "type": _safe_text(alert.alert_type, 80),
                "count": _safe_count(alert.count),
                "window": _safe_text(alert.window, 20),
                "message": _safe_message(alert.message),
                "created_at": _safe_text(alert.created_at, 40) or current_time,
                "acknowledged": bool(previous.get("acknowledged", False)),
                "acknowledged_at": _optional_timestamp(previous.get("acknowledged_at")),
                "suppressed_until": _optional_timestamp(previous.get("suppressed_until")),
                "resolved": bool(previous.get("resolved", False)),
                "resolved_at": _optional_timestamp(previous.get("resolved_at")),
            }
        self._trim()
        self._persist()
        return self.states()

    def states(self) -> list[dict[str, Any]]:
        return [dict(self._states[key]) for key in sorted(self._states)]

    def get(self, alert_id: str) -> dict[str, Any]:
        key = _safe_text(alert_id, 80)
        try:
            return dict(self._states[key])
        except KeyError as exc:
            raise AlertStateError("unknown alert_id") from exc

    def acknowledge(
        self,
        alert_id: str,
        *,
        now: _datetime.datetime | None = None,
    ) -> dict[str, Any]:
        state = self._mutable_state(alert_id)
        state["acknowledged"] = True
        state["acknowledged_at"] = _utc_now(now).isoformat(timespec="seconds")
        self._persist()
        return dict(state)

    def suppress(
        self,
        alert_id: str,
        duration: str,
        *,
        now: _datetime.datetime | None = None,
    ) -> dict[str, Any]:
        if duration not in SUPPRESSION_DURATIONS:
            raise AlertStateError(
                f"unsupported suppression duration: {duration}"
            )
        state = self._mutable_state(alert_id)
        until = _utc_now(now) + _datetime.timedelta(
            seconds=SUPPRESSION_DURATIONS[duration]
        )
        state["suppressed_until"] = until.isoformat(timespec="seconds")
        self._persist()
        return dict(state)

    def resolve(
        self,
        alert_id: str,
        *,
        now: _datetime.datetime | None = None,
    ) -> dict[str, Any]:
        state = self._mutable_state(alert_id)
        state["resolved"] = True
        state["resolved_at"] = _utc_now(now).isoformat(timespec="seconds")
        self._persist()
        return dict(state)

    def clear(self, alert_id: str | None = None) -> None:
        if alert_id is None:
            self._states.clear()
        else:
            self._states.pop(_safe_text(alert_id, 80), None)
        self._persist()

    def visible(
        self,
        alerts: Iterable[ProviderHealthAlert],
        *,
        include_suppressed: bool = False,
        now: _datetime.datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return current alerts enriched with local display state."""
        current = _utc_now(now)
        current_alerts = list(alerts)
        states = self.sync(current_alerts, now=current)
        result: list[dict[str, Any]] = []
        for state in states:
            if not _is_current_alert(state, current_alerts):
                continue
            suppressed = _is_future(state.get("suppressed_until"), current)
            if suppressed and not include_suppressed and not state.get("resolved"):
                continue
            enriched = dict(state)
            enriched["status"] = _display_status(state, suppressed=suppressed)
            result.append(enriched)
        return result

    def filter_states(
        self,
        states: Iterable[dict[str, Any]],
        *,
        provider: str | None = None,
        severity: str | None = None,
        alert_type: str | None = None,
        status: str | None = None,
        window: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filter already-loaded local alert state without side effects."""
        provider_value = _normalized_filter(provider)
        severity_value = _normalized_filter(severity)
        type_value = _normalized_filter(alert_type)
        status_value = _normalized_filter(status)
        window_value = _normalized_filter(window)
        if status_value and status_value not in ALERT_STATUSES:
            raise AlertStateError("unsupported alert status filter")
        result: list[dict[str, Any]] = []
        for state in states:
            if provider_value and state.get("provider") != provider_value:
                continue
            if severity_value and str(state.get("severity", "")).upper() != severity_value:
                continue
            if type_value and state.get("type") != type_value:
                continue
            if status_value and state.get("status") != status_value:
                continue
            if window_value and state.get("window") != window_value:
                continue
            result.append(dict(state))
        return result

    def export_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "max_alerts": self.max_alerts,
            "alerts": self.states(),
        }

    def _mutable_state(self, alert_id: str) -> dict[str, Any]:
        key = _safe_text(alert_id, 80)
        if key not in self._states:
            raise AlertStateError("unknown alert_id")
        return self._states[key]

    def _trim(self) -> None:
        if len(self._states) <= self.max_alerts:
            return
        keys = sorted(
            self._states,
            key=lambda key: self._states[key].get("created_at", ""),
        )
        self._states = {key: self._states[key] for key in keys[-self.max_alerts :]}

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or raw.get("schema_version") != self.schema_version:
                return
            alerts = raw.get("alerts")
            if not isinstance(alerts, list):
                return
            for item in alerts:
                if not isinstance(item, dict):
                    continue
                alert_id = _safe_text(item.get("alert_id"), 80)
                if not alert_id:
                    continue
                self._states[alert_id] = {
                    "alert_id": alert_id,
                    "provider": _safe_text(item.get("provider"), 80),
                    "severity": _safe_text(item.get("severity"), 30),
                    "type": _safe_text(item.get("type"), 80),
                    "count": _safe_count(item.get("count")),
                    "window": _safe_text(item.get("window"), 20),
                    "message": _safe_message(item.get("message")),
                    "created_at": _optional_timestamp(item.get("created_at")),
                    "acknowledged": bool(item.get("acknowledged", False)),
                    "acknowledged_at": _optional_timestamp(item.get("acknowledged_at")),
                    "suppressed_until": _optional_timestamp(item.get("suppressed_until")),
                    "resolved": bool(item.get("resolved", False)),
                    "resolved_at": _optional_timestamp(item.get("resolved_at")),
                }
            self._trim()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._states = {}

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.export_data(), indent=2, ensure_ascii=False) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix="provider-alert-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _is_current_alert(state: dict[str, Any], alerts: Iterable[ProviderHealthAlert]) -> bool:
    ids = {alert.alert_id for alert in alerts}
    return state.get("alert_id") in ids


def _display_status(state: dict[str, Any], *, suppressed: bool) -> str:
    if state.get("resolved"):
        return "RESOLVED"
    if suppressed:
        return "SUPPRESSED"
    if state.get("acknowledged"):
        return "ACKNOWLEDGED"
    return "ACTIVE"


def _is_future(value: Any, now: _datetime.datetime) -> bool:
    if not value:
        return False
    parsed = _parse_timestamp(value)
    return parsed is not None and parsed > now.timestamp()


def _utc_now(value: _datetime.datetime | None) -> _datetime.datetime:
    current = value or _datetime.datetime.now(_datetime.timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=_datetime.timezone.utc)
    return current.astimezone(_datetime.timezone.utc)


def _parse_timestamp(value: Any) -> float | None:
    try:
        parsed = _datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _normalized_filter(value: Any) -> str | None:
    if value is None:
        return None
    text = _safe_text(value, 80)
    return None if not text or text.casefold() == "all" else text


def _safe_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()[:limit]


def _safe_message(value: Any) -> str:
    text = _safe_text(value, 300)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)(key|token|secret|password|prompt|response|content)=[^\s&]+",
        "<redacted>",
        text,
    )
    text = re.sub(r"(?i)(AIza[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]+)", "<redacted-key>", text)
    return text


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_timestamp(value: Any) -> str | None:
    text = _safe_text(value, 40)
    return text or None


__all__ = [
    "ALERT_STATUSES",
    "AlertStateError",
    "ProviderAlertStateStore",
    "SUPPRESSION_DURATIONS",
]
