"""
Autopilot pipeline for dvx.

A thin orchestrator that sequences:

    interview → consensus plan → run (with architect gate; deslop opt-in)

Each phase writes its own artifact, so a failure mid-pipeline is resumable
via `dvx autopilot --resume <slug>` (or the per-phase subcommand).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from context import slug_from
from interview import spec_path as interview_spec_path
from state import get_dvx_dir, get_dvx_root, load_state, save_state

logger = logging.getLogger(__name__)

PHASE_INTERVIEW = "interview"
PHASE_PLANNING = "planning"
PHASE_RUNNING = "running"
PHASE_COMPLETE = "complete"
PHASE_FAILED = "failed"

PIPELINE_ORDER = (PHASE_INTERVIEW, PHASE_PLANNING, PHASE_RUNNING, PHASE_COMPLETE)


@dataclass
class AutopilotPlan:
    """Resolved set of phases autopilot will run for a given invocation."""
    task: str
    slug: str
    plan_file: str
    skip_interview: bool
    skip_consensus: bool
    no_deslop: bool
    resume_from: Optional[str] = None
    model: Optional[str] = None


def plan_file_for_slug(slug: str) -> str:
    return f"PLAN-{slug}.md"


def interview_artifact_exists(slug: str, project_dir: Optional[str] = None) -> bool:
    return interview_spec_path(slug, project_dir).exists()


def plan_artifact_exists(plan_file: str, project_dir: Optional[str] = None) -> bool:
    path = Path(plan_file)
    if path.is_absolute():
        return path.exists()
    cwd = Path(project_dir) if project_dir else Path.cwd()
    return (cwd / path).exists()


def next_phase(current: Optional[str]) -> str:
    """Return the phase that should run next, or `complete` if we're done."""
    if current is None or current == PHASE_FAILED:
        return PHASE_INTERVIEW
    try:
        idx = PIPELINE_ORDER.index(current)
    except ValueError:
        return PHASE_INTERVIEW
    if idx + 1 >= len(PIPELINE_ORDER):
        return PHASE_COMPLETE
    return PIPELINE_ORDER[idx + 1]


def resolve_starting_phase(
    plan: AutopilotPlan,
    project_dir: Optional[str] = None,
) -> str:
    """
    Pick the starting phase based on flags, resume state, and on-disk artifacts.

    Precedence:
      1. `--resume` reads state.json for the slug and restarts from the
         phase recorded there.
      2. Flags (`--skip-interview`, `--skip-consensus`, `--plan-file`)
         advance the starting phase.
      3. On-disk artifacts advance the starting phase (idempotent reruns
         don't re-do completed phases).
    """
    if plan.resume_from:
        stored = _load_state_phase(plan.plan_file, project_dir)
        if stored in PIPELINE_ORDER and stored != PHASE_COMPLETE:
            return stored
        # Treat missing/unknown resume state as a fresh start for safety.
        logger.info("No resumable phase found; starting from first phase")

    if plan_artifact_exists(plan.plan_file, project_dir):
        return PHASE_RUNNING
    if plan.skip_interview and plan.skip_consensus:
        return PHASE_RUNNING
    if interview_artifact_exists(plan.slug, project_dir):
        return PHASE_PLANNING
    if plan.skip_interview:
        return PHASE_PLANNING
    return PHASE_INTERVIEW


def _load_state_phase(plan_file: str, project_dir: Optional[str]) -> Optional[str]:
    """Return the autopilot_phase recorded in state, if any."""
    state = load_state(plan_file, project_dir)
    if state is None:
        return None
    return state.autopilot_phase


def record_phase(
    plan_file: str,
    phase: str,
    project_dir: Optional[str] = None,
) -> None:
    """Persist the autopilot phase in per-plan state."""
    state = load_state(plan_file, project_dir)
    if state is None:
        # Seed state if autopilot is the first thing to touch this plan.
        from state import create_initial_state

        state = create_initial_state(plan_file, project_dir)
    state.autopilot_phase = phase
    save_state(state, project_dir)


