"""Manual, local-only archive and rotation for Arena diagnostics state."""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from arena_alerts import ProviderAlertStateStore
from arena_history import ProviderHealthHistory


_ARCHIVE_NAME = re.compile(r"^provider-(health-history|alert-state)-\d{8}-\d{6}\.json$")


class ArchiveError(ValueError):
    """Raised for invalid archive requests or invalid local source data."""


def archive_local_diagnostics(
    history: ProviderHealthHistory,
    alerts: ProviderAlertStateStore,
    output_dir: str | Path,
    *,
    history_enabled: bool = False,
    alerts_enabled: bool = False,
    now: _datetime.datetime | None = None,
) -> list[Path]:
    """Archive selected local files, validate them, then reset active state.

    Archive files are created first and parsed back successfully before any
    active source is rotated. If rotation fails, original source bytes are
    restored best-effort and the error is reported to the caller.
    """
    if not history_enabled and not alerts_enabled:
        raise ArchiveError("select history, alerts, or all")

    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = _utc_now(now).strftime("%Y%m%d-%H%M%S")
    entries: list[dict[str, Any]] = []
    if history_enabled:
        entries.append(
            _entry(
                history.path,
                directory / f"provider-health-history-{timestamp}.json",
                _history_payload(history, timestamp),
                _reset_history,
                history,
            )
        )
    if alerts_enabled:
        entries.append(
            _entry(
                alerts.path,
                directory / f"provider-alert-state-{timestamp}.json",
                _alert_payload(alerts, timestamp),
                _reset_alerts,
                alerts,
            )
        )

    for entry in entries:
        _validate_source_file(entry["source"], "events" if entry["owner"] is history else "alerts")
    _validate_destinations(entries)
    originals = {
        entry["source"]: _read_optional_bytes(entry["source"])
        for entry in entries
        if entry["source"] is not None
    }
    created: list[Path] = []
    try:
        for entry in entries:
            _write_validated_json(entry["destination"], entry["payload"])
            created.append(entry["destination"])
        for entry in entries:
            if entry["source"] is not None and entry["source"].is_file():
                entry["reset"](entry["owner"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        for source, content in originals.items():
            if content is not None:
                _restore_bytes(source, content)
        for destination in created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise ArchiveError(f"archive rotation failed; active data preserved: {exc}") from exc
    return created


def _entry(
    source: Path | None,
    destination: Path,
    payload: dict[str, Any],
    reset: Callable[[Any], None],
    owner: Any,
) -> dict[str, Any]:
    return {
        "source": source,
        "destination": destination,
        "payload": payload,
        "reset": reset,
        "owner": owner,
    }


def _history_payload(history: ProviderHealthHistory, timestamp: str) -> dict[str, Any]:
    data = history.export_data()
    events = data["events"]
    return {
        "archive_timestamp": timestamp,
        "source": "provider-health-history.json",
        "event_count": len(events),
        "provider_count": len({item.get("provider") for item in events}),
        "events": events,
    }


def _alert_payload(alerts: ProviderAlertStateStore, timestamp: str) -> dict[str, Any]:
    data = alerts.export_data()
    values = data["alerts"]
    return {
        "archive_timestamp": timestamp,
        "source": "provider-alert-state.json",
        "alert_count": len(values),
        "alerts": values,
    }


def _validate_source_file(path: Path | None, collection_key: str) -> None:
    if path is None or not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"invalid local source: {path.name}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or not isinstance(raw.get(collection_key), list)
    ):
        raise ArchiveError(f"invalid local source schema: {path.name}")


def _validate_destinations(entries: list[dict[str, Any]]) -> None:
    destinations = [entry["destination"] for entry in entries]
    if len(set(destinations)) != len(destinations):
        raise ArchiveError("archive destination collision")
    for destination in destinations:
        if not _ARCHIVE_NAME.fullmatch(destination.name):
            raise ArchiveError("unsafe archive filename")
        if destination.exists():
            raise FileExistsError(f"archive already exists: {destination}")


def _write_validated_json(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix="arena-archive-", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        parsed = json.loads(Path(temporary).read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ArchiveError("archive validation failed")
        os.replace(temporary, destination)
        if json.loads(destination.read_text(encoding="utf-8")) != parsed:
            raise ArchiveError("archive post-write validation failed")
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_optional_bytes(path: Path | None) -> bytes | None:
    if path is None or not path.is_file():
        return None
    return path.read_bytes()


def _restore_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="arena-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _reset_history(history: ProviderHealthHistory) -> None:
    history.clear()


def _reset_alerts(alerts: ProviderAlertStateStore) -> None:
    alerts.clear()


def _utc_now(value: _datetime.datetime | None) -> _datetime.datetime:
    current = value or _datetime.datetime.now(_datetime.timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=_datetime.timezone.utc)
    return current.astimezone(_datetime.timezone.utc)


__all__ = ["ArchiveError", "archive_local_diagnostics"]
