"""Arena session metadata; no Hermes chat/session migration happens here."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from arena_projects import _find
from arena_state import ArenaStateError, ArenaStateStore


class ArenaSessions:
    def __init__(self, store: ArenaStateStore):
        self.store = store

    def list(self, project_id: str | None = None) -> list[dict]:
        sessions = self.store.read()["sessions"]
        if project_id is not None:
            sessions = [item for item in sessions if item["project_id"] == project_id]
        return [dict(item) for item in sessions]

    def get(self, session_id: str) -> dict:
        return dict(_find(self.store.read()["sessions"], session_id, "Session", key="session_id"))

    def create(self, project_id: str, title: str = "Neue Arena-Session") -> dict:
        clean_title = title.strip() or "Neue Arena-Session"
        now = _now()
        session = {
            "session_id": f"session-{uuid4().hex[:12]}",
            "project_id": project_id,
            "title": clean_title,
            "created_at": now,
            "updated_at": now,
            "status": "NEW",
        }

        def add(data: dict) -> dict:
            _find(data["projects"], project_id, "Projekt")
            data["sessions"].append(session)
            return dict(session)

        return self.store.update(add)

    def update(self, session_id: str, **changes: str) -> dict:
        allowed = {"title", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ArenaStateError(f"Nicht erlaubte Sessionfelder: {', '.join(sorted(unknown))}")

        def edit(data: dict) -> dict:
            session = _find(data["sessions"], session_id, "Session", key="session_id")
            if "title" in changes:
                session["title"] = changes["title"].strip() or session["title"]
            if "status" in changes:
                session["status"] = changes["status"].strip().upper() or session["status"]
            session["updated_at"] = _now()
            return dict(session)

        return self.store.update(edit)

    def delete(self, session_id: str) -> None:
        def remove(data: dict) -> None:
            _find(data["sessions"], session_id, "Session", key="session_id")
            data["sessions"] = [item for item in data["sessions"] if item["session_id"] != session_id]

        self.store.update(remove)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
