"""Tests for goal queue processing (dvx watch / dvx clear)."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import goals as goals_module
from claude_session import DEFAULT_CLAUDE_MODEL, SessionResult
from goals import (
    DEFAULT_TODO_DIR,
    ITEM_TYPE_GOAL,
    ITEM_TYPE_RUN,
    MERGE_FILE_NAME,
    MERGE_STATUS_CLAIMED,
    MERGE_STATUS_LOCAL_MERGED,
    STATUS_BRANCHED,
    STATUS_CLAIMED,
    STATUS_COMMITTED,
    STATUS_GOAL_DELETED,
    STATUS_MERGED,
    STATUS_RAN,
    GoalState,
    _step_create_branch,
    branch_name_for_goal,
    claim_merge_request,
    claim_next_goal,
    clear_goal_state,
    current_goal_content_file,
    enqueue_new_goals,
    goals_state_file,
    item_type_for_file,
    load_goal_state,
    process_current_goal,
    process_merge_request,
    queued_goal_snapshot_file,
    queued_goal_snapshot_manifest_file,
    run_goal_watch,
    run_item_with_orchestrator,
    save_goal_state,
    scan_goal_files,
)


def run_git(args, cwd: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_runner(side_effect=None, success=True):
    """Build a fake Claude runner that records calls."""
    calls = []

    def runner(arg):
        calls.append(arg)
        if side_effect:
            side_effect(arg)
        return SessionResult(output="", session_id="test-session", success=success)

    runner.calls = calls
    return runner


def noop_commit_runner(goals_dir):
    return SessionResult(output="", session_id="test-session", success=True)


def fail_goal_runner(content):
    raise AssertionError("goal runner should not be called")


def fail_commit_runner(goals_dir):
    raise AssertionError("commit runner should not be called")


def fail_merge_runner(target):
    raise AssertionError("merge conflict runner should not be called")


class GitRepoTestCase:
    """Base: temp git repo on branch 'work' with a watched work directory."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        run_git(["init", "-b", "main"], self.temp_dir)
        run_git(["config", "user.name", "DVX Test"], self.temp_dir)
        run_git(["config", "user.email", "dvx@example.com"], self.temp_dir)
        (Path(self.temp_dir) / "README.md").write_text("# test\n")
        run_git(["add", "README.md"], self.temp_dir)
        run_git(["commit", "-m", "init"], self.temp_dir)
        run_git(["checkout", "-b", "work"], self.temp_dir)
        (Path(self.temp_dir) / "goals").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def new_state(self) -> GoalState:
        return GoalState(watch_branch="work", goals_dir="./goals")

    def add_goal(self, name: str, content: str = "Do the thing.\n") -> Path:
        goal = Path(self.temp_dir) / "goals" / name
        goal.write_text(content)
        return goal


