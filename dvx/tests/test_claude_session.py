"""Tests for claude_session module - stream-json parsing."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_session import (
    SessionResult,
    _format_tool_params,
    _parse_stream_event,
    _parse_stream_output,
)


class TestStatusOverridesTruth:
    """
    Tests that status overrides are the source of truth for task completion.

    BUG: When Claude re-parses a plan file, it might mark tasks as "done" based
    on [x] markers. These statuses get cached. _apply_status_overrides only
    updates tasks that ARE in the overrides dict - tasks NOT in overrides keep
    whatever status came from the cache. This causes the orchestrator to think
    tasks are complete when they haven't been implemented.

    FIX: Tasks NOT in the status overrides should default to PENDING, not keep
    whatever status was in the cache.
    """

    def test_tasks_not_in_overrides_should_be_pending(self):
        """
        Tasks not in the status override file should be PENDING, regardless
        of what status Claude assigned them during parsing.

        This prevents the finalizer from being called too soon when Claude
        mistakenly marks unimplemented tasks as "done".
        """
        import os
        import tempfile

        from plan_parser import (
            Task,
            TaskStatus,
            _apply_status_overrides,
            _save_status_override,
        )

        # Create a temp directory to act as project root
        with tempfile.TemporaryDirectory() as tmpdir:
            # Change to temp directory so .dvx/task-status.json is created there
            original_cwd = os.getcwd()
            os.chdir(tmpdir)

            try:
                # Simulate: cache has 5 tasks, ALL marked as "done" by Claude's parsing
                # (This happens when Claude sees [x] markers and marks tasks done)
                cached_tasks = [
                    Task(id="1", title="Task 1", description="", status=TaskStatus.DONE, line_number=1),
                    Task(id="2", title="Task 2", description="", status=TaskStatus.DONE, line_number=2),
                    Task(id="3", title="Task 3", description="", status=TaskStatus.DONE, line_number=3),
                    Task(id="4", title="Task 4", description="", status=TaskStatus.DONE, line_number=4),
                    Task(id="5", title="Task 5", description="", status=TaskStatus.DONE, line_number=5),
                ]

                # But the status tracking file only has tasks 1-2 marked as done
                # (because only tasks 1-2 were actually implemented)
                Path(".dvx").mkdir(exist_ok=True)
                _save_status_override("1", TaskStatus.DONE)
                _save_status_override("2", TaskStatus.DONE)

                # Apply status overrides
                result_tasks = _apply_status_overrides(cached_tasks)

                # BUG: Tasks 3-5 are NOT in overrides, so they keep cached "done" status
                # FIX: Tasks NOT in overrides should be PENDING

                # Check tasks 1-2 are DONE (correctly)
                assert result_tasks[0].status == TaskStatus.DONE, "Task 1 should be DONE"
                assert result_tasks[1].status == TaskStatus.DONE, "Task 2 should be DONE"

                # Check tasks 3-5 are PENDING (the fix)
                # Currently this will FAIL because they remain DONE from cache
                assert result_tasks[2].status == TaskStatus.PENDING, \
                    f"Task 3 should be PENDING (not in overrides), but is {result_tasks[2].status}"
                assert result_tasks[3].status == TaskStatus.PENDING, \
                    f"Task 4 should be PENDING (not in overrides), but is {result_tasks[3].status}"
                assert result_tasks[4].status == TaskStatus.PENDING, \
                    f"Task 5 should be PENDING (not in overrides), but is {result_tasks[4].status}"

            finally:
                os.chdir(original_cwd)


class TestFormatToolParams:
    """Tests for _format_tool_params function."""

    def test_read_tool(self):
        """Read tool shows file path."""
        params = _format_tool_params('Read', {'file_path': '/src/main.go'})
        assert params == '/src/main.go'

    def test_read_tool_empty(self):
        """Read tool with empty input."""
        params = _format_tool_params('Read', {})
        assert params == ''

    def test_write_tool(self):
        """Write tool shows path and content preview."""
        params = _format_tool_params('Write', {
            'file_path': '/src/main.go',
            'content': 'package main\n\nfunc main() {\n\tfmt.Println("Hello")\n}'
        })
        assert params.startswith('/src/main.go, "')
        assert 'package main' in params

    def test_write_tool_long_content(self):
        """Write tool truncates long content."""
        long_content = 'x' * 100
        params = _format_tool_params('Write', {
            'file_path': '/src/main.go',
            'content': long_content
        })
        assert '...' in params
        assert len(params) < 100

    def test_edit_tool(self):
        """Edit tool shows path and old_string preview."""
        params = _format_tool_params('Edit', {
            'file_path': '/src/main.go',
            'old_string': 'func oldName()',
            'new_string': 'func newName()'
        })
        assert '/src/main.go' in params
        assert 'func oldName' in params

    def test_bash_tool(self):
        """Bash tool shows command."""
        params = _format_tool_params('Bash', {'command': 'go build ./...'})
        assert params == '"go build ./..."'

    def test_bash_tool_long_command(self):
        """Bash tool truncates long commands."""
        long_cmd = 'echo ' + 'x' * 200
        params = _format_tool_params('Bash', {'command': long_cmd})
        assert len(params) < 110  # 100 chars + quotes

    def test_glob_tool(self):
        """Glob tool shows pattern."""
        params = _format_tool_params('Glob', {'pattern': '**/*.go'})
        assert params == '**/*.go'

    def test_grep_tool_with_path(self):
        """Grep tool shows pattern and path."""
        params = _format_tool_params('Grep', {
            'pattern': 'func main',
            'path': 'src/'
        })
        assert params == '"func main", src/'

    def test_grep_tool_without_path(self):
        """Grep tool shows just pattern when no path."""
        params = _format_tool_params('Grep', {'pattern': 'TODO'})
        assert params == '"TODO"'

    def test_task_tool(self):
        """Task tool shows description."""
        params = _format_tool_params('Task', {
            'description': 'explore codebase',
            'prompt': 'Find all API endpoints'
        })
        assert params == 'explore codebase'

    def test_todo_write_tool(self):
        """TodoWrite tool shows item count."""
        params = _format_tool_params('TodoWrite', {
            'todos': [
                {'content': 'Task 1', 'status': 'pending'},
                {'content': 'Task 2', 'status': 'pending'},
                {'content': 'Task 3', 'status': 'completed'},
            ]
        })
        assert params == '3 items'

    def test_unknown_tool(self):
        """Unknown tools show generic key=value format."""
        params = _format_tool_params('CustomTool', {
            'arg1': 'value1',
            'arg2': 'value2',
            'arg3': 'value3'  # Should be truncated
        })
        assert 'arg1=value1' in params
        assert 'arg2=value2' in params
        assert 'arg3' not in params  # Only first 2 shown

    def test_unknown_tool_long_value(self):
        """Unknown tools truncate long values."""
        params = _format_tool_params('CustomTool', {
            'data': 'x' * 100
        })
        assert '...' in params

    def test_empty_input(self):
        """Empty input returns empty string."""
        params = _format_tool_params('Read', {})
        assert params == ''

    def test_none_input(self):
        """None-like input returns empty string."""
        params = _format_tool_params('Read', None)
        assert params == ''


class TestParseStreamEvent:
    """Tests for _parse_stream_event function."""

    def test_assistant_text_event(self):
        """Parse assistant event with text content."""
        event = {
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'text', 'text': 'I will implement the feature now.'}
                ]
            }
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert '[text]' in log_msg
        assert 'implement the feature' in log_msg

    def test_assistant_tool_use_event(self):
        """Parse assistant event with tool use."""
        event = {
            'type': 'assistant',
            'message': {
                'content': [
                    {
                        'type': 'tool_use',
                        'name': 'Read',
                        'input': {'file_path': '/src/main.go'}
                    }
                ]
            }
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert '[tool] Read(/src/main.go)' in log_msg

    def test_user_event_silent(self):
        """User events (tool results) return empty log message."""
        event = {
            'type': 'user',
            'message': {
                'content': [{'type': 'tool_result', 'content': 'file contents...'}]
            }
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert log_msg == ''

    def test_result_event(self):
        """Parse result event."""
        event = {
            'type': 'result',
            'result': 'Task completed successfully.',
            'session_id': 'abc-123'
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert '[result]' in log_msg

    def test_system_event(self):
        """Parse system event."""
        event = {
            'type': 'system',
            'message': 'Session initialized'
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert '[system]' in log_msg

    def test_error_event(self):
        """Parse error event."""
        event = {
            'type': 'error',
            'error': {'message': 'Rate limit exceeded'}
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert '[error]' in log_msg
        assert 'Rate limit' in log_msg

    def test_empty_line(self):
        """Empty line returns None and empty message."""
        parsed, log_msg = _parse_stream_event('')
        assert parsed is None
        assert log_msg == ''

    def test_whitespace_line(self):
        """Whitespace-only line returns None and empty message."""
        parsed, log_msg = _parse_stream_event('   \n\t  ')
        assert parsed is None
        assert log_msg == ''

    def test_invalid_json(self):
        """Invalid JSON returns None and empty message."""
        parsed, log_msg = _parse_stream_event('not valid json {')
        assert parsed is None
        assert log_msg == ''

    def test_unknown_event_type(self):
        """Unknown event type shows type in brackets."""
        event = {'type': 'custom_event', 'data': 'something'}
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert parsed == event
        assert '[custom_event]' in log_msg

    def test_text_truncation(self):
        """Long text content is truncated."""
        long_text = 'x' * 500
        event = {
            'type': 'assistant',
            'message': {
                'content': [{'type': 'text', 'text': long_text}]
            }
        }
        line = json.dumps(event)

        parsed, log_msg = _parse_stream_event(line)

        assert len(log_msg) < 300  # Should be truncated
        assert '...' in log_msg


class TestParseStreamOutput:
    """Tests for _parse_stream_output function."""

    def test_extract_result_and_session_id(self):
        """Extract result and session_id from result event."""
        events = [
            {'type': 'system', 'message': 'Starting'},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Working...'}]}},
            {'type': 'result', 'result': 'Task completed.', 'session_id': 'abc-123'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert text == 'Task completed.'
        assert session_id == 'abc-123'
        assert blocked is False
        assert reason is None

    def test_session_id_from_any_event(self):
        """Session ID can be extracted from any event that has it."""
        events = [
            {'type': 'system', 'session_id': 'early-id'},
            {'type': 'result', 'result': 'Done.'}  # No session_id here
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert session_id == 'early-id'

    def test_blocked_marker_detected(self):
        """[BLOCKED: reason] in result should be detected."""
        events = [
            {'type': 'result', 'result': 'I am [BLOCKED: need credentials] and cannot continue.', 'session_id': 'abc'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert blocked is True
        assert reason == 'need credentials'

    def test_blocked_file_mention(self):
        """Mention of .dvx/BLOCKED should trigger blocked."""
        events = [
            {'type': 'result', 'result': 'Context written to .dvx/BLOCKED for review.'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert blocked is True

    def test_blocked_md_mention(self):
        """Mention of BLOCKED.md should trigger blocked."""
        events = [
            {'type': 'result', 'result': 'See BLOCKED.md for details.'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert blocked is True

    def test_empty_events(self):
        """Empty event list returns defaults."""
        text, session_id, blocked, reason = _parse_stream_output([])

        assert text == ''
        assert session_id is None
        assert blocked is False
        assert reason is None

    def test_no_result_event(self):
        """Events without result event return empty text."""
        events = [
            {'type': 'system', 'message': 'Starting'},
            {'type': 'assistant', 'message': {'content': []}}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert text == ''

    def test_multiple_result_events(self):
        """Last result event wins."""
        events = [
            {'type': 'result', 'result': 'First result', 'session_id': 'first'},
            {'type': 'result', 'result': 'Second result', 'session_id': 'second'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert text == 'Second result'
        assert session_id == 'second'

    def test_non_dict_events_ignored(self):
        """Non-dict events in list are ignored."""
        events = [
            None,
            'string event',
            123,
            {'type': 'result', 'result': 'Valid result', 'session_id': 'abc'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert text == 'Valid result'
        assert session_id == 'abc'

    def test_blocked_reason_extraction(self):
        """Blocked reason should be extracted correctly."""
        events = [
            {'type': 'result', 'result': '[BLOCKED: This is a multi-word reason here]'}
        ]

        text, session_id, blocked, reason = _parse_stream_output(events)

        assert blocked is True
        assert reason == 'This is a multi-word reason here'


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
