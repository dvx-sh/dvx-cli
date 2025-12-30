"""Tests for state module."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from state import (
    Phase,
    State,
    clear_blocked,
    create_initial_state,
    ensure_dvx_dir,
    get_decisions,
    get_dvx_dir,
    increment_iteration,
    load_state,
    log_decision,
    reset_state,
    save_state,
    set_current_task,
    set_overseer_session,
    update_phase,
    write_blocked_context,
)


class TestState:
    """Tests for State dataclass."""

    def test_state_defaults(self):
        """State should have correct defaults."""
        state = State(plan_file="PLAN.md")

        assert state.plan_file == "PLAN.md"
        assert state.current_task_id is None
        assert state.current_task_title is None
        assert state.phase == "idle"
        assert state.iteration_count == 0
        assert state.max_iterations == 3
        assert state.step_mode is False

    def test_state_to_dict(self):
        """State should serialize to dict."""
        state = State(plan_file="PLAN.md", phase="implementing")
        d = state.to_dict()

        assert d["plan_file"] == "PLAN.md"
        assert d["phase"] == "implementing"

    def test_state_from_dict(self):
        """State should deserialize from dict."""
        d = {"plan_file": "PLAN.md", "phase": "reviewing", "iteration_count": 2}
        state = State.from_dict(d)

        assert state.plan_file == "PLAN.md"
        assert state.phase == "reviewing"
        assert state.iteration_count == 2


class TestPhase:
    """Tests for Phase enum."""

    def test_phase_values(self):
        """Phase enum should have expected values."""
        assert Phase.IDLE.value == "idle"
        assert Phase.IMPLEMENTING.value == "implementing"
        assert Phase.REVIEWING.value == "reviewing"
        assert Phase.FIXING.value == "fixing"
        assert Phase.TESTING.value == "testing"
        assert Phase.COMMITTING.value == "committing"
        assert Phase.BLOCKED.value == "blocked"
        assert Phase.PAUSED.value == "paused"
        assert Phase.COMPLETE.value == "complete"


class TestStateManagement:
    """Tests for state management functions using temp directories."""

    def setup_method(self):
        """Create a temp directory for each test."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_dvx_dir(self):
        """get_dvx_dir should return .dvx path."""
        dvx_dir = get_dvx_dir(self.temp_dir)
        assert dvx_dir == Path(self.temp_dir) / ".dvx"

    def test_ensure_dvx_dir_creates_directory(self):
        """ensure_dvx_dir should create .dvx directory."""
        dvx_dir = ensure_dvx_dir(self.temp_dir)

        assert dvx_dir.exists()
        assert dvx_dir.is_dir()

    def test_ensure_dvx_dir_creates_gitignore(self):
        """ensure_dvx_dir should create .gitignore."""
        dvx_dir = ensure_dvx_dir(self.temp_dir)
        gitignore = dvx_dir / ".gitignore"

        assert gitignore.exists()
        content = gitignore.read_text()
        assert "*" in content

    def test_load_state_returns_none_when_no_state(self):
        """load_state should return None when no state file."""
        state = load_state(self.temp_dir)
        assert state is None

    def test_save_and_load_state(self):
        """save_state and load_state should round-trip."""
        original = State(plan_file="PLAN-test.md", phase="implementing")
        save_state(original, self.temp_dir)

        loaded = load_state(self.temp_dir)

        assert loaded is not None
        assert loaded.plan_file == "PLAN-test.md"
        assert loaded.phase == "implementing"
        assert loaded.updated_at is not None

    def test_reset_state(self):
        """reset_state should remove state file."""
        state = State(plan_file="PLAN.md")
        save_state(state, self.temp_dir)

        reset_state(self.temp_dir)

        assert load_state(self.temp_dir) is None

    def test_create_initial_state(self):
        """create_initial_state should create and save state."""
        state = create_initial_state("PLAN-new.md", self.temp_dir)

        assert state.plan_file == "PLAN-new.md"
        assert state.phase == "idle"
        assert state.started_at is not None

        # Should be saved
        loaded = load_state(self.temp_dir)
        assert loaded is not None
        assert loaded.plan_file == "PLAN-new.md"

    def test_update_phase(self):
        """update_phase should update and save phase."""
        create_initial_state("PLAN.md", self.temp_dir)

        state = update_phase(Phase.IMPLEMENTING, self.temp_dir)

        assert state.phase == "implementing"

        loaded = load_state(self.temp_dir)
        assert loaded.phase == "implementing"

    def test_set_current_task(self):
        """set_current_task should update task info."""
        create_initial_state("PLAN.md", self.temp_dir)

        state = set_current_task("1.1", "Implement feature", self.temp_dir)

        assert state.current_task_id == "1.1"
        assert state.current_task_title == "Implement feature"
        assert state.iteration_count == 0

    def test_increment_iteration(self):
        """increment_iteration should increment and check max."""
        create_initial_state("PLAN.md", self.temp_dir)

        state, exceeded = increment_iteration(self.temp_dir)
        assert state.iteration_count == 1
        assert exceeded is False

        state, exceeded = increment_iteration(self.temp_dir)
        assert state.iteration_count == 2
        assert exceeded is False

        state, exceeded = increment_iteration(self.temp_dir)
        assert state.iteration_count == 3
        assert exceeded is False

        state, exceeded = increment_iteration(self.temp_dir)
        assert state.iteration_count == 4
        assert exceeded is True  # max_iterations is 3

    def test_set_overseer_session(self):
        """set_overseer_session should store session ID."""
        create_initial_state("PLAN.md", self.temp_dir)

        state = set_overseer_session("session-abc-123", self.temp_dir)

        assert state.overseer_session_id == "session-abc-123"

    def test_write_blocked_context(self):
        """write_blocked_context should create blocked file."""
        blocked_file = write_blocked_context(
            reason="Need API key",
            context="Cannot access external service",
            session_id="abc-123",
            plan_file="PLAN-test.md",
            project_dir=self.temp_dir
        )

        assert blocked_file.exists()
        content = blocked_file.read_text()
        assert "Need API key" in content
        assert "Cannot access external service" in content
        assert "abc-123" in content

    def test_clear_blocked(self):
        """clear_blocked should remove file and reset phase."""
        create_initial_state("PLAN.md", self.temp_dir)
        update_phase(Phase.BLOCKED, self.temp_dir)
        write_blocked_context("test", "context", project_dir=self.temp_dir)

        clear_blocked(self.temp_dir)

        # File should be gone
        dvx_dir = get_dvx_dir(self.temp_dir)
        assert not (dvx_dir / "blocked-context.md").exists()

        # Phase should be reset
        state = load_state(self.temp_dir)
        assert state.phase == "idle"

    def test_log_decision_creates_file(self):
        """log_decision should create DECISIONS file."""
        log_decision(
            topic="database",
            decision="Use PostgreSQL",
            reasoning="Better JSON support",
            alternatives=["MySQL", "SQLite"],
            project_dir=self.temp_dir
        )

        dvx_dir = get_dvx_dir(self.temp_dir)
        decision_file = dvx_dir / "DECISIONS-database.md"

        assert decision_file.exists()
        content = decision_file.read_text()
        assert "PostgreSQL" in content
        assert "JSON support" in content
        assert "MySQL" in content

    def test_log_decision_appends(self):
        """log_decision should append to existing file."""
        log_decision("test", "First", "Reason 1", ["A"], self.temp_dir)
        log_decision("test", "Second", "Reason 2", ["B"], self.temp_dir)

        dvx_dir = get_dvx_dir(self.temp_dir)
        decision_file = dvx_dir / "DECISIONS-test.md"
        content = decision_file.read_text()

        assert "First" in content
        assert "Second" in content

    def test_get_decisions(self):
        """get_decisions should return all decision files."""
        log_decision("topic1", "D1", "R1", [], self.temp_dir)
        log_decision("topic2", "D2", "R2", [], self.temp_dir)

        decisions = get_decisions(self.temp_dir)

        assert len(decisions) == 2
        names = [d.name for d in decisions]
        assert "DECISIONS-topic1.md" in names
        assert "DECISIONS-topic2.md" in names

    def test_get_decisions_empty(self):
        """get_decisions should return empty list when no decisions."""
        decisions = get_decisions(self.temp_dir)
        assert decisions == []