class TestGoalPrompt:
    def test_condition_references_snapshot_file_not_inline_content(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run_claude(prompt, cwd=None, model=None, timeout=None):
            captured["prompt"] = prompt
            return SessionResult(output="", session_id="s", success=True, tool_use_count=3)

        monkeypatch.setattr(goals_module, "run_claude", fake_run_claude)

        # Far larger than the /goal condition cap - must never be inlined.
        big_goal = "Do many things.\n" * 700
        result = goals_module.run_goal_with_claude(big_goal, project_dir=str(tmp_path))

        assert result.success
        prompt = captured["prompt"]
        assert prompt.startswith("/goal ")
        condition = prompt[len("/goal "):]
        assert len(condition) <= goals_module.GOAL_CONDITION_MAX_CHARS
        assert big_goal not in prompt
        content_file = current_goal_content_file(str(tmp_path))
        assert str(content_file) in prompt
        assert content_file.read_text() == big_goal

    def test_gpt_goal_uses_codex_prompt_not_claude_slash_goal(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run_claude(prompt, cwd=None, model=None, timeout=None):
            captured["prompt"] = prompt
            captured["model"] = model
            return SessionResult(output="", session_id="s", success=True, tool_use_count=None)

        monkeypatch.setattr(goals_module, "run_claude", fake_run_claude)

        result = goals_module.run_goal_with_claude(
            "Build with Codex.\n",
            project_dir=str(tmp_path),
            model="gpt-5.5",
        )

        assert result.success
        assert captured["model"] == "gpt-5.5"
        assert not captured["prompt"].startswith("/goal ")
        assert "Make a concise plan" in captured["prompt"]
        assert "Do not ask for user input" in captured["prompt"]

    def test_snapshot_file_rewritten_when_missing(self, tmp_path):
        prompt = goals_module.build_goal_prompt("Small goal.\n", project_dir=str(tmp_path))
        content_file = current_goal_content_file(str(tmp_path))
        assert content_file.read_text() == "Small goal.\n"
        assert str(content_file) in prompt


class TestGoalStatePersistence:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_returns_none_when_missing(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        assert load_goal_state() is None

    def test_save_load_roundtrip(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        state = GoalState(
            watch_branch="work",
            goals_dir="./goals",
            queue=["GOAL-b.md"],
            current={"goal_file": "GOAL-a.md", "branch": "goal-a", "status": STATUS_BRANCHED},
        )
        save_goal_state(state)

        loaded = load_goal_state()
        assert loaded is not None
        assert loaded.watch_branch == "work"
        assert loaded.queue == ["GOAL-b.md"]
        assert loaded.current["status"] == STATUS_BRANCHED
        assert loaded.updated_at is not None

    def test_save_leaves_no_temp_file(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        save_goal_state(GoalState(watch_branch="work", goals_dir="./goals"))

        state_dir = goals_state_file().parent
        assert goals_state_file().exists()
        assert list(state_dir.glob("*.tmp")) == []
        # The state file must always be valid JSON.
        json.loads(goals_state_file().read_text())

    def test_clear_removes_state_but_not_goals_dir(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        goals_dir = Path("goals")
        goals_dir.mkdir()
        goal = goals_dir / "GOAL-keep-me.md"
        goal.write_text("# goal\n")
        save_goal_state(GoalState(watch_branch="work", goals_dir="./goals", queue=["GOAL-keep-me.md"]))

        assert clear_goal_state() is True

        assert load_goal_state() is None
        assert not goals_state_file().parent.exists()
        assert goal.exists()

    def test_clear_returns_false_when_no_state(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        assert clear_goal_state() is False


class TestBranchNameForGoal:
    def test_simple_goal_file(self):
        assert branch_name_for_goal("GOAL-add-feature-x.md") == "goal-add-feature-x"

    def test_sanitizes_odd_characters(self):
        assert branch_name_for_goal("GOAL-Add Feature!!.md") == "goal-add-feature"

    def test_raises_when_nothing_usable(self):
        with pytest.raises(ValueError):
            branch_name_for_goal("---.md")


class TestQueueScanning(GitRepoTestCase):
    @pytest.mark.parametrize(
        ("file_name", "expected"),
        [
            ("GOAL.md", ITEM_TYPE_GOAL),
            ("GOAL-do-something.md", ITEM_TYPE_GOAL),
            ("GOALfoo.md", ITEM_TYPE_GOAL),
            ("GOAL.txt", ITEM_TYPE_RUN),
            ("PLAN-x.md", ITEM_TYPE_RUN),
            ("TASK-x.md", ITEM_TYPE_RUN),
            ("follow-these-instructions.txt", ITEM_TYPE_RUN),
        ],
    )
    def test_item_type_for_file(self, file_name, expected):
        assert item_type_for_file(file_name) == expected

    def test_scan_includes_goal_and_run_files_but_not_merge_control(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-one.md")
        (Path("goals") / "notes.txt").write_text("run this\n")
        (Path("goals") / "PLAN-x.md").write_text("plan work\n")
        (Path("goals") / MERGE_FILE_NAME).write_text("")
        (Path("goals") / "nested").mkdir()
        import os

        os.utime(Path("goals") / "GOAL-one.md", (1, 1))
        os.utime(Path("goals") / "PLAN-x.md", (2, 2))
        os.utime(Path("goals") / "notes.txt", (3, 3))

        assert scan_goal_files(Path("goals")) == [
            "GOAL-one.md",
            "PLAN-x.md",
            "notes.txt",
        ]

    def test_enqueue_adds_new_goals_once(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-one.md")
        state = self.new_state()

        assert enqueue_new_goals(state) == ["GOAL-one.md"]
        assert enqueue_new_goals(state) == []
        assert state.queue == ["GOAL-one.md"]

    def test_enqueue_keeps_saved_queue_oldest_first(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        newer = self.add_goal("GOAL-newer.md")
        import os

        os.utime(newer, (20, 20))
        state = self.new_state()
        enqueue_new_goals(state)

        older = self.add_goal("GOAL-older.md")
        os.utime(older, (10, 10))

        assert enqueue_new_goals(state) == ["GOAL-older.md"]
        assert state.queue == ["GOAL-older.md", "GOAL-newer.md"]

    def test_enqueue_skips_current_goal(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-one.md")
        state = self.new_state()
        state.current = {"goal_file": "GOAL-one.md", "branch": "goal-one", "status": STATUS_RAN}

        assert enqueue_new_goals(state) == []

    def test_enqueue_skips_failed_goal_files_that_remain(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-empty.md", "   \n")
        state = self.new_state()
        state.queue = ["GOAL-empty.md"]

        assert claim_next_goal(state) is None
        assert [f["goal_file"] for f in state.failed] == ["GOAL-empty.md"]

        assert enqueue_new_goals(state) == []
        assert state.queue == []

    def test_claim_snapshots_content_and_pops_queue(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-one.md", "Build feature one.\n")
        state = self.new_state()
        enqueue_new_goals(state)

        current = claim_next_goal(state)

        assert current["goal_file"] == "GOAL-one.md"
        assert current["branch"] == "goal-one"
        assert current["status"] == STATUS_CLAIMED
        assert state.queue == []
        assert current_goal_content_file().read_text() == "Build feature one.\n"
        assert current["dirty_baseline"] == []
        assert state.blocked is None
        # Claim is persisted so a crash right after still knows the goal.
        assert load_goal_state().current["goal_file"] == "GOAL-one.md"

    def test_claim_honors_user_edit_to_queued_goal_before_claim(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        goal = self.add_goal("GOAL-one.md", "Original goal.\n")
        state = self.new_state()
        enqueue_new_goals(state)
        goal.write_text("Edited before claim.\n")

        current = claim_next_goal(state)

        assert current["goal_file"] == "GOAL-one.md"
        assert current_goal_content_file().read_text() == "Edited before claim.\n"
        assert goal.read_text() == "Edited before claim.\n"

    def test_claim_treats_user_deleted_queued_goal_as_missing(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        goal = self.add_goal("GOAL-one.md", "Original goal.\n")
        state = self.new_state()
        enqueue_new_goals(state)
        goal.unlink()

        current = claim_next_goal(state)

        assert current is None
        assert not goal.exists()
        assert state.current is None
        assert state.queue == []
        assert [f["goal_file"] for f in state.failed] == ["GOAL-one.md"]
        assert state.failed[0]["reason"] == "missing"

    def test_claim_blocks_on_preexisting_untracked_dirty_tree(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        Path("local.txt").write_text("user work before watcher\n")
        goal = self.add_goal("GOAL-noop.md")
        state = self.new_state()
        enqueue_new_goals(state)

        assert claim_next_goal(state) is None

        assert state.current is None
        assert state.queue == ["GOAL-noop.md"]
        assert state.failed == []
        assert state.blocked["goal_file"] == "GOAL-noop.md"
        assert state.blocked["dirty_paths"] == ["local.txt"]
        assert "working tree is dirty" in state.blocked["reason"]
        assert goal.exists()
        assert Path("local.txt").read_text() == "user work before watcher\n"

    def test_claim_blocks_on_preexisting_tracked_dirty_tree(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        Path("tracked.txt").write_text("clean\n")
        run_git(["add", "tracked.txt"], self.temp_dir)
        run_git(["commit", "-m", "add tracked"], self.temp_dir)
        Path("tracked.txt").write_text("user edit before watcher\n")
        self.add_goal("GOAL-noop.md")
        state = self.new_state()
        enqueue_new_goals(state)

        assert claim_next_goal(state) is None

        assert state.current is None
        assert state.queue == ["GOAL-noop.md"]
        assert state.blocked["dirty_paths"] == ["tracked.txt"]

    def test_claim_after_dirty_tree_cleanup_retries_same_goal(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        dirty = Path("local.txt")
        dirty.write_text("user work before watcher\n")
        self.add_goal("GOAL-noop.md")
        state = self.new_state()
        enqueue_new_goals(state)

        assert claim_next_goal(state) is None
        dirty.unlink()

        current = claim_next_goal(state)

        assert current["goal_file"] == "GOAL-noop.md"
        assert state.queue == []
        assert state.blocked is None

    def test_claim_rejects_branch_name_collision(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        run_git(["branch", "goal-collision"], self.temp_dir)
        self.add_goal("GOAL-collision.md", "This name collides.\n")
        state = self.new_state()
        enqueue_new_goals(state)

        assert claim_next_goal(state) is None

        assert state.current is None
        assert state.queue == []
        assert [f["goal_file"] for f in state.failed] == ["GOAL-collision.md"]
        assert "already exists" in state.failed[0]["reason"]

    def test_claim_allows_hidden_relative_goals_dir(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        hidden_goals = Path(".goals")
        hidden_goals.mkdir()
        (hidden_goals / "GOAL-hidden.md").write_text("Hidden inbox goal.\n")
        state = GoalState(watch_branch="work", goals_dir="./.goals")
        enqueue_new_goals(state)

        current = claim_next_goal(state)

        assert current["goal_file"] == "GOAL-hidden.md"
        assert state.blocked is None

    def test_claim_skips_empty_and_missing_goals(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-empty.md", "   \n")
        state = self.new_state()
        state.queue = ["GOAL-vanished.md", "GOAL-empty.md", "GOAL-real.md"]
        self.add_goal("GOAL-real.md", "Real goal.\n")

        current = claim_next_goal(state)

        assert current["goal_file"] == "GOAL-real.md"
        assert [f["goal_file"] for f in state.failed] == ["GOAL-vanished.md", "GOAL-empty.md"]

    def test_claim_returns_none_on_empty_queue(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        state = self.new_state()
        assert claim_next_goal(state) is None


class TestProcessGoal(GitRepoTestCase):
    def test_create_branch_persists_watcher_ownership_before_outer_transition(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-crash-window.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        ok, error = _step_create_branch(state)

        assert ok, error
        assert state.current["branch_created_by_watcher"] is True
        # Simulate a crash before process_current_goal records STATUS_BRANCHED.
        recovered = load_goal_state()
        assert recovered.current["status"] == STATUS_CLAIMED
        assert recovered.current["branch_created_by_watcher"] is True

    def test_recovers_if_crash_occurs_after_branch_create_before_ownership_save(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-crash-window.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        real_save_goal_state = goals_module.save_goal_state

        def crash_after_branch_exists(state_to_save, project_dir=None):
            branch = state_to_save.current["branch"]
            exists = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=self.temp_dir,
            ).returncode == 0
            if exists:
                raise RuntimeError("crash after branch create")
            real_save_goal_state(state_to_save, project_dir)

        monkeypatch.setattr(goals_module, "save_goal_state", crash_after_branch_exists)
        with pytest.raises(RuntimeError, match="crash after branch create"):
            _step_create_branch(state)

        monkeypatch.setattr(goals_module, "save_goal_state", real_save_goal_state)
        recovered = load_goal_state()
        assert recovered.current["status"] == STATUS_CLAIMED
        assert recovered.current["branch_created_by_watcher"] is False

        ok, error = process_current_goal(
            recovered, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok, error
        assert load_goal_state().current is None
        assert run_git(["branch", "--list", "goal-crash-window"], self.temp_dir) == ""

    def test_happy_path_runs_merges_and_cleans_up(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md", "Add feature x.\n")
        self.add_goal("GOAL-later.md", "A queued goal that must not be committed.\n")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        def implement(content):
            assert "Add feature x." in content
            Path("feature.txt").write_text("implemented\n")

        claude_runner = make_runner(side_effect=implement)

        def commit_in_groups(goals_dir):
            run_git(["add", "feature.txt"], self.temp_dir)
            run_git(["commit", "-m", "feat: add feature x"], self.temp_dir)
            return SessionResult(output="", session_id="s", success=True)

        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=commit_in_groups
        )

        assert ok, error
        assert claude_runner.calls and claude_runner.calls[0].startswith("Add feature x.")
        # Back on the watch branch with the work merged in.
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"
        assert (Path(self.temp_dir) / "feature.txt").exists()
        merge_subject = run_git(["log", "-1", "--pretty=%s"], self.temp_dir)
        assert "goal-add-feature-x" in merge_subject
        # Working branch deleted, goal file deleted, queued goal untouched.
        branches = run_git(["branch", "--list", "goal-add-feature-x"], self.temp_dir)
        assert branches == ""
        assert not (Path(self.temp_dir) / "goals" / "GOAL-add-feature-x.md").exists()
        assert (Path(self.temp_dir) / "goals" / "GOAL-later.md").exists()
        # State advanced: nothing current, completion recorded, persisted.
        assert state.current is None
        assert [c["goal_file"] for c in state.completed] == ["GOAL-add-feature-x.md"]
        assert load_goal_state().current is None

    def test_non_goal_file_runs_orchestrator_flow_inside_watch_wrapper(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("TODO-add-feature-x.md", "Use the run loop.\n")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        assert state.current["item_type"] == ITEM_TYPE_RUN
        calls = []

        def run_runner(plan_file):
            calls.append(plan_file)
            snapshot = Path(plan_file)
            assert snapshot.parent.name == goals_module.RUN_ITEM_CONTENT_DIR
            assert snapshot.name.startswith("todo-add-feature-x-")
            assert snapshot != current_goal_content_file()
            assert snapshot.read_text() == "Use the run loop.\n"
            Path("run-feature.txt").write_text("implemented by run\n")
            return SessionResult(output="", session_id="s", success=True)

        ok, error = process_current_goal(
            state,
            claude_runner=fail_goal_runner,
            run_runner=run_runner,
            commit_runner=noop_commit_runner,
        )

        assert ok, error
        assert len(calls) == 1
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"
        assert (Path(self.temp_dir) / "run-feature.txt").exists()
        assert not (Path(self.temp_dir) / "goals" / "TODO-add-feature-x.md").exists()
        assert run_git(["branch", "--list", "todo-add-feature-x"], self.temp_dir) == ""
        assert state.completed[0]["goal_file"] == "TODO-add-feature-x.md"
        assert state.completed[0]["item_type"] == ITEM_TYPE_RUN

    def test_legacy_non_goal_state_migrates_to_unique_run_snapshot(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("TODO-legacy.md", "Legacy run item.\n")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        original_content_file = Path(state.current.pop("content_file"))
        original_content_file.unlink()
        legacy_content_file = current_goal_content_file()
        legacy_content_file.write_text("Legacy run item.\n")
        save_goal_state(state)

        plan_files = []

        def run_runner(plan_file):
            plan_files.append(Path(plan_file))
            assert Path(plan_file).parent.name == goals_module.RUN_ITEM_CONTENT_DIR
            assert Path(plan_file).name.startswith("todo-legacy-")
            assert Path(plan_file).read_text() == "Legacy run item.\n"
            Path("legacy-output.txt").write_text("done\n")
            return SessionResult(output="", session_id="s", success=True)

        ok, error = process_current_goal(
            state,
            claude_runner=fail_goal_runner,
            run_runner=run_runner,
            commit_runner=noop_commit_runner,
        )

        assert ok, error
        assert len(plan_files) == 1
        assert not legacy_content_file.exists()
        assert (Path(self.temp_dir) / "legacy-output.txt").exists()

    def test_run_flow_commits_cannot_touch_watched_inbox(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("TODO-unsafe.md", "Run unsafe work.\n")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        def commit_watched_input(plan_file):
            run_git(["add", "goals/TODO-unsafe.md"], self.temp_dir)
            run_git(["commit", "-m", "bad: commit watched input"], self.temp_dir)
            return SessionResult(output="", session_id="s", success=True)

        ok, error = process_current_goal(
            state,
            claude_runner=fail_goal_runner,
            run_runner=commit_watched_input,
            commit_runner=fail_commit_runner,
        )

        assert ok is False
        assert "excluded paths" in error
        assert state.current["status"] == STATUS_BRANCHED
        assert (Path(self.temp_dir) / "goals" / "TODO-unsafe.md").exists()

    def test_run_item_with_orchestrator_uses_default_model(self, monkeypatch):
        captured = {}

        def fake_run_orchestrator(plan_file, model=None):
            captured["plan_file"] = plan_file
            captured["model"] = model
            return 0

        monkeypatch.setattr("orchestrator.run_orchestrator", fake_run_orchestrator)

        result = run_item_with_orchestrator("PLAN-do-work.md")

        assert result.success
        assert captured == {
            "plan_file": "PLAN-do-work.md",
            "model": DEFAULT_CLAUDE_MODEL,
        }

    def test_run_item_with_orchestrator_preserves_gpt_model(self, monkeypatch):
        captured = {}

        def fake_run_orchestrator(plan_file, model=None):
            captured["plan_file"] = plan_file
            captured["model"] = model
            return 0

        monkeypatch.setattr("orchestrator.run_orchestrator", fake_run_orchestrator)

        result = run_item_with_orchestrator("PLAN-do-work.md", model="gpt-5.5")

        assert result.success
        assert captured == {
            "plan_file": "PLAN-do-work.md",
            "model": "gpt-5.5",
        }

    def test_merge_conflict_resolution_preserves_gpt_model(self, monkeypatch):
        captured = {}

        def fake_run_claude(prompt, cwd=None, model=None, timeout=None):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["timeout"] = timeout
            return SessionResult(output="", session_id="s", success=True, tool_use_count=1)

        monkeypatch.setattr(goals_module, "run_claude", fake_run_claude)

        result = goals_module.resolve_merge_conflicts_with_claude(
            target="origin/main",
            goals_dir=".dvx/todo",
            model="gpt-5.5",
        )

        assert result.success
        assert captured["model"] == "gpt-5.5"
        assert "stopped on conflicts" in captured["prompt"]
        assert ".dvx/todo" in captured["prompt"]

    def test_merge_pushes_watch_branch_to_origin(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = tmp_path / "origin.git"
        run_git(["init", "--bare", str(origin)], self.temp_dir)
        run_git(["remote", "add", "origin", str(origin)], self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        claude_runner = make_runner(
            side_effect=lambda content: Path("feature.txt").write_text("done\n")
        )

        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=noop_commit_runner
        )

        assert ok, error
        # Remote reviewers see the merged goal work on the watch branch.
        local_tip = run_git(["rev-parse", "work"], self.temp_dir)
        remote_tip = run_git(["rev-parse", "work"], str(origin))
        assert local_tip == remote_tip

    def test_push_failure_preserves_state_and_retry_pushes(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = tmp_path / "origin.git"  # does not exist yet - push will fail
        run_git(["remote", "add", "origin", str(origin)], self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        claude_runner = make_runner(
            side_effect=lambda content: Path("feature.txt").write_text("done\n")
        )

        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "push" in error
        # The merge landed locally; the step stays uncompleted for retry.
        assert state.current["status"] == STATUS_COMMITTED

        # Once the remote exists, the retry re-merges (no-op) and pushes.
        run_git(["init", "--bare", str(origin)], self.temp_dir)
        ok, error = process_current_goal(
            state, claude_runner=fail_goal_runner, commit_runner=fail_commit_runner
        )
        assert ok, error
        assert state.current is None
        local_tip = run_git(["rev-parse", "work"], self.temp_dir)
        remote_tip = run_git(["rev-parse", "work"], str(origin))
        assert local_tip == remote_tip

    def test_fallback_commit_excludes_goals_and_dvx_state(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        self.add_goal("GOAL-later.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        claude_runner = make_runner(
            side_effect=lambda content: Path("feature.txt").write_text("done\n")
        )

        # The logical-groups session commits nothing; the fallback must.
        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=noop_commit_runner
        )

        assert ok, error
        tracked = run_git(["ls-files"], self.temp_dir).splitlines()
        assert "feature.txt" in tracked
        assert not any(p.startswith("goals/") for p in tracked)
        assert not any(p.startswith(".dvx/") for p in tracked)
        assert (Path(self.temp_dir) / "goals" / "GOAL-later.md").exists()

    def test_fallback_commit_excludes_absolute_goals_dir(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        goals_dir = str(Path(self.temp_dir) / "goals")
        self.add_goal("GOAL-add-feature-x.md")
        self.add_goal("GOAL-later.md")
        state = GoalState(watch_branch="work", goals_dir=goals_dir)
        enqueue_new_goals(state)
        claim_next_goal(state)

        claude_runner = make_runner(
            side_effect=lambda content: Path("feature.txt").write_text("done\n")
        )

        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=noop_commit_runner
        )

        assert ok, error
        tracked = run_git(["ls-files"], self.temp_dir).splitlines()
        assert "feature.txt" in tracked
        assert not any(p.startswith("goals/") for p in tracked)
        assert (Path(self.temp_dir) / "goals" / "GOAL-later.md").exists()

    def test_successful_commit_runner_cannot_commit_excluded_paths(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        self.add_goal("GOAL-later.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        claude_runner = make_runner(
            side_effect=lambda content: Path("feature.txt").write_text("done\n")
        )

        def commit_everything(goals_dir):
            run_git(["add", "-A"], self.temp_dir)
            run_git(["commit", "-m", "bad: commit everything"], self.temp_dir)
            return SessionResult(output="", session_id="s", success=True)

        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=commit_everything
        )

        assert ok is False
        assert "excluded paths" in error
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "goal-add-feature-x"
        assert state.current["status"] == STATUS_GOAL_DELETED
        run_git(["checkout", "work"], self.temp_dir)
        tracked = run_git(["ls-files"], self.temp_dir).splitlines()
        assert "feature.txt" not in tracked
        assert not any(p.startswith("goals/") for p in tracked)
        assert not any(p.startswith(".dvx/") for p in tracked)

    def test_preexisting_dirty_paths_are_blocked_before_goal_runs(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        Path("local.txt").write_text("user work before watcher\n")
        self.add_goal("GOAL-noop.md")
        state = self.new_state()
        enqueue_new_goals(state)

        assert claim_next_goal(state) is None

        assert state.current is None
        assert state.queue == ["GOAL-noop.md"]
        assert state.blocked["dirty_paths"] == ["local.txt"]
        tracked = run_git(["ls-files"], self.temp_dir).splitlines()
        assert "local.txt" not in tracked
        assert Path("local.txt").read_text() == "user work before watcher\n"

    def test_no_changes_still_completes(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-noop.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok, error
        assert state.current is None
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"

    def test_claude_failure_preserves_state_for_retry(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        ok, error = process_current_goal(
            state,
            claude_runner=make_runner(success=False),
            commit_runner=noop_commit_runner,
        )

        assert ok is False
        assert "Agent session failed" in error
        # The branch step completed; the run step did not - retry resumes there.
        assert state.current["status"] == STATUS_BRANCHED
        assert load_goal_state().current["status"] == STATUS_BRANCHED

        # Retry with a working runner completes the goal.
        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )
        assert ok, error
        assert state.current is None

    def test_goal_rejection_output_fails_run_step(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        # The /goal command rejects oversized conditions with plain text and
        # exits 0 - the watcher must not mistake that for a completed goal.
        def rejected_runner(content):
            return SessionResult(
                output="Goal condition is limited to 4000 characters (got 6953)",
                session_id="s",
                success=True,
                tool_use_count=0,
            )

        ok, error = process_current_goal(
            state, claude_runner=rejected_runner, commit_runner=fail_commit_runner
        )

        assert ok is False
        assert "/goal rejected the goal" in error
        assert state.current["status"] == STATUS_BRANCHED
        assert (Path(self.temp_dir) / "goals" / "GOAL-add-feature-x.md").exists()

    def test_session_without_tool_use_fails_run_step(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        def idle_runner(content):
            return SessionResult(
                output="All done!",
                session_id="s",
                success=True,
                tool_use_count=0,
            )

        ok, error = process_current_goal(
            state, claude_runner=idle_runner, commit_runner=fail_commit_runner
        )

        assert ok is False
        assert "without doing any work" in error
        assert state.current["status"] == STATUS_BRANCHED
        assert (Path(self.temp_dir) / "goals" / "GOAL-add-feature-x.md").exists()

    def test_truncated_session_fails_run_step(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        # Rate-limited/crashed sessions end without a result event - the goal
        # cannot be trusted as done even if the process exited 0.
        def truncated_runner(content):
            return SessionResult(
                output="partial work...",
                session_id="s",
                success=True,
                tool_use_count=12,
                result_event_seen=False,
            )

        ok, error = process_current_goal(
            state, claude_runner=truncated_runner, commit_runner=fail_commit_runner
        )

        assert ok is False
        assert "truncated" in error
        assert state.current["status"] == STATUS_BRANCHED
        assert (Path(self.temp_dir) / "goals" / "GOAL-add-feature-x.md").exists()

    def test_session_with_tool_use_completes(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-noop.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        def working_runner(content):
            return SessionResult(
                output="Verified - nothing to change.",
                session_id="s",
                success=True,
                tool_use_count=7,
            )

        ok, error = process_current_goal(
            state, claude_runner=working_runner, commit_runner=noop_commit_runner
        )

        assert ok, error
        assert state.current is None

    def test_merge_conflict_aborts_and_keeps_state(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-conflict.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        # Diverge: goal branch and watch branch both change the same file.
        ok, error = _step_create_branch(state)
        assert ok, error
        Path("clash.txt").write_text("goal version\n")
        run_git(["add", "clash.txt"], self.temp_dir)
        run_git(["commit", "-m", "goal change"], self.temp_dir)
        run_git(["checkout", "work"], self.temp_dir)
        Path("clash.txt").write_text("watch version\n")
        run_git(["add", "clash.txt"], self.temp_dir)
        run_git(["commit", "-m", "watch change"], self.temp_dir)

        state.current["status"] = STATUS_COMMITTED
        save_goal_state(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "merge" in error.lower()
        assert state.current["status"] == STATUS_COMMITTED
        # The failed merge was aborted - no merge in progress.
        assert not (Path(self.temp_dir) / ".git" / "MERGE_HEAD").exists()

    def test_resumes_from_ran_status(self, monkeypatch):
        """Crash after Claude finished: goal file still present, work uncommitted."""
        monkeypatch.chdir(self.temp_dir)
        goal = self.add_goal("GOAL-resume.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        ok, error = _step_create_branch(state)
        assert ok, error
        Path("resumed.txt").write_text("work done before crash\n")
        state.current["status"] = STATUS_RAN
        save_goal_state(state)

        claude_runner = make_runner()
        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=noop_commit_runner
        )

        assert ok, error
        # Claude is not re-run when the run step already completed.
        assert claude_runner.calls == []
        assert not goal.exists()
        assert (Path(self.temp_dir) / "resumed.txt").exists()
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"
        assert state.current is None

    def test_resumes_from_merged_status(self, monkeypatch):
        """Crash after merge: only branch deletion and bookkeeping remain."""
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-finish.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        ok, error = _step_create_branch(state)
        assert ok, error
        run_git(["checkout", "work"], self.temp_dir)
        (Path(self.temp_dir) / "goals" / "GOAL-finish.md").unlink()
        state.current["status"] = STATUS_MERGED
        save_goal_state(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok, error
        assert run_git(["branch", "--list", "goal-finish"], self.temp_dir) == ""
        assert state.current is None
        assert [c["goal_file"] for c in state.completed] == ["GOAL-finish.md"]

    def test_finish_refuses_to_delete_unmerged_goal_branch_work(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-finish.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error

        Path("merged.txt").write_text("merged work\n")
        run_git(["add", "merged.txt"], self.temp_dir)
        run_git(["commit", "-m", "goal merged work"], self.temp_dir)
        run_git(["checkout", "work"], self.temp_dir)
        run_git(["merge", "--no-ff", "-m", "merge goal", "goal-finish"], self.temp_dir)
        run_git(["checkout", "goal-finish"], self.temp_dir)
        Path("unmerged.txt").write_text("new work after merge\n")
        run_git(["add", "unmerged.txt"], self.temp_dir)
        run_git(["commit", "-m", "unmerged goal work"], self.temp_dir)
        run_git(["checkout", "work"], self.temp_dir)

        state.current["status"] = STATUS_MERGED
        save_goal_state(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "not fully merged" in error
        assert run_git(["branch", "--list", "goal-finish"], self.temp_dir) != ""
        assert state.current["status"] == STATUS_MERGED

    def test_merge_refuses_recreated_goal_branch_without_watcher_marker(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-hijack.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error

        run_git(["checkout", "work"], self.temp_dir)
        run_git(["branch", "-D", "goal-hijack"], self.temp_dir)
        run_git(["checkout", "-b", "goal-hijack", "work"], self.temp_dir)
        Path("hijack.txt").write_text("foreign branch work\n")
        run_git(["add", "hijack.txt"], self.temp_dir)
        run_git(["commit", "-m", "foreign work"], self.temp_dir)
        run_git(["checkout", "work"], self.temp_dir)

        state.current["status"] = STATUS_COMMITTED
        save_goal_state(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "watcher ownership" in error
        assert state.current["status"] == STATUS_COMMITTED
        assert not Path("hijack.txt").exists()

    def test_merge_refuses_bad_ownership_before_checkout(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-hijack.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error

        run_git(["checkout", "work"], self.temp_dir)
        run_git(["branch", "-D", "goal-hijack"], self.temp_dir)
        run_git(["checkout", "-b", "goal-hijack", "work"], self.temp_dir)
        Path("hijack.txt").write_text("foreign branch work\n")
        run_git(["add", "hijack.txt"], self.temp_dir)
        run_git(["commit", "-m", "foreign work"], self.temp_dir)

        state.current["status"] = STATUS_COMMITTED
        save_goal_state(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "watcher ownership" in error
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "goal-hijack"
        assert state.current["status"] == STATUS_COMMITTED

    def test_finish_refuses_bad_ownership_before_checkout(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-finish.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error

        run_git(["checkout", "work"], self.temp_dir)
        run_git(["branch", "-D", "goal-finish"], self.temp_dir)
        run_git(["checkout", "-b", "goal-finish", "work"], self.temp_dir)
        Path("hijack.txt").write_text("foreign branch work\n")
        run_git(["add", "hijack.txt"], self.temp_dir)
        run_git(["commit", "-m", "foreign work"], self.temp_dir)

        state.current["status"] = STATUS_MERGED
        save_goal_state(state)

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "watcher ownership" in error
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "goal-finish"
        assert state.current["status"] == STATUS_MERGED

    def test_commit_validation_survives_crash_after_unsafe_commit(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        self.add_goal("GOAL-later.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error
        Path("feature.txt").write_text("done\n")
        (Path("goals") / "GOAL-add-feature-x.md").unlink()
        state.current["status"] = STATUS_GOAL_DELETED
        save_goal_state(state)

        def commit_everything(goals_dir):
            run_git(["add", "-A"], self.temp_dir)
            run_git(["commit", "-m", "bad: commit everything"], self.temp_dir)
            return SessionResult(output="", session_id="s", success=True)

        def crash_before_validation(state_to_validate, base_oid):
            raise RuntimeError("crash before commit validation")

        monkeypatch.setattr(
            goals_module,
            "_validate_commits_did_not_include_excluded_paths",
            crash_before_validation,
        )
        with pytest.raises(RuntimeError, match="crash before commit validation"):
            process_current_goal(
                state,
                claude_runner=make_runner(),
                commit_runner=commit_everything,
            )

        monkeypatch.undo()
        monkeypatch.chdir(self.temp_dir)
        recovered = load_goal_state()
        assert recovered.current["status"] == STATUS_GOAL_DELETED
        assert recovered.current["commit_validation_base"]

        ok, error = process_current_goal(
            recovered, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "excluded paths" in error
        run_git(["checkout", "work"], self.temp_dir)
        tracked = run_git(["ls-files"], self.temp_dir).splitlines()
        assert "feature.txt" not in tracked
        assert not any(p.startswith("goals/") for p in tracked)
        assert not any(p.startswith(".dvx/") for p in tracked)

    def test_commit_validation_rejects_excluded_paths_hidden_by_later_commit(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-add-feature-x.md")
        self.add_goal("GOAL-later.md")
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)

        claude_runner = make_runner(
            side_effect=lambda content: Path("feature.txt").write_text("done\n")
        )

        def commit_then_remove_excluded(goals_dir):
            run_git(["add", "-A"], self.temp_dir)
            run_git(["commit", "-m", "bad: include excluded"], self.temp_dir)
            run_git(["rm", "--cached", "goals/GOAL-later.md", ".dvx/.gitignore"], self.temp_dir)
            run_git(["commit", "-m", "remove excluded from final tree"], self.temp_dir)
            return SessionResult(output="", session_id="s", success=True)

        ok, error = process_current_goal(
            state, claude_runner=claude_runner, commit_runner=commit_then_remove_excluded
        )

        assert ok is False
        assert "excluded paths" in error
        assert state.current["status"] == STATUS_GOAL_DELETED

    def test_later_queued_goal_edit_after_current_claim_is_preserved(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        second = self.add_goal("GOAL-second.md", "Second original.\n")
        self.add_goal("GOAL-first.md", "First goal.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(second, (2, 2))
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        second.write_text("Second edited before runner.\n")

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok, error
        assert second.read_text() == "Second edited before runner.\n"

    def test_later_queued_goal_delete_after_current_claim_is_preserved(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        second = self.add_goal("GOAL-second.md", "Second original.\n")
        self.add_goal("GOAL-first.md", "First goal.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(second, (2, 2))
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        second.unlink()

        ok, error = process_current_goal(
            state, claude_runner=make_runner(), commit_runner=noop_commit_runner
        )

        assert ok, error
        assert not second.exists()


class TestRunGoalWatch(GitRepoTestCase):
    def test_default_watch_dir_is_dvx_todo(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)

        rc = run_goal_watch("work", once=True)

        assert rc == 0
        assert Path(DEFAULT_TODO_DIR).is_dir()
        assert load_goal_state().goals_dir == DEFAULT_TODO_DIR

    def test_once_with_empty_dir_returns_immediately(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        shutil.rmtree(Path("goals"))

        rc = run_goal_watch("work", goals_dir="./goals", once=True)

        assert rc == 0
        assert Path("goals").exists()

    def test_continuous_watch_prints_waiting_notice_once_while_polling(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.chdir(self.temp_dir)
        sleeps = []

        def stop_after_repeated_polling(interval):
            sleeps.append(interval)
            if len(sleeps) == 3:
                raise RuntimeError("stop watch loop")

        monkeypatch.setattr(goals_module.time, "sleep", stop_after_repeated_polling)

        with pytest.raises(RuntimeError, match="stop watch loop"):
            run_goal_watch("work", goals_dir="./goals", poll_interval=0.01, once=False)

        out = capsys.readouterr().out
        assert out.count("Watching for work files in: ./goals") == 1

    def test_continuous_watch_reprints_waiting_notice_after_work_finishes(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.chdir(self.temp_dir)
        sleeps = []

        def add_work_then_stop(interval):
            sleeps.append(interval)
            if len(sleeps) == 1:
                self.add_goal("GOAL-one.md", "One goal.\n")
            elif len(sleeps) == 2:
                raise RuntimeError("stop watch loop")

        monkeypatch.setattr(goals_module.time, "sleep", add_work_then_stop)

        with pytest.raises(RuntimeError, match="stop watch loop"):
            run_goal_watch(
                "work",
                goals_dir="./goals",
                poll_interval=0.01,
                once=False,
                claude_runner=make_runner(),
                commit_runner=noop_commit_runner,
            )

        out = capsys.readouterr().out
        assert out.count("Watching for work files in: ./goals") == 2
        assert "Item complete and merged into work." in out

    def test_processes_all_pending_goals_in_order(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal.\n")
        # Make arrival order deterministic.
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))

        order = []

        def implement(content):
            order.append(content.strip())
            Path(f"out-{len(order)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 0
        assert order == ["First goal.", "Second goal."]
        state = load_goal_state()
        assert state.current is None
        assert state.queue == []
        assert [c["goal_file"] for c in state.completed] == ["GOAL-first.md", "GOAL-second.md"]
        assert scan_goal_files(Path("goals")) == []
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"

    def test_processes_oldest_file_before_saved_newer_queue_item(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        newer = self.add_goal("GOAL-newer.md", "Newer goal.\n")
        import os

        os.utime(newer, (20, 20))
        state = self.new_state()
        state.queue = ["GOAL-newer.md"]
        save_goal_state(state)

        older = self.add_goal("GOAL-older.md", "Older goal.\n")
        os.utime(older, (10, 10))

        order = []

        def implement(content):
            order.append(content.strip())
            Path(f"out-{len(order)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 0
        assert order == ["Older goal.", "Newer goal."]
        state = load_goal_state()
        assert [c["goal_file"] for c in state.completed] == ["GOAL-older.md", "GOAL-newer.md"]

    def test_watch_dispatches_goal_files_to_goal_and_other_files_to_run(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("TODO-second.md", "Second run item.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "TODO-second.md", (2, 2))

        goal_seen = []
        run_seen = []

        def implement_goal(content):
            goal_seen.append(content.strip())
            Path("goal-output.txt").write_text("goal flow\n")

        def run_item(plan_file):
            run_seen.append(Path(plan_file).read_text().strip())
            Path("run-output.txt").write_text("run flow\n")
            return SessionResult(output="", session_id="s", success=True)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement_goal),
            run_runner=run_item,
            commit_runner=noop_commit_runner,
        )

        assert rc == 0
        assert goal_seen == ["First goal."]
        assert run_seen == ["Second run item."]
        assert (Path(self.temp_dir) / "goal-output.txt").exists()
        assert (Path(self.temp_dir) / "run-output.txt").exists()
        state = load_goal_state()
        assert [c["item_type"] for c in state.completed] == [ITEM_TYPE_GOAL, ITEM_TYPE_RUN]
        assert scan_goal_files(Path("goals")) == []

    def test_non_goal_watch_items_get_distinct_run_state_paths(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("TODO-first.md", "First run item.\n")
        self.add_goal("PLAN-second.md", "Second run item.\n")
        import os

        os.utime(Path("goals") / "TODO-first.md", (1, 1))
        os.utime(Path("goals") / "PLAN-second.md", (2, 2))

        plan_files = []

        def run_item(plan_file):
            plan_files.append(Path(plan_file))
            Path(f"run-output-{len(plan_files)}.txt").write_text(Path(plan_file).read_text())
            return SessionResult(output="", session_id="s", success=True)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            run_runner=run_item,
            commit_runner=noop_commit_runner,
        )

        assert rc == 0
        assert len(plan_files) == 2
        assert plan_files[0].parent.name == goals_module.RUN_ITEM_CONTENT_DIR
        assert plan_files[1].parent.name == goals_module.RUN_ITEM_CONTENT_DIR
        assert plan_files[0].name != plan_files[1].name
        state = load_goal_state()
        assert [c["item_type"] for c in state.completed] == [ITEM_TYPE_RUN, ITEM_TYPE_RUN]

    def test_queued_goal_content_change_during_runner_blocks_without_overwrite(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        seen = []

        def implement(content):
            seen.append(content.strip())
            if len(seen) == 1:
                (Path("goals") / "GOAL-second.md").write_text("Corrupted by first goal.\n")
            Path(f"out-{len(seen)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 1
        assert seen == ["First goal."]
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted by first goal.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"
        state = load_goal_state()
        assert state.current["goal_file"] == "GOAL-first.md"
        assert state.current["status"] == STATUS_BRANCHED
        assert state.queue == ["GOAL-second.md"]

    def test_queued_goal_delete_during_runner_blocks_without_recreate(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        seen = []

        def implement(content):
            seen.append(content.strip())
            if len(seen) == 1:
                (Path("goals") / "GOAL-second.md").unlink()
            Path(f"out-{len(seen)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 1
        assert seen == ["First goal."]
        assert not (Path("goals") / "GOAL-second.md").exists()
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"
        state = load_goal_state()
        assert state.current["goal_file"] == "GOAL-first.md"
        assert state.current["status"] == STATUS_BRANCHED
        assert state.queue == ["GOAL-second.md"]

    def test_active_snapshot_blocks_retry_after_crash_before_restore(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error
        state.current["status"] = STATUS_BRANCHED
        save_goal_state(state)

        ok, error = goals_module._begin_queued_goal_guard(state)
        assert ok, error
        (Path("goals") / "GOAL-second.md").write_text("Corrupted before crash.\n")

        runner = make_runner()
        recovered = load_goal_state()
        ok, error = process_current_goal(
            recovered, claude_runner=runner, commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "Queued items changed" in error
        assert runner.calls == []
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted before crash.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"

    def test_runner_cannot_hide_queued_goal_change_by_deleting_manifest(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        seen = []

        def implement(content):
            seen.append(content.strip())
            if len(seen) == 1:
                (Path("goals") / "GOAL-second.md").write_text("Corrupted after manifest delete.\n")
                queued_goal_snapshot_manifest_file().unlink()
            Path(f"out-{len(seen)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 1
        assert seen == ["First goal."]
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted after manifest delete.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"
        state = load_goal_state()
        assert state.current["queued_goal_guard"]
        assert state.current["status"] == STATUS_BRANCHED

    def test_runner_cannot_hide_queued_goal_change_by_tampering_snapshot(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        seen = []

        def implement(content):
            seen.append(content.strip())
            if len(seen) == 1:
                corrupted = "Corrupted after snapshot tamper.\n"
                (Path("goals") / "GOAL-second.md").write_text(corrupted)
                queued_goal_snapshot_file("GOAL-second.md").write_text(corrupted)
            Path(f"out-{len(seen)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 1
        assert seen == ["First goal."]
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted after snapshot tamper.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == (
            "Corrupted after snapshot tamper.\n"
        )
        state = load_goal_state()
        assert state.current["queued_goal_guard"]
        assert state.current["status"] == STATUS_BRANCHED

    def test_runner_cannot_persist_tampered_guard_for_retry(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        corrupted = "Corrupted everywhere.\n"
        corrupt_entry = {
            "goal_file": "GOAL-second.md",
            "existed": True,
            "sha256": goals_module._content_sha256(corrupted),
        }
        seen = []

        def implement(content):
            seen.append(content.strip())
            if len(seen) == 1:
                (Path("goals") / "GOAL-second.md").write_text(corrupted)
                queued_goal_snapshot_file("GOAL-second.md").write_text(corrupted)
                queued_goal_snapshot_manifest_file().write_text(
                    json.dumps({"goals": [corrupt_entry]}, indent=2)
                )
                disk_state = json.loads(goals_state_file().read_text())
                disk_state["current"]["queued_goal_guard"]["goals"] = [corrupt_entry]
                goals_state_file().write_text(json.dumps(disk_state, indent=2))
            Path(f"out-{len(seen)}.txt").write_text(content)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
        )

        assert rc == 1
        assert seen == ["First goal."]
        recovered = load_goal_state()
        persisted_guard = recovered.current["queued_goal_guard"]["goals"]
        assert persisted_guard != [corrupt_entry]
        assert persisted_guard[0]["sha256"] == goals_module._content_sha256("Second goal original.\n")

        runner = make_runner()
        ok, error = process_current_goal(
            recovered, claude_runner=runner, commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "manifest changed" in error
        assert runner.calls == []
        assert (Path("goals") / "GOAL-second.md").read_text() == corrupted

    def test_missing_manifest_active_guard_blocks_retry(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        state = self.new_state()
        enqueue_new_goals(state)
        claim_next_goal(state)
        ok, error = _step_create_branch(state)
        assert ok, error
        state.current["status"] = STATUS_BRANCHED
        save_goal_state(state)

        ok, error = goals_module._begin_queued_goal_guard(state)
        assert ok, error
        queued_goal_snapshot_manifest_file().unlink()
        (Path("goals") / "GOAL-second.md").write_text("Corrupted after manifest delete.\n")

        runner = make_runner()
        recovered = load_goal_state()
        ok, error = process_current_goal(
            recovered, claude_runner=runner, commit_runner=noop_commit_runner
        )

        assert ok is False
        assert "manifest is missing" in error
        assert runner.calls == []
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted after manifest delete.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"

    def test_queued_goal_content_change_during_commit_runner_blocks(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))
        seen = []

        def implement(content):
            seen.append(content.strip())
            Path(f"out-{len(seen)}.txt").write_text(content)

        commit_calls = []

        def commit_and_corrupt(goals_dir):
            commit_calls.append(goals_dir)
            run_git(["add", f"out-{len(commit_calls)}.txt"], self.temp_dir)
            run_git(["commit", "-m", f"goal output {len(commit_calls)}"], self.temp_dir)
            if len(commit_calls) == 1:
                (Path("goals") / "GOAL-second.md").write_text("Corrupted by commit runner.\n")
            return SessionResult(output="", session_id="s", success=True)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=commit_and_corrupt,
        )

        assert rc == 1
        assert seen == ["First goal."]
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted by commit runner.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"
        state = load_goal_state()
        assert state.current["goal_file"] == "GOAL-first.md"
        assert state.current["status"] == STATUS_GOAL_DELETED
        assert state.queue == ["GOAL-second.md"]

    def test_commit_retry_keeps_blocking_after_queued_goal_conflict(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-first.md", "First goal.\n")
        self.add_goal("GOAL-second.md", "Second goal original.\n")
        import os

        os.utime(Path("goals") / "GOAL-first.md", (1, 1))
        os.utime(Path("goals") / "GOAL-second.md", (2, 2))

        def implement(content):
            Path("out-1.txt").write_text(content)

        def commit_and_corrupt(goals_dir):
            run_git(["add", "out-1.txt"], self.temp_dir)
            run_git(["commit", "-m", "goal output"], self.temp_dir)
            (Path("goals") / "GOAL-second.md").write_text("Corrupted by commit runner.\n")
            return SessionResult(output="", session_id="s", success=True)

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=commit_and_corrupt,
        )
        assert rc == 1

        recovered = load_goal_state()
        ok, error = process_current_goal(
            recovered,
            claude_runner=make_runner(),
            commit_runner=noop_commit_runner,
        )

        assert ok is False
        assert "Queued items changed" in error
        assert recovered.current["status"] == STATUS_GOAL_DELETED
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "goal-first"
        assert (Path("goals") / "GOAL-second.md").read_text() == "Corrupted by commit runner.\n"
        assert queued_goal_snapshot_file("GOAL-second.md").read_text() == "Second goal original.\n"

    def test_once_blocks_dirty_tree_without_running_goal(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        Path("local.txt").write_text("user work before watcher\n")
        self.add_goal("GOAL-noop.md")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            commit_runner=fail_commit_runner,
        )

        assert rc == 1
        state = load_goal_state()
        assert state.current is None
        assert state.queue == ["GOAL-noop.md"]
        assert state.blocked["dirty_paths"] == ["local.txt"]
        assert (Path(self.temp_dir) / "goals" / "GOAL-noop.md").exists()
        assert Path("local.txt").read_text() == "user work before watcher\n"

    def test_once_blocks_tracked_rename_into_goals_without_running_item(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        Path("tracked.txt").write_text("tracked user work\n")
        run_git(["add", "tracked.txt"], self.temp_dir)
        run_git(["commit", "-m", "add tracked"], self.temp_dir)
        run_git(["mv", "tracked.txt", "goals/tracked.txt"], self.temp_dir)
        self.add_goal("GOAL-noop.md")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            commit_runner=fail_commit_runner,
        )

        assert rc == 1
        state = load_goal_state()
        assert state.current is None
        assert state.queue == ["tracked.txt", "GOAL-noop.md"]
        assert state.blocked["dirty_paths"] == ["tracked.txt"]
        assert (Path(self.temp_dir) / "goals" / "GOAL-noop.md").exists()
        assert (Path(self.temp_dir) / "goals" / "tracked.txt").read_text() == "tracked user work\n"

    def test_once_blocks_dirty_path_that_only_strips_to_goals_prefix(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        Path(" goals").mkdir()
        Path(" goals/user.txt").write_text("user work before watcher\n")
        self.add_goal("GOAL-noop.md")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            commit_runner=fail_commit_runner,
        )

        assert rc == 1
        state = load_goal_state()
        assert state.current is None
        assert state.queue == ["GOAL-noop.md"]
        assert state.blocked["dirty_paths"] == [" goals/"]
        assert Path(" goals/user.txt").read_text() == "user work before watcher\n"

    def test_once_retries_blocked_goal_after_dirty_tree_cleanup(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        dirty = Path("local.txt")
        dirty.write_text("user work before watcher\n")
        self.add_goal("GOAL-noop.md", "Noop goal.\n")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            commit_runner=fail_commit_runner,
        )
        assert rc == 1

        dirty.unlink()
        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(),
            commit_runner=noop_commit_runner,
        )

        assert rc == 0
        state = load_goal_state()
        assert state.current is None
        assert state.queue == []
        assert state.blocked is None
        assert [c["goal_file"] for c in state.completed] == ["GOAL-noop.md"]

    def test_continuous_watch_does_not_relog_unchanged_blocked_goal(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.chdir(self.temp_dir)
        dirty = Path("local.txt")
        dirty.write_text("user work before watcher\n")
        self.add_goal("GOAL-noop.md", "Noop goal.\n")
        sleeps = []

        def sleep_once_then_clean(interval):
            sleeps.append(interval)
            if len(sleeps) == 2:
                dirty.unlink()
            if len(sleeps) == 3:
                raise RuntimeError("stop watch loop")

        monkeypatch.setattr(goals_module.time, "sleep", sleep_once_then_clean)

        with pytest.raises(RuntimeError, match="stop watch loop"):
            run_goal_watch(
                "work",
                goals_dir="./goals",
                poll_interval=0.01,
                once=False,
                claude_runner=make_runner(),
                commit_runner=noop_commit_runner,
            )

        out = capsys.readouterr().out
        assert out.count("Blocked item GOAL-noop.md") == 1
        state = load_goal_state()
        assert state.blocked is None
        assert [c["goal_file"] for c in state.completed] == ["GOAL-noop.md"]

    def test_blocked_dirty_path_comparison_is_order_insensitive(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        state = self.new_state()
        state.blocked = {
            "goal_file": "GOAL-noop.md",
            "reason": "working tree is dirty outside the watched directory and .dvx",
            "dirty_paths": ["b.txt", "a.txt"],
        }
        monkeypatch.setattr(goals_module, "_dirty_paths", lambda goals_dir: ["a.txt", "b.txt"])

        changed, error = goals_module._blocked_dirty_paths_changed(state)

        assert error == ""
        assert changed is False

    def test_recovers_after_failure(self, monkeypatch):
        """A failed run leaves state behind; the next run finishes the goal."""
        monkeypatch.chdir(self.temp_dir)
        self.add_goal("GOAL-flaky.md", "Flaky goal.\n")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(success=False),
            commit_runner=noop_commit_runner,
        )
        assert rc == 1
        assert load_goal_state().current["status"] == STATUS_BRANCHED

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(),
            commit_runner=noop_commit_runner,
        )
        assert rc == 0
        state = load_goal_state()
        assert state.current is None
        assert [c["goal_file"] for c in state.completed] == ["GOAL-flaky.md"]

    def test_uses_saved_watch_branch_when_resuming_pending_work(self, monkeypatch):
        """The original watch branch wins when there is pending work to resume."""
        monkeypatch.chdir(self.temp_dir)
        save_goal_state(
            GoalState(watch_branch="work", goals_dir="./goals", queue=["GOAL-one.md"])
        )
        self.add_goal("GOAL-one.md")

        rc = run_goal_watch(
            "some-other-branch",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(),
            commit_runner=noop_commit_runner,
        )

        assert rc == 0
        assert load_goal_state().watch_branch == "work"
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"

    def test_idle_state_is_discarded_on_start(self, monkeypatch, capsys):
        """Idle saved state has nothing to resume; a restart starts fresh."""
        monkeypatch.chdir(self.temp_dir)
        save_goal_state(GoalState(watch_branch="no-longer-exists", goals_dir="./goals"))

        rc = run_goal_watch("work", goals_dir="./goals", once=True)

        assert rc == 0
        assert "Recovered goal state" not in capsys.readouterr().out
        assert load_goal_state().watch_branch == "work"

    def test_idle_state_with_history_is_discarded_on_start(self, monkeypatch):
        """Completed/failed history alone does not make saved state resumable."""
        monkeypatch.chdir(self.temp_dir)
        stale = GoalState(watch_branch="no-longer-exists", goals_dir="./goals")
        stale.completed = [{"goal_file": "GOAL-old.md"}]
        stale.failed = [{"goal_file": "GOAL-bad.md", "reason": "empty goal file"}]
        save_goal_state(stale)

        rc = run_goal_watch("work", goals_dir="./goals", once=True)

        assert rc == 0
        state = load_goal_state()
        assert state.watch_branch == "work"
        assert state.completed == []
        assert state.failed == []

    def test_pending_work_with_missing_watch_branch_errors_before_claiming(
        self,
        monkeypatch,
        capsys,
    ):
        """Resumable state whose watch branch is gone fails loudly and untouched."""
        monkeypatch.chdir(self.temp_dir)
        save_goal_state(
            GoalState(
                watch_branch="no-longer-exists",
                goals_dir="./goals",
                queue=["GOAL-one.md"],
            )
        )
        self.add_goal("GOAL-one.md")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            commit_runner=fail_commit_runner,
        )

        assert rc == 1
        out = capsys.readouterr().out
        assert "no-longer-exists" in out
        assert "dvx clear" in out
        state = load_goal_state()
        assert state.current is None
        assert state.queue == ["GOAL-one.md"]


class TestMergeRequest(GitRepoTestCase):
    """MERGE control file: merge the watch branch into a remote target branch."""

    def setup_remote(self, tmp_path) -> Path:
        origin = tmp_path / "origin.git"
        run_git(["init", "--bare", str(origin)], self.temp_dir)
        run_git(["remote", "add", "origin", str(origin)], self.temp_dir)
        run_git(["push", "origin", "main"], self.temp_dir)
        run_git(["remote", "set-head", "origin", "main"], self.temp_dir)
        return origin

    def write_merge_file(self, content: str = "") -> Path:
        merge_file = Path(self.temp_dir) / "goals" / MERGE_FILE_NAME
        merge_file.write_text(content)
        return merge_file

    def commit_on_work(self, name="work.txt", content="work\n", message="work commit"):
        (Path(self.temp_dir) / name).write_text(content)
        run_git(["add", name], self.temp_dir)
        run_git(["commit", "-m", message], self.temp_dir)

    def commit_on_origin_branch(self, origin, tmp_path, branch, name, content, message):
        """Advance a branch on the bare origin via a throwaway clone."""
        clone = tmp_path / f"clone-{name}"
        run_git(["clone", "--branch", branch, str(origin), str(clone)], self.temp_dir)
        run_git(["config", "user.name", "DVX Other"], str(clone))
        run_git(["config", "user.email", "other@example.com"], str(clone))
        (clone / name).write_text(content)
        run_git(["add", name], str(clone))
        run_git(["commit", "-m", message], str(clone))
        run_git(["push", "origin", branch], str(clone))
        shutil.rmtree(clone, ignore_errors=True)

    def test_empty_merge_file_merges_into_default_branch(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        self.commit_on_work()
        merge_file = self.write_merge_file("")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=fail_goal_runner,
            commit_runner=fail_commit_runner,
            merge_runner=fail_merge_runner,
        )

        assert rc == 0
        work_tip = run_git(["rev-parse", "work"], self.temp_dir)
        assert run_git(["rev-parse", "main"], str(origin)) == work_tip
        assert run_git(["rev-parse", "work"], str(origin)) == work_tip
        assert not merge_file.exists()
        state = load_goal_state()
        assert state.merge is None
        assert state.completed and state.completed[0]["merged_into"] == "origin/main"

    def test_named_target_with_divergent_history_merges_cleanly(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        self.commit_on_work()
        self.commit_on_origin_branch(
            origin, tmp_path, "main", "upstream.txt", "upstream\n", "upstream change"
        )
        merge_file = self.write_merge_file("main\n")
        state = self.new_state()

        claimed, error = claim_merge_request(state)
        assert error == ""
        assert claimed and claimed["remote"] == "origin" and claimed["target"] == "main"
        assert not merge_file.exists()

        ok, error = process_merge_request(state, merge_runner=fail_merge_runner)
        assert ok, error
        assert state.merge is None
        # The watch branch now holds both histories, and origin/main matches it.
        work_tip = run_git(["rev-parse", "work"], self.temp_dir)
        assert run_git(["rev-parse", "main"], str(origin)) == work_tip
        assert (Path(self.temp_dir) / "upstream.txt").exists()
        assert (Path(self.temp_dir) / "work.txt").exists()
        # Still on the watch branch, ready for the next goal.
        assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.temp_dir) == "work"

    def test_merge_takes_precedence_over_queued_goals(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        self.commit_on_work()
        work_tip = run_git(["rev-parse", "work"], self.temp_dir)
        self.add_goal("GOAL-after-merge.md")
        self.write_merge_file("")

        origin_main_when_goal_ran = {}

        def implement(content):
            origin_main_when_goal_ran["tip"] = run_git(["rev-parse", "main"], str(origin))
            Path("feature.txt").write_text("done\n")

        rc = run_goal_watch(
            "work",
            goals_dir="./goals",
            once=True,
            claude_runner=make_runner(side_effect=implement),
            commit_runner=noop_commit_runner,
            merge_runner=fail_merge_runner,
        )

        assert rc == 0
        # By the time the queued goal started, the merge had already
        # fast-forwarded origin/main to the pre-goal watch branch tip.
        assert origin_main_when_goal_ran["tip"] == work_tip

    def test_merge_conflicts_resolved_by_merge_runner(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        # Both sides change README.md so the merge conflicts.
        self.commit_on_work("README.md", "# work side\n", "work readme")
        self.commit_on_origin_branch(
            origin, tmp_path, "main", "README.md", "# upstream side\n", "upstream readme"
        )
        self.write_merge_file("main\n")
        state = self.new_state()
        claimed, error = claim_merge_request(state)
        assert claimed, error

        def resolve(target):
            assert target == "origin/main"
            Path("README.md").write_text("# merged\n")
            run_git(["add", "README.md"], self.temp_dir)
            run_git(["commit", "--no-edit"], self.temp_dir)

        merge_runner = make_runner(side_effect=resolve)
        ok, error = process_merge_request(state, merge_runner=merge_runner)

        assert ok, error
        assert len(merge_runner.calls) == 1
        assert state.merge is None
        work_tip = run_git(["rev-parse", "work"], self.temp_dir)
        assert run_git(["rev-parse", "main"], str(origin)) == work_tip
        assert (Path(self.temp_dir) / "README.md").read_text() == "# merged\n"

    def test_unresolved_conflicts_abort_and_preserve_state(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        self.commit_on_work("README.md", "# work side\n", "work readme")
        self.commit_on_origin_branch(
            origin, tmp_path, "main", "README.md", "# upstream side\n", "upstream readme"
        )
        self.write_merge_file("main\n")
        state = self.new_state()
        claimed, error = claim_merge_request(state)
        assert claimed, error

        # The session claims success but never resolves anything.
        ok, error = process_merge_request(state, merge_runner=make_runner())

        assert ok is False
        assert "not fully resolved" in error
        # The half-done merge was aborted and the request stays claimed for retry.
        assert not goals_module._merge_in_progress()
        assert state.merge is not None
        assert state.merge["status"] == MERGE_STATUS_CLAIMED
        # The abort restored the conflicted file; only the (untracked,
        # normally gitignored) .dvx/ state dir shows up in this bare test repo.
        porcelain = run_git(["status", "--porcelain"], self.temp_dir)
        assert porcelain in ("", "?? .dvx/")
        assert (Path(self.temp_dir) / "README.md").read_text() == "# work side\n"

    def test_recovers_from_merge_left_in_progress(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        self.commit_on_work("README.md", "# work side\n", "work readme")
        self.commit_on_origin_branch(
            origin, tmp_path, "main", "README.md", "# upstream side\n", "upstream readme"
        )
        self.write_merge_file("main\n")
        state = self.new_state()
        claimed, error = claim_merge_request(state)
        assert claimed, error

        # Simulate a crash mid-conflict: a merge left in progress on disk.
        run_git(["fetch", "origin"], self.temp_dir)
        result = subprocess.run(
            ["git", "merge", "origin/main"],
            cwd=self.temp_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert goals_module._merge_in_progress()

        def resolve(target):
            Path("README.md").write_text("# merged\n")
            run_git(["add", "README.md"], self.temp_dir)
            run_git(["commit", "--no-edit"], self.temp_dir)

        ok, error = process_merge_request(state, merge_runner=make_runner(side_effect=resolve))

        assert ok, error
        assert state.merge is None
        work_tip = run_git(["rev-parse", "work"], self.temp_dir)
        assert run_git(["rev-parse", "main"], str(origin)) == work_tip

    def test_push_race_refetches_and_retries(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        origin = self.setup_remote(tmp_path)
        self.commit_on_work()
        self.write_merge_file("main\n")
        state = self.new_state()
        claimed, error = claim_merge_request(state)
        assert claimed, error

        # Complete the local merge, then advance origin/main before the push -
        # the race where another process merges to main first.
        ok, error = goals_module._step_merge_local(state, fail_merge_runner)
        assert ok, error
        state.merge["status"] = MERGE_STATUS_LOCAL_MERGED
        save_goal_state(state)
        self.commit_on_origin_branch(
            origin, tmp_path, "main", "racer.txt", "raced\n", "racer change"
        )

        ok, error = process_merge_request(state, merge_runner=fail_merge_runner)

        assert ok, error
        assert state.merge is None
        # The retry picked up the racer's commit instead of clobbering it.
        work_tip = run_git(["rev-parse", "work"], self.temp_dir)
        assert run_git(["rev-parse", "main"], str(origin)) == work_tip
        assert (Path(self.temp_dir) / "racer.txt").exists()

    def test_multi_word_merge_file_rejected(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        self.setup_remote(tmp_path)
        merge_file = self.write_merge_file("merge into the origin/dev branch\n")
        state = self.new_state()

        claimed, error = claim_merge_request(state)

        assert claimed is None and error == ""
        assert state.merge is None
        assert state.failed and "single branch name" in state.failed[0]["reason"]
        assert not merge_file.exists()

    def test_missing_target_branch_rejected(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        self.setup_remote(tmp_path)
        merge_file = self.write_merge_file("nope\n")
        state = self.new_state()

        claimed, error = claim_merge_request(state)

        assert claimed is None and error == ""
        assert state.failed and "branch not found on origin: nope" in state.failed[0]["reason"]
        assert not merge_file.exists()

    def test_target_equal_to_watch_branch_rejected(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        self.setup_remote(tmp_path)
        merge_file = self.write_merge_file("work\n")
        state = self.new_state()

        claimed, error = claim_merge_request(state)

        assert claimed is None and error == ""
        assert state.failed and "watch branch" in state.failed[0]["reason"]
        assert not merge_file.exists()

    def test_merge_without_remote_rejected(self, monkeypatch):
        monkeypatch.chdir(self.temp_dir)
        merge_file = self.write_merge_file("")
        state = self.new_state()

        claimed, error = claim_merge_request(state)

        assert claimed is None and error == ""
        assert state.failed and "requires a git remote" in state.failed[0]["reason"]
        assert not merge_file.exists()

    def test_dirty_tree_blocks_merge_claim_and_keeps_file(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        self.setup_remote(tmp_path)
        (Path(self.temp_dir) / "README.md").write_text("# dirty\n")
        merge_file = self.write_merge_file("")
        state = self.new_state()

        claimed, error = claim_merge_request(state)

        assert claimed is None and error == ""
        assert state.merge is None
        assert state.blocked and state.blocked["merge_file"] == MERGE_FILE_NAME
        assert state.blocked["dirty_paths"] == ["README.md"]
        # The request is not consumed; it retries once the tree is clean.
        assert merge_file.exists()

    def test_deleting_merge_file_clears_merge_block(self, monkeypatch, tmp_path):
        monkeypatch.chdir(self.temp_dir)
        self.setup_remote(tmp_path)
        (Path(self.temp_dir) / "README.md").write_text("# dirty\n")
        merge_file = self.write_merge_file("")
        state = self.new_state()
        claim_merge_request(state)
        assert state.blocked

        merge_file.unlink()
        claimed, error = claim_merge_request(state)

        assert claimed is None and error == ""
        assert state.blocked is None
