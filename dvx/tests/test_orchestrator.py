"""Tests for orchestrator module - especially keyword matching logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orchestrator import (
    parse_decisions,
    parse_escalation_result,
    parse_finalizer_result,
    parse_review_result,
)


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

    def test_approved_overridden_by_issues(self):
        """Approval should be overridden if issues are present."""
        result = parse_review_result("[APPROVED] but you should be careful about X")
        assert result["approved"] is False
        assert result["has_issues"] is True

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

    # === FALSE POSITIVE TESTS (documenting current behavior) ===

    def test_false_positive_no_security_issues(self):
        """CURRENT BEHAVIOR: 'no security issues' still triggers critical.

        This documents the false positive - 'security' anywhere triggers it.
        """
        result = parse_review_result("I checked and there are no security issues.")
        # This is a FALSE POSITIVE - current behavior triggers on 'security'
        assert result["critical"] is True

    def test_false_positive_security_is_good(self):
        """CURRENT BEHAVIOR: 'security is good' triggers critical."""
        result = parse_review_result("The security is good here.")
        assert result["critical"] is True

    def test_false_positive_consider_in_description(self):
        """CURRENT BEHAVIOR: 'consider' in any context triggers issues."""
        result = parse_review_result("I will consider this approved. Great work!")
        assert result["has_issues"] is True
        # Even though intent was to approve, 'consider' triggers issues

    def test_false_positive_should_be_in_positive(self):
        """CURRENT BEHAVIOR: 'should be' in any context triggers issues."""
        result = parse_review_result("This should be easy to merge. [APPROVED]")
        assert result["has_issues"] is True

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

    def test_issues_override_approved(self):
        """If both markers present, issues takes precedence."""
        result = parse_finalizer_result("[APPROVED]\n[ISSUES]\n### Issue 1: Problem")
        assert result["approved"] is False
        assert result["has_issues"] is True

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
