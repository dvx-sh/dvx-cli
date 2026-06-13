"""
Main orchestrator loop for dvx.

Coordinates the implement → review → fix → test → commit cycle.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

from claude_session import SessionResult, claude_model_override, resolve_claude_model, run_claude
from plan_parser import (
    Task,
    TaskStatus,
    get_next_pending_task,
    get_plan_summary,
    update_task_status,
)
from state import (
    Phase,
    State,
    create_initial_state,
    ensure_dvx_dir,
    get_dvx_dir,
    increment_iteration,
    load_state,
    log_decision,
    save_state,
    set_current_task,
    set_overseer_session,
    update_phase,
    write_blocked_context,
)

logger = logging.getLogger(__name__)

# Path to skills directory
SKILLS_DIR = Path(__file__).parent / "skills"


def _resolve_claude_model(model: Optional[str] = None) -> Optional[str]:
    return resolve_claude_model(model)


def parse_decisions(output: str) -> list[dict]:
    """
    Parse decision markers from Claude's output.

    Looks for:
    [DECISION: topic]
    Decision: What was decided
    Reasoning: Why
    Alternatives: What else was considered
    """
    import re

    decisions = []
    pattern = r'\[DECISION:\s*([^\]]+)\]\s*Decision:\s*(.+?)\s*Reasoning:\s*(.+?)\s*Alternatives:\s*(.+?)(?=\[DECISION:|$)'

    matches = re.findall(pattern, output, re.DOTALL | re.IGNORECASE)
    for match in matches:
        topic, decision, reasoning, alternatives = match
        # Parse alternatives (usually a list)
        alt_list = [a.strip().lstrip('- ') for a in alternatives.strip().split('\n') if a.strip()]
        decisions.append({
            'topic': topic.strip(),
            'decision': decision.strip(),
            'reasoning': reasoning.strip(),
            'alternatives': alt_list,
        })

    return decisions


def log_decisions_from_output(output: str, plan_file: str) -> None:
    """Parse and log any decisions from Claude's output."""
    decisions = parse_decisions(output)
    for d in decisions:
        log_decision(
            topic=d['topic'],
            decision=d['decision'],
            reasoning=d['reasoning'],
            alternatives=d['alternatives'],
            plan_file=plan_file,
        )
        logger.info(f"Logged decision: {d['topic']}")


def load_skill(name: str) -> str:
    """
    Load a skill file and return its content (without YAML frontmatter).

    Skills are markdown files with YAML frontmatter that can be used both:
    - As Claude Code commands (/dvx:skill_name)
    - As prompt templates loaded by the orchestrator
    """
    skill_file = SKILLS_DIR / f"{name}.md"
    if not skill_file.exists():
        raise FileNotFoundError(f"Skill not found: {skill_file}")

    content = skill_file.read_text()

    # Strip YAML frontmatter (everything between first --- and second ---)
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()

    return content


def run_skill(
    skill_name: str,
    args: dict[str, str],
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
) -> SessionResult:
    """
    Load a skill file, substitute arguments, and run it.

    Args:
        skill_name: Name of the skill (e.g., "implement", "review")
        args: Dictionary of argument name -> value
        model: Optional model override
        session_id: Optional session ID for continuity
        append_system_prompt: Optional additional system prompt

    Returns:
        SessionResult from the Claude session
    """
    content = load_skill(skill_name)

    # Replace $ARGUMENTS.name with actual values
    for name, value in args.items():
        content = content.replace(f"$ARGUMENTS.{name}", str(value))

    return run_claude(
        content,
        model=_resolve_claude_model(model),
        session_id=session_id,
        append_system_prompt=append_system_prompt,
    )


def get_change_stats() -> dict:
    """
    Get statistics about the current git changes.

    Returns dict with:
        - files_changed: int
        - files_deleted: int
        - files_added: int
        - insertions: int
        - deletions: int
        - is_massive: bool - True if changes exceed thresholds for human review
    """
    stats = {
        'files_changed': 0,
        'files_deleted': 0,
        'files_added': 0,
        'insertions': 0,
        'deletions': 0,
        'is_massive': False,
        'summary': '',
    }

    try:
        # Get file counts by status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            status = line[:2].strip()
            if status == 'D':
                stats['files_deleted'] += 1
            elif status == '??':
                stats['files_added'] += 1
            stats['files_changed'] += 1

        # Get line counts
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stats['summary'] = result.stdout

        # Parse the summary line (e.g., "52 files changed, 124 insertions(+), 19810 deletions(-)")
        lines = result.stdout.strip().split('\n')
        if lines:
            summary_line = lines[-1]
            import re
            ins_match = re.search(r'(\d+) insertion', summary_line)
            del_match = re.search(r'(\d+) deletion', summary_line)
            if ins_match:
                stats['insertions'] = int(ins_match.group(1))
            if del_match:
                stats['deletions'] = int(del_match.group(1))

        # Determine if this is a "massive" change requiring human review
        #
        # We're permissive about additions and refactoring, but cautious about deletions:
        # - Large additions (50k+ lines): Fine, new code being added
        # - Even changes (50k added, 35k deleted): Fine, likely refactoring
        # - Large deletions without additions: Concerning, significant code removal
        #
        # Thresholds:
        # - >20 files deleted: Always flag (significant structural change)
        # - Deletions > 10x insertions AND >2000 deletions: Flag (mass deletion)
        #
        insertions = stats['insertions'] or 1  # Avoid division by zero
        deletions = stats['deletions']
        deletion_ratio = deletions / insertions

        is_mass_deletion = (
            deletions > 2000 and
            deletion_ratio > 10
        )
        is_many_files_deleted = stats['files_deleted'] > 20

        if is_mass_deletion or is_many_files_deleted:
            stats['is_massive'] = True
            if is_mass_deletion:
                logger.info(f"Detected mass deletion: {deletions:,} deletions vs {stats['insertions']:,} insertions "
                           f"({deletion_ratio:.1f}x ratio)")
            if is_many_files_deleted:
                logger.info(f"Detected many files deleted: {stats['files_deleted']} files")

    except Exception as e:
        logger.error(f"Error getting change stats: {e}")

    return stats


