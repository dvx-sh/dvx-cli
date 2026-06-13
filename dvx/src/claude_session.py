"""
Agent session management.

Wraps Claude Code and Codex CLIs for non-interactive usage while preserving
the legacy Claude-specific helpers used by interactive flows.
"""

import io
import json
import logging
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DVX_MODEL_ENV_VAR = "DVX_MODEL"
MODEL_CHECK_PROMPT = "Reply with exactly OK."
MODEL_CHECK_TIMEOUT_SECONDS = 60
CLAUDE_EFFORT = "high"
CODEX_EFFORT = "xhigh"
GPT_MODEL_PREFIX = "gpt-"

_CLAUDE_MODEL_OVERRIDE: ContextVar[Optional[str]] = ContextVar(
    "_CLAUDE_MODEL_OVERRIDE",
    default=None,
)


def _normalize_model(model: Optional[str]) -> Optional[str]:
    if model is None:
        return None
    model = model.strip()
    return model or None


def resolve_command_model(model: Optional[str] = None) -> str:
    """Resolve user-facing model precedence: CLI flag, DVX_MODEL, default."""
    return (
        _normalize_model(model)
        or _normalize_model(os.environ.get(DVX_MODEL_ENV_VAR))
        or DEFAULT_CLAUDE_MODEL
    )


@contextmanager
def claude_model_override(model: Optional[str]):
    """Temporarily force all Claude CLI launches in this context to use one model."""
    model = _normalize_model(model)
    if model is None:
        yield
        return
    token = _CLAUDE_MODEL_OVERRIDE.set(model)
    try:
        yield
    finally:
        _CLAUDE_MODEL_OVERRIDE.reset(token)


def resolve_claude_model(model: Optional[str] = None) -> str:
    """Resolve the model for an individual Claude CLI launch."""
    return (
        _normalize_model(_CLAUDE_MODEL_OVERRIDE.get())
        or _normalize_model(model)
        or _normalize_model(os.environ.get(DVX_MODEL_ENV_VAR))
        or DEFAULT_CLAUDE_MODEL
    )


def resolve_agent_model(model: Optional[str] = None) -> str:
    """Resolve the selected agent model for model-aware Claude/Codex launches."""
    return resolve_claude_model(model)


def is_gpt_model(model: Optional[str]) -> bool:
    """Return True when a resolved model should be handled by Codex."""
    normalized = _normalize_model(model)
    return bool(normalized and normalized.lower().startswith(GPT_MODEL_PREFIX))


def agent_kind_for_model(model: Optional[str]) -> str:
    """Return the agent CLI family for a model: ``codex`` for gpt-*, else Claude."""
    return "codex" if is_gpt_model(resolve_command_model(model)) else "claude"


def _looks_like_model_unavailable(detail: str) -> bool:
    text = detail.lower()
    return any(
        marker in text
        for marker in (
            "issue with the selected model",
            "may not exist or you may not have access",
            "model does not exist",
            "unknown model",
            "invalid model",
            "model_not_found",
        )
    )


def _claude_failure_reason(retcode: int, model: str, stderr_output: str, text_output: str) -> str:
    detail = (stderr_output.strip() or text_output.strip() or "unknown error").strip()
    if _looks_like_model_unavailable(detail):
        return (
            f"Claude model '{model}' is unavailable in this Claude CLI session. "
            f"Set --model or {DVX_MODEL_ENV_VAR} to an available model. Details: {detail}"
        )
    return f"claude exited with status {retcode}: {detail}"


