"""Arena project registry; it only changes Arena metadata, never project folders."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from arena_state import ArenaStateError, ArenaStateStore


class ArenaProjects:
    def __init__(self, store: ArenaStateStore):
        self.store = store

    def list(self) -> list[dict]:
        return [dict(item) for item in self.store.read()["projects"]]

    def get(self, project_id: str) -> dict:
        for project in self.list():
            if project["id"] == project_id:
                return project
        raise ArenaStateError(f"Arena-Projekt nicht gefunden: {project_id}")

    def create(self, name: str, path: str | Path, project_id: str | None = None) -> dict:
        clean_name = name.strip()
        if not clean_name:
            raise ArenaStateError("Projektname darf nicht leer sein")
        project_path = str(Path(path).expanduser().resolve())
        now = _now()
        project = {
            "id": project_id or f"project-{uuid4().hex[:12]}",
            "name": clean_name,
            "path": project_path,
            "created_at": now,
            "updated_at": now,
        }

        def add(data: dict) -> dict:
            if any(item["id"] == project["id"] for item in data["projects"]):
                raise ArenaStateError(f"Projekt-ID existiert bereits: {project['id']}")
            data["projects"].append(project)
            return dict(project)

        return self.store.update(add)

    def update(self, project_id: str, **changes: str) -> dict:
        allowed = {"name", "path"}
        unknown = set(changes) - allowed
        if unknown:
            raise ArenaStateError(f"Nicht erlaubte Projektfelder: {', '.join(sorted(unknown))}")

        def edit(data: dict) -> dict:
            project = _find(data["projects"], project_id, "Projekt")
            if "name" in changes:
                if not changes["name"].strip():
                    raise ArenaStateError("Projektname darf nicht leer sein")
                project["name"] = changes["name"].strip()
            if "path" in changes:
                project["path"] = str(Path(changes["path"]).expanduser().resolve())
            project["updated_at"] = _now()
            return dict(project)

        return self.store.update(edit)

    def delete(self, project_id: str) -> None:
        def remove(data: dict) -> None:
            _find(data["projects"], project_id, "Projekt")
            data["projects"] = [item for item in data["projects"] if item["id"] != project_id]
            data["sessions"] = [item for item in data["sessions"] if item["project_id"] != project_id]

        self.store.update(remove)


def _find(items: list[dict], item_id: str, label: str, key: str = "id") -> dict:
    for item in items:
        if item.get(key) == item_id:
            return item
    raise ArenaStateError(f"Arena-{label} nicht gefunden: {item_id}")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
