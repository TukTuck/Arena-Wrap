"""Arena-local agent metadata; Hermes profiles remain outside this layer."""

from __future__ import annotations

from arena_projects import _find
from arena_state import ArenaStateError, ArenaStateStore


class ArenaAgents:
    def __init__(self, store: ArenaStateStore):
        self.store = store

    def list(self) -> list[dict]:
        return [dict(item) for item in self.store.read()["agents"]]

    def get(self, agent_id: str) -> dict:
        return dict(_find(self.store.read()["agents"], agent_id, "Agent"))

    def create(
        self,
        agent_id: str,
        name: str,
        description: str = "",
        profile: str | None = None,
        enabled: bool = True,
    ) -> dict:
        clean_id = agent_id.strip()
        clean_name = name.strip()
        if not clean_id or not clean_name:
            raise ArenaStateError("Agent-ID und Agent-Name dürfen nicht leer sein")
        agent = {
            "id": clean_id,
            "name": clean_name,
            "description": description.strip(),
            "profile": profile.strip() if isinstance(profile, str) and profile.strip() else None,
            "enabled": bool(enabled),
        }

        def add(data: dict) -> dict:
            if any(item["id"] == clean_id for item in data["agents"]):
                raise ArenaStateError(f"Agent-ID existiert bereits: {clean_id}")
            data["agents"].append(agent)
            return dict(agent)

        return self.store.update(add)

    def update(self, agent_id: str, **changes) -> dict:
        allowed = {"name", "description", "profile", "enabled"}
        unknown = set(changes) - allowed
        if unknown:
            raise ArenaStateError(f"Nicht erlaubte Agentfelder: {', '.join(sorted(unknown))}")

        def edit(data: dict) -> dict:
            agent = _find(data["agents"], agent_id, "Agent")
            for key, value in changes.items():
                agent[key] = value.strip() if isinstance(value, str) else bool(value) if key == "enabled" else value
            return dict(agent)

        return self.store.update(edit)

    def delete(self, agent_id: str) -> None:
        def remove(data: dict) -> None:
            _find(data["agents"], agent_id, "Agent")
            data["agents"] = [item for item in data["agents"] if item["id"] != agent_id]

        self.store.update(remove)
