"""Phase-7 tests: no Desktop, provider, model, or global Hermes state is used."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arena_agents import ArenaAgents
from arena_projects import ArenaProjects
from arena_runtime import RuntimeConfig
from arena_sessions import ArenaSessions
from arena_state import ArenaStateStore
from arena_version import VERSION


class ArenaClientTests(unittest.TestCase):
    def test_runtime_config_is_explicit_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hermes_root = root / "runtime" / "hermes-agent"
            (hermes_root / "hermes_cli").mkdir(parents=True)
            (hermes_root / "hermes_cli" / "main.py").write_text("# test\n", encoding="utf-8")
            (hermes_root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
            python = hermes_root / "venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"test")
            desktop = root / "desktop" / "Hermes.exe"
            desktop.parent.mkdir()
            desktop.write_bytes(b"test")
            config_path = root / "arena-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "arena_version": "test",
                        "hermes_version": "test",
                        "runtime_mode": "standalone",
                        "hermes_root": "runtime/hermes-agent",
                        "hermes_home": "state/hermes-home",
                        "desktop_user_data_dir": "state/desktop-data",
                        "arena_state_dir": "state/arena",
                        "python_executable": "runtime/hermes-agent/venv/Scripts/python.exe",
                        "desktop_executable": "desktop/Hermes.exe",
                    }
                ),
                encoding="utf-8",
            )

            config = RuntimeConfig.load(config_path)
            self.assertEqual(config.arena_version, VERSION)
            config.validate()
            env = config.environment()

            self.assertEqual(env["HERMES_HOME"], str(root / "state" / "hermes-home"))
            self.assertEqual(env["HERMES_DESKTOP_HERMES_ROOT"], str(hermes_root))
            self.assertEqual(env["HERMES_DESKTOP_PYTHON"], str(python))
            self.assertEqual(env["HERMES_DESKTOP_IGNORE_EXISTING"], "1")

    def test_project_session_and_agent_metadata_crud(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ArenaStateStore(Path(raw) / "arena-state")
            store.initialize()
            projects = ArenaProjects(store)
            sessions = ArenaSessions(store)
            agents = ArenaAgents(store)

            project = projects.create("Test Arena", raw)
            self.assertEqual(projects.get(project["id"])["name"], "Test Arena")
            updated = projects.update(project["id"], name="Updated Arena")
            self.assertEqual(updated["name"], "Updated Arena")

            session = sessions.create(project["id"], "Metadata only")
            self.assertEqual(sessions.get(session["session_id"])["status"], "NEW")
            sessions.update(session["session_id"], status="ready")
            self.assertEqual(sessions.get(session["session_id"])["status"], "READY")

            self.assertGreaterEqual(len(agents.list()), 4)
            agent = agents.create("local-test", "Local Test Agent")
            self.assertEqual(agents.get(agent["id"])["profile"], None)

            projects.delete(project["id"])
            self.assertEqual(projects.list(), [])
            self.assertEqual(sessions.list(), [])
            self.assertTrue(Path(raw).is_dir())


if __name__ == "__main__":
    unittest.main()
