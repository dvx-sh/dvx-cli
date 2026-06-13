"""Tests for CLI deep-interview session persistence."""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cli import cmd_interview
from interview import load_state as load_interview_state
from interview import new_state as new_interview_state
from interview import save_state as save_interview_state


class TestInterviewResumePersistence:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def teardown_method(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_incomplete_run_persists_session_id_for_resume(self, monkeypatch):
        launched = {}

        monkeypatch.setattr("cli._check_selected_model", lambda model: (True, ""))
        monkeypatch.setattr(
            "cli.start_session",
            lambda prompt, cwd=None: SimpleNamespace(
                output="**Q:** What should success look like?\n",
                session_id="session-123",
                success=True,
            ),
        )

        def fake_launch_interactive(**kwargs):
            launched.update(kwargs)

        monkeypatch.setattr("cli.launch_interactive", fake_launch_interactive)

        args = SimpleNamespace(task="build x", profile="standard", slug="build-x")
        rc = cmd_interview(args)

        assert rc == 1
        state = load_interview_state("build-x")
        assert state is not None
        assert state.session_id == "session-123"
        assert launched["session_id"] == "session-123"
        assert launched["initial_prompt"] is None

    def test_resume_reuses_saved_session_id(self, monkeypatch):
        state = new_interview_state(
            task="build x",
            profile="standard",
            brownfield=False,
            slug="build-x",
        )
        state.session_id = "session-123"
        save_interview_state(state)

        launched = {}

        monkeypatch.setattr("cli._check_selected_model", lambda model: (True, ""))

        def fake_launch_interactive(**kwargs):
            launched.update(kwargs)

        monkeypatch.setattr("cli.launch_interactive", fake_launch_interactive)
        monkeypatch.setattr(
            "cli.start_session",
            lambda prompt, cwd=None: (_ for _ in ()).throw(AssertionError("should not reseed")),
        )

        args = SimpleNamespace(task="build x", profile="standard", slug="build-x")
        rc = cmd_interview(args)

        assert rc == 1
        assert launched["session_id"] == "session-123"
        assert launched["initial_prompt"] is None
