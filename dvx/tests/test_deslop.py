"""Tests for the deslop pass integration."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orchestrator import (
    CHANGED_FILES_MANIFEST,
    compute_changed_files,
    is_deslop_noop,
    load_changed_files_manifest,
    write_changed_files_manifest,
)


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


class TestChangedFilesManifest:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_write_and_load(self):
        # Use a temp project dir by cd'ing.
        import os

        cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            plan_file = "PLAN-x.md"
            write_changed_files_manifest(plan_file, ["a.py", "b.py"])
            assert load_changed_files_manifest(plan_file) == ["a.py", "b.py"]
        finally:
            os.chdir(cwd)

    def test_load_missing_returns_empty(self):
        import os

        cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            assert load_changed_files_manifest("PLAN-none.md") == []
        finally:
            os.chdir(cwd)

    def test_manifest_file_location(self):
        import os

        cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            plan_file = "PLAN-x.md"
            path = write_changed_files_manifest(plan_file, ["one.py"])
            assert path.name == CHANGED_FILES_MANIFEST
            assert "PLAN-x.md" in str(path)
            assert ".dvx" in str(path)
        finally:
            os.chdir(cwd)


class TestComputeChangedFiles:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        _git(["init", "-b", "main"], self.temp_dir)
        _git(["config", "user.email", "t@example.com"], self.temp_dir)
        _git(["config", "user.name", "Test"], self.temp_dir)
        (Path(self.temp_dir) / "seed.txt").write_text("seed\n")
        _git(["add", "seed.txt"], self.temp_dir)
        _git(["commit", "-m", "seed"], self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_when_clean(self):
        import os

        cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            assert compute_changed_files("HEAD") == []
        finally:
            os.chdir(cwd)

    def test_captures_modified_files(self):
        import os

        cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            Path("seed.txt").write_text("modified\n")
            Path("new.txt").write_text("fresh\n")
            result = compute_changed_files("HEAD")
            assert "seed.txt" in result
            assert "new.txt" in result
        finally:
            os.chdir(cwd)


class TestIsDeslopNoop:
    def test_detects_marker(self):
        assert is_deslop_noop("[DESLOP_NOOP]") is True

    def test_case_insensitive(self):
        assert is_deslop_noop("[deslop_noop]\nbody") is True

    def test_missing_marker(self):
        assert is_deslop_noop("cleaned 3 files") is False

    def test_empty(self):
        assert is_deslop_noop("") is False
        assert is_deslop_noop(None) is False


class TestStateDeslopFields:
    def test_state_exposes_deslop_fields(self):
        from state import State

        state = State(plan_file="PLAN.md")
        assert state.deslop_run is False
        assert state.deslop_skipped_files == []

    def test_deslop_fields_round_trip(self):
        from state import State

        state = State(plan_file="PLAN.md")
        state.deslop_run = True
        state.deslop_skipped_files = ["a.py", "b.py"]
        d = state.to_dict()
        restored = State.from_dict(d)
        assert restored.deslop_run is True
        assert restored.deslop_skipped_files == ["a.py", "b.py"]