def get_git_diff(max_size: int = 15000) -> str:
    """
    Get the current git diff for review.

    Args:
        max_size: Maximum diff size in characters. If exceeded, uses --stat summary only.
                  Default 15KB to leave room for prompt template and plan file.
    """
    try:
        # Always get the stat summary first (it's small and useful)
        stat_result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stat_summary = stat_result.stdout

        # Check diff size before loading full content
        size_result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = size_result.stdout
        diff_size = len(diff)

        # For large diffs, only use stat summary
        if diff_size > max_size:
            logger.info(f"Diff too large ({diff_size:,} chars), using --stat summary only")
            diff = (
                f"[Diff too large ({diff_size:,} chars) - showing summary only]\n\n"
                f"{stat_summary}\n"
                f"[Reviewer: Focus on verifying build/tests pass. Use `git diff HEAD` for details if needed.]"
            )
        else:
            # Include stat summary at top for context, then full diff
            diff = f"Summary:\n{stat_summary}\n\nFull diff:\n{diff}"

        # Also get untracked files
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = result.stdout

        return f"Git Status:\n{status}\n\nGit Diff:\n{diff}"
    except Exception as e:
        logger.error(f"Error getting git diff: {e}")
        return f"Error getting git diff: {e}"


def validate_task_safety(task: Task) -> tuple[bool, str]:
    """
    Check if a task contains forbidden operations.

    Returns:
        (is_safe, warning_message)
    """
    task_text = f"{task.title} {task.description}".lower()

    # Forbidden patterns
    forbidden_patterns = [
        ("merge to main", "merging to main branch"),
        ("merge to master", "merging to master branch"),
        ("push to main", "pushing to main branch"),
        ("push to master", "pushing to master branch"),
        ("deploy to", "deployment operations"),
        ("deploy the", "deployment operations"),
        ("production deploy", "production deployment"),
        ("release to", "release operations"),
        ("merge branch", "branch merging"),
        ("git merge main", "merging main"),
        ("git merge master", "merging master"),
    ]

    for pattern, description in forbidden_patterns:
        if pattern in task_text:
            return False, f"Task contains forbidden operation: {description}"

    return True, ""


def run_task_splitter(task: Task, plan_file: str) -> SessionResult:
    """
    Analyze a task to determine if it should be split into subtasks.

    Uses Claude to evaluate task complexity and propose subtasks if needed.
    """
    logger.info(f"Analyzing task {task.id} for potential splitting")

    plan_content = Path(plan_file).read_text()

    return run_skill("split-task", {
        "task_id": task.id,
        "task_title": task.title,
        "task_description": task.description,
        "plan_file": plan_file,
        "plan_content": plan_content,
    })


def parse_split_result(output: str) -> dict:
    """
    Parse the task splitter's output.

    Returns dict with:
        - should_split: bool
        - subtasks: str - the subtasks section if splitting
        - output: str - full output
    """
    output_lower = output.lower()

    should_split = "[split]" in output_lower
    no_split = "[no_split]" in output_lower

    # Extract subtasks section if present
    subtasks = ""
    if should_split and "## subtasks" in output_lower:
        # Find the subtasks section
        import re
        match = re.search(r'## Subtasks\s*\n(.*)', output, re.DOTALL | re.IGNORECASE)
        if match:
            subtasks = match.group(1).strip()

    return {
        "should_split": should_split and not no_split,
        "subtasks": subtasks,
        "output": output,
    }


def apply_task_split(plan_file: str, task: Task, subtasks: str) -> SessionResult:
    """
    Update the PLAN file to replace a task with its subtasks.

    Has Claude do the actual file editing to handle various plan formats.
    """
    logger.info(f"Applying task split for {task.id}")

    prompt = f"""Update the plan file to split task {task.id} into subtasks.

## Plan File
{plan_file}

## Original Task
Task {task.id}: {task.title}

## New Subtasks to Replace It
{subtasks}

## Instructions

1. Read the plan file
2. Find task {task.id} ("{task.title}")
3. Replace it with the subtasks above
4. Keep the same format/style as the rest of the plan
5. Mark the original task {task.id} as replaced (you can add a note or just remove it)
6. The subtasks should be {task.id}.1, {task.id}.2, etc.

Important:
- Preserve the plan file's existing format
- Each subtask needs [ ] checkbox if that's the plan's style
- Include the full description for each subtask
"""

    return run_claude(prompt, model=_resolve_claude_model())


def run_implementer(task: Task, plan_file: str, feedback: Optional[str] = None) -> SessionResult:
    """
    Run a fresh implementer session for a task.

    Args:
        task: The task to implement
        plan_file: Path to the plan file
        feedback: Optional feedback from reviewer to address
    """
    logger.info(f"Running implementer for task {task.id}: {task.title}")

    skill_name = "implement-fix" if feedback else "implement"

    return run_skill(skill_name, {
        "task_id": task.id,
        "task_title": task.title,
        "task_description": task.description,
        "plan_file": plan_file,
        "feedback": feedback or "",
    })


def run_reviewer(plan_file: str, task: Task, overseer_session_id: Optional[str] = None) -> tuple[SessionResult, Optional[str]]:
    """
    Run the reviewer (overseer) to review changes.

    Returns: (result, session_id)
    """
    logger.info("Running reviewer...")

    git_diff = get_git_diff()

    result = run_skill("review", {
        "task_id": task.id,
        "task_title": task.title,
        "plan_file": plan_file,
        "git_diff": git_diff,
    }, session_id=overseer_session_id)

    return result, result.session_id


def parse_review_result(output: str) -> dict:
    """
    Parse the reviewer's output to determine next action.

    Returns dict with:
        - approved: bool
        - has_issues: bool
        - missing_tests: bool
        - suggestions: str
        - critical: bool
    """
    output_lower = output.lower()

    # Check for explicit approval signal - takes precedence over heuristic issue detection
    explicit_approved = "[approved]" in output_lower
    heuristic_approved = (
        "lgtm" in output_lower or
        "looks good" in output_lower
    )

    # Check for explicit issues signal
    explicit_issues = (
        "[issues]" in output_lower or
        "[suggestions]" in output_lower
    )
    # Heuristic issue detection - only used when no explicit approval
    heuristic_issues = (
        "should be" in output_lower or
        "consider" in output_lower or
        "recommend" in output_lower
    )
    # has_issues is true if explicit, or if heuristic AND no explicit approval
    has_issues = explicit_issues or (heuristic_issues and not explicit_approved)

    # Check for missing tests
    missing_tests = (
        "missing test" in output_lower or
        "no test" in output_lower or
        "add test" in output_lower or
        "needs test" in output_lower
    )

    # Check for critical issues
    # Note: Avoid broad matches like "security" which trigger on positive mentions
    # e.g., "Security: ✅ Tenant scope enforced..." should not be flagged
    critical = (
        "[critical]" in output_lower or
        "[blocked]" in output_lower or
        "critical issue" in output_lower or
        "security vulnerability" in output_lower or
        "security risk" in output_lower or
        "security flaw" in output_lower
    )

    # Explicit approval overrides heuristic issues (but not critical)
    # Heuristic approval requires no issues and no critical
    approved = (
        (explicit_approved and not critical) or
        (heuristic_approved and not has_issues and not critical)
    )

    return {
        "approved": approved,
        "has_issues": has_issues,
        "missing_tests": missing_tests,
        "suggestions": output,  # Full output as suggestions
        "critical": critical,
    }


