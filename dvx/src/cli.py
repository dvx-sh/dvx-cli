#!/usr/bin/env python3
"""
dvx - Development Orchestrator CLI.

Automates the implement → review → test → commit development loop.
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from autopilot import (
    AutopilotPlan,
    build_plan_from_args,
    plan_artifact_exists,
    run_pipeline,
    write_autopilot_summary,
)
from autopilot import (
    summarize as summarize_autopilot,
)
from claude_session import (
    check_claude_model_available,
    claude_model_override,
    launch_interactive,
    resolve_command_model,
    start_session,
)
from consensus import (
    MAX_ITERATIONS,
    make_skill_caller,
    render_no_consensus_summary,
    run_consensus,
    validate_plan,
)
from context import load_latest_content, slug_from, slug_from_plan_file
from goals import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TODO_DIR,
    clear_goal_state,
    run_goal_watch,
)
from interview import (
    PROFILE_THRESHOLDS,
    get_profile,
    load_spec,
    render_transcript,
    validate_spec,
)
from interview import (
    load_state as load_interview_state,
)
from interview import (
    new_state as new_interview_state,
)
from interview import (
    save_state as save_interview_state,
)
from interview import (
    spec_path as interview_spec_path,
)
from orchestrator import load_skill, run_orchestrator, run_skill
from plan_parser import (
    clear_status_for_plan,
    get_next_pending_task,
    get_plan_summary,
    sync_plan_state,
)
from state import (
    Phase,
    clear_blocked,
    get_decisions,
    get_dvx_dir,
    get_dvx_root,
    load_state,
    save_state,
    update_phase,
)

# Skills directory in the package
SKILLS_DIR = Path(__file__).parent / "skills"


def _selected_model_from_args(args) -> str:
    return resolve_command_model(getattr(args, "model", None))


def _check_selected_model(model: str) -> tuple[bool, str]:
    return check_claude_model_available(model)


def ensure_skills_installed(
    skills_dir: Optional[Path] = None,
    commands_dir: Optional[Path] = None,
) -> None:
    """
    Install dvx skills to ~/.claude/commands/dvx/.

    This copies all skill files from the package to the Claude Code commands
    directory, making them available as /dvx:* commands. Skills that no longer
    exist in the package are removed so deleted skills don't linger as
    /dvx:* commands.
    """
    import shutil

    if skills_dir is None:
        skills_dir = SKILLS_DIR
    if commands_dir is None:
        commands_dir = Path.home() / ".claude" / "commands" / "dvx"

    if not skills_dir.exists():
        return

    commands_dir.mkdir(parents=True, exist_ok=True)

    # Copy all skill files (except templates)
    installed_names = set()
    for skill_file in skills_dir.glob("*.md"):
        if skill_file.name.startswith("_"):
            continue  # Skip template files
        installed_names.add(skill_file.name)
        target = commands_dir / skill_file.name
        # Only copy if source is newer or target doesn't exist
        if not target.exists() or skill_file.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(skill_file, target)

    # Prune skills removed from the package
    for installed in commands_dir.glob("*.md"):
        if installed.name not in installed_names:
            installed.unlink()


def get_user_input_from_editor() -> str:
    """Open $EDITOR to get user input, return the text."""
    import os
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Describe your plan\n\n# Delete this line and write your plan description here.\n# Save and exit when done.\n")
        temp_path = f.name

    try:
        subprocess.run([editor, temp_path], check=True)
        with open(temp_path, "r") as f:
            content = f.read()
        # Remove the template lines if unchanged
        lines = [line for line in content.split("\n") if not line.startswith("#")]
        return "\n".join(lines).strip()
    finally:
        os.unlink(temp_path)


def cmd_plan(args) -> int:
    """
    Generate or update a plan file using Claude Code.

    Input can be piped or entered via editor.
    If plan_file is provided, uses that name; otherwise Claude suggests one.
    """
    import sys

    plan_file = args.plan_file if hasattr(args, "plan_file") and args.plan_file else None

    # Get input: piped or editor
    if not sys.stdin.isatty():
        # Input is piped
        user_input = sys.stdin.read().strip()
    else:
        # Open editor
        print("Opening editor to capture plan description...")
        user_input = get_user_input_from_editor()

    if not user_input:
        print("Error: No input provided.")
        return 1

    model = _selected_model_from_args(args)
    ok, error = _check_selected_model(model)
    if not ok:
        print(f"Error: {error}")
        return 1

    print("Generating plan with Claude...")
    print()

    # Check if updating existing file
    existing_content = ""
    if plan_file and Path(plan_file).exists():
        existing_content = Path(plan_file).read_text()
        action = "update"
    else:
        action = "create"

    snapshot_content = ""
    snapshot_path = getattr(args, "snapshot", None)
    if snapshot_path:
        snapshot_file = Path(snapshot_path)
        if not snapshot_file.exists():
            print(f"Error: Snapshot file not found: {snapshot_path}")
            return 1
        snapshot_content = snapshot_file.read_text()

    interview_spec_content = ""
    if plan_file:
        slug_for_plan = slug_from_plan_file(plan_file)
        if not snapshot_content:
            snapshot_content = load_latest_content(slug_for_plan) or ""
        spec_body = load_spec(slug_for_plan)
        if spec_body:
            interview_spec_content = spec_body
            print(f"Using interview spec: {interview_spec_path(slug_for_plan)}")

    # --consensus: run Planner → Architect → Critic loop and write the result.
    if getattr(args, "consensus", False):
        if action == "update":
            print("Error: --consensus is for new plans; use `dvx plan` without --consensus to update.")
            return 1

        print(f"Running consensus planning (up to {MAX_ITERATIONS} iterations)...")
        print()

        skill_caller = make_skill_caller(run_skill, model=model)
        try:
            result = run_consensus(
                task=user_input,
                call_skill=skill_caller,
                snapshot_content=snapshot_content,
                interview_spec=interview_spec_content,
            )
        except RuntimeError as exc:
            print(f"Error: consensus loop failed - {exc}")
            return 1

        if not plan_file:
            plan_file = _derive_plan_filename_from_task(user_input)

        missing = validate_plan(result.final_plan)
        if missing:
            print(f"Warning: final plan is missing required sections: {missing}")

        Path(plan_file).write_text(result.final_plan.rstrip() + "\n")
        verdict = "APPROVED" if result.approved else "NO-CONSENSUS"
        print(f"{verdict}: wrote {plan_file} after {len(result.iterations)} iteration(s)")
        if not result.approved:
            print(render_no_consensus_summary(result))
        return 0

    # Use skills instead of inline prompts
    if action == "update":
        result = run_skill("update-plan", {
            "plan_file": plan_file,
            "changes": user_input,
            "existing_content": existing_content,
        }, model=model)
    else:
        result = run_skill("create-plan", {
            "requirements": user_input,
            "output_file": plan_file or "",
            "snapshot_content": snapshot_content,
            "interview_spec": interview_spec_content,
        }, model=model)

    if not result.success:
        print(f"Error: Claude failed - {result.block_reason or 'unknown error'}")
        return 1

    output = result.output.strip()

    # Extract filename if Claude suggested one
    if not plan_file:
        lines = output.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("FILENAME:"):
                plan_file = line.replace("FILENAME:", "").strip()
                # Remove the FILENAME line from output
                output = "\n".join(lines[:i]).strip()
                break

        if not plan_file:
            plan_file = "PLAN-new.md"

    # Write the plan file
    Path(plan_file).write_text(output + "\n")
    print(f"{'Updated' if action == 'update' else 'Created'}: {plan_file}")

    # Show summary
    line_count = len(output.split("\n"))
    print(f"  {line_count} lines")

    return 0


def _derive_plan_filename_from_task(task: str) -> str:
    """Return a `PLAN-<slug>.md` filename derived from a task description."""
    return f"PLAN-{slug_from(task)}.md"


def _autopilot_interview_phase(plan: AutopilotPlan, project_dir: Optional[str]) -> int:
    if plan.skip_interview:
        print("[autopilot] skipping interview phase (--skip-interview)")
        return 0
    from types import SimpleNamespace

    interview_args = SimpleNamespace(
        task=plan.task,
        profile="standard",
        slug=plan.slug,
        model=getattr(plan, "model", None),
    )
    print("[autopilot] phase: interview")
    return cmd_interview(interview_args)


def _autopilot_planning_phase(plan: AutopilotPlan, project_dir: Optional[str]) -> int:
    from types import SimpleNamespace

    if plan_artifact_exists(plan.plan_file, project_dir):
        print(f"[autopilot] plan file already exists: {plan.plan_file} (skipping planning)")
        return 0

    plan_args = SimpleNamespace(
        plan_file=plan.plan_file,
        snapshot=None,
        consensus=not plan.skip_consensus,
        model=getattr(plan, "model", None),
    )

    # cmd_plan reads from stdin for requirements when there is no interview
    # spec — feed it the task via an in-memory replacement so autopilot can
    # stay non-interactive after the interview phase.
    original_stdin = sys.stdin
    import io

    sys.stdin = io.StringIO(plan.task + "\n")
    try:
        print("[autopilot] phase: planning")
        return cmd_plan(plan_args)
    finally:
        sys.stdin = original_stdin


def _autopilot_running_phase(plan: AutopilotPlan, project_dir: Optional[str]) -> int:
    from types import SimpleNamespace

    run_args = SimpleNamespace(
        plan_file=plan.plan_file,
        force=False,
        step=False,
        no_deslop=plan.no_deslop,
        model=getattr(plan, "model", None),
    )
    print("[autopilot] phase: running")
    return cmd_run(run_args)


def cmd_autopilot(args) -> int:
    """
    Sequence: interview → consensus plan → run (with finalize + deslop).

    Each phase writes its own artifact so a failure in any one phase is
    resumable via `dvx autopilot --resume <slug>` (or the phase-level
    subcommand directly).
    """
    task: str = (args.task or "").strip()
    resume_slug: Optional[str] = getattr(args, "resume", None)

    if not task and not resume_slug:
        print("Error: provide a task string or --resume <slug>.")
        return 1

    plan = build_plan_from_args(
        task=task,
        skip_interview=getattr(args, "skip_interview", False),
        skip_consensus=getattr(args, "skip_consensus", False),
        no_deslop=getattr(args, "no_deslop", False),
        explicit_plan_file=getattr(args, "plan_file", None),
        resume_slug=resume_slug,
        model=getattr(args, "model", None),
    )

    print(f"[autopilot] task: {plan.task}")
    print(f"[autopilot] slug: {plan.slug}")
    print(f"[autopilot] plan file target: {plan.plan_file}")

    rc = run_pipeline(
        plan,
        interview_fn=_autopilot_interview_phase,
        planning_fn=_autopilot_planning_phase,
        running_fn=_autopilot_running_phase,
    )

    summary = summarize_autopilot(plan, rc)
    print()
    print("=" * 60)
    print("AUTOPILOT SUMMARY")
    print("=" * 60)
    print(summary)
    write_autopilot_summary(plan, summary)
    return rc


def cmd_interview(args) -> int:
    """
    Launch an interactive deep-interview session for a task.

    Claude runs the Socratic loop in-session; Python seeds the skill
    prompt, persists resumable state, and checks for the resulting
    `.dvx/specs/interview-<slug>.md` after the session exits.
    """
    task: str = args.task.strip()
    if not task:
        print("Error: task description is required.")
        return 1

    profile = args.profile
    if profile not in PROFILE_THRESHOLDS:
        print(f"Error: unknown profile '{profile}'. Valid: {sorted(PROFILE_THRESHOLDS)}")
        return 1

    slug = args.slug or slug_from(task)
    project_dir: Optional[str] = None
    model = _selected_model_from_args(args)
    ok, error = _check_selected_model(model)
    if not ok:
        print(f"Error: {error}")
        return 1

    state = load_interview_state(slug, project_dir)
    if state is None:
        threshold, max_rounds = get_profile(profile)
        brownfield = Path(".git").exists()
        state = new_interview_state(
            task=task,
            profile=profile,
            brownfield=brownfield,
            slug=slug,
        )
        save_interview_state(state, project_dir)
        print(f"Starting deep-interview ({profile}, threshold {threshold:.2f}, max {max_rounds} rounds)")
    else:
        print(f"Resuming interview for slug '{slug}' at round {len(state.rounds) + 1}")
    print(f"Task: {task}")

    snapshot_content = load_latest_content(slug, project_dir) or ""
    prior_transcript = render_transcript(state) if state.rounds else ""

    skill_content = load_skill("interview")
    prompt = (
        skill_content.replace("$ARGUMENTS.task", task)
        .replace("$ARGUMENTS.slug", slug)
        .replace("$ARGUMENTS.profile", profile)
        .replace("$ARGUMENTS.threshold", f"{state.threshold:.2f}")
        .replace("$ARGUMENTS.max_rounds", str(state.max_rounds))
        .replace("$ARGUMENTS.snapshot_content", snapshot_content)
        .replace("$ARGUMENTS.prior_transcript", prior_transcript)
    )

    spec_path_hint = interview_spec_path(slug, project_dir)
    prompt += (
        "\n\n## Output path\n\n"
        f"When the interview converges, write the finalized spec to "
        f"`{spec_path_hint}` yourself (create the directory if missing). "
        "Include the Metadata, Intent, Desired outcome, In-scope, "
        "Out-of-scope / Non-goals, Decision boundaries, Constraints, "
        "Acceptance criteria, Assumptions, and Transcript sections. "
        "Only output `[INTERVIEW_COMPLETE]` once the file is on disk.\n"
    )

    interview_session_id = state.session_id
    if not interview_session_id:
        with claude_model_override(model):
            seed = start_session(prompt, cwd=project_dir)
        if not seed.success or not seed.session_id:
            reason = seed.block_reason or "failed to create interview session"
            print(f"Error: {reason}")
            return 1
        interview_session_id = seed.session_id
        state.session_id = interview_session_id
        save_interview_state(state, project_dir)
        if seed.output.strip():
            print()
            print(seed.output.strip())

    print()
    print("Launching interactive Claude session for the interview.")
    print("Type `/exit` when the spec has been written to return to dvx.")
    launch_interactive(
        session_id=interview_session_id,
        initial_prompt=None,
        plan_file=None,
        auto_explain=False,
        model=model,
    )

    print()
    body = load_spec(slug, project_dir)
    if body is None:
        print(f"No spec was written. Run `dvx interview --slug {slug}` again to resume.")
        return 1

    missing = validate_spec(body)
    if missing:
        print(f"Warning: spec is missing required sections: {missing}")
        print(f"Edit {interview_spec_path(slug, project_dir)} to add them.")

    state.finished = True
    save_interview_state(state, project_dir)
    print(f"Interview complete. Spec at: {interview_spec_path(slug, project_dir)}")
    return 0


class VerboseLogFilter(logging.Filter):
    """Filter to exclude verbose internal logs from console output.

    These logs go to the file handler but not the console, keeping
    CLI output clean and focused on orchestrator progress.
    """

    # Modules whose logs should only go to file, not console
    FILE_ONLY_MODULES = {"claude_session", "plan_parser"}

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name not in self.FILE_ONLY_MODULES


def setup_logging(verbose: bool = False, plan_file: Optional[str] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(message)s" if verbose else "%(message)s"

    # Console handler - filtered to exclude verbose internal logs
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(format_str))
    if not verbose:
        console_handler.addFilter(VerboseLogFilter())

    logging.basicConfig(
        level=level,
        handlers=[console_handler],
    )

    # Set up file logging in plan-specific or root .dvx directory
    if plan_file:
        dvx_dir = get_dvx_dir(plan_file)
        dvx_dir.mkdir(parents=True, exist_ok=True)
    else:
        dvx_dir = get_dvx_root()
        dvx_dir.mkdir(exist_ok=True)

    # File handler - gets ALL logs including claude_session
    log_file = dvx_dir / "dvx.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)


def check_git_environment() -> tuple[bool, str]:
    """
    Verify we're in a git repo and not on main/master branch.

    Returns: (ok, error_message)
    """
    # Check if we're in a git repository
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "Not in a git repository. dvx requires a git-managed project."

    # Get current branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "Could not determine current git branch."

    branch = result.stdout.strip()

    if branch in ("main", "master"):
        return False, f"""Cannot run dvx on '{branch}' branch.

