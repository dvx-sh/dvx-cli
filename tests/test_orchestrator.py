"""Tests for orchestrator module - especially keyword matching logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import orchestrator as orchestrator_module
from claude_session import SessionResult
from orchestrator import (
    claude_model_override,
    is_already_complete,
    parse_decisions,
    parse_escalation_result,
    parse_finalizer_result,
    parse_review_result,
)


class TestClaudeModelOverride:
    """Tests for forcing one Claude model across an orchestrator run."""

    def test_override_replaces_explicit_skill_model(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(orchestrator_module, "load_skill", lambda name: "Prompt")

        def fake_run_claude(prompt, model=None, session_id=None, append_system_prompt=None):
            captured["model"] = model
            return SessionResult(output="", session_id="s", success=True)

        monkeypatch.setattr(orchestrator_module, "run_claude", fake_run_claude)

        with claude_model_override("custom-override-model"):
            orchestrator_module.run_skill("finalize", {}, model="opus")

        assert captured["model"] == "custom-override-model"

    def test_override_applies_to_direct_claude_calls(self, monkeypatch):
        captured = {}

        def fake_run_claude(prompt, model=None):
            captured["model"] = model
            return SessionResult(output="", session_id="s", success=True)

        monkeypatch.setattr(orchestrator_module, "run_claude", fake_run_claude)

        with claude_model_override("custom-override-model"):
            orchestrator_module.run_polish_commit()

        assert captured["model"] == "custom-override-model"


class TestParseReviewResult:
    """Tests for parse_review_result keyword matching."""

    # === APPROVAL TESTS ===

    def test_approved_with_marker(self):
        """[APPROVED] marker should approve."""
        result = parse_review_result("[APPROVED]\n\nLooks good!")
        assert result["approved"] is True
        assert result["critical"] is False

    def test_approved_lgtm(self):
        """'lgtm' should approve."""
        result = parse_review_result("LGTM, ship it!")
        assert result["approved"] is True

    def test_approved_looks_good(self):
        """'looks good' should approve."""
        result = parse_review_result("This looks good to me.")
        assert result["approved"] is True

    # === ISSUES TESTS ===

    def test_issues_marker(self):
        """[ISSUES] marker should flag issues."""
        result = parse_review_result("[ISSUES]\n\n1. Fix the bug")
        assert result["has_issues"] is True
        assert result["approved"] is False

    def test_issues_should_be(self):
        """'should be' should flag issues."""
        result = parse_review_result("The variable should be renamed.")
        assert result["has_issues"] is True

    def test_issues_consider(self):
        """'consider' should flag issues."""
        result = parse_review_result("You might consider using a different approach.")
        assert result["has_issues"] is True

    def test_issues_recommend(self):
        """'recommend' should flag issues."""
        result = parse_review_result("I recommend adding error handling.")
        assert result["has_issues"] is True

    def test_explicit_approved_overrides_heuristic_issues(self):
        """Explicit [APPROVED] overrides heuristic issue detection like 'should be'."""
        result = parse_review_result("[APPROVED] but you should be careful about X")
        assert result["approved"] is True
        assert result["has_issues"] is False  # Heuristic ignored with explicit approval

    # === MISSING TESTS ===

    def test_missing_tests_marker(self):
        """'missing test' should flag missing tests."""
        result = parse_review_result("There are missing tests for the new function.")
        assert result["missing_tests"] is True

    def test_no_test(self):
        """'no test' should flag missing tests."""
        result = parse_review_result("There is no test coverage for this.")
        assert result["missing_tests"] is True

    def test_add_test(self):
        """'add test' should flag missing tests."""
        result = parse_review_result("Please add tests for the edge cases.")
        assert result["missing_tests"] is True

    def test_needs_test(self):
        """'needs test' should flag missing tests."""
        result = parse_review_result("This function needs tests.")
        assert result["missing_tests"] is True

    # === CRITICAL TESTS ===

    def test_critical_marker(self):
        """[CRITICAL] marker should flag critical."""
        result = parse_review_result("[CRITICAL]\n\nThis will break production!")
        assert result["critical"] is True
        assert result["approved"] is False

    def test_blocked_marker(self):
        """[BLOCKED] marker should flag critical."""
        result = parse_review_result("[BLOCKED]\n\nNeed clarification on requirements.")
        assert result["critical"] is True

    def test_critical_issue_phrase(self):
        """'critical issue' phrase should flag critical."""
        result = parse_review_result("There is a critical issue with the logic.")
        assert result["critical"] is True

    def test_security_keyword(self):
        """'security' keyword should flag critical."""
        result = parse_review_result("This has a security vulnerability.")
        assert result["critical"] is True

    # === FALSE POSITIVE AVOIDANCE TESTS ===
    # These tests verify the implementation correctly avoids false positives

    def test_no_false_positive_security_positive_mention(self):
        """'security' in positive context should NOT trigger critical.

        Only specific phrases like 'security vulnerability' trigger critical.
        """
        result = parse_review_result("I checked and there are no security issues.")
        assert result["critical"] is False

    def test_no_false_positive_security_is_good(self):
        """'security is good' should NOT trigger critical."""
        result = parse_review_result("The security is good here.")
        assert result["critical"] is False

    def test_heuristic_consider_without_approval(self):
        """'consider' triggers issues when there's no explicit approval."""
        result = parse_review_result("I will consider this approved. Great work!")
        assert result["has_issues"] is True
        # Without [APPROVED], heuristic detection still applies

    def test_no_false_positive_should_be_with_approval(self):
        """'should be' with explicit [APPROVED] should NOT trigger issues."""
        result = parse_review_result("This should be easy to merge. [APPROVED]")
        assert result["has_issues"] is False  # Explicit approval overrides heuristic
        assert result["approved"] is True

    # === EDGE CASES ===

    def test_empty_output(self):
        """Empty output should not approve or flag anything."""
        result = parse_review_result("")
        assert result["approved"] is False
        assert result["has_issues"] is False
        assert result["critical"] is False
        assert result["missing_tests"] is False

    def test_case_insensitive(self):
        """Matching should be case insensitive."""
        result = parse_review_result("[approved]")
        assert result["approved"] is True

        result = parse_review_result("[CRITICAL]")
        assert result["critical"] is True

    def test_suggestions_contains_full_output(self):
        """Suggestions should contain the full output."""
        output = "Some review text here."
        result = parse_review_result(output)
        assert result["suggestions"] == output