def run_escalater(
    task: Task,
    trigger_source: str,
    trigger_reason: str,
    context: str,
) -> SessionResult:
    """
    Run the escalater to evaluate a trigger and decide next steps.

    Uses Opus for deep reasoning.

    Args:
        task: The current task
        trigger_source: Where the trigger came from (e.g., "implementer", "reviewer")
        trigger_reason: Why the trigger was raised
        context: Full context of the situation

    Returns:
        SessionResult with [PROCEED] or [ESCALATE] decision
    """
    logger.info(f"Running escalater for trigger from {trigger_source}: {trigger_reason}")

    # Use Opus with extended thinking for thorough analysis
    return run_skill(
        "escalate",
        {
            "task_id": task.id,
            "task_title": task.title,
            "trigger_source": trigger_source,
            "trigger_reason": trigger_reason,
            "context": context,
        },
        model="opus",
        append_system_prompt="Use extended thinking to thoroughly analyze this situation before making a decision.",
    )


def parse_escalation_result(output: str) -> dict:
    """
    Parse the escalater's output to determine next action.

    Returns dict with:
        - proceed: bool - True if [PROCEED], False if [ESCALATE]
        - analysis: str - The analysis section
        - action_plan: str - The action plan if proceeding
        - escalation_reason: str - Why escalation is needed if escalating
    """
    output_lower = output.lower()

    proceed = "[proceed]" in output_lower
    escalate = "[escalate]" in output_lower

    return {
        "proceed": proceed and not escalate,
        "escalate": escalate,
        "output": output,
    }


def build_trigger_context(result: SessionResult, fallback_context: str = "") -> str:
    """
    Build context for a trigger, ensuring we capture error details.

    When a session fails, the output may be empty but block_reason has the error.
    This ensures the actual error is visible in blocked context.

    Args:
        result: The SessionResult from a failed session
        fallback_context: Optional fallback context if both output and block_reason are empty
    """
    context_parts = []

    # Include output if present
    if result.output and result.output.strip():
        context_parts.append(result.output.strip())

    # Include block_reason if present (this is where "Prompt is too long" would be)
    if result.block_reason:
        context_parts.append(f"\n**Error**: {result.block_reason}")

    # If we have nothing, use fallback
    if not context_parts:
        return fallback_context or "(No output or error captured)"

    return "\n".join(context_parts)


def is_already_complete(output: str) -> bool:
    """
    Check if implementer found the task was already complete.

    The implementer outputs [ALREADY_COMPLETE] when it detects the task
    has already been implemented in the codebase.
    """
    return "[already_complete]" in output.lower()


def needs_split(output: str) -> bool:
    """
    Check if implementer determined the task needs splitting.

    The implementer outputs [NEEDS_SPLIT] when it assesses the task
    is too large for a single implementation pass.
    """
    return "[needs_split]" in output.lower()