dvx should be run on a feature branch, not directly on {branch}.

To fix this:
  1. Create a new branch:  git checkout -b feature/my-feature
  2. Then run:             dvx run <plan-file>

This ensures all changes are isolated and can be reviewed before merging."""

    return True, ""


def check_watch_git_environment() -> tuple[bool, str]:
    """
    Verify we're in a git repo and on a named branch.

    Returns: (ok, branch_or_error_message)
    """
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "Not in a git repository. dvx requires a git-managed project."

    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "Could not determine current git branch."

    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return False, "dvx watch requires a named branch, not a detached HEAD."

    return True, branch


def is_queue_file(filepath: str) -> bool:
    """Check if the file is a YAML queue file."""
    return filepath.endswith(('.yaml', '.yml'))


def load_queue(queue_file: str) -> list[str]:
    """Load the list of plan files from a YAML queue file."""
    with open(queue_file, 'r') as f:
        data = yaml.safe_load(f)

    # Support both a plain list and a dict with a 'plans' key
    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and 'plans' in data:
        return data['plans']
    else:
        raise ValueError("Invalid queue file format: expected a list or dict with 'plans' key")


def save_queue(queue_file: str, plans: list[str]) -> None:
    """Save the remaining plans to a YAML queue file."""
    Path(queue_file).parent.mkdir(parents=True, exist_ok=True)
    with open(queue_file, 'w') as f:
        yaml.dump(plans, f, default_flow_style=False)


def get_continuation_queue_path(plan_file: str, original_queue_name: str) -> str:
    """Get the path where the continuation queue should be saved."""
    dvx_dir = get_dvx_dir(plan_file)
    return str(dvx_dir / original_queue_name)


def find_continuation_queue(plan_file: str) -> Optional[str]:
    """Find a continuation queue file in the plan's .dvx directory."""
    dvx_dir = get_dvx_dir(plan_file)
    if not dvx_dir.exists():
        return None

    # Look for any .yaml or .yml file
    for f in dvx_dir.iterdir():
        if f.suffix in ('.yaml', '.yml') and f.is_file():
            return str(f)

    return None


