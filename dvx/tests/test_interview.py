"""Tests for the deep-interview module."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from interview import (
    DIMENSIONS_BROWNFIELD,
    DIMENSIONS_GREENFIELD,
    PROFILE_THRESHOLDS,
    REQUIRED_SPEC_SECTIONS,
    InterviewState,
    ambiguity_score,
    extract_last_question,
    get_profile,
    load_spec,
    load_state,
    new_state,
    record_round,
    render_spec,
    render_transcript,
    save_state,
    should_stop,
    spec_path,
    state_path,
    validate_spec,
    write_spec,
)


class TestProfileThresholds:
    def test_all_profiles_present(self):
        assert set(PROFILE_THRESHOLDS) == {"quick", "standard", "deep"}

    def test_ordering(self):
        q, _ = get_profile("quick")
        s, _ = get_profile("standard")
        d, _ = get_profile("deep")
        assert q > s > d

    def test_max_rounds_ordering(self):
        _, q_max = get_profile("quick")
        _, s_max = get_profile("standard")
        _, d_max = get_profile("deep")
        assert q_max < s_max < d_max

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError):
            get_profile("extreme")


class TestAmbiguityScore:
    def test_greenfield_weights_sum_to_one(self):
        assert abs(sum(DIMENSIONS_GREENFIELD.values()) - 1.0) < 1e-9

    def test_brownfield_weights_sum_to_one(self):
        assert abs(sum(DIMENSIONS_BROWNFIELD.values()) - 1.0) < 1e-9

    def test_all_clear_is_zero_ambiguity(self):
        scores = {k: 1.0 for k in DIMENSIONS_GREENFIELD}
        assert ambiguity_score(scores, brownfield=False) == pytest.approx(0.0)

    def test_all_unclear_is_one_ambiguity(self):
        scores = {k: 0.0 for k in DIMENSIONS_GREENFIELD}
        assert ambiguity_score(scores, brownfield=False) == pytest.approx(1.0)

    def test_missing_dimensions_count_as_zero(self):
        scores = {}
        assert ambiguity_score(scores) == pytest.approx(1.0)

    def test_clamps_negative_scores(self):
        scores = {k: -0.5 for k in DIMENSIONS_GREENFIELD}
        assert ambiguity_score(scores) == pytest.approx(1.0)

    def test_clamps_above_one(self):
        scores = {k: 2.0 for k in DIMENSIONS_GREENFIELD}
        assert ambiguity_score(scores) == pytest.approx(0.0)


class TestShouldStop:
    def _make_state(self, rounds=0, amb=1.0, gates=False, pressure=False, max_rounds=5):
        state = new_state(
            task="build X", profile="quick", brownfield=False, slug="build-x"
        )
        state.max_rounds = max_rounds
        state.threshold = 0.30
        for i in range(rounds):
            scores = {k: 1.0 - amb for k in DIMENSIONS_GREENFIELD}
            record_round(state, f"Q{i+1}", f"A{i+1}", scores)
            state.rounds[-1].ambiguity = amb
        if gates:
            state.non_goals_captured = True
            state.decision_boundaries_captured = True
        if pressure:
            state.pressure_passes = 1
        return state

    def test_stops_at_max_rounds(self):
        state = self._make_state(rounds=5, max_rounds=5)
        stop, reason = should_stop(state)
        assert stop is True
        assert reason == "max-rounds"

    def test_does_not_stop_when_below_threshold_without_gates(self):
        state = self._make_state(rounds=2, amb=0.1, gates=False, pressure=False)
        stop, _ = should_stop(state)
        assert stop is False

    def test_does_not_stop_when_gates_ok_but_no_pressure_pass(self):
        state = self._make_state(rounds=2, amb=0.1, gates=True, pressure=False)
        stop, _ = should_stop(state)
        assert stop is False

    def test_stops_when_fully_converged(self):
        state = self._make_state(rounds=2, amb=0.1, gates=True, pressure=True)
        stop, reason = should_stop(state)
        assert stop is True
        assert reason == "converged"


class TestRecordRound:
    def test_updates_ambiguity_and_round_count(self):
        state = new_state(task="t", profile="standard", slug="t")
        scores = {k: 0.6 for k in DIMENSIONS_GREENFIELD}
        rnd = record_round(state, "Q1", "A1", scores)
        assert rnd.number == 1
        assert state.rounds == [rnd]
        assert rnd.ambiguity == pytest.approx(0.4, rel=1e-6)

    def test_flags_gate_captures(self):
        state = new_state(task="t", profile="standard", slug="t")
        record_round(
            state,
            "Q",
            "A",
            {k: 0.5 for k in DIMENSIONS_GREENFIELD},
            non_goals_hit=True,
            decision_boundaries_hit=True,
            pressure_pass=True,
        )
        assert state.non_goals_captured is True
        assert state.decision_boundaries_captured is True
        assert state.pressure_passes == 1


class TestPersistence:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load_state(self):
        state = new_state(task="build X", profile="quick", slug="build-x")
        record_round(
            state,
            "Q1",
            "A1",
            {k: 0.5 for k in DIMENSIONS_GREENFIELD},
        )
        save_state(state, project_dir=self.temp_dir)

        loaded = load_state("build-x", project_dir=self.temp_dir)
        assert loaded is not None
        assert loaded.task == "build X"
        assert loaded.profile == "quick"
        assert len(loaded.rounds) == 1
        assert loaded.rounds[0].question == "Q1"

    def test_load_none_when_missing(self):
        assert load_state("missing-slug", project_dir=self.temp_dir) is None

    def test_state_path_in_per_slug_dir(self):
        path = state_path("foo", project_dir=self.temp_dir)
        assert path.parent.name == "foo"
        assert path.name == "interview-state.json"

    def test_spec_path_in_specs_dir(self):
        path = spec_path("foo", project_dir=self.temp_dir)
        assert path.parent.name == "specs"
        assert path.name == "interview-foo.md"

    def test_write_and_load_spec(self):
        state = new_state(task="build X", profile="quick", slug="build-x")
        body = render_spec(
            state,
            intent="Clarify X",
            desired_outcome="X works",
            in_scope="A, B",
            non_goals="C",
            decision_boundaries="Claude may pick defaults",
            constraints="no breaking changes",
            acceptance_criteria="1. X behaves",
            assumptions="Assumed: Y. Resolved: Z.",
        )
        path = write_spec(state, body, project_dir=self.temp_dir)

        assert path.exists()
        assert load_spec("build-x", project_dir=self.temp_dir) == body
        assert load_spec("absent", project_dir=self.temp_dir) is None


class TestRenderSpec:
    def test_contains_required_sections(self):
        state = new_state(task="build X", profile="standard", slug="x")
        body = render_spec(
            state,
            intent="i",
            desired_outcome="o",
            in_scope="s",
            non_goals="n",
            decision_boundaries="d",
            constraints="c",
            acceptance_criteria="a",
            assumptions="u",
        )
        for heading in REQUIRED_SPEC_SECTIONS:
            assert heading in body
        assert "## Metadata" in body
        assert validate_spec(body) == []

    def test_validate_spec_detects_missing(self):
        body = "# stub\n\n## Intent\n\n..."
        missing = validate_spec(body)
        assert "## Desired outcome" in missing

    def test_render_includes_transcript(self):
        state = new_state(task="build X", profile="standard", slug="x")
        record_round(
            state,
            "What does success look like?",
            "Green tests on CI.",
            {k: 0.8 for k in DIMENSIONS_GREENFIELD},
            justification="User was specific",
        )
        body = render_spec(
            state,
            intent="i",
            desired_outcome="o",
            in_scope="s",
            non_goals="n",
            decision_boundaries="d",
            constraints="c",
            acceptance_criteria="a",
            assumptions="u",
        )
        assert "What does success look like?" in body
        assert "Green tests on CI." in body


class TestRenderTranscript:
    def test_empty_state(self):
        state = new_state(task="t", profile="standard", slug="t")
        assert "(no rounds recorded)" in render_transcript(state)

    def test_contains_each_round(self):
        state = new_state(task="t", profile="standard", slug="t")
        for i in range(3):
            record_round(
                state,
                f"Q{i+1}",
                f"A{i+1}",
                {k: 0.5 for k in DIMENSIONS_GREENFIELD},
            )
        transcript = render_transcript(state)
        for i in range(3):
            assert f"Q{i+1}" in transcript
            assert f"A{i+1}" in transcript


class TestExtractLastQuestion:
    def test_finds_trailing_question(self):
        text = "blah blah\n\n**Q:** What color is the car?"
        assert extract_last_question(text) == "What color is the car?"

    def test_prefers_last_question(self):
        text = "**Q:** First?\n...\n**Q:** Second?"
        assert extract_last_question(text) == "Second?"

    def test_no_question(self):
        assert extract_last_question("no question here") is None


class TestInterviewStateRoundTrip:
    def test_to_dict_from_dict(self):
        state = new_state(task="t", profile="deep", brownfield=True, slug="t")
        record_round(
            state,
            "Q",
            "A",
            {k: 0.5 for k in DIMENSIONS_BROWNFIELD},
        )
        restored = InterviewState.from_dict(state.to_dict())
        assert restored.task == state.task
        assert restored.brownfield is True
        assert len(restored.rounds) == 1
        assert restored.rounds[0].question == "Q"
