"""
Claude Code session management.

Wraps the claude CLI for non-interactive usage with session persistence.
"""

import io
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionResult:
    """Result from a Claude Code session."""
    output: str
    session_id: Optional[str]
    success: bool
    blocked: bool = False
    block_reason: Optional[str] = None


def _format_tool_params(tool_name: str, tool_input: dict) -> str:
    """Format tool parameters for logging."""
    if not tool_input:
        return ""

    # Format based on common tool types
    if tool_name == 'Read':
        return tool_input.get('file_path', '')
    elif tool_name == 'Write':
        path = tool_input.get('file_path', '')
        content = tool_input.get('content', '')
        preview = content[:50].replace('\n', '\\n') + '...' if len(content) > 50 else content.replace('\n', '\\n')
        return f'{path}, "{preview}"'
    elif tool_name == 'Edit':
        path = tool_input.get('file_path', '')
        old = tool_input.get('old_string', '')[:30].replace('\n', '\\n')
        return f'{path}, "{old}..."'
    elif tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        return f'"{cmd[:100]}"' if len(cmd) > 100 else f'"{cmd}"'
    elif tool_name == 'Glob':
        return tool_input.get('pattern', '')
    elif tool_name == 'Grep':
        pattern = tool_input.get('pattern', '')
        path = tool_input.get('path', '')
        return f'"{pattern}", {path}' if path else f'"{pattern}"'
    elif tool_name == 'Task':
        desc = tool_input.get('description', '')
        return desc
    elif tool_name == 'TodoWrite':
        todos = tool_input.get('todos', [])
        return f'{len(todos)} items'
    else:
        # Generic: show first few keys/values
        items = []
        for k, v in list(tool_input.items())[:2]:
            if isinstance(v, str) and len(v) > 50:
                v = v[:50] + '...'
            items.append(f'{k}={v}')
        return ', '.join(items)


def _parse_stream_event(line: str) -> tuple[Optional[dict], str]:
    """
    Parse a single stream-json event line.

    Returns: (parsed_dict or None, log_message)
    """
    if not line.strip():
        return None, ""

    try:
        data = json.loads(line)
        event_type = data.get('type', 'unknown')

        # Create human-readable log message based on event type
        if event_type == 'assistant':
            # Assistant message with content
            message = data.get('message', {})
            content = message.get('content', [])
            if content and isinstance(content, list):
                for block in content:
                    if block.get('type') == 'tool_use':
                        tool_name = block.get('name', 'unknown')
                        tool_input = block.get('input', {})
                        params = _format_tool_params(tool_name, tool_input)
                        return data, f"[tool] {tool_name}({params})"
                    elif block.get('type') == 'text':
                        text = block.get('text', '')[:200]
                        return data, f"[text] {text}..."
            return data, f"[{event_type}]"
        elif event_type == 'user':
            # Tool results from user - not interesting to log
            return data, ""
        elif event_type == 'result':
            return data, "[result] Session complete"
        elif event_type == 'system':
            return data, f"[system] {data.get('message', '')[:100]}"
        elif event_type == 'error':
            return data, f"[error] {data.get('error', {}).get('message', 'unknown')}"
        else:
            return data, f"[{event_type}]"
    except json.JSONDecodeError:
        return None, ""  # Skip unparseable lines silently


def _parse_stream_output(events: list[dict]) -> tuple[str, Optional[str], bool, Optional[str]]:
    """
    Parse collected stream-json events for final result.

    With --output-format stream-json, Claude outputs newline-delimited JSON.
    The final 'result' event contains:
    - session_id: UUID of the session
    - result: The text response

    Returns: (text_output, session_id, is_blocked, block_reason)
    """
    text_output = ""
    session_id = None
    is_blocked = False
    block_reason = None

    for data in events:
        if not isinstance(data, dict):
            continue

        # Extract session_id from any event that has it
        if 'session_id' in data:
            session_id = data['session_id']

        # The 'result' event contains the final output
        if data.get('type') == 'result':
            text_output = data.get('result', '')
            if 'session_id' in data:
                session_id = data['session_id']
            logger.debug(f"Parsed session_id from result: {session_id}")

    # Check for block signals in the text output
    check_text = text_output
    if '[BLOCKED:' in check_text:
        is_blocked = True
        start = check_text.find('[BLOCKED:')
        end = check_text.find(']', start)
        if end > start:
            block_reason = check_text[start + 9:end].strip()

    # Also check for BLOCKED file creation mention
    if '.dvx/BLOCKED' in check_text or 'BLOCKED.md' in check_text:
        is_blocked = True

    return text_output, session_id, is_blocked, block_reason