def run_with_continuation(
    plan_file: str,
    step_mode: bool = False,
    no_deslop: bool = False,
    model: Optional[str] = None,
) -> int:
    """
    Run orchestrator and handle continuation queue on success.

    If the plan completes successfully and there's a continuation queue,
    re-exec dvx to process the next plan in the queue.
    """
    result = run_orchestrator(plan_file, step_mode=step_mode, no_deslop=no_deslop, model=model)

    if result == 0:
        # Plan completed successfully - check for continuation
        continuation = find_continuation_queue(plan_file)
        if continuation:
            print()
            print("=" * 60)
            print("CONTINUING TO NEXT PLAN IN QUEUE")
            print("=" * 60)
            print(f"Continuation queue: {continuation}")
            print()

            # Re-exec ourselves with the continuation queue
            # Using os.execv replaces this process entirely
            python = sys.executable
            argv = [python, __file__, 'run']
            if model:
                argv.extend(['--model', model])
            argv.append(continuation)
            os.execv(python, argv)

    return result


def cmd_run(args) -> int:
    """Run orchestration with command/env/default model resolution."""
    model = _selected_model_from_args(args)
    ok, error = _check_selected_model(model)
    if not ok:
        print(f"Error: {error}")
        return 1
    with claude_model_override(model):
        return _cmd_run_with_model(args, model)


