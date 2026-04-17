"""Tests for the autopilot pipeline."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from autopilot import (
    PHASE_COMPLETE,
    PHASE_FAILED,
    PHASE_INTERVIEW,
    PHASE_PLANNING,
    PHASE_RUNNING,
    PIPELINE_ORDER,
    AutopilotPlan,
    build_plan_from_args,
    next_phase,
    record_phase,
    resolve_starting_phase,
    run_pipeline,
    summarize,
)
from interview import spec_path as interview_spec_path


class TestNextPhase:
    def test_from_none_starts_at_interview(self):
        assert next_phase(None) == PHASE_INTERVIEW

    def test_from_failed_restarts_at_interview(self):
        assert next_phase(PHASE_FAILED) == PHASE_INTERVIEW

    def test_advances_through_pipeline(self):
        assert next_phase(PHASE_INTERVIEW) == PHASE_PLANNING
        assert next_phase(PHASE_PLANNING) == PHASE_RUNNING
        assert next_phase(PHASE_RUNNING) == PHASE_COMPLETE

    def test_from_complete_is_complete(self):
        assert next_phase(PHASE_COMPLETE) == PHASE_COMPLETE

    def test_pipeline_order_invariant(self):
        """The pipeline order must remain interview → planning → running → complete."""
        assert PIPELINE_ORDER == (
            PHASE_INTERVIEW,
            PHASE_PLANNING,
            PHASE_RUNNING,
            PHASE_COMPLETE,
        )


class TestBuildPlanFromArgs:
    def test_basic(self):
        plan = build_plan_from_args(
            task="add a healthz endpoint",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
        )
        assert plan.task == "add a healthz endpoint"
        assert plan.slug == "add-a-healthz-endpoint"
        assert plan.plan_file == "PLAN-add-a-healthz-endpoint.md"
        assert plan.skip_interview is False
        assert plan.skip_consensus is False
        assert plan.no_deslop is False

    def test_explicit_plan_file_overrides_default(self):
        plan = build_plan_from_args(
            task="add healthz",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
            explicit_plan_file="plans/custom.md",
        )
        assert plan.plan_file == "plans/custom.md"

    def test_resume_slug_overrides_derivation(self):
        plan = build_plan_from_args(
            task="",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
            resume_slug="existing-slug",
        )
        assert plan.slug == "existing-slug"
        assert plan.plan_file == "PLAN-existing-slug.md"
        assert plan.resume_from == "existing-slug"


class TestResolveStartingPhase:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def teardown_method(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fresh_run_starts_at_interview(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
        )
        assert resolve_starting_phase(plan) == PHASE_INTERVIEW

    def test_skip_interview_flag_advances(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=True,
            skip_consensus=False,
            no_deslop=False,
        )
        assert resolve_starting_phase(plan) == PHASE_PLANNING

    def test_interview_spec_on_disk_advances(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
        )
        spec = interview_spec_path("x")
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("# spec\n")
        assert resolve_starting_phase(plan) == PHASE_PLANNING

    def test_plan_file_on_disk_skips_to_running(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
        )
        Path("PLAN-x.md").write_text("# plan\n")
        assert resolve_starting_phase(plan) == PHASE_RUNNING

    def test_skip_both_starts_at_running(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=True,
            skip_consensus=True,
            no_deslop=False,
        )
        assert resolve_starting_phase(plan) == PHASE_RUNNING

    def test_resume_reads_recorded_phase(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
            resume_from="x",
        )
        record_phase("PLAN-x.md", PHASE_PLANNING)
        assert resolve_starting_phase(plan) == PHASE_PLANNING

    def test_resume_with_no_state_falls_through(self):
        plan = AutopilotPlan(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
            resume_from="x",
        )
        # No state recorded; fall through to artifact detection.
        assert resolve_starting_phase(plan) == PHASE_INTERVIEW


class TestRunPipeline:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def teardown_method(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _plan(self, **overrides) -> AutopilotPlan:
        defaults = dict(
            task="x",
            slug="x",
            plan_file="PLAN-x.md",
            skip_interview=False,
            skip_consensus=False,
            no_deslop=False,
        )
        defaults.update(overrides)
        return AutopilotPlan(**defaults)

    def test_all_phases_run_on_success(self):
        calls: list[str] = []

        def fn(name: str):
            def _run(_plan, _project_dir):
                calls.append(name)
                return 0

            return _run

        plan = self._plan()
        rc = run_pipeline(
            plan,
            interview_fn=fn("interview"),
            planning_fn=fn("planning"),
            running_fn=fn("running"),
        )
        assert rc == 0
        assert calls == ["interview", "planning", "running"]

    def test_phase_failure_records_failed_and_stops(self):
        calls: list[str] = []

        def ok(name: str):
            def _run(_plan, _project_dir):
                calls.append(name)
                return 0

            return _run

        def fail(_plan, _project_dir):
            calls.append("planning")
            return 2

        plan = self._plan()
        rc = run_pipeline(
            plan,
            interview_fn=ok("interview"),
            planning_fn=fail,
            running_fn=ok("running"),
        )
        assert rc == 2
        assert calls == ["interview", "planning"]

    def test_skip_interview_starts_at_planning(self):
        calls: list[str] = []

        def tracker(name: str):
            def _run(_plan, _project_dir):
                calls.append(name)
                return 0

            return _run

        plan = self._plan(skip_interview=True)
        rc = run_pipeline(
            plan,
            interview_fn=tracker("interview"),
            planning_fn=tracker("planning"),
            running_fn=tracker("running"),
        )
        assert rc == 0
        assert calls == ["planning", "running"]

    def test_existing_plan_file_skips_to_running(self):
        calls: list[str] = []

        def tracker(name: str):
            def _run(_plan, _project_dir):
                calls.append(name)
                return 0

            return _run

        Path("PLAN-x.md").write_text("# plan\n")
        plan = self._plan()
        rc = run_pipeline(
            plan,
            interview_fn=tracker("interview"),
            planning_fn=tracker("planning"),
            running_fn=tracker("running"),
        )
        assert rc == 0
        assert calls == ["running"]

    def test_summarize_after_run(self):
        def ok(_plan, _project_dir):
            return 0

        plan = self._plan()
        rc = run_pipeline(plan, interview_fn=ok, planning_fn=ok, running_fn=ok)
        summary = summarize(plan, rc)
        assert "task: x" in summary
        assert "slug: x" in summary
        assert "exit code: 0" in summary