def run_claude(
    prompt: str,
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 1200,
    model: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
) -> SessionResult:
    """
    Run Claude Code with the given prompt.

    Args:
        prompt: The prompt to send to Claude
        cwd: Working directory (defaults to current)
        session_id: Optional session ID to resume
        timeout: Timeout in seconds (default 20 minutes)
        model: Optional model to use (e.g., 'opus', 'sonnet')
        append_system_prompt: Optional text to append to system prompt

    Returns:
        SessionResult with output, session_id, and status
    """
    cwd = cwd or os.getcwd()

    cmd = ['claude', '--dangerously-skip-permissions', '--output-format', 'stream-json', '--verbose']

    if model:
        cmd.extend(['--model', model])

    if append_system_prompt:
        cmd.extend(['--append-system-prompt', append_system_prompt])

    if session_id:
        cmd.extend(['--resume', session_id])

    cmd.extend(['-p', prompt])

    logger.info(f"Running claude in {cwd}")
    logger.debug(f"Command: {' '.join(cmd)}")
    logger.debug(f"Prompt: {prompt[:200]}...")

    try:
        # Use Popen to stream output in real-time
        # Use binary mode to avoid UTF-8 decoding issues with partial reads
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wrap streams with TextIOWrapper for proper UTF-8 handling
        stdout_reader = io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace')
        stderr_reader = io.TextIOWrapper(process.stderr, encoding='utf-8', errors='replace')

        # Collect stream events while logging in real-time
        stream_events = []
        stderr_lines = []
        start_time = time.time()

        # Stream stdout and stderr in real-time
        while True:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > timeout:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)

            # Check if process has finished
            retcode = process.poll()

            # Read available stdout (stream-json: one JSON object per line)
            line = stdout_reader.readline()
            if line:
                parsed, log_msg = _parse_stream_event(line)
                if parsed:
                    stream_events.append(parsed)
                if log_msg:
                    logger.info(f"[claude] {log_msg}")

            # Read available stderr
            line = stderr_reader.readline()
            if line:
                stderr_lines.append(line.rstrip('\n'))
                logger.warning(f"[claude stderr] {line.rstrip()}")

            # Exit if process finished and no more output
            if retcode is not None:
                # Drain remaining output
                for line in stdout_reader:
                    parsed, log_msg = _parse_stream_event(line)
                    if parsed:
                        stream_events.append(parsed)
                    if log_msg:
                        logger.info(f"[claude] {log_msg}")
                for line in stderr_reader:
                    stderr_lines.append(line.rstrip('\n'))
                    logger.warning(f"[claude stderr] {line.rstrip()}")
                break

        stderr_output = '\n'.join(stderr_lines)
        if stderr_output:
            logger.warning(f"Claude stderr: {stderr_output}")

        logger.debug(f"Collected {len(stream_events)} stream events")

        text, sid, blocked, block_reason = _parse_stream_output(stream_events)

        return SessionResult(
            output=text,
            session_id=sid or session_id,  # Keep existing if not returned
            success=retcode == 0,
            blocked=blocked,
            block_reason=block_reason,
        )

    except subprocess.TimeoutExpired:
        logger.error(f"Claude timed out after {timeout}s")
        return SessionResult(
            output="",
            session_id=session_id,
            success=False,
            blocked=True,
            block_reason="Timeout - session took too long",
        )
    except FileNotFoundError:
        logger.error("claude command not found - is Claude Code installed?")
        return SessionResult(
            output="",
            session_id=None,
            success=False,
            blocked=True,
            block_reason="claude command not found",
        )
    except Exception as e:
        logger.error(f"Error running claude: {e}")
        return SessionResult(
            output="",
            session_id=session_id,
            success=False,
            blocked=True,
            block_reason=str(e),
        )


def start_session(prompt: str, cwd: Optional[str] = None) -> SessionResult:
    """Start a new Claude session."""
    return run_claude(prompt, cwd=cwd, session_id=None)


def resume_session(session_id: str, prompt: str, cwd: Optional[str] = None) -> SessionResult:
    """Resume an existing Claude session."""
    return run_claude(prompt, cwd=cwd, session_id=session_id)


def run_oneshot(prompt: str, cwd: Optional[str] = None) -> SessionResult:
    """Run a one-shot Claude command (no session persistence expected)."""
    return run_claude(prompt, cwd=cwd, session_id=None)


def launch_interactive(
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> None:
    """
    Launch Claude Code in interactive mode for human intervention.

    Args:
        cwd: Working directory
        session_id: Optional session ID to resume (use None for fresh session)
        initial_prompt: Optional prompt to start the session with

    This blocks until the user exits the session.
    """
    cwd = cwd or os.getcwd()

    cmd = ['claude', '--dangerously-skip-permissions']
    if session_id:
        cmd.extend(['--resume', session_id])
    if initial_prompt:
        cmd.extend(['-p', initial_prompt])

    logger.info("Launching interactive Claude session...")
    print("\n" + "=" * 60)
    print("INTERACTIVE SESSION - Resolve the issue, then type /exit")
    print("=" * 60 + "\n")

    subprocess.run(cmd, cwd=cwd)

    print("\n" + "=" * 60)
    print("Interactive session ended. Run 'dvx run' to proceed.")
    print("=" * 60 + "\n")