def _cmd_run_with_model(args, model: str) -> int:
    """
    Run orchestration - handles all states automatically.

    - No state: starts fresh with the plan file
    - Blocked: launches interactive Claude session to resolve, then continues
    - Paused: continues to next task
    - In progress: continues orchestration
    - YAML queue: process files in sequence, continuing on success
    """
    # Verify git environment
    ok, error = check_git_environment()
    if not ok:
        print(f"Error: {error}")
        return 1

    input_file = str(Path(args.plan_file))

    # Handle YAML queue file
    if is_queue_file(input_file):
        if not Path(input_file).exists():
            print(f"Error: Queue file not found: {input_file}")
            return 1

        try:
            plans = load_queue(input_file)
        except Exception as e:
            print(f"Error loading queue file: {e}")
            return 1

        if not plans:
            print("Queue is empty - all plans completed!")
            # Clean up the queue file
            Path(input_file).unlink()
            return 0

        # Pop the first plan
        plan_file = plans[0]
        remaining = plans[1:]

        print(f"Queue: {len(plans)} plans remaining")
        print(f"  Next: {plan_file}")
        if remaining:
            print(f"  After: {', '.join(remaining[:3])}{'...' if len(remaining) > 3 else ''}")
        print()

        # Save remaining plans to .dvx/{plan_file}/{queue_name}.yaml
        queue_name = Path(input_file).name
        if remaining:
            continuation_path = get_continuation_queue_path(plan_file, queue_name)
            save_queue(continuation_path, remaining)
            print(f"Remaining queue saved to: {continuation_path}")
            print()
    else:
        plan_file = input_file

    if not Path(plan_file).exists():
        print(f"Error: Plan file not found: {plan_file}")
        return 1

    # Check state first - skip sync for blocked/paused/complete states
    state = load_state(plan_file)
    step_mode = args.step
    no_deslop = bool(getattr(args, "no_deslop", False))

    # Handle blocked state - launch interactive session to resolve
    if state is not None and state.phase == Phase.BLOCKED.value:
        print(f"Resuming blocked orchestration: {state.plan_file}")
        print(f"Current task: {state.current_task_id} - {state.current_task_title}")
        print()

        blocked_file = get_dvx_dir(plan_file) / "blocked-context.md"
        blocked_context = ""
        if blocked_file.exists():
            blocked_context = blocked_file.read_text()
            print("Blocked context:")
            print("-" * 40)
            lines = blocked_context.split('\n')[:15]
            print('\n'.join(lines))
            if len(blocked_context.split('\n')) > 15:
                print("...")
            print("-" * 40)
            print()

        print("Launching interactive Claude session to resolve...")
        print("Type /exit when done to return to dvx.")
        print()

        # Start a FRESH session with just the blocked context (not the accumulated overseer session)
        # This avoids context bloat from previous tasks
        # Load the resolve-blocked skill and substitute arguments
        skill_content = load_skill("resolve-blocked")
        initial_prompt = skill_content.replace("$ARGUMENTS.plan_file", state.plan_file)
        initial_prompt = initial_prompt.replace("$ARGUMENTS.task_id", state.current_task_id or "")
        initial_prompt = initial_prompt.replace("$ARGUMENTS.task_title", state.current_task_title or "")
        initial_prompt = initial_prompt.replace("$ARGUMENTS.blocked_reason", "See context below")
        initial_prompt = initial_prompt.replace("$ARGUMENTS.context", blocked_context)

        launch_interactive(initial_prompt=initial_prompt, plan_file=plan_file, model=model)

        print()
        print("Interactive session ended.")
        print("Clearing blocked state and continuing...")
        print()

        clear_blocked(plan_file)

        # Sync state with plan file BEFORE continuing orchestration
        # The interactive session may have completed the task and updated the plan
        print(f"Syncing plan state: {plan_file}")
        sync_result = sync_plan_state(plan_file)
        if sync_result['synced'] > 0 or sync_result['added'] > 0:
            print(f"  Updated: {sync_result['synced']} synced, {sync_result['added']} added from plan markers")

        return run_with_continuation(
            state.plan_file,
            step_mode=state.step_mode,
            no_deslop=no_deslop,
            model=model,
        )

    # === PLANNER: Sync state with plan file ===
    # This ensures the status tracking file matches the plan's [x] markers.
    # Handles cases where: user manually updated plan, escalator completed tasks,
    # or dvx clean was run but status file wasn't properly cleared.
    # Skip for paused/complete states since we're just resuming.
    if state is None or state.phase not in (Phase.PAUSED.value, Phase.COMPLETE.value):
        print(f"Syncing plan state: {plan_file}")
        sync_result = sync_plan_state(plan_file)
        if sync_result['synced'] > 0 or sync_result['added'] > 0:
            print(f"  Updated: {sync_result['synced']} synced, {sync_result['added']} added from plan markers")

    if state is None:
        print(f"Starting orchestration of: {plan_file}")
        print()

        summary = get_plan_summary(plan_file)
        print(f"Plan: {summary['total']} tasks")
        print(f"  Done: {summary['done']}")
        print(f"  In Progress: {summary['in_progress']}")
        print(f"  Pending: {summary['pending']}")
        print()

        next_task = get_next_pending_task(plan_file)
        if not next_task:
            print("No pending tasks found!")
            return 0

        if step_mode:
            print("Step mode: Will pause after each task for review")
            print()

        print(f"Starting with task {next_task.id}: {next_task.title}")
        print("=" * 60)
        print()

        return run_with_continuation(
            plan_file,
            step_mode=step_mode,
            no_deslop=no_deslop,
            model=model,
        )

    elif state.phase == Phase.PAUSED.value:
        print(f"Resuming from step-mode pause: {state.plan_file}")
        print()

        update_phase(Phase.IDLE, plan_file)
        return run_with_continuation(
            state.plan_file,
            step_mode=state.step_mode,
            no_deslop=no_deslop,
            model=model,
        )

    elif state.phase == Phase.COMPLETE.value:
        print(f"Plan already complete: {state.plan_file}")
        summary = get_plan_summary(state.plan_file)
        print(f"All {summary['total']} tasks done!")

        # Check for continuation queue even when already complete
        continuation = find_continuation_queue(plan_file)
        if continuation:
            print()
            print("=" * 60)
            print("CONTINUING TO NEXT PLAN IN QUEUE")
            print("=" * 60)
            print(f"Continuation queue: {continuation}")
            print()

            python = sys.executable
            argv = [python, __file__, 'run']
            if model:
                argv.extend(['--model', model])
            argv.append(continuation)
            os.execv(python, argv)

        return 0

    else:
        print(f"Continuing orchestration of: {state.plan_file}")
        print(f"Phase: {state.phase}")
        print(f"Current task: {state.current_task_id or 'none'} - {state.current_task_title or ''}")
        print()

        if step_mode and not state.step_mode:
            state.step_mode = True
            save_state(state)
            print("Step mode enabled")
            print()

        return run_with_continuation(
            state.plan_file,
            step_mode=state.step_mode,
            no_deslop=no_deslop,
            model=model,
        )