def get_branch_info() -> tuple[str, str]:
    """
    Get current branch and base branch (main or master).

    Returns: (current_branch, base_branch)
    """
    try:
        # Get current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        current_branch = result.stdout.strip()

        # Determine base branch (main or master)
        result = subprocess.run(
            ["git", "branch", "--list", "main", "master"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        branches = result.stdout.strip().split('\n')
        base_branch = "main" if any("main" in b for b in branches) else "master"

        return current_branch, base_branch
    except Exception as e:
        logger.error(f"Error getting branch info: {e}")
        return "HEAD", "main"


def run_polish_fix(suggestions: str, plan_file: str) -> SessionResult:
    """
    Run implementer to address polish suggestions.

    Args:
        suggestions: The polisher's suggestions to address
        plan_file: Path to the plan file

    Returns:
        SessionResult from the fix attempt
    """
    logger.info("Running implementer to address polish suggestions")

    prompt = f"""The polisher has reviewed all changes and has suggestions for improvement.

## Polisher Suggestions

{suggestions}

## Instructions

### For Quick Wins (Implement Now)
1. Implement the quick win fixes directly
2. These should be low-risk, obvious improvements
3. Run tests after to verify nothing broke

### For Deferred Work (Create FIX Files)
1. Create a FIX file in `plans/` directory for each deferred item (create directory if needed)
2. Use this format:

```markdown
# FIX: [Descriptive Title]

**Priority:** High | Medium | Low

## Problem

[Clear description of what needs fixing and why it matters]

## Solution

[Specific implementation approach]

## Files to Modify

- [ ] path/to/file1 - [what to change]
- [ ] path/to/file2 - [what to change]

## Notes

[Any context a future session needs]
```

3. Include enough context that a new session can implement the fix without prior knowledge

### Summary
- Quick wins: implement now
- Deferred work: create FIX files to capture the improvements for later
"""

    return run_claude(prompt, model=_resolve_claude_model())


def run_polish_commit() -> SessionResult:
    """Commit polish improvements (excluding FIX files)."""
    logger.info("Running polish commit...")

    prompt = """The polish improvements have been applied. Please commit them.

1. Stage modified source files (code changes from quick wins)
2. DO NOT stage FIX-*.md files in plans/ - leave them uncommitted so the user can review
3. Create a commit with message: "polish: address review suggestions"
   - In the commit body, briefly list the improvements made
   - Focus on the "why" - what was improved and why it matters

Use this format:
```
polish: address review suggestions

- Brief description of improvement 1
- Brief description of improvement 2
```

IMPORTANT: Leave plans/FIX-*.md files unstaged. The user will review these and decide whether to implement them now or later.
"""

    return run_claude(prompt, model=_resolve_claude_model())


def run_finalizer(plan_file: str) -> SessionResult:
    """
    Run the finalizer to review all changes before merge.

    Uses Opus for thorough final review.

    Args:
        plan_file: Path to the plan file

    Returns:
        SessionResult with [APPROVED] or [ISSUES] decision
    """
    logger.info(f"Running finalizer for {plan_file}")

    # Get branch info
    current_branch, base_branch = get_branch_info()

    # Read plan content
    plan_content = Path(plan_file).read_text()

    # Use Opus with extended thinking for thorough review
    return run_skill(
        "finalize",
        {
            "plan_file": plan_file,
            "current_branch": current_branch,
            "base_branch": base_branch,
            "plan_content": plan_content,
        },
        model="opus",
        append_system_prompt="Use extended thinking to thoroughly review all changes before approving.",
    )


FINALIZE_VERDICTS = ("APPROVED", "SUGGESTIONS", "ISSUES", "CRITICAL")


def parse_finalizer_result(output: str) -> dict:
    """
    Parse the finalizer's output to determine next action.

    The verdict tag MUST be the first non-empty line of the output.
    Anything else is a parse error and the orchestrator treats it as a
    block.

    Returns dict with:
        - verdict: str | None - The parsed tag, uppercased; None on parse error
        - approved: bool - True if [APPROVED]
        - has_issues: bool - True if [ISSUES] found
        - has_suggestions: bool - True if [SUGGESTIONS] found
        - has_critical: bool - True if [CRITICAL] found
        - parse_error: bool - True if the first line is not a known verdict
        - issues: list[str] - List of issues if not approved
        - suggestions: str - Full suggestions text for implementer
        - output: str - Full output
    """
    verdict = _extract_first_line_verdict(output)
    parse_error = verdict is None

    has_issues = verdict == "ISSUES"
    has_suggestions = verdict == "SUGGESTIONS"
    has_critical = verdict == "CRITICAL"
    approved = verdict == "APPROVED"

    issues: list[str] = []
    if has_issues:
        import re
        issue_pattern = r'###\s*Issue\s*\d+[:\s]+(.+?)(?=###\s*Issue|\Z|##\s*Action)'
        matches = re.findall(issue_pattern, output, re.DOTALL | re.IGNORECASE)
        issues = [m.strip() for m in matches if m.strip()]

    return {
        "verdict": verdict,
        "approved": approved,
        "has_issues": has_issues,
        "has_suggestions": has_suggestions,
        "has_critical": has_critical,
        "parse_error": parse_error,
        "issues": issues,
        "suggestions": output if has_suggestions else "",
        "output": output,
    }


def _extract_first_line_verdict(output: str) -> Optional[str]:
    """Return the uppercased verdict tag on the first non-empty line, or None."""
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for tag in FINALIZE_VERDICTS:
            if stripped.upper().startswith(f"[{tag}]"):
                return tag
        return None
    return None


def run_finalizer_fix(issues: str, plan_file: str) -> SessionResult:
    """
    Run implementer to fix issues found by the finalizer.

    Args:
        issues: Description of issues to fix
        plan_file: Path to the plan file

    Returns:
        SessionResult from the fix attempt
    """
    logger.info("Running implementer to fix finalizer issues")

    prompt = f"""The finalizer has reviewed all changes and found issues that need to be addressed.

## Issues to Fix

{issues}

## Instructions

1. Carefully read each issue
2. Fix the problems identified
3. Run any relevant tests to verify your fixes
4. Stage and commit your fixes with a clear commit message

The plan file is: {plan_file}

Focus on addressing the specific issues listed above. Do not make unrelated changes.
"""

    return run_claude(prompt, model=_resolve_claude_model())


CHANGED_FILES_MANIFEST = "changed-files.txt"
SESSION_BASE_HEAD = "session-base-head.txt"
DESLOP_LOG = "deslop-log.md"


def compute_changed_files(base_ref: str = "HEAD") -> list[str]:
    """
    Return files modified since `base_ref` in the current working tree.

    Combines `git diff --name-only <base_ref> HEAD` with working tree
    changes from `git status --porcelain` so the result reflects both
    committed and uncommitted session work.
    """
    changed: set[str] = set()
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if diff.returncode == 0:
            for line in diff.stdout.splitlines():
                line = line.strip()
                if _is_session_source_file(line):
                    changed.add(line)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                if len(line) < 4:
                    continue
                # Porcelain format: "XY path" (with optional rename marker).
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                if _is_session_source_file(path):
                    changed.add(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to compute changed files: {exc}")
    return sorted(changed)


def _is_session_source_file(path: str) -> bool:
    normalized = path.strip()
    return bool(normalized) and not normalized.startswith(".dvx/")


def write_changed_files_manifest(plan_file: str, files: list[str]) -> Path:
    """Persist the session's changed-files list to `.dvx/<plan>/changed-files.txt`."""
    dvx_dir = ensure_dvx_dir(plan_file)
    path = dvx_dir / CHANGED_FILES_MANIFEST
    path.write_text("\n".join(files) + ("\n" if files else ""))
    return path


def write_session_base_head(plan_file: str, head_sha: str) -> Path:
    """Persist the starting HEAD for the current orchestration session."""
    dvx_dir = ensure_dvx_dir(plan_file)
    path = dvx_dir / SESSION_BASE_HEAD
    path.write_text(head_sha.strip() + "\n" if head_sha.strip() else "")
    return path


def load_session_base_head(plan_file: str) -> str:
    """Load the starting HEAD for the current orchestration session."""
    path = get_dvx_dir(plan_file) / SESSION_BASE_HEAD
    if not path.exists():
        return ""
    return path.read_text().strip()


def load_changed_files_manifest(plan_file: str) -> list[str]:
    """Load a previously-written changed-files manifest, or [] if missing."""
    path = get_dvx_dir(plan_file) / CHANGED_FILES_MANIFEST
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def is_deslop_noop(output: str) -> bool:
    """Return True if the deslop skill reported a no-op."""
    return "[DESLOP_NOOP]" in (output or "").upper()


def _record_finalize_verdict(plan_file: str, verdict: str, iteration: int) -> None:
    """Persist the finalize verdict to state."""
    state = load_state(plan_file)
    if state is None:
        logger.warning(f"Cannot record finalize verdict; no state for {plan_file}")
        return
    state.finalize_verdict = verdict
    state.finalize_iterations = iteration
    save_state(state)


def run_deslop(changed_files: list[str], plan_file: str) -> SessionResult:
    """Run the deslop skill on the session's changed files."""
    logger.info(f"Running deslop on {len(changed_files)} file(s)")
    return run_skill(
        "deslop",
        {
            "changed_files": "\n".join(changed_files),
            "plan_file": plan_file,
        },
        model="opus",
    )


def _run_deslop_pass(plan_file: str, state: State) -> None:
    """
    Run the deslop cleanup after the finalizer has approved the run.

    - Scope is strictly the session's changed files.
    - If post-deslop regression tests fail, the last cleanup commit is
      reverted and the failure is logged; the run still proceeds to
      completion because the pre-deslop state was already APPROVED.
    """
    print()
    print("Running deslop pass on session-changed files...")

    changed = load_changed_files_manifest(plan_file)
    if not changed:
        base_head = load_session_base_head(plan_file) or "HEAD"
        changed = compute_changed_files(base_ref=base_head)
        if changed:
            write_changed_files_manifest(plan_file, changed)

    if not changed:
        print("  No changed files to deslop.")
        state.deslop_run = True
        save_state(state)
        return

    before_head = _current_head_sha()
    result = run_deslop(changed, plan_file)

    if not result.success:
        logger.warning(f"Deslop skill failed: {result.block_reason or 'unknown error'}")
        print("  Deslop skill failed; leaving files untouched.")
        state.deslop_run = True
        save_state(state)
        return

    if is_deslop_noop(result.output):
        print("  Deslop reported no-op — nothing worth cleaning.")
        state.deslop_run = True
        save_state(state)
        return

    after_head = _current_head_sha()
    if before_head == after_head:
        logger.info("Deslop skill ran but produced no new commits")

    # Regression check: run project tests if we can detect how.
    tests_ok, tests_report = _run_regression_tests()
    if not tests_ok:
        logger.warning(f"Deslop regression test failed: {tests_report}")
        print(f"  Regression after deslop: {tests_report}")
        _revert_last_commit(before_head)
        _log_deslop_skip(plan_file, changed, tests_report)
        state.deslop_run = True
        state.deslop_skipped_files = list(changed)
        save_state(state)
        return

    print("  Deslop complete; regression tests pass.")
    state.deslop_run = True
    save_state(state)


def _current_head_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _run_regression_tests() -> tuple[bool, str]:
    """
    Best-effort regression check after deslop.

    Tries `pytest -q`, then `npm test --silent`, then `go test ./...`.
    If none are applicable, returns (True, "no test command detected").
    """
    commands: list[list[str]] = []
    if Path("pyproject.toml").exists() or Path("pytest.ini").exists() or Path("tests").is_dir():
        commands.append(["pytest", "-q"])
    if Path("package.json").exists():
        commands.append(["npm", "test", "--silent"])
    if Path("go.mod").exists():
        commands.append(["go", "test", "./..."])

    if not commands:
        return True, "no test command detected"

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if result.returncode != 0:
                return False, f"{' '.join(cmd)} exited {result.returncode}"
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            return False, f"{' '.join(cmd)} raised: {exc}"

    return True, "tests passed"


def _revert_last_commit(before_sha: str) -> None:
    """Reset the last commit(s) back to `before_sha` — soft reset, keep files."""
    if not before_sha:
        return
    try:
        subprocess.run(
            ["git", "reset", "--hard", before_sha],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to revert deslop commit: {exc}")


def _log_deslop_skip(plan_file: str, files: list[str], reason: str) -> None:
    dvx_dir = ensure_dvx_dir(plan_file)
    log_path = dvx_dir / DESLOP_LOG
    entry = (
        f"## {datetime_now_iso()}\n\n"
        f"Skipped deslop on {len(files)} file(s) — {reason}.\n\n"
        "Files:\n"
        + "\n".join(f"- `{f}`" for f in files)
        + "\n\n---\n"
    )
    if log_path.exists():
        log_path.write_text(log_path.read_text() + entry)
    else:
        log_path.write_text(f"# Deslop log: {plan_file}\n\n{entry}")


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def cleanup_plan(plan_file: str) -> bool:
    """
    Finalize the plan by committing any pending changes and marking complete.

    The plan file is LEFT IN PLACE so users can review that all tasks were done.
    Preserves the .dvx/{plan}/ directory with DECISIONS files for reference.

    Args:
        plan_file: Path to the plan file

    Returns:
        True if cleanup succeeded, False otherwise
    """
    logger.info(f"Finalizing completed plan: {plan_file}")

    try:
        # Check for any uncommitted changes (finalizer may have made fixes)
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if status_result.stdout.strip():
            # There are uncommitted changes - commit them
            logger.info("Committing pending changes from finalization...")

            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            result = subprocess.run(
                ["git", "commit", "-m", f"Complete {plan_file}\n\nAll tasks in the plan have been implemented, reviewed, and finalized.\n\n🤖 Generated with dvx"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"Commit failed: {result.stderr}")
            else:
                logger.info("Committed finalization changes")
        else:
            logger.info("No pending changes to commit")

        # Update state to complete (preserve .dvx directory with DECISIONS)
        update_phase(Phase.COMPLETE, plan_file)
        logger.info(f"Plan finalized. State and DECISIONS preserved in {get_dvx_dir(plan_file)}")

        return True

    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        return False


def evaluate_trigger(
    state: State,
    task: Task,
    trigger_source: str,
    trigger_reason: str,
    context: str,
    session_id: Optional[str] = None,
) -> tuple[bool, Optional[int]]:
    """
    Evaluate a trigger using the escalater.

    Args:
        state: Current orchestrator state
        task: The current task
        trigger_source: Where the trigger came from
        trigger_reason: Why the trigger was raised
        context: Full context
        session_id: Optional session ID from the triggering session

    Returns:
        (should_continue, exit_code) - if should_continue is True, proceed with orchestration
        if False, exit_code is the return code (1 for blocked)
    """
    print(f"  Evaluating trigger from {trigger_source}...")

    result = run_escalater(task, trigger_source, trigger_reason, context)

    if not result.success:
        # Escalater itself failed - fall back to blocking with full context
        blocked_context = f"""## Task

**{task.id}**: {task.title}

{task.description}

## Trigger

**Source**: {trigger_source}
**Reason**: {trigger_reason}

## Original Context

{context}

## Escalater Failure

{result.block_reason or 'unknown error'}
"""
        return False, handle_blocked(
            state,
            f"Escalater failed: {result.block_reason or 'unknown error'}",
            blocked_context,
            session_id=session_id,
        )

    decision = parse_escalation_result(result.output)

    if decision["proceed"]:
        print("  Escalater decided to proceed.")
        logger.info(f"Escalater proceeding: {trigger_reason}")
        # Log the escalater's decision
        log_decisions_from_output(result.output, state.plan_file)
        return True, None

    # Escalater decided to escalate to human
    print("  Escalater decided to escalate to human.")

    # Build comprehensive blocked context for human review
    blocked_context = f"""## Task

**{task.id}**: {task.title}

{task.description}

## Trigger

**Source**: {trigger_source}
**Reason**: {trigger_reason}

## Original Context

{context}

## Escalater Analysis

{result.output if result.output else "(No analysis output captured)"}
"""

    return False, handle_blocked(
        state,
        f"Escalated by escalater: {trigger_reason}",
        blocked_context,
        session_id=session_id,
    )


def run_implementer_commit(task: Task, plan_file: str) -> SessionResult:
    """Tell the implementer to update the plan and commit."""
    logger.info("Running implementer commit...")

    return run_skill("commit-task", {
        "task_id": task.id,
        "task_title": task.title,
        "plan_file": plan_file,
    })


def handle_blocked(state: State, reason: str, context: str, session_id: Optional[str] = None) -> int:
    """
    Handle a blocked state - write context for review.

    Args:
        state: Current orchestrator state
        reason: Why we're blocked
        context: Full context of the blockage
        session_id: Optional session ID from the blocking Claude session (overrides state.overseer_session_id)
    """
    logger.warning(f"Blocked: {reason}")

    update_phase(Phase.BLOCKED, state.plan_file)
    # Use provided session_id, fall back to overseer session
    session_id = session_id or state.overseer_session_id
    blocked_file = write_blocked_context(reason, context, state.plan_file, session_id=session_id)

    print()
    print("=" * 60)
    print("BLOCKED")
    print("=" * 60)
    print(f"Reason: {reason}")
    print()
    print(f"Context written to: {blocked_file}")
    print()
    print(f"Run `dvx run {state.plan_file}` and when resolved type `/exit` - dvx will continue.")
    print()
    print(f"dvx is blocked on {state.plan_file}.")
    return 1  # Exit with error to signal blocked state


def run_orchestrator(
    plan_file: str,
    step_mode: bool = False,
    no_deslop: bool = False,
    model: Optional[str] = None,
) -> int:
    """
    Main orchestration loop.

    Args:
        plan_file: Path to the plan file
        step_mode: If True, pause after each task completion for review
        no_deslop: If True, skip the post-approval deslop cleanup pass
        model: Optional model override for every Claude call in this run

    Returns: 0 on success/completion, 1 on error/blocked/paused
    """
    try:
        selected_model = resolve_claude_model(model)
        with claude_model_override(selected_model):
            return _run_orchestrator_inner(plan_file, step_mode, no_deslop=no_deslop)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        print(f"\nInterrupted. Run 'dvx run {plan_file}' to resume.")
        return 1
    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        print(f"\nError: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        print(f"\nUnexpected error: {e}")
        print("Check .dvx/dvx.log for details.")
        return 1


def _run_finalization(plan_file: str, state: State, no_deslop: bool = False) -> int:
    """
    Run the finalization process after all tasks are complete.

    Single-pass flow: the finalizer reviews all changes and returns one of:
    - [APPROVED] — ready for merge
    - [SUGGESTIONS] — optional improvements to apply, then re-review
    - [ISSUES] — bugs/failures that block merge, fix then re-review

    The plan file is LEFT IN PLACE so users can review that all tasks were done.

    Args:
        plan_file: Path to the plan file
        state: Current orchestrator state

    Returns:
        0 on success, 1 on error/blocked
    """
    summary = get_plan_summary(plan_file)

    print()
    print("=" * 60)
    print("FINALIZING")
    print("=" * 60)
    print(f"All {summary['total']} tasks completed!")
    print()

    update_phase(Phase.FINALIZING, plan_file)

    # === FINALIZE: Single review loop ===
    print("Running final review...")

    max_finalizer_iterations = 3
    iteration = 0

    while iteration < max_finalizer_iterations:
        iteration += 1

        # Run the finalizer
        print(f"  Final review (attempt {iteration}/{max_finalizer_iterations})...")
        finalizer_result = run_finalizer(plan_file)

        if not finalizer_result.success:
            # Finalizer itself failed - use escalater
            should_continue, exit_code = evaluate_trigger(
                state,
                Task(id="finalizer", title="Final review", description="", status=TaskStatus.DONE),
                "finalizer",
                f"Finalizer failed: {finalizer_result.block_reason or 'unknown error'}",
                build_trigger_context(finalizer_result),
                session_id=finalizer_result.session_id,
            )
            if not should_continue:
                return exit_code
            # Escalater decided to proceed - treat as approved
            _record_finalize_verdict(plan_file, "APPROVED", iteration)
            break

        result = parse_finalizer_result(finalizer_result.output)

        if result["parse_error"]:
            msg = (
                "Finalizer output did not start with a recognized verdict tag "
                "([APPROVED] / [SUGGESTIONS] / [ISSUES] / [CRITICAL])."
            )
            _record_finalize_verdict(plan_file, "BLOCKED", iteration)
            return handle_blocked(
                state,
                msg,
                f"{msg}\n\n---\n\nFull finalizer output:\n\n{finalizer_result.output}",
                session_id=finalizer_result.session_id,
            )

        if result["has_critical"]:
            _record_finalize_verdict(plan_file, "CRITICAL", iteration)
            return handle_blocked(
                state,
                "Finalizer flagged a CRITICAL issue that cannot auto-resolve.",
                finalizer_result.output,
                session_id=finalizer_result.session_id,
            )

        if result["approved"]:
            print("  Finalizer approved all changes!")
            _record_finalize_verdict(plan_file, "APPROVED", iteration)
            break

        if result["has_suggestions"]:
            print("  Finalizer has suggestions - implementing improvements...")
            logger.info("Finalizer found suggestions to address")

            fix_result = run_polish_fix(result["suggestions"], plan_file)

            if not fix_result.success or fix_result.blocked:
                reason = fix_result.block_reason or "Polish fix implementation failed"
                should_continue, exit_code = evaluate_trigger(
                    state,
                    Task(id="finalizer-fix", title="Address finalizer suggestions", description="", status=TaskStatus.IN_PROGRESS),
                    "implementer",
                    reason,
                    build_trigger_context(fix_result),
                    session_id=fix_result.session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided to proceed - re-run finalizer

            # Log any decisions made during fix
            log_decisions_from_output(fix_result.output, plan_file)

            # Commit the polish improvements
            print("  Committing improvements...")
            commit_result = run_polish_commit()

            if not commit_result.success or commit_result.blocked:
                reason = commit_result.block_reason or "Polish commit failed"
                should_continue, exit_code = evaluate_trigger(
                    state,
                    Task(id="finalizer-commit", title="Commit improvements", description="", status=TaskStatus.IN_PROGRESS),
                    "implementer",
                    reason,
                    build_trigger_context(commit_result),
                    session_id=commit_result.session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided to proceed - re-run finalizer

            print("  Improvements committed!")
            # Loop back to run finalizer again
            continue

        if result["has_issues"]:
            print("  Finalizer found issues - running fixes...")
            logger.info(f"Finalizer found {len(result['issues'])} issues")

            # Run implementer to fix the issues
            fix_result = run_finalizer_fix(result["output"], plan_file)

            if not fix_result.success or fix_result.blocked:
                reason = fix_result.block_reason or "Fix implementation failed"
                should_continue, exit_code = evaluate_trigger(
                    state,
                    Task(id="finalizer-fix", title="Fix finalizer issues", description="", status=TaskStatus.IN_PROGRESS),
                    "implementer",
                    reason,
                    build_trigger_context(fix_result),
                    session_id=fix_result.session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided to proceed - continue to re-run finalizer

            # Log any decisions made during fix
            log_decisions_from_output(fix_result.output, plan_file)

            # Loop back to run finalizer again
            continue

        # If we get here, finalizer output was unclear - treat as approved
        logger.warning("Finalizer output unclear, treating as approved")
        break

    else:
        # Exceeded max iterations — treat as terminal block; do not mark
        # the plan as complete just because the orchestrator ran out of
        # tries. The user decides whether to accept the current state.
        print(f"  Max finalizer iterations ({max_finalizer_iterations}) reached")
        _record_finalize_verdict(plan_file, "BLOCKED", max_finalizer_iterations)
        return handle_blocked(
            state,
            f"Finalizer fix loop exceeded {max_finalizer_iterations} iterations without APPROVED",
            "The finalizer and implementer could not converge on an approved state.",
        )

    # === DESLOP PASS (post-APPROVED only) ===
    if no_deslop:
        logger.info("Skipping deslop pass (--no-deslop)")
    else:
        _run_deslop_pass(plan_file, state)

    # === FINALIZE ===
    print()
    print("  Finalizing plan...")

    if cleanup_plan(plan_file):
        dvx_dir = get_dvx_dir(plan_file)
        print()
        print("=" * 60)
        print("COMPLETE")
        print("=" * 60)
        print(f"Plan {plan_file} successfully completed!")
        print()
        print(f"Plan file kept for review: {plan_file}")
        print(f"State and DECISIONS preserved in: {dvx_dir}")
        print()
        print("The branch is ready for merge.")
        print()
        print(f"To clean up after merge: dvx clean {plan_file}")
        print()
        print(f"dvx successfully completed {plan_file}")
        return 0
    else:
        print("  Warning: Finalization encountered issues, but plan is complete.")
        update_phase(Phase.COMPLETE, plan_file)
        return 0


def _run_orchestrator_inner(plan_file: str, step_mode: bool = False, no_deslop: bool = False) -> int:
    """Inner orchestration loop (wrapped by run_orchestrator for error handling)."""
    # Ensure .dvx directory exists for this plan
    ensure_dvx_dir(plan_file)

    # Load or create state
    state = load_state(plan_file)
    if state is None:
        state = create_initial_state(plan_file)
        state.step_mode = step_mode
        save_state(state)
    elif step_mode and not state.step_mode:
        # Enable step mode if requested
        state.step_mode = step_mode
        save_state(state)

    # Freeze the pre-run HEAD so deslop can later recover files that were
    # committed during this orchestration session.
    if not load_session_base_head(plan_file):
        write_session_base_head(plan_file, _current_head_sha())

    logger.info(f"Starting orchestration of {plan_file}")

    while True:
        # Get next task
        task = get_next_pending_task(plan_file)
        if task is None:
            # Check if we're done
            summary = get_plan_summary(plan_file)
            if summary['pending'] == 0 and summary['in_progress'] == 0:
                # All tasks complete - run finalizer
                return _run_finalization(plan_file, state, no_deslop=no_deslop)
            else:
                logger.warning("No pending tasks but plan not complete - check for blocked tasks")
                return 1

        print()
        print(f"Task {task.id}: {task.title}")
        print("-" * 60)

        # === TASK SAFETY CHECK ===
        is_safe, safety_warning = validate_task_safety(task)
        if not is_safe:
            print(f"  BLOCKED: {safety_warning}")
            print("  This task contains forbidden operations (merge/deploy).")
            print("  Skipping - please remove or modify this task manually.")
            logger.warning(f"Task {task.id} blocked for safety: {safety_warning}")

            # Mark as blocked and continue to next task
            update_task_status(plan_file, task.id, TaskStatus.BLOCKED)
            continue

        # Set current task
        set_current_task(task.id, task.title, plan_file)
        update_task_status(plan_file, task.id, TaskStatus.IN_PROGRESS)

        # === IMPLEMENTATION PHASE ===
        update_phase(Phase.IMPLEMENTING, plan_file)
        print(f"  Implementing task {task.id}...")
        logger.info(f"Implementing task {task.id}")

        impl_result = run_implementer(task, plan_file)

        # Log any decisions made during implementation
        log_decisions_from_output(impl_result.output, plan_file)

        # Check if task was already complete (short-circuit)
        if is_already_complete(impl_result.output):
            print(f"  Task {task.id} already complete - skipping to next task")
            logger.info(f"Task {task.id} detected as already complete")
            update_task_status(plan_file, task.id, TaskStatus.DONE)

            # Reset iteration count for next task
            state = load_state(plan_file)
            state.iteration_count = 0
            save_state(state)

            # Continue to next task (skip review/fix/test/commit)
            continue

        # Check if implementer determined the task needs splitting
        if needs_split(impl_result.output):
            print(f"  Task {task.id} needs splitting - running task splitter...")
            logger.info(f"Task {task.id} flagged as needing split by implementer")

            split_result = run_task_splitter(task, plan_file)

            if split_result.success:
                split_analysis = parse_split_result(split_result.output)

                if split_analysis["should_split"] and split_analysis["subtasks"]:
                    print(f"  Splitting task {task.id} into subtasks...")
                    apply_result = apply_task_split(plan_file, task, split_analysis["subtasks"])

                    if apply_result.success:
                        print("  Plan updated with subtasks. Restarting with first subtask...")
                        update_task_status(plan_file, task.id, TaskStatus.DONE)

                        from plan_parser import clear_cache
                        clear_cache()

                        continue
                    else:
                        logger.warning(f"Failed to apply task split: {apply_result.block_reason}")
                        print("  Failed to update plan - proceeding with review")
                else:
                    logger.info("Splitter decided task doesn't need splitting after all")
                    print("  Splitter determined task is appropriately scoped - proceeding with review")
            else:
                logger.warning(f"Task splitter failed: {split_result.block_reason}")
                print("  Split analysis failed - proceeding with review")

        if not impl_result.success or impl_result.blocked:
            reason = impl_result.block_reason or ("Implementation failed" if not impl_result.success else "Implementer is blocked")
            should_continue, exit_code = evaluate_trigger(
                state, task, "implementer", reason,
                build_trigger_context(impl_result), session_id=impl_result.session_id,
            )
            if not should_continue:
                return exit_code
            # Escalater decided to proceed - continue to review

        # === REVIEW PHASE ===
        update_phase(Phase.REVIEWING, plan_file)
        logger.info("Reviewing implementation...")

        # Check for massive deletions that require human review
        change_stats = get_change_stats()
        if change_stats['is_massive']:
            insertions = change_stats['insertions']
            deletions = change_stats['deletions']
            ratio = deletions / (insertions or 1)
            print(f"  ⚠️  Large deletion detected: {deletions:,} deletions vs {insertions:,} additions ({ratio:.1f}x)")

            massive_context = f"""## Large Deletion Detected

This task deleted significantly more code than it added, which requires human verification.

### Why This Was Blocked

| Metric | Value |
|--------|-------|
| Files deleted | {change_stats['files_deleted']} |
| Lines added | {insertions:,} |
| Lines deleted | {deletions:,} |
| Deletion ratio | {ratio:.1f}x |

> **Policy**: Large additions and refactoring (similar add/delete counts) are auto-approved.
> Large deletions without corresponding additions require human review to prevent accidental code loss.

### Change Summary

```
{change_stats['summary']}
```

### Verification Checklist (Must Complete)

1. [ ] **Deletions match task**: Every deleted file/function is mentioned in the task description
2. [ ] **No collateral damage**: Only files related to this task were modified
3. [ ] **Build passes**: `make build` or `go build ./...`
4. [ ] **Tests pass**: `make test` or `go test ./...`

### Recommendation

Do NOT commit until you've verified the deletions are intentional. If any deleted code seems unrelated to the task, investigate before proceeding.

### Next Steps

Once verified, commit and mark complete:
```bash
git add -A
git commit -m "Task {task.id}: {task.title}"

# Update task status
cat > .dvx/task-status.json << 'EOF'
# Edit to mark task {task.id} as "done"
EOF
```

Then type `/exit` to continue orchestration.
"""
            return handle_blocked(
                state,
                f"Large deletion requires human review ({deletions:,} deletions, {ratio:.1f}x ratio)",
                massive_context,
            )

        review_result, session_id = run_reviewer(
            plan_file,
            task,
            state.overseer_session_id,
        )

        if session_id:
            set_overseer_session(session_id, plan_file)

        if not review_result.success:
            should_continue, exit_code = evaluate_trigger(
                state, task, "reviewer", "Review failed",
                build_trigger_context(review_result), session_id=review_result.session_id,
            )
            if not should_continue:
                return exit_code
            # Escalater decided to proceed - treat as approved

        # Check if reviewer hit prompt limits
        if "prompt is too long" in review_result.output.lower():
            logger.warning("Reviewer hit prompt limit - treating as massive change")
            change_stats = get_change_stats()
            total_lines = change_stats['insertions'] + change_stats['deletions']

            overflow_context = f"""## Review Failed: Prompt Too Long

The reviewer could not process this task because the changes are too large for automated review.

### Why This Was Blocked

| Metric | Value |
|--------|-------|
| Files changed | {change_stats['files_changed']} |
| Files deleted | {change_stats['files_deleted']} |
| Lines changed | {total_lines:,} |

This exceeded the context limit for automated review. Manual verification is required.

### Change Summary

```
{change_stats['summary']}
```

### Verification Checklist (Must Complete)

1. [ ] **Changes match task**: Every modification aligns with the task description
2. [ ] **No unintended changes**: Only files related to this task were modified
3. [ ] **Build passes**: `make build` or `go build ./...`
4. [ ] **Tests pass**: `make test` or `go test ./...`

### Recommendation

Review the `git diff --stat` output above. If any files seem unrelated to the task, investigate with `git diff <file>` before committing.

### Next Steps

Once verified, commit and mark complete:
```bash
git add -A
git commit -m "Task {task.id}: {task.title}"

# Update task status to mark task {task.id} as "done"
```

Then type `/exit` to continue orchestration.
"""
            return handle_blocked(
                state,
                f"Changes too large for automated review ({change_stats['files_changed']} files)",
                overflow_context,
            )

        review = parse_review_result(review_result.output)

        # === FIX PHASE (if needed) ===
        iteration = 0
        while review['has_issues'] and not review['approved']:
            iteration += 1
            state, exceeded = increment_iteration(plan_file)

            if exceeded:
                should_continue, exit_code = evaluate_trigger(
                    state, task, "orchestrator",
                    f"Max iterations ({state.max_iterations}) exceeded - review loop not converging",
                    f"Last review feedback:\n{review['suggestions']}",
                    session_id=session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided to allow more iterations - reset and continue
                state.iteration_count = 0
                save_state(state)

            if review['critical']:
                should_continue, exit_code = evaluate_trigger(
                    state, task, "reviewer", "Critical issue found in review",
                    review['suggestions'], session_id=session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided critical issue is not blocking - continue

            print(f"  Review iteration {iteration}: addressing feedback...")
            update_phase(Phase.FIXING, plan_file)

            # Run implementer with feedback
            impl_result = run_implementer(task, plan_file, feedback=review['suggestions'])

            # Log any decisions made during fix
            log_decisions_from_output(impl_result.output, plan_file)

            if not impl_result.success or impl_result.blocked:
                reason = impl_result.block_reason or "Fix implementation failed"
                should_continue, exit_code = evaluate_trigger(
                    state, task, "implementer", reason,
                    build_trigger_context(impl_result), session_id=impl_result.session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided to proceed - continue to re-review

            # Re-review
            update_phase(Phase.REVIEWING, plan_file)
            review_result, session_id = run_reviewer(
                plan_file,
                task,
                state.overseer_session_id,
            )

            if session_id:
                set_overseer_session(session_id, plan_file)

            review = parse_review_result(review_result.output)

        # === TEST PHASE ===
        if review['missing_tests']:
            print("  Adding missing tests...")
            update_phase(Phase.TESTING, plan_file)

            test_result = run_skill("add-tests", {
                "task_id": task.id,
                "task_title": task.title,
                "reviewer_notes": review['suggestions'],
            })

            if not test_result.success or test_result.blocked:
                reason = test_result.block_reason or "Test writing failed"
                should_continue, exit_code = evaluate_trigger(
                    state, task, "implementer", reason,
                    build_trigger_context(test_result), session_id=test_result.session_id,
                )
                if not should_continue:
                    return exit_code
                # Escalater decided to proceed without tests

        # === COMMIT PHASE ===
        print("  Committing changes...")
        update_phase(Phase.COMMITTING, plan_file)

        commit_result = run_implementer_commit(task, plan_file)

        if not commit_result.success or commit_result.blocked:
            reason = commit_result.block_reason or "Commit failed"
            should_continue, exit_code = evaluate_trigger(
                state, task, "implementer", reason,
                build_trigger_context(commit_result), session_id=commit_result.session_id,
            )
            if not should_continue:
                return exit_code
            # Escalater decided commit issue is not blocking - mark task done anyway

        # Mark task as done
        update_task_status(plan_file, task.id, TaskStatus.DONE)
        print(f"  Task {task.id} complete!")

        # Reset iteration count for next task
        state = load_state(plan_file)
        state.iteration_count = 0
        save_state(state)

        # Step mode: pause after each task for review
        if state.step_mode:
            print()
            print("=" * 60)
            print("PAUSED (step mode)")
            print("=" * 60)
            print(f"Task {task.id} ({task.title}) completed and committed.")
            print()
            print(f"Review the changes, then run 'dvx run {plan_file}' for next task.")
            print()
            update_phase(Phase.PAUSED, plan_file)
            return 0  # Exit cleanly to allow review

        # Continue to next task...


if __name__ == "__main__":
    # For testing
    import sys
    if len(sys.argv) > 1:
        run_orchestrator(sys.argv[1])
    else:
        print("Usage: python orchestrator.py <plan-file>")
