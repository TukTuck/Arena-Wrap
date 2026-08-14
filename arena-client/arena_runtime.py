"""Arena's explicit Hermes runtime boundary.

This module deliberately has no global Hermes defaults. Every runtime path must
come from the Arena configuration file and is validated before a process starts.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from arena_version import VERSION

_READY_RE = re.compile(r"HERMES_BACKEND_READY\s+port=(\d+)")
_READY_MESSAGE = "Hermes backend is ready. Finalizing desktop startup"
_GLOBAL_HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
_GLOBAL_DESKTOP = Path(os.environ.get("APPDATA", "")) / "Hermes"


class ArenaRuntimeError(RuntimeError):
    """Raised when an Arena runtime is incomplete or unsafe to launch."""


@dataclass(frozen=True)
class RuntimeConfig:
    config_file: Path
    arena_version: str
    hermes_version: str
    runtime_mode: str
    hermes_root: Path
    hermes_home: Path
    desktop_user_data_dir: Path
    arena_state_dir: Path
    python_executable: Path
    desktop_executable: Path
    backend_host: str = "127.0.0.1"
    backend_port: int = 0
    provider: str | None = None
    log_level: str = "info"
    provider_pool: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, config_file: str | Path) -> "RuntimeConfig":
        path = Path(config_file).expanduser().resolve()
        if not path.is_file():
            raise ArenaRuntimeError(f"Arena-Konfiguration nicht gefunden: {path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArenaRuntimeError(f"Arena-Konfiguration unlesbar: {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ArenaRuntimeError("Arena-Konfiguration muss ein JSON-Objekt sein")

        base = path.parent

        def required_string(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ArenaRuntimeError(f"Pflichtfeld fehlt oder ist leer: {name}")
            if "$" in value or "%" in value:
                raise ArenaRuntimeError(
                    f"Nicht aufgelöster Platzhalter in {name}; Arena verlangt explizite Pfade: {value}"
                )
            return value.strip()

        def path_value(name: str) -> Path:
            value = Path(required_string(name)).expanduser()
            return (base / value).resolve() if not value.is_absolute() else value.resolve()

        runtime_mode = required_string("runtime_mode").lower()
        if runtime_mode not in {"development", "standalone"}:
            raise ArenaRuntimeError("runtime_mode muss 'development' oder 'standalone' sein")

        backend_port = raw.get("backend_port", 0)
        if not isinstance(backend_port, int) or not 0 <= backend_port <= 65535:
            raise ArenaRuntimeError("backend_port muss eine Zahl zwischen 0 und 65535 sein")

        required_string("arena_version")
        return cls(
            config_file=path,
            # The JSON field remains a compatibility requirement; the
            # application version itself comes from arena_version.py.
            arena_version=VERSION,
            hermes_version=required_string("hermes_version"),
            runtime_mode=runtime_mode,
            hermes_root=path_value("hermes_root"),
            hermes_home=path_value("hermes_home"),
            desktop_user_data_dir=path_value("desktop_user_data_dir"),
            arena_state_dir=path_value("arena_state_dir"),
            python_executable=path_value("python_executable"),
            desktop_executable=path_value("desktop_executable"),
            backend_host=str(raw.get("backend_host", "127.0.0.1")),
            backend_port=backend_port,
            provider=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
            log_level=str(raw.get("log_level", "info")),
            provider_pool=dict(raw.get("provider_pool", {})) if isinstance(raw.get("provider_pool"), dict) else {},
        )

    def as_public_dict(self) -> dict[str, Any]:
        """Return diagnostics without environment secrets or token material."""
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, Path):
                result[key] = str(value)
        result["provider_pool"] = _public_config(result.get("provider_pool", {}))
        return result

    def validate(self) -> None:
        """Fail closed if any runtime path could fall back to global Hermes."""
        if not self.hermes_root.is_dir():
            raise ArenaRuntimeError(f"Hermes-Checkout fehlt: {self.hermes_root}")
        if not (self.hermes_root / "hermes_cli" / "main.py").is_file():
            raise ArenaRuntimeError(f"Kein gültiger Hermes-Checkout: {self.hermes_root}")
        if not (self.hermes_root / "pyproject.toml").is_file():
            raise ArenaRuntimeError(f"pyproject.toml fehlt im Hermes-Checkout: {self.hermes_root}")
        if not self.python_executable.is_file():
            raise ArenaRuntimeError(f"Arena-Python fehlt: {self.python_executable}")
        if not self.desktop_executable.is_file():
            raise ArenaRuntimeError(f"Hermes-Desktop fehlt: {self.desktop_executable}")

        self._assert_inside(self.python_executable, self.hermes_root, "python_executable")
        if self.hermes_home == self.hermes_root:
            raise ArenaRuntimeError("HERMES_HOME darf nicht der Hermes-Checkout sein")
        if self.desktop_user_data_dir == self.hermes_home:
            raise ArenaRuntimeError("Electron User Data muss von HERMES_HOME getrennt sein")
        if self.arena_state_dir in {self.hermes_home, self.desktop_user_data_dir}:
            raise ArenaRuntimeError("Arena-State muss von Hermes- und Electron-State getrennt sein")

        forbidden = {
            _norm(_GLOBAL_HERMES),
            _norm(_GLOBAL_HERMES / "hermes-agent"),
            _norm(_GLOBAL_DESKTOP),
        }
        for name, value in (
            ("hermes_root", self.hermes_root),
            ("hermes_home", self.hermes_home),
            ("desktop_user_data_dir", self.desktop_user_data_dir),
            ("arena_state_dir", self.arena_state_dir),
            ("python_executable", self.python_executable),
        ):
            if _norm(value) in forbidden or _is_under(value, _GLOBAL_HERMES):
                raise ArenaRuntimeError(f"{name} zeigt auf globale Hermes-Daten: {value}")

        if self.backend_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ArenaRuntimeError("Arena-Launcher erlaubt nur ein lokales Backend")

    @staticmethod
    def _assert_inside(path: Path, parent: Path, name: str) -> None:
        if not _is_under(path, parent):
            raise ArenaRuntimeError(f"{name} muss innerhalb des Arena-Hermes-Checkouts liegen: {path}")

    def environment(self) -> dict[str, str]:
        self.validate()
        env = os.environ.copy()
        env.update(
            {
                "HERMES_HOME": str(self.hermes_home),
                "HERMES_DESKTOP_USER_DATA_DIR": str(self.desktop_user_data_dir),
                "HERMES_DESKTOP_HERMES_ROOT": str(self.hermes_root),
                "HERMES_DESKTOP_PYTHON": str(self.python_executable),
                "HERMES_DESKTOP_IGNORE_EXISTING": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        return env


class ArenaRuntimeManager:
    """Start, observe, health-check, and stop only the configured Arena Desktop."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self.backend_port: int | None = None
        self._log_offset = 0

    @property
    def log_file(self) -> Path:
        return self.config.hermes_home / "logs" / "desktop.log"

    def start(self) -> int:
        self.config.validate()
        self.config.hermes_home.mkdir(parents=True, exist_ok=True)
        self.config.desktop_user_data_dir.mkdir(parents=True, exist_ok=True)
        self._log_offset = self.log_file.stat().st_size if self.log_file.exists() else 0

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self.process = subprocess.Popen(
                [str(self.config.desktop_executable)],
                cwd=str(self.config.desktop_executable.parent),
                env=self.config.environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ArenaRuntimeError(f"Arena Desktop konnte nicht gestartet werden: {exc}") from exc
        return self.process.pid

    def wait_until_ready(self, timeout: float = 45.0) -> int:
        if self.process is None:
            raise ArenaRuntimeError("Arena Desktop wurde noch nicht gestartet")

        deadline = time.monotonic() + timeout
        seen = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise ArenaRuntimeError(
                    f"Arena Desktop beendet sich vor dem Backend-Ready (Exit {self.process.returncode}).\n{seen[-2000:]}"
                )
            if self.log_file.exists():
                text = self.log_file.read_text(encoding="utf-8", errors="replace")
                if len(text) < self._log_offset:
                    self._log_offset = 0
                delta = text[self._log_offset :]
                self._log_offset = len(text)
                seen += delta
                match = _READY_RE.search(delta)
                if match and _READY_MESSAGE in seen:
                    port = int(match.group(1))
                    # Existing HERMES_HOME logs can contain an old READY line.
                    # Accept it only when that port is currently listening.
                    if _port_is_open(self.config.backend_host, port):
                        self.backend_port = port
                        return self.backend_port
            time.sleep(0.25)
        raise ArenaRuntimeError(f"Timeout beim Arena-Desktop-Ready. Letztes Log:\n{seen[-3000:]}")

    def health_check(self) -> dict[str, int]:
        if self.backend_port is None:
            raise ArenaRuntimeError("Kein Backend-Port bekannt")
        result: dict[str, int] = {}
        for endpoint in ("/api/health", "/api/status", "/openapi.json"):
            url = f"http://{self.config.backend_host}:{self.backend_port}{endpoint}"
            request = urllib.request.Request(url, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    result[endpoint] = int(response.status)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise ArenaRuntimeError(f"Arena-Backend nicht erreichbar ({endpoint}): {exc}") from exc
        return result

    def stop(self) -> None:
        """Stop only the PID started by this manager, including its child tree."""
        if self.process is None or self.process.poll() is not None:
            return
        pid = self.process.pid
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            self.process.terminate()
        self.process = None


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _public_config(value: Any, key: str = "") -> Any:
    """Redact secret-shaped configuration keys recursively."""
    secret_words = ("key", "secret", "token", "password", "credential", "authorization")
    if isinstance(value, dict):
        return {
            str(name): "<redacted>" if any(word in str(name).lower() for word in secret_words)
            else _public_config(item, str(name))
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_public_config(item, key) for item in value]
    return value


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