def cmd_watch(args) -> int:
    """
    Watch the work directory and process files one at a time.

    Each watched file is queued in .dvx/watch/state.json and worked on in its
    own branch by Claude Code. GOAL*.md files use the /goal command;
    all other files use the same loop as `dvx run`. Changes are committed,
    merged back into the branch watch started on (which is then pushed to its
    remote, when one exists), and cleaned up. All state transitions are
    persisted atomically so the watcher recovers from a crash or kill at any
    point - just re-run `dvx watch`.

    Dropping a MERGE file in the watched directory merges the watch branch
    into the remote's default branch (empty file) or the branch named in
    the file. The merge runs between watched items, ahead of the queue.
    """
    ok, branch_or_error = check_watch_git_environment()
    if not ok:
        print(f"Error: {branch_or_error}")
        return 1

    model = _selected_model_from_args(args)
    ok, error = _check_selected_model(model)
    if not ok:
        print(f"Error: {error}")
        return 1

    print("Press Ctrl-C to stop.")
    try:
        with claude_model_override(model):
            return run_goal_watch(
                start_branch=branch_or_error,
                goals_dir=args.goals,
                poll_interval=args.poll_interval,
                once=args.once,
                model=model,
            )
    except KeyboardInterrupt:
        print()
        print("Watch stopped. Re-run `dvx watch` to continue where it left off.")
        return 130


