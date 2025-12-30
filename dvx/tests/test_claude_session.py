"""Tests for claude_session module - especially _parse_output."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_session import _parse_output, SessionResult


class TestParseOutput:
    """Tests for _parse_output function."""

    # === JSON PARSING TESTS ===

    def test_parse_json_output(self):
        """Parse valid JSON output with session_id and result."""
        data = {
            "session_id": "abc-123",
            "result": "Implementation complete.",
            "type": "result"
        }
        raw = json.dumps(data)

        text, session_id, blocked, reason = _parse_output(raw)

        assert session_id == "abc-123"
        assert text == "Implementation complete."
        assert blocked is False
        assert reason is None

    def test_parse_json_without_session_id(self):
        """Parse JSON without session_id."""
        data = {"result": "Some output", "type": "result"}
        raw = json.dumps(data)

        text, session_id, blocked, reason = _parse_output(raw)

        assert session_id is None
        assert text == "Some output"

    def test_parse_non_json_output(self):
        """Non-JSON output should be returned as-is."""
        raw = "Just plain text output"

        text, session_id, blocked, reason = _parse_output(raw)

        assert text == raw
        assert session_id is None
        assert blocked is False

    def test_parse_mixed_output_with_json_line(self):
        """Parse output with JSON on one line among text."""
        raw = 'Some text\n{"session_id": "xyz-789", "result": "Done"}\nMore text'

        text, session_id, blocked, reason = _parse_output(raw)

        assert session_id == "xyz-789"

    # === BLOCKED SIGNAL TESTS ===

    def test_blocked_marker_detected(self):
        """[BLOCKED: reason] should be detected."""
        raw = "Working on task...\n[BLOCKED: need API credentials]\nCannot proceed."

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is True
        assert reason == "need API credentials"

    def test_blocked_marker_with_json(self):
        """Blocked marker in JSON result should be detected."""
        data = {
            "session_id": "abc-123",
            "result": "I am [BLOCKED: missing database schema] and cannot continue."
        }
        raw = json.dumps(data)

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is True
        assert reason == "missing database schema"
        assert session_id == "abc-123"

    def test_blocked_file_mention(self):
        """Mention of .dvx/BLOCKED file should trigger blocked."""
        raw = "I have written the context to .dvx/BLOCKED for review."

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is True

    def test_blocked_md_file_mention(self):
        """Mention of BLOCKED.md file should trigger blocked."""
        raw = "See BLOCKED.md for details on the issue."

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is True

    def test_not_blocked_without_marker(self):
        """Output without blocked markers should not be blocked."""
        raw = "Task completed successfully. All tests pass."

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is False
        assert reason is None

    # === EDGE CASES ===

    def test_empty_output(self):
        """Empty output should return defaults."""
        text, session_id, blocked, reason = _parse_output("")

        assert text == ""
        assert session_id is None
        assert blocked is False
        assert reason is None

    def test_whitespace_only_output(self):
        """Whitespace-only output should handle gracefully."""
        text, session_id, blocked, reason = _parse_output("   \n\n   ")

        assert blocked is False

    def test_malformed_json(self):
        """Malformed JSON should fall back to raw text."""
        raw = '{"session_id": "abc", incomplete'

        text, session_id, blocked, reason = _parse_output(raw)

        assert text == raw
        assert session_id is None

    def test_blocked_reason_extraction(self):
        """Blocked reason should be extracted correctly."""
        raw = "[BLOCKED: This is a multi-word reason here]"

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is True
        assert reason == "This is a multi-word reason here"

    def test_blocked_with_no_closing_bracket(self):
        """Blocked marker without closing bracket."""
        raw = "[BLOCKED: incomplete marker"

        text, session_id, blocked, reason = _parse_output(raw)

        # Should still detect blocked but may not extract reason
        assert blocked is True

    def test_multiple_blocked_markers(self):
        """Multiple blocked markers - first one wins."""
        raw = "[BLOCKED: first reason] and [BLOCKED: second reason]"

        text, session_id, blocked, reason = _parse_output(raw)

        assert blocked is True
        assert reason == "first reason"


class TestSessionResult:
    """Tests for SessionResult dataclass."""

    def test_default_values(self):
        """SessionResult should have correct defaults."""
        result = SessionResult(output="test", session_id=None, success=True)

        assert result.output == "test"
        assert result.session_id is None
        assert result.success is True
        assert result.blocked is False
        assert result.block_reason is None

    def test_with_all_values(self):
        """SessionResult with all values set."""
        result = SessionResult(
            output="error",
            session_id="abc-123",
            success=False,
            blocked=True,
            block_reason="timeout"
        )

        assert result.output == "error"
        assert result.session_id == "abc-123"
        assert result.success is False
        assert result.blocked is True
        assert result.block_reason == "timeout"
