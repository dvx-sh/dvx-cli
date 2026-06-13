"""Tests for the finalize verdict parsing and gate semantics."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orchestrator import (
    FINALIZE_VERDICTS,
    _extract_verdict,
    parse_finalizer_result,
)
from state import (
    State,
    create_initial_state,
    load_state,
    save_state,
)


class TestExtractVerdict:
    def test_first_line_approved(self):
        assert _extract_verdict("[APPROVED]\n\ndetails") == "APPROVED"

    def test_first_line_issues(self):
        assert _extract_verdict("[ISSUES]\nissue 1") == "ISSUES"

    def test_tag_prefixed_summary(self):
        assert _extract_verdict("[APPROVED] All good") == "APPROVED"
        assert _extract_verdict("[ISSUES] Found one blocker") == "ISSUES"

    def test_first_line_suggestions(self):
        assert _extract_verdict("[SUGGESTIONS]\nmore") == "SUGGESTIONS"

    def test_first_line_critical(self):
        assert _extract_verdict("[CRITICAL]\nbad") == "CRITICAL"

    def test_leading_blank_line(self):
        assert _extract_verdict("\n\n[APPROVED]\n...") == "APPROVED"

    def test_case_insensitive(self):
        assert _extract_verdict("[approved]\nbody") == "APPROVED"

    def test_verdict_buried_is_detected(self):
        """If the verdict is not on the first non-empty line, parse still returns it."""
        assert _extract_verdict("prose prose\n[APPROVED]") == "APPROVED"

    def test_robust_parsing_formats(self):
        """Should detect tags even with markdown or prefix text."""
        assert _extract_verdict("**[APPROVED]**") == "APPROVED"
        assert _extract_verdict("Verdict: [APPROVED]") == "APPROVED"
        assert _extract_verdict("### [APPROVED]") == "APPROVED"
        assert _extract_verdict("Final verdict: [APPROVED]") == "APPROVED"
        assert _extract_verdict("- [ISSUES]") == "ISSUES"

        # But should NOT detect conversational mentions
        assert _extract_verdict("I'll give [APPROVED] if you want") is None
        assert _extract_verdict("The verdict is [APPROVED]") is None

    def test_empty_output(self):
        assert _extract_verdict("") is None
        assert _extract_verdict("\n\n\n") is None

    def test_all_verdicts_recognized(self):
        for tag in FINALIZE_VERDICTS:
            assert _extract_verdict(f"[{tag}]\n") == tag


class TestParseFinalizerResult:
    def test_approved_sets_approved_and_clears_others(self):
        r = parse_finalizer_result("[APPROVED]\nclean")
        assert r["verdict"] == "APPROVED"
        assert r["approved"] is True
        assert r["has_issues"] is False
        assert r["has_suggestions"] is False
        assert r["has_critical"] is False
        assert r["parse_error"] is False

    def test_issues_extracts_issues_list(self):
        body = (
            "[ISSUES]\n\n"
            "### Issue 1: Type mismatch\nDetails A\n\n"
            "### Issue 2: Missing test\nDetails B\n\n"
            "## Action Required\nTrailing\n"
        )
        r = parse_finalizer_result(body)
        assert r["verdict"] == "ISSUES"
        assert r["has_issues"] is True
        assert r["approved"] is False
        assert len(r["issues"]) == 2
        assert "Type mismatch" in r["issues"][0]
        assert "Missing test" in r["issues"][1]

    def test_suggestions_does_not_set_issues(self):
        r = parse_finalizer_result("[SUGGESTIONS]\nquick wins")
        assert r["verdict"] == "SUGGESTIONS"
        assert r["has_suggestions"] is True
        assert r["has_issues"] is False
        assert r["approved"] is False

    def test_critical_routes_to_block(self):
        r = parse_finalizer_result("[CRITICAL]\nbroken build")
        assert r["verdict"] == "CRITICAL"
        assert r["has_critical"] is True
        assert r["approved"] is False

    def test_no_parse_error_on_buried_tag(self):
        # A buried tag should now be extracted properly
        r = parse_finalizer_result("Here is my review:\n[APPROVED]\n")
        assert r["parse_error"] is False
        assert r["approved"] is True
        assert r["verdict"] == "APPROVED"

    def test_parse_error_on_no_tag(self):
        r = parse_finalizer_result("no tag at all")
        assert r["parse_error"] is True
        assert r["verdict"] is None


class TestStateVerdictField:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.plan_file = "PLAN-gate.md"

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_verdict_is_none(self):
        state = create_initial_state(self.plan_file, self.temp_dir)
        assert state.finalize_verdict is None
        assert state.finalize_iterations == 0

    def test_verdict_persists_across_load(self):
        state = create_initial_state(self.plan_file, self.temp_dir)
        state.finalize_verdict = "APPROVED"
        state.finalize_iterations = 2
        save_state(state, self.temp_dir)

        loaded = load_state(self.plan_file, self.temp_dir)
        assert loaded.finalize_verdict == "APPROVED"
        assert loaded.finalize_iterations == 2

    def test_state_from_dict_tolerates_extra_keys(self):
        # Forward compatibility: an older state file without finalize_verdict
        # should still load.
        bare = {"plan_file": self.plan_file, "phase": "idle"}
        state = State.from_dict(bare)
        assert state.plan_file == self.plan_file
        assert state.finalize_verdict is None

    def test_state_from_dict_drops_unknown_keys(self):
        # Newer on-disk state with extra keys should load cleanly.
        data = {"plan_file": self.plan_file, "phase": "idle", "future_field": 42}
        state = State.from_dict(data)
        assert state.plan_file == self.plan_file
        assert not hasattr(state, "future_field")