def cmd_clear(args) -> int:
    """
    Clear all watch-processing state, leaving the watched directory untouched.

    Running `dvx watch` afterwards re-discovers the work files and starts
    processing them from scratch.
    """
    if clear_goal_state():
        print("Cleared watch state (.dvx/watch/). Work files were left in place.")
    else:
        print("No watch state to clear.")
    return 0


def cmd_status(args) -> int:
    """Show current orchestration status for a plan."""
    plan_file = args.plan_file
    state = load_state(plan_file)

    if state is None:
        print(f"No active orchestration for: {plan_file}")
        print(f"Use 'dvx run {plan_file}' to begin.")
        return 0

    print(f"DVX Status: {plan_file}")
    print("=" * 40)
    print(f"Phase: {state.phase}")
    print(f"Current task: {state.current_task_id or 'none'} - {state.current_task_title or ''}")
    print(f"Iteration: {state.iteration_count}/{state.max_iterations}")
    print(f"Step mode: {'yes' if state.step_mode else 'no'}")
    if state.finalize_verdict is not None:
        print(f"Finalize verdict: {state.finalize_verdict} (after {state.finalize_iterations} finalize iteration(s))")
    print(f"Started: {state.started_at}")
    print(f"Updated: {state.updated_at}")
    print()

    if state.phase == Phase.PAUSED.value:
        print("PAUSED - Task completed, waiting for review")
        print(f"Run 'dvx run {plan_file}' to continue.")
    elif state.phase == Phase.BLOCKED.value:
        blocked_file = get_dvx_dir(plan_file) / "blocked-context.md"
        print(f"BLOCKED - See {blocked_file} for details")
        print(f"Run 'dvx run {plan_file}' to resolve and continue.")
    else:
        try:
            summary = get_plan_summary(state.plan_file)
            print(f"Progress: {summary['done']}/{summary['total']} tasks complete")
        except FileNotFoundError:
            print(f"Warning: Plan file not found: {state.plan_file}")

    return 0