# Phase callables receive the plan and the project dir; they return the
# exit code for the phase (0 = success, non-zero = failure).
PhaseFn = Callable[[AutopilotPlan, Optional[str]], int]


def run_pipeline(
    plan: AutopilotPlan,
    interview_fn: PhaseFn,
    planning_fn: PhaseFn,
    running_fn: PhaseFn,
    project_dir: Optional[str] = None,
) -> int:
    """
    Execute the autopilot pipeline.

    Each phase callable is injected so the caller can bind it to the real
    CLI commands or to a test double. The pipeline itself has no knowledge
    of Claude or argparse.
    """
    starting = resolve_starting_phase(plan, project_dir)
    phase_fns = {
        PHASE_INTERVIEW: interview_fn,
        PHASE_PLANNING: planning_fn,
        PHASE_RUNNING: running_fn,
    }

    current = starting
    while current != PHASE_COMPLETE:
        record_phase(plan.plan_file, current, project_dir)
        fn = phase_fns[current]
        logger.info(f"[autopilot] entering phase: {current}")
        rc = fn(plan, project_dir)
        if rc != 0:
            logger.warning(f"[autopilot] phase {current} exited {rc}")
            record_phase(plan.plan_file, PHASE_FAILED, project_dir)
            return rc
        current = next_phase(current)

    record_phase(plan.plan_file, PHASE_COMPLETE, project_dir)
    logger.info("[autopilot] pipeline complete")
    return 0


def summarize(plan: AutopilotPlan, rc: int, project_dir: Optional[str] = None) -> str:
    """Render a short summary for the user after the pipeline finishes."""
    phase = _load_state_phase(plan.plan_file, project_dir)
    state = load_state(plan.plan_file, project_dir)
    verdict = state.finalize_verdict if state else None
    deslop = "yes" if state and state.deslop_run else "no"
    lines = [
        f"task: {plan.task}",
        f"slug: {plan.slug}",
        f"plan: {plan.plan_file}",
        f"final phase: {phase or 'n/a'}",
        f"finalize verdict: {verdict or 'n/a'}",
        f"deslop ran: {deslop}",
        f"exit code: {rc}",
    ]
    if state and state.deslop_skipped_files:
        lines.append(
            "deslop-skipped files: " + ", ".join(state.deslop_skipped_files)
        )
    return "\n".join(lines)


def write_autopilot_summary(plan: AutopilotPlan, body: str, project_dir: Optional[str] = None) -> Path:
    """Append the pipeline summary to `.dvx/<plan>/autopilot-summary.md`."""
    dvx_dir = get_dvx_dir(plan.plan_file, project_dir)
    dvx_dir.mkdir(parents=True, exist_ok=True)
    path = dvx_dir / "autopilot-summary.md"
    existing = path.read_text() if path.exists() else ""
    header = "# Autopilot summary\n\n" if not existing else ""
    path.write_text(existing + header + body + "\n\n---\n\n")
    return path


def dvx_root(project_dir: Optional[str] = None) -> Path:
    return get_dvx_root(project_dir)


def build_plan_from_args(
    task: str,
    skip_interview: bool,
    skip_consensus: bool,
    no_deslop: bool,
    explicit_plan_file: Optional[str] = None,
    resume_slug: Optional[str] = None,
    model: Optional[str] = None,
) -> AutopilotPlan:
    """Turn argparse inputs into a resolved AutopilotPlan."""
    if resume_slug:
        slug = resume_slug
        task_value = task or resume_slug
    else:
        slug = slug_from(task)
        task_value = task

    if explicit_plan_file:
        plan_file = explicit_plan_file
    else:
        plan_file = plan_file_for_slug(slug)

    return AutopilotPlan(
        task=task_value,
        slug=slug,
        plan_file=plan_file,
        skip_interview=bool(skip_interview),
        skip_consensus=bool(skip_consensus),
        no_deslop=bool(no_deslop),
        resume_from=resume_slug,
        model=model,
    )