def check_claude_model_available(
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: int = MODEL_CHECK_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Run a minimal Claude request so commands fail before mutating state."""
    selected = resolve_command_model(model)
    with claude_model_override(selected):
        result = run_claude(
            MODEL_CHECK_PROMPT,
            cwd=cwd,
            timeout=timeout,
            model=selected,
            disable_tools=True,
        )
    if result.success:
        return True, ""
    reason = result.block_reason or result.output.strip() or "Claude CLI model check failed"
    return (
        False,
        f"Claude model '{selected}' is not available. "
        f"Use --model <model> or {DVX_MODEL_ENV_VAR}=<model> to choose an available model. "
        f"Details: {reason}",
    )


def check_agent_model_available(
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: int = MODEL_CHECK_TIMEOUT_SECONDS,
    allow_codex: bool = True,
    command_name: Optional[str] = None,
) -> tuple[bool, str]:
    """Validate the selected model through the CLI that will actually run it."""
    selected = resolve_command_model(model)
    if is_gpt_model(selected):
        if not allow_codex:
            target = f" for {command_name}" if command_name else ""
            return (
                False,
                f"Codex/GPT model '{selected}' is not supported{target} yet. "
                "Use a Claude model for this command.",
            )

        with claude_model_override(selected):
            result = run_codex(
                MODEL_CHECK_PROMPT,
                cwd=cwd,
                timeout=timeout,
                model=selected,
                disable_tools=True,
            )
        if result.success:
            return True, ""
        reason = result.block_reason or result.output.strip() or "Codex CLI model check failed"
        return (
            False,
            f"Codex model '{selected}' is not available. "
            f"Use --model <model> or {DVX_MODEL_ENV_VAR}=<model> to choose an available model. "
            f"Details: {reason}",
        )

    return check_claude_model_available(selected, cwd=cwd, timeout=timeout)


@dataclass
class SessionResult:
    """Result from a Claude Code session."""
    output: str
    session_id: Optional[str]
    success: bool
    blocked: bool = False
    block_reason: Optional[str] = None
    # Number of tool_use blocks observed in the session. None means unknown
    # (e.g. results built by callers that did not stream events).
    tool_use_count: Optional[int] = None
    # Whether the stream contained a final result event. A cleanly finished
    # session always emits one; False means the session was truncated (rate
    # limit, crash). None means unknown.
    result_event_seen: Optional[bool] = None


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

    Falls back to collecting text from assistant events when the result event
    is missing or empty (e.g., rate-limited sessions that terminate early).

    Returns: (text_output, session_id, is_blocked, block_reason)
    """
    text_output = ""
    streamed_text_parts = []
    session_id = None
    is_blocked = False
    block_reason = None

    for data in events:
        if not isinstance(data, dict):
            continue

        # Extract session_id from any event that has it
        if 'session_id' in data:
            session_id = data['session_id']

        # Collect text from assistant message content blocks as fallback
        if data.get('type') == 'assistant':
            message = data.get('message', {})
            content = message.get('content', [])
            if isinstance(content, list):
                for block in content:
                    if block.get('type') == 'text':
                        streamed_text_parts.append(block.get('text', ''))

        # The 'result' event contains the final output
        if data.get('type') == 'result':
            text_output = data.get('result', '')
            if 'session_id' in data:
                session_id = data['session_id']
            logger.debug(f"Parsed session_id from result: {session_id}")

    # Fall back to streamed text if result event was empty/missing
    # (happens when rate limits truncate the session)
    if not text_output and streamed_text_parts:
        text_output = '\n'.join(streamed_text_parts)
        logger.warning(f"No result event; recovered {len(text_output)} chars from streamed text")

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


def _result_is_error(events: list[dict]) -> bool:
    """True if the final result event reported an error (exit code 0 or not)."""
    for data in events:
        if isinstance(data, dict) and data.get('type') == 'result':
            if data.get('is_error', False):
                return True
    return False


def _count_tool_uses(events: list[dict]) -> int:
    """Count tool_use blocks across assistant events."""
    count = 0
    for data in events:
        if not isinstance(data, dict) or data.get('type') != 'assistant':
            continue
        content = data.get('message', {}).get('content', [])
        if isinstance(content, list):
            count += sum(1 for block in content if block.get('type') == 'tool_use')
    return count


def run_claude(
    prompt: str,
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 1800,
    model: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
    disable_tools: bool = False,
) -> SessionResult:
    """
    Run Claude Code with the given prompt.

    Args:
        prompt: The prompt to send to Claude
        cwd: Working directory (defaults to current)
        session_id: Optional session ID to resume
        timeout: Timeout in seconds (default 30 minutes)
        model: Optional model to use (e.g., 'opus', 'sonnet')
        append_system_prompt: Optional text to append to system prompt
        disable_tools: If True, disable all tools (useful for pure text extraction)

    Returns:
        SessionResult with output, session_id, and status
    """
    cwd = cwd or os.getcwd()
    model = resolve_claude_model(model)

    cmd = [
        'claude',
        '--dangerously-skip-permissions',
        '--output-format',
        'stream-json',
        '--verbose',
        '--effort',
        CLAUDE_EFFORT,
    ]

    if disable_tools:
        cmd.extend(['--tools', ''])

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

        # Pump both pipes from dedicated threads. A single loop alternating
        # blocking readline() calls stalls on whichever pipe is silent, which
        # stops real-time logging, prevents the timeout check from running,
        # and lets the child block (and get truncated) on a full stdout pipe.
        stream_events = []
        stderr_lines = []

        def pump_stdout():
            for line in stdout_reader:
                parsed, log_msg = _parse_stream_event(line)
                if parsed:
                    stream_events.append(parsed)
                if log_msg:
                    logger.info(f"[claude] {log_msg}")

        def pump_stderr():
            for line in stderr_reader:
                stderr_lines.append(line.rstrip('\n'))
                logger.warning(f"[claude stderr] {line.rstrip()}")

        pumps = [
            threading.Thread(target=pump_stdout, daemon=True),
            threading.Thread(target=pump_stderr, daemon=True),
        ]
        for pump in pumps:
            pump.start()

        try:
            retcode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        for pump in pumps:
            pump.join(timeout=30)

        stderr_output = '\n'.join(stderr_lines)
        if stderr_output:
            logger.warning(f"Claude stderr: {stderr_output}")

        logger.debug(f"Collected {len(stream_events)} stream events")

        text, sid, blocked, block_reason = _parse_stream_output(stream_events)
        is_error = _result_is_error(stream_events)
        if is_error:
            logger.warning("Claude result event reported an error despite session ending")

        failure_reason = None
        if retcode != 0:
            failure_reason = _claude_failure_reason(retcode, model, stderr_output, text)

        return SessionResult(
            output=text,
            session_id=sid or session_id,  # Keep existing if not returned
            success=retcode == 0 and not is_error,
            blocked=blocked or failure_reason is not None,
            block_reason=(
                block_reason
                or failure_reason
                or ("session result reported an error" if is_error else None)
            ),
            tool_use_count=_count_tool_uses(stream_events),
            result_event_seen=any(
                isinstance(e, dict) and e.get('type') == 'result' for e in stream_events
            ),
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


def _codex_session_id(events: list[dict]) -> Optional[str]:
    """Extract a Codex session ID from tolerant JSONL event shapes."""
    for data in events:
        if not isinstance(data, dict):
            continue
        for key in ("session_id", "sessionId", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _codex_text_from_events(events: list[dict]) -> str:
    """Best-effort fallback text extraction from Codex JSONL events."""
    parts: list[str] = []
    for data in events:
        if not isinstance(data, dict):
            continue
        for key in ("result", "message", "text", "output"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return "\n".join(parts)


def _codex_result_event_seen(events: list[dict], last_message: str) -> bool:
    if last_message.strip():
        return True
    result_types = {
        "result",
        "completed",
        "complete",
        "task_complete",
        "turn_complete",
        "agent_message",
        "response.completed",
    }
    for data in events:
        if isinstance(data, dict) and str(data.get("type", "")).lower() in result_types:
            return True
    return False


def _codex_result_is_error(events: list[dict]) -> bool:
    error_types = {"error", "failed", "failure", "response.failed"}
    for data in events:
        if not isinstance(data, dict):
            continue
        if data.get("is_error") is True:
            return True
        if str(data.get("type", "")).lower() in error_types:
            return True
    return False


def _count_codex_tool_events(events: list[dict]) -> int:
    """Count obvious Codex tool events, failing closed at zero when unclassified."""
    count = 0
    tool_markers = ("tool", "exec", "patch", "command")
    for data in events:
        if not isinstance(data, dict):
            continue
        event_type = str(data.get("type", "")).lower()
        item_type = str(data.get("item_type", data.get("itemType", ""))).lower()
        if any(marker in event_type for marker in tool_markers) or any(
            marker in item_type for marker in tool_markers
        ):
            count += 1
    return count


def run_codex(
    prompt: str,
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 1800,
    model: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
    disable_tools: bool = False,
) -> SessionResult:
    """
    Run Codex non-interactively with YOLO permissions for GPT-family models.

    ``disable_tools`` is accepted for API symmetry with ``run_claude``; Codex
    exec has no matching flag, so model checks rely on a no-op prompt instead.
    """
    del disable_tools
    cwd = cwd or os.getcwd()
    model = resolve_agent_model(model)
    prompt_parts = []
    if append_system_prompt:
        prompt_parts.append(append_system_prompt.strip())
    prompt_parts.append(prompt)
    final_prompt = "\n\n".join(part for part in prompt_parts if part)

    with tempfile.NamedTemporaryFile(prefix="dvx-codex-last-", suffix=".txt", delete=False) as output_file:
        last_message_path = output_file.name

    cmd = [
        "codex",
        "exec",
        "--model",
        model,
        "--cd",
        cwd,
        "--sandbox",
        "danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        f'model_reasoning_effort="{CODEX_EFFORT}"',
        "--json",
        "--output-last-message",
        last_message_path,
    ]
    if session_id:
        logger.debug("Ignoring session_id for codex exec; sessions are non-interactive per invocation")
    cmd.append(final_prompt)

    logger.info(f"Running codex in {cwd}")
    logger.debug(f"Command: {' '.join(cmd[:-1])} <prompt>")
    logger.debug(f"Prompt: {final_prompt[:200]}...")

    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        stream_events: list[dict] = []
        raw_lines: list[str] = []
        for raw in completed.stdout.splitlines():
            if not raw.strip():
                continue
            raw_lines.append(raw)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                stream_events.append(event)

        last_message = ""
        try:
            with open(last_message_path, "r", encoding="utf-8", errors="replace") as f:
                last_message = f.read().strip()
        except FileNotFoundError:
            last_message = ""

        event_text = _codex_text_from_events(stream_events)
        output = last_message or event_text or completed.stdout.strip()
        is_error = _codex_result_is_error(stream_events)
        failure_reason = None
        if completed.returncode != 0 or is_error:
            detail = (
                completed.stderr.strip()
                or output.strip()
                or "\n".join(raw_lines).strip()
                or "unknown error"
            )
            failure_reason = f"codex exited with status {completed.returncode}: {detail}"

        if completed.stderr.strip():
            logger.warning(f"Codex stderr: {completed.stderr.strip()}")

        return SessionResult(
            output=output,
            session_id=_codex_session_id(stream_events) or session_id,
            success=completed.returncode == 0 and not is_error,
            blocked=failure_reason is not None,
            block_reason=failure_reason,
            tool_use_count=_count_codex_tool_events(stream_events),
            result_event_seen=_codex_result_event_seen(stream_events, last_message),
        )
    except subprocess.TimeoutExpired:
        logger.error(f"Codex timed out after {timeout}s")
        return SessionResult(
            output="",
            session_id=session_id,
            success=False,
            blocked=True,
            block_reason="Timeout - codex session took too long",
        )
    except FileNotFoundError:
        logger.error("codex command not found - is Codex CLI installed?")
        return SessionResult(
            output="",
            session_id=None,
            success=False,
            blocked=True,
            block_reason="codex command not found",
        )
    except Exception as e:
        logger.error(f"Error running codex: {e}")
        return SessionResult(
            output="",
            session_id=session_id,
            success=False,
            blocked=True,
            block_reason=str(e),
        )
    finally:
        try:
            os.unlink(last_message_path)
        except OSError:
            pass


def run_agent(
    prompt: str,
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 1800,
    model: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
    disable_tools: bool = False,
) -> SessionResult:
    """Dispatch a non-interactive prompt to Claude or Codex based on model family."""
    selected = resolve_agent_model(model)
    if is_gpt_model(selected):
        return run_codex(
            prompt=prompt,
            cwd=cwd,
            session_id=session_id,
            timeout=timeout,
            model=selected,
            append_system_prompt=append_system_prompt,
            disable_tools=disable_tools,
        )
    return run_claude(
        prompt=prompt,
        cwd=cwd,
        session_id=session_id,
        timeout=timeout,
        model=selected,
        append_system_prompt=append_system_prompt,
        disable_tools=disable_tools,
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
    plan_file: Optional[str] = None,
    auto_explain: bool = True,
    model: Optional[str] = None,
) -> None:
    """
    Launch Claude Code in interactive mode for human intervention.

    Args:
        cwd: Working directory
        session_id: Optional session ID to resume (use None for fresh session)
        initial_prompt: Optional prompt to start the session with
        plan_file: Optional plan file path for resume instructions
        auto_explain: If True, Claude will immediately explain the blocking reason
        model: Optional model override for the interactive Claude session

    This blocks until the user exits the session.
    """
    cwd = cwd or os.getcwd()
    model = resolve_claude_model(model)

    cmd = ['claude', '--dangerously-skip-permissions', '--effort', CLAUDE_EFFORT]
    if model:
        cmd.extend(['--model', model])
    if session_id:
        cmd.extend(['--resume', session_id])
    if initial_prompt:
        # Use --append-system-prompt to provide context while keeping session interactive
        cmd.extend(['--append-system-prompt', initial_prompt])

    # If auto_explain, pass an initial prompt to trigger immediate explanation
    if auto_explain and initial_prompt:
        cmd.append('Explain the blocking reason and what needs to be done.')

    logger.info("Launching interactive Claude session...")
    print("\n" + "=" * 60)
    print("INTERACTIVE SESSION - Type /exit when done")
    print("=" * 60)
    print("Resolve the blocking issue below. Confirm your fix works")
    print("(run tests, check output, etc.) before exiting.\n")

    subprocess.run(cmd, cwd=cwd)

    print("\n" + "=" * 60)
    if plan_file:
        print(f"Interactive session ended. Run 'dvx run {plan_file}' to proceed.")
    else:
        print("Interactive session ended.")
    print("=" * 60 + "\n")