def cmd_decisions(args) -> int:
    """Show decisions made during orchestration for a plan."""
    plan_file = args.plan_file
    decision_files = get_decisions(plan_file)

    if not decision_files:
        print(f"No decisions recorded for: {plan_file}")
        return 0

    print(f"Decisions for: {plan_file}")
    print("=" * 40)

    for decision_file in decision_files:
        print(f"\n{decision_file.name}:")
        print("-" * 40)
        print(decision_file.read_text())

    return 0


def cmd_clean(args) -> int:
    """Delete .dvx/ directory or plan-specific subdirectory and clear related state."""
    import shutil

    from plan_parser import clear_cache, clear_status

    plan_file = args.plan_file if hasattr(args, 'plan_file') and args.plan_file else None

    if plan_file:
        # Clean specific plan
        dvx_dir = get_dvx_dir(plan_file)
        if dvx_dir.exists():
            shutil.rmtree(dvx_dir)
            print(f"Removed {dvx_dir}")
        else:
            print(f"No state directory for: {plan_file}")

        # Also clear status and cache for this plan
        # This ensures a clean restart when running again
        if Path(plan_file).exists():
            clear_status_for_plan(plan_file)
            print(f"Cleared task statuses for: {plan_file}")
    else:
        # Clean entire .dvx directory
        dvx_dir = get_dvx_root()
        if dvx_dir.exists():
            shutil.rmtree(dvx_dir)
            print(f"Removed {dvx_dir}")
        else:
            print("No .dvx/ directory to clean.")

        # Also clear all caches and statuses
        clear_cache()
        clear_status()
        print("Cleared all caches and statuses")

    return 0


