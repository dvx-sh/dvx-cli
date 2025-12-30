"""
Claude Code session management.

Wraps the claude CLI for non-interactive usage with session persistence.
"""

import json
import logging
import os
import subprocess
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


def _parse_output(raw_output: str) -> tuple[str, Optional[str], bool, Optional[str]]:
    """
    Parse claude JSON output for session ID and block signals.

    With --output-format json, Claude returns a JSON object with:
    - session_id: UUID of the session
    - result: The text response
    - type: "result" for successful completion

    Returns: (text_output, session_id, is_blocked, block_reason)
    """
    text_output = raw_output
    session_id = None
    is_blocked = False
    block_reason = None

    # Parse JSON output from --output-format json
    try:
        # The output should be a single JSON object
        data = json.loads(raw_output.strip())
        if isinstance(data, dict):
            session_id = data.get('session_id')
            text_output = data.get('result', raw_output)
            logger.debug(f"Parsed session_id: {session_id}")
    except json.JSONDecodeError:
        # Fallback: try line-by-line parsing in case of mixed output
        try:
            lines = raw_output.strip().split('\n')
            for line in lines:
                if line.startswith('{'):
                    try:
                        data = json.loads(line)
                        if 'session_id' in data:
                            session_id = data['session_id']
                        if 'result' in data:
                            text_output = data.get('result', raw_output)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    # Check for block signals in the text output
    check_text = text_output if text_output else raw_output
    if '[BLOCKED:' in check_text:
        is_blocked = True
        # Extract reason
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

    cmd = ['claude', '--dangerously-skip-permissions', '--output-format', 'json']

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
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout
        if result.stderr:
            logger.warning(f"Claude stderr: {result.stderr}")

        # Log Claude's full output for debugging
        logger.debug("=" * 60)
        logger.debug("CLAUDE OUTPUT START")
        logger.debug("=" * 60)
        for line in output.split('\n'):
            logger.debug(line)
        logger.debug("=" * 60)
        logger.debug("CLAUDE OUTPUT END")
        logger.debug("=" * 60)

        text, sid, blocked, block_reason = _parse_output(output)

        return SessionResult(
            output=text,
            session_id=sid or session_id,  # Keep existing if not returned
            success=result.returncode == 0,
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


def launch_interactive(cwd: Optional[str] = None, session_id: Optional[str] = None) -> None:
    """
    Launch Claude Code in interactive mode for human intervention.

    This blocks until the user exits the session.
    """
    cwd = cwd or os.getcwd()

    cmd = ['claude', '--dangerously-skip-permissions']
    if session_id:
        cmd.extend(['--resume', session_id])

    logger.info("Launching interactive Claude session...")
    print("\n" + "=" * 60)
    print("INTERACTIVE SESSION - Resolve the issue, then type /exit")
    print("=" * 60 + "\n")

    subprocess.run(cmd, cwd=cwd)

    print("\n" + "=" * 60)
    print("Interactive session ended. Run 'dvx run' to proceed.")
    print("=" * 60 + "\n")