class TestParseEscalationResult:
    """Tests for parse_escalation_result."""

    def test_proceed_marker(self):
        """[PROCEED] marker should set proceed=True."""
        result = parse_escalation_result("[PROCEED]\n\n## Analysis\nIssue resolved.")
        assert result["proceed"] is True
        assert result["escalate"] is False

    def test_escalate_marker(self):
        """[ESCALATE] marker should set escalate=True."""
        result = parse_escalation_result("[ESCALATE]\n\n## Why Escalation Required\nNeed human input.")
        assert result["proceed"] is False
        assert result["escalate"] is True

    def test_escalate_overrides_proceed(self):
        """If both markers present, escalate takes precedence."""
        result = parse_escalation_result("[PROCEED]\n[ESCALATE]\nConfused output")
        assert result["proceed"] is False
        assert result["escalate"] is True

    def test_no_markers(self):
        """No markers should default to not proceed, not escalate."""
        result = parse_escalation_result("Some analysis without clear decision.")
        assert result["proceed"] is False
        assert result["escalate"] is False

    def test_case_insensitive(self):
        """Markers should be case insensitive."""
        result = parse_escalation_result("[proceed]\nProceeding with fix.")
        assert result["proceed"] is True

        result = parse_escalation_result("[ESCALATE]\nNeed help.")
        assert result["escalate"] is True

    def test_output_preserved(self):
        """Full output should be preserved in result."""
        output = "[PROCEED]\n\nFull analysis here."
        result = parse_escalation_result(output)
        assert result["output"] == output


class TestParseDecisions:
    """Tests for parse_decisions pattern matching."""

    def test_single_decision(self):
        """Parse a single decision block."""
        output = """
[DECISION: database-choice]
Decision: Use PostgreSQL for the database
Reasoning: Better support for complex queries and JSON
Alternatives:
- MySQL
- SQLite
"""
        decisions = parse_decisions(output)
        assert len(decisions) == 1
        assert decisions[0]["topic"] == "database-choice"
        assert "PostgreSQL" in decisions[0]["decision"]
        assert "complex queries" in decisions[0]["reasoning"]
        assert "MySQL" in decisions[0]["alternatives"]

    def test_multiple_decisions(self):
        """Parse multiple decision blocks."""
        output = """
[DECISION: auth-method]
Decision: Use JWT tokens
Reasoning: Stateless and scalable
Alternatives:
- Session cookies
- OAuth only

[DECISION: framework]
Decision: Use FastAPI
Reasoning: Modern async support
Alternatives:
- Flask
- Django
"""
        decisions = parse_decisions(output)
        assert len(decisions) == 2
        assert decisions[0]["topic"] == "auth-method"
        assert decisions[1]["topic"] == "framework"

    def test_no_decisions(self):
        """Return empty list when no decisions found."""
        output = "Just some regular output without decisions."
        decisions = parse_decisions(output)
        assert decisions == []

    def test_decision_with_multiline_alternatives(self):
        """Parse alternatives that span multiple lines."""
        output = """
[DECISION: api-style]
Decision: Use REST
Reasoning: Team familiarity
Alternatives:
- GraphQL
- gRPC
- SOAP (legacy)
"""
        decisions = parse_decisions(output)
        assert len(decisions) == 1
        assert len(decisions[0]["alternatives"]) == 3
        assert "GraphQL" in decisions[0]["alternatives"]
        assert "gRPC" in decisions[0]["alternatives"]

    def test_case_insensitive_markers(self):
        """Decision markers should be case insensitive."""
        output = """
[decision: Test]
decision: Something
reasoning: Because
alternatives:
- Option A
"""
        decisions = parse_decisions(output)
        assert len(decisions) == 1