def main() -> int:
    # Install/update skills to ~/.claude/commands/dvx/
    ensure_skills_installed()

    parser = argparse.ArgumentParser(
        prog="dvx",
        description="Development Orchestrator - Automated implement/review/test/commit cycles",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run
    run_parser = subparsers.add_parser("run", help="Run orchestration (start, continue, or resolve)")
    run_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    run_parser.add_argument("-f", "--force", action="store_true", help="Force restart with new plan file")
    run_parser.add_argument("-s", "--step", action="store_true", help="Step mode: pause after each task for review")
    run_parser.add_argument("--no-deslop", action="store_true", help="Skip the post-approval deslop cleanup pass")
    run_parser.add_argument(
        "--model",
        help="Claude model to use (overrides DVX_MODEL; default: claude-opus-4-8)",
    )
    run_parser.set_defaults(func=cmd_run)

    # watch
    watch_parser = subparsers.add_parser(
        "watch",
        help=(
            "Watch a work directory and process GOAL*.md via /goal, other files via dvx run "
            "(a MERGE file there merges the watch branch into a remote branch)"
        ),
    )
    watch_parser.add_argument(
        "--todo",
        dest="goals",
        default=DEFAULT_TODO_DIR,
        help=f"Directory to watch for work files (default: {DEFAULT_TODO_DIR})",
    )
    watch_parser.add_argument(
        "--goals",
        dest="goals",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    watch_parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Seconds between checks for new work files",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending work files and exit instead of waiting for new ones",
    )
    watch_parser.add_argument(
        "--model",
        help="Claude model to use (overrides DVX_MODEL; default: claude-opus-4-8)",
    )
    watch_parser.set_defaults(func=cmd_watch)

    # clear
    clear_parser = subparsers.add_parser(
        "clear",
        help="Clear watch-processing state (leaves the watched directory untouched)",
    )
    clear_parser.set_defaults(func=cmd_clear)

    # status
    status_parser = subparsers.add_parser("status", help="Show current status for a plan")
    status_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    status_parser.set_defaults(func=cmd_status)

    # decisions
    decisions_parser = subparsers.add_parser("decisions", help="Show decisions made for a plan")
    decisions_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    decisions_parser.set_defaults(func=cmd_decisions)

    # clean
    clean_parser = subparsers.add_parser("clean", help="Delete .dvx/ directory (all) or plan-specific state")
    clean_parser.add_argument("plan_file", nargs="?", help="Path to PLAN-*.md file (optional, cleans all if omitted)")
    clean_parser.set_defaults(func=cmd_clean)

    # autopilot
    autopilot_parser = subparsers.add_parser(
        "autopilot",
        help="Sequence interview → consensus plan → run end-to-end",
    )
    autopilot_parser.add_argument(
        "task",
        nargs="?",
        default="",
        help="Task description to plan and build (omit with --resume)",
    )
    autopilot_parser.add_argument(
        "--skip-interview",
        action="store_true",
        help="Skip the interview phase; use the task string as requirements directly",
    )
    autopilot_parser.add_argument(
        "--skip-consensus",
        action="store_true",
        help="Skip the consensus loop; produce a single-perspective plan",
    )
    autopilot_parser.add_argument(
        "--no-deslop",
        action="store_true",
        help="Skip the post-approval deslop cleanup pass",
    )
    autopilot_parser.add_argument(
        "--plan-file",
        dest="plan_file",
        help="Use an existing plan file and start from the run phase",
    )
    autopilot_parser.add_argument(
        "--resume",
        help="Resume an in-progress autopilot run by slug",
    )
    autopilot_parser.add_argument(
        "--model",
        help="Claude model to use for all autopilot phases (overrides DVX_MODEL; default: claude-opus-4-8)",
    )
    autopilot_parser.set_defaults(func=cmd_autopilot)

    # interview
    interview_parser = subparsers.add_parser(
        "interview",
        help="Run a deep-interview session that produces an execution-ready spec",
    )
    interview_parser.add_argument("task", help="The task to clarify through interview")
    interview_profile = interview_parser.add_mutually_exclusive_group()
    interview_profile.add_argument(
        "--quick",
        dest="profile",
        action="store_const",
        const="quick",
        help="Quick profile (threshold 0.30, max 5 rounds)",
    )
    interview_profile.add_argument(
        "--standard",
        dest="profile",
        action="store_const",
        const="standard",
        help="Standard profile (threshold 0.20, max 12 rounds) — default",
    )
    interview_profile.add_argument(
        "--deep",
        dest="profile",
        action="store_const",
        const="deep",
        help="Deep profile (threshold 0.15, max 20 rounds)",
    )
    interview_parser.add_argument(
        "--slug",
        help="Override the auto-derived slug for state and spec filenames",
    )
    interview_parser.add_argument(
        "--model",
        help="Claude model to use (overrides DVX_MODEL; default: claude-opus-4-8)",
    )
    interview_parser.set_defaults(func=cmd_interview, profile="standard")

    # plan
    plan_parser = subparsers.add_parser("plan", help="Generate or update a plan file with Claude")
    plan_parser.add_argument("plan_file", nargs="?", help="Path to PLAN-*.md file (optional)")
    plan_parser.add_argument(
        "--snapshot",
        help="Path to a .dvx/context/ snapshot to use as grounding context",
    )
    plan_parser.add_argument(
        "--consensus",
        action="store_true",
        help="Run Planner/Architect/Critic consensus loop (up to 5 iterations)",
    )
    plan_parser.add_argument(
        "--model",
        help="Claude model to use (overrides DVX_MODEL; default: claude-opus-4-8)",
    )
    plan_parser.set_defaults(func=cmd_plan)

    args = parser.parse_args()

    # Get plan_file for logging if available
    plan_file = getattr(args, 'plan_file', None)
    setup_logging(args.verbose, plan_file)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
