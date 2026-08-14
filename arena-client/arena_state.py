"""Arena-owned metadata state; never reads or migrates Hermes databases."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


class ArenaStateError(RuntimeError):
    """Raised when Arena metadata state is invalid or cannot be persisted."""


DEFAULT_AGENTS = [
    {
        "id": "worker-alpha",
        "name": "worker-alpha",
        "description": "Arena local agent metadata placeholder",
        "profile": None,
        "enabled": True,
    },
    {
        "id": "worker-beta",
        "name": "worker-beta",
        "description": "Arena local agent metadata placeholder",
        "profile": None,
        "enabled": True,
    },
    {
        "id": "verifier",
        "name": "verifier",
        "description": "Arena local verifier metadata placeholder",
        "profile": None,
        "enabled": True,
    },
    {
        "id": "synthesizer",
        "name": "synthesizer",
        "description": "Arena local synthesizer metadata placeholder",
        "profile": None,
        "enabled": True,
    },
]


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "projects": [],
        "sessions": [],
        "agents": [dict(agent) for agent in DEFAULT_AGENTS],
    }


class ArenaStateStore:
    """Small JSON store for Arena metadata, independent from Hermes state."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = self.root / "arena-state.json"

    def exists(self) -> bool:
        return self.path.is_file()

    def initialize(self) -> dict[str, Any]:
        if not self.exists():
            self._write(empty_state())
        return self.read()

    def read(self) -> dict[str, Any]:
        if not self.exists():
            return empty_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArenaStateError(f"Arena-State unlesbar: {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ArenaStateError(f"Unbekanntes Arena-State-Schema: {self.path}")
        for collection in ("projects", "sessions", "agents"):
            if not isinstance(data.get(collection), list):
                raise ArenaStateError(f"Ungültige Arena-State-Sammlung: {collection}")
        return data

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        data = self.read()
        result = mutator(data)
        self._write(data)
        return result

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="arena-state-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise ArenaStateError(f"Arena-State konnte nicht gespeichert werden: {exc}") from exc