class TestParseFinalizerResult:
    """Tests for parse_finalizer_result."""

    def test_approved_marker(self):
        """[APPROVED] marker should set approved=True."""
        result = parse_finalizer_result("""
[APPROVED]

## Summary
All changes look good.

## Quality Assessment
- Code quality: Excellent
- Test coverage: Good
""")
        assert result["approved"] is True
        assert result["has_issues"] is False
        assert result["issues"] == []

    def test_issues_marker(self):
        """[ISSUES] marker should set has_issues=True."""
        result = parse_finalizer_result("""
[ISSUES]

## Summary
Found some problems.

### Issue 1: Missing error handling
**Severity**: major
**Location**: src/api.py:45
**Description**: No try/catch around database call

### Issue 2: Test coverage
**Severity**: minor
**Description**: New function lacks tests
""")
        assert result["approved"] is False
        assert result["has_issues"] is True
        assert len(result["issues"]) == 2

    def test_first_line_wins_approved(self):
        """Under the first-line-only contract, the first tag wins."""
        result = parse_finalizer_result("[APPROVED]\n[ISSUES]\n### Issue 1: Problem")
        assert result["approved"] is True
        assert result["has_issues"] is False

    def test_no_markers(self):
        """No markers should default to not approved, no issues."""
        result = parse_finalizer_result("Some analysis without clear decision.")
        assert result["approved"] is False
        assert result["has_issues"] is False

    def test_case_insensitive(self):
        """Markers should be case insensitive."""
        result = parse_finalizer_result("[approved]\nAll good!")
        assert result["approved"] is True

        result = parse_finalizer_result("[issues]\n### Issue 1: Bug")
        assert result["has_issues"] is True

    def test_output_preserved(self):
        """Full output should be preserved in result."""
        output = "[APPROVED]\n\nFull review here."
        result = parse_finalizer_result(output)
        assert result["output"] == output

    def test_issue_extraction(self):
        """Issues should be extracted from the output."""
        output = """
[ISSUES]

### Issue 1: Security vulnerability
**Severity**: critical
SQL injection possible in user input handling.

### Issue 2: Performance
**Severity**: minor
Consider caching the database results.

## Action Required
Fix these before merge.
"""
        result = parse_finalizer_result(output)
        assert len(result["issues"]) == 2
        assert "Security vulnerability" in result["issues"][0]
        assert "Performance" in result["issues"][1]

    # === SUGGESTIONS TESTS ===

    def test_suggestions_marker(self):
        """[SUGGESTIONS] marker should set has_suggestions=True."""
        result = parse_finalizer_result("""
[SUGGESTIONS]

## Quick Wins (Implement Now)

1. Remove unused import
   - File: src/api.py
   - Priority: LOW
""")
        assert result["approved"] is False
        assert result["has_issues"] is False
        assert result["has_suggestions"] is True
        assert result["suggestions"] != ""

    def test_suggestions_not_approved(self):
        """[SUGGESTIONS] should not count as approved."""
        result = parse_finalizer_result("[SUGGESTIONS]\n\n## Quick Wins\n1. Cleanup")
        assert result["approved"] is False
        assert result["has_suggestions"] is True

    def test_first_line_wins_suggestions(self):
        """Under the first-line-only contract, the first tag wins."""
        result = parse_finalizer_result("[SUGGESTIONS]\n[ISSUES]\n### Issue 1: Bug")
        assert result["has_suggestions"] is True
        assert result["has_issues"] is False

    def test_suggestions_case_insensitive(self):
        """[suggestions] marker should be case insensitive."""
        result = parse_finalizer_result("[suggestions]\n## Quick Wins\n1. Cleanup")
        assert result["has_suggestions"] is True

    def test_no_suggestions_by_default(self):
        """No markers should default to no suggestions."""
        result = parse_finalizer_result("Some analysis without clear decision.")
        assert result["has_suggestions"] is False
        assert result["suggestions"] == ""

    def test_approved_not_suggestions(self):
        """[APPROVED] alone should not have suggestions."""
        result = parse_finalizer_result("[APPROVED]\nAll good!")
        assert result["has_suggestions"] is False
        assert result["suggestions"] == ""


