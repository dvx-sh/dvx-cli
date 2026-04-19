"""
Deep-interview helpers for dvx.

Runs a Socratic clarification loop that turns vague task descriptions into
execution-ready specs with explicit non-goals, decision boundaries, and
testable acceptance criteria. Python owns the state machine and scoring
rubric; the skill prompt owns the per-round conversation.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from context import slug_from
from state import ensure_dvx_dir, get_dvx_dir

logger = logging.getLogger(__name__)

SPECS_DIR_NAME = "specs"
INTERVIEW_STATE_FILE = "interview-state.json"
SPEC_FILENAME_TEMPLATE = "interview-{slug}.md"

PROFILE_THRESHOLDS = {
    "quick": (0.30, 5),
    "standard": (0.20, 12),
    "deep": (0.15, 20),
}

DIMENSIONS_GREENFIELD = {
    "intent": 0.30,
    "outcome": 0.25,
    "scope": 0.20,
    "constraints": 0.15,
    "success": 0.10,
}

DIMENSIONS_BROWNFIELD = {
    "intent": 0.27,
    "outcome": 0.23,
    "scope": 0.18,
    "constraints": 0.13,
    "success": 0.09,
    "context": 0.10,
}


REQUIRED_SPEC_SECTIONS = (
    "## Intent",
    "## Desired outcome",
    "## In-scope",
    "## Out-of-scope / Non-goals",
    "## Decision boundaries",
    "## Constraints",
    "## Acceptance criteria",
    "## Assumptions exposed + resolutions",
    "## Transcript",
)


@dataclass
class InterviewRound:
    """One round of the interview loop."""
    number: int
    question: str
    answer: str
    scores: dict[str, float]
    ambiguity: float
    justification: str = ""


@dataclass
class InterviewState:
    """Resumable state for a running interview."""
    slug: str
    task: str
    profile: str
    threshold: float
    max_rounds: int
    brownfield: bool = False
    context_snapshot_path: Optional[str] = None
    session_id: Optional[str] = None
    rounds: list[InterviewRound] = field(default_factory=list)
    started_at: str = ""
    updated_at: str = ""
    finished: bool = False
    pressure_passes: int = 0
    non_goals_captured: bool = False
    decision_boundaries_captured: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["rounds"] = [asdict(r) for r in self.rounds]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "InterviewState":
        rounds = [InterviewRound(**r) for r in data.pop("rounds", [])]
        state = cls(**data)
        state.rounds = rounds
        return state


def get_profile(profile: str) -> tuple[float, int]:
    """Return (threshold, max_rounds) for a named profile."""
    if profile not in PROFILE_THRESHOLDS:
        raise ValueError(
            f"Unknown interview profile: {profile!r}. "
            f"Valid: {sorted(PROFILE_THRESHOLDS)}"
        )
    return PROFILE_THRESHOLDS[profile]


def ambiguity_score(dimension_scores: dict[str, float], brownfield: bool = False) -> float:
    """
    Compute 0..1 ambiguity from per-dimension clarity scores.

    Higher ambiguity = less clear. Dimension scores themselves are 0..1
    where 1.0 is fully clear. Missing dimensions count as 0 (fully
    ambiguous).
    """
    weights = DIMENSIONS_BROWNFIELD if brownfield else DIMENSIONS_GREENFIELD
    total = 0.0
    for name, weight in weights.items():
        clarity = max(0.0, min(1.0, float(dimension_scores.get(name, 0.0))))
        total += clarity * weight
    return max(0.0, min(1.0, 1.0 - total))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(slug: str, project_dir: Optional[str] = None) -> Path:
    """Return the per-slug working directory (may not exist)."""
    return get_dvx_dir(slug, project_dir)


def _ensure_state_dir(slug: str, project_dir: Optional[str] = None) -> Path:
    return ensure_dvx_dir(slug, project_dir)


def state_path(slug: str, project_dir: Optional[str] = None) -> Path:
    return _state_dir(slug, project_dir) / INTERVIEW_STATE_FILE


def spec_path(slug: str, project_dir: Optional[str] = None) -> Path:
    specs_dir = (
        _state_dir(slug, project_dir).parent / SPECS_DIR_NAME
    )
    return specs_dir / SPEC_FILENAME_TEMPLATE.format(slug=slug)


def ensure_specs_dir(project_dir: Optional[str] = None) -> Path:
    """Ensure `.dvx/specs/` exists and return it."""
    from state import get_dvx_root
    specs_dir = get_dvx_root(project_dir) / SPECS_DIR_NAME
    specs_dir.mkdir(parents=True, exist_ok=True)
    return specs_dir


def load_state(slug: str, project_dir: Optional[str] = None) -> Optional[InterviewState]:
    path = state_path(slug, project_dir)
    if not path.exists():
        return None
    try:
        return InterviewState.from_dict(json.loads(path.read_text()))
    except Exception as exc:
        logger.warning(f"Failed to load interview state {path}: {exc}")
        return None


def save_state(state: InterviewState, project_dir: Optional[str] = None) -> Path:
    _ensure_state_dir(state.slug, project_dir)
    state.updated_at = _now_iso()
    path = state_path(state.slug, project_dir)
    path.write_text(json.dumps(state.to_dict(), indent=2))
    return path


def new_state(
    task: str,
    profile: str = "standard",
    brownfield: bool = False,
    snapshot_path: Optional[str] = None,
    slug: Optional[str] = None,
) -> InterviewState:
    threshold, max_rounds = get_profile(profile)
    return InterviewState(
        slug=slug or slug_from(task),
        task=task,
        profile=profile,
        threshold=threshold,
        max_rounds=max_rounds,
        brownfield=brownfield,
        context_snapshot_path=snapshot_path,
        started_at=_now_iso(),
    )


def should_stop(state: InterviewState) -> tuple[bool, str]:
    """
    Decide whether the interview loop should halt.

    Returns (stop, reason). Halts when:
    - Max rounds reached (capped)
    - Ambiguity ≤ threshold AND both gates captured AND at least one pressure pass
    """
    if len(state.rounds) >= state.max_rounds:
        return True, "max-rounds"

    if not state.rounds:
        return False, ""

    latest = state.rounds[-1]
    gates_ok = state.non_goals_captured and state.decision_boundaries_captured
    pressure_ok = state.pressure_passes >= 1
    if latest.ambiguity <= state.threshold and gates_ok and pressure_ok:
        return True, "converged"
    return False, ""


def record_round(
    state: InterviewState,
    question: str,
    answer: str,
    scores: dict[str, float],
    justification: str = "",
    non_goals_hit: bool = False,
    decision_boundaries_hit: bool = False,
    pressure_pass: bool = False,
) -> InterviewRound:
    """Append a round, recompute ambiguity, update gates, persist nothing."""
    round_number = len(state.rounds) + 1
    amb = ambiguity_score(scores, brownfield=state.brownfield)
    rnd = InterviewRound(
        number=round_number,
        question=question,
        answer=answer,
        scores=scores,
        ambiguity=amb,
        justification=justification,
    )
    state.rounds.append(rnd)
    if non_goals_hit:
        state.non_goals_captured = True
    if decision_boundaries_hit:
        state.decision_boundaries_captured = True
    if pressure_pass:
        state.pressure_passes += 1
    return rnd


def render_transcript(state: InterviewState) -> str:
    if not state.rounds:
        return "(no rounds recorded)"
    parts = []
    for r in state.rounds:
        parts.append(f"### Round {r.number} (ambiguity {r.ambiguity:.2f})")
        parts.append(f"**Q:** {r.question.strip()}")
        parts.append(f"**A:** {r.answer.strip()}")
        if r.justification:
            parts.append(f"_Scoring rationale:_ {r.justification.strip()}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_spec(
    state: InterviewState,
    intent: str,
    desired_outcome: str,
    in_scope: str,
    non_goals: str,
    decision_boundaries: str,
    constraints: str,
    acceptance_criteria: str,
    assumptions: str,
) -> str:
    """Render the final spec markdown body."""
    final_amb = state.rounds[-1].ambiguity if state.rounds else 1.0
    snapshot_line = (
        f"- **Context snapshot:** `{state.context_snapshot_path}`"
        if state.context_snapshot_path
        else "- **Context snapshot:** (none)"
    )
    return (
        f"# Interview Spec: {state.task.strip()}\n\n"
        "## Metadata\n\n"
        f"- **Slug:** `{state.slug}`\n"
        f"- **Profile:** {state.profile}\n"
        f"- **Rounds used:** {len(state.rounds)} / max {state.max_rounds}\n"
        f"- **Final ambiguity:** {final_amb:.2f} (threshold {state.threshold:.2f})\n"
        f"{snapshot_line}\n"
        f"- **Brownfield:** {'yes' if state.brownfield else 'no'}\n\n"
        "## Intent\n\n"
        f"{intent.strip() or '(unspecified)'}\n\n"
        "## Desired outcome\n\n"
        f"{desired_outcome.strip() or '(unspecified)'}\n\n"
        "## In-scope\n\n"
        f"{in_scope.strip() or '(unspecified)'}\n\n"
        "## Out-of-scope / Non-goals\n\n"
        f"{non_goals.strip() or '(none)'}\n\n"
        "## Decision boundaries\n\n"
        f"{decision_boundaries.strip() or '(none)'}\n\n"
        "## Constraints\n\n"
        f"{constraints.strip() or '(none)'}\n\n"
        "## Acceptance criteria\n\n"
        f"{acceptance_criteria.strip() or '(none)'}\n\n"
        "## Assumptions exposed + resolutions\n\n"
        f"{assumptions.strip() or '(none)'}\n\n"
        "## Transcript\n\n"
        f"{render_transcript(state)}"
    )


def write_spec(
    state: InterviewState,
    body: str,
    project_dir: Optional[str] = None,
) -> Path:
    """Write the final spec file for the interview."""
    ensure_specs_dir(project_dir)
    path = spec_path(state.slug, project_dir)
    path.write_text(body)
    logger.info(f"Wrote interview spec to {path}")
    return path


def load_spec(slug: str, project_dir: Optional[str] = None) -> Optional[str]:
    path = spec_path(slug, project_dir)
    if not path.exists():
        return None
    return path.read_text()


def validate_spec(body: str) -> list[str]:
    """Return a list of missing required sections."""
    return [s for s in REQUIRED_SPEC_SECTIONS if s not in body]


_QUESTION_RE = re.compile(r"(^|\n)\s*\*\*Q:\*\*\s*(.+?)(?=\n|$)", re.MULTILINE)


def extract_last_question(text: str) -> Optional[str]:
    """Find the most recent `**Q:** ...` line in a response, if any."""
    matches = _QUESTION_RE.findall(text)
    if not matches:
        return None
    return matches[-1][1].strip()