class TestIsAlreadyComplete:
    """Tests for is_already_complete detection."""

    def test_already_complete_marker(self):
        """[ALREADY_COMPLETE] marker should be detected."""
        assert is_already_complete("[ALREADY_COMPLETE] Task was already implemented.")

    def test_already_complete_case_insensitive(self):
        """Detection should be case insensitive."""
        assert is_already_complete("[already_complete] Done.")
        assert is_already_complete("[Already_Complete] Done.")
        assert is_already_complete("[ALREADY_COMPLETE] Done.")

    def test_already_complete_in_longer_output(self):
        """Should detect marker in longer output."""
        output = """
I checked the codebase and found that this task has already been implemented.

The migration file exists at migrations/001_add_phase.sql and includes:
- The 'phase' column with DEFAULT 'relationships'
- The 'datasource_id' column

[ALREADY_COMPLETE]

The plan file has been updated to mark this task as done.
"""
        assert is_already_complete(output)

    def test_no_marker(self):
        """Should return False when marker is not present."""
        assert not is_already_complete("Task implemented successfully.")
        assert not is_already_complete("[APPROVED] Looks good.")
        assert not is_already_complete("Already done with implementation.")


class TestFinalizationLoop:
    """Tests for finalizer retry behavior."""

    def test_repeated_suggestions_do_not_block_after_retry_limit(self, monkeypatch):
        """Optional suggestions should not become a human-intervention block."""
        plan_file = "PLAN-finalizer.md"
        state = orchestrator_module.State(plan_file=plan_file)
        finalizer_calls = []
        polish_calls = []
        committed = []
        verdicts = []

        monkeypatch.setattr(
            orchestrator_module,
            "get_plan_summary",
            lambda _plan_file: {"total": 0, "pending": 0, "in_progress": 0},
        )
        monkeypatch.setattr(orchestrator_module, "update_phase", lambda *_args, **_kwargs: state)
        monkeypatch.setattr(orchestrator_module, "_run_deslop_pass", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(orchestrator_module, "cleanup_plan", lambda _plan_file: True)
        monkeypatch.setattr(orchestrator_module, "log_decisions_from_output", lambda *_args, **_kwargs: None)

        def fake_record(_plan_file, verdict, iteration):
            verdicts.append((verdict, iteration))

        def fake_finalizer(_plan_file):
            finalizer_calls.append(_plan_file)
            return SessionResult(
                output="[SUGGESTIONS]\n\n## Deferred Work\nAlready captured in FIX files.",
                session_id="finalizer-session",
                success=True,
            )

        def fake_polish_fix(suggestions, _plan_file):
            polish_calls.append(suggestions)
            return SessionResult(output="Working tree clean; nothing to do.", session_id="fix-session", success=True)

        def fake_polish_commit():
            committed.append(True)
            return SessionResult(output="Nothing to commit.", session_id="commit-session", success=True)

        def fail_blocked(*_args, **_kwargs):
            raise AssertionError("suggestions retry cap should not block")

        monkeypatch.setattr(orchestrator_module, "_record_finalize_verdict", fake_record)
        monkeypatch.setattr(orchestrator_module, "run_finalizer", fake_finalizer)
        monkeypatch.setattr(orchestrator_module, "run_polish_fix", fake_polish_fix)
        monkeypatch.setattr(orchestrator_module, "run_polish_commit", fake_polish_commit)
        monkeypatch.setattr(orchestrator_module, "handle_blocked", fail_blocked)

        assert orchestrator_module._run_finalization(plan_file, state) == 0

        assert len(finalizer_calls) == 3
        assert len(polish_calls) == 2
        assert len(committed) == 2
        assert verdicts == [("SUGGESTIONS", 1), ("SUGGESTIONS", 2), ("SUGGESTIONS", 3)]

    def test_deslop_preserves_recorded_finalize_verdict(self, monkeypatch, tmp_path):
        """Deslop state saves should not restore a stale pre-finalizer verdict."""
        plan_file = "PLAN-finalizer.md"
        monkeypatch.chdir(tmp_path)

        stale_state = orchestrator_module.State(
            plan_file=plan_file,
            finalize_verdict="BLOCKED",
            finalize_iterations=3,
        )
        current_state = orchestrator_module.State(
            plan_file=plan_file,
            finalize_verdict="APPROVED",
            finalize_iterations=1,
        )
        orchestrator_module.save_state(current_state)

        monkeypatch.setattr(orchestrator_module, "load_changed_files_manifest", lambda _plan_file: [])
        monkeypatch.setattr(orchestrator_module, "load_session_base_head", lambda _plan_file: "")
        monkeypatch.setattr(orchestrator_module, "compute_changed_files", lambda **_kwargs: [])

        orchestrator_module._run_deslop_pass(plan_file, stale_state)

        saved = orchestrator_module.load_state(plan_file)
        assert saved.finalize_verdict == "APPROVED"
        assert saved.finalize_iterations == 1
        assert saved.deslop_run is True
