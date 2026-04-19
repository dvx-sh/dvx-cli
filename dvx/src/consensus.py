"""
Consensus planning loop for dvx.

Runs Planner → Architect → Critic sequentially (never parallel) up to 5
iterations, producing a RALPLAN-DR-structured plan. Parallel execution would
race on the shared plan draft and produce inconsistent verdicts; the loop
lives in Python so the ordering is enforced, not documented.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5

REQUIRED_PLAN_SECTIONS = (
    "## Principles",
    "## Decision Drivers",
    "## Viable Options",
    "## Acceptance Criteria",
    "## Implementation Steps",
    "## Risks and Mitigations",
    "## Verification Steps",
    "## ADR",
    "## Available-Agent-Types Roster",
    "## Changelog",
)


ARCHITECT_TAG_PASS = "[ARCH_PASS]"
ARCHITECT_TAG_CONCERNS = "[ARCH_CONCERNS]"
CRITIC_TAG_APPROVE = "[APPROVE]"
CRITIC_TAG_ITERATE = "[ITERATE]"
CRITIC_TAG_REJECT = "[REJECT]"

VERDICT_UNPARSED = "UNPARSED"


@dataclass
class IterationRecord:
    """What happened in one consensus iteration."""
    iteration: int
    plan_draft: str
    architect_verdict: str
    architect_output: str
    critic_verdict: str
    critic_output: str


@dataclass
class ConsensusResult:
    """Final outcome of the consensus loop."""
    approved: bool
    final_plan: str
    iterations: list[IterationRecord] = field(default_factory=list)
    stopped_reason: str = ""


def parse_architect_verdict(output: str) -> str:
    """Return the architect verdict tag found in the output."""
    text = (output or "").upper()
    if "[ARCH_PASS]" in text:
        return ARCHITECT_TAG_PASS
    if "[ARCH_CONCERNS]" in text:
        return ARCHITECT_TAG_CONCERNS
    return VERDICT_UNPARSED


def parse_critic_verdict(output: str) -> str:
    """Return the critic verdict tag found in the output."""
    text = (output or "").upper()
    for tag in (CRITIC_TAG_APPROVE, CRITIC_TAG_ITERATE, CRITIC_TAG_REJECT):
        if tag in text:
            return tag
    return VERDICT_UNPARSED


def validate_plan(body: str) -> list[str]:
    """Return required sections missing from a plan draft."""
    return [s for s in REQUIRED_PLAN_SECTIONS if s not in body]


_PLAN_FENCE_RE = re.compile(r"^```markdown\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def extract_plan_body(output: str) -> str:
    """
    Extract the plan body from a planner's output.

    Accepts either a raw markdown plan that starts with `# Plan:` or a
    plan wrapped in a ```markdown fenced block. Falls back to the full
    output if no marker is present.
    """
    match = _PLAN_FENCE_RE.search(output)
    if match:
        return match.group(1).strip()
    idx = output.find("# Plan:")
    if idx != -1:
        return output[idx:].strip()
    return (output or "").strip()


# A skill caller takes a dict of arguments and returns the skill output.
SkillCaller = Callable[[str, dict[str, str]], str]


def run_consensus(
    task: str,
    call_skill: SkillCaller,
    snapshot_content: str = "",
    interview_spec: str = "",
    max_iterations: int = MAX_ITERATIONS,
) -> ConsensusResult:
    """
    Run the Planner → Architect → Critic loop until approval or cap.

    `call_skill(skill_name, args)` is injected so the loop can be exercised
    without touching Claude in tests.
    """
    iterations: list[IterationRecord] = []
    prior_feedback = ""
    last_plan_draft = ""

    for attempt in range(1, max_iterations + 1):
        planner_out = call_skill(
            "consensus-planner",
            {
                "task": task,
                "snapshot_content": snapshot_content,
                "interview_spec": interview_spec,
                "prior_feedback": prior_feedback,
                "iteration": str(attempt),
            },
        )
        plan_draft = extract_plan_body(planner_out)
        last_plan_draft = plan_draft

        architect_out = call_skill(
            "architect",
            {"plan_draft": plan_draft, "task": task},
        )
        architect_verdict = parse_architect_verdict(architect_out)

        critic_out = call_skill(
            "critic",
            {
                "plan_draft": plan_draft,
                "architect_verdict": architect_verdict,
                "architect_feedback": architect_out,
                "interview_spec": interview_spec,
                "task": task,
            },
        )
        critic_verdict = parse_critic_verdict(critic_out)

        iterations.append(
            IterationRecord(
                iteration=attempt,
                plan_draft=plan_draft,
                architect_verdict=architect_verdict,
                architect_output=architect_out,
                critic_verdict=critic_verdict,
                critic_output=critic_out,
            )
        )

        if critic_verdict == CRITIC_TAG_APPROVE:
            logger.info(f"Consensus approved after {attempt} iteration(s)")
            return ConsensusResult(
                approved=True,
                final_plan=plan_draft,
                iterations=iterations,
                stopped_reason="approved",
            )

        if critic_verdict == CRITIC_TAG_REJECT:
            logger.info("Critic rejected — loop continues with combined feedback")

        prior_feedback = _combine_feedback(architect_out, critic_out, critic_verdict)

    logger.warning(f"Consensus not reached after {max_iterations} iterations")
    return ConsensusResult(
        approved=False,
        final_plan=_add_no_consensus_preamble(last_plan_draft, max_iterations),
        iterations=iterations,
        stopped_reason=f"max-iterations-{max_iterations}",
    )


def _combine_feedback(architect_out: str, critic_out: str, critic_verdict: str) -> str:
    """Bundle architect and critic feedback for the next planner round."""
    header = (
        f"Previous architect verdict: {parse_architect_verdict(architect_out)}\n"
        f"Previous critic verdict: {critic_verdict}\n"
    )
    return (
        header
        + "\n## Architect feedback\n\n"
        + (architect_out or "").strip()
        + "\n\n## Critic feedback\n\n"
        + (critic_out or "").strip()
    )


def _add_no_consensus_preamble(plan: str, max_iterations: int) -> str:
    """Prepend the no-consensus notice to the final plan body."""
    notice = (
        f"> **Consensus not reached after {max_iterations} iterations.** "
        "The best draft is included below; treat its open questions as "
        "items for human decision before implementation starts.\n\n"
    )
    if plan.startswith("> **Consensus not reached"):
        return plan
    return notice + plan


def render_no_consensus_summary(result: ConsensusResult) -> str:
    """Render a short summary of why consensus was not reached."""
    lines = [
        f"Iterations run: {len(result.iterations)} / {MAX_ITERATIONS}",
    ]
    for rec in result.iterations:
        lines.append(
            f"- Round {rec.iteration}: architect={rec.architect_verdict}, "
            f"critic={rec.critic_verdict}"
        )
    return "\n".join(lines)


def critic_suggested_reject(result: ConsensusResult) -> bool:
    """Return True if the last iteration ended in a rejection."""
    if not result.iterations:
        return False
    return result.iterations[-1].critic_verdict == CRITIC_TAG_REJECT


def build_planner_args(
    task: str,
    snapshot_content: str,
    interview_spec: str,
    prior_feedback: str,
    iteration: int,
) -> dict[str, str]:
    """Preserve the exact argument contract the skill loader expects."""
    return {
        "task": task,
        "snapshot_content": snapshot_content,
        "interview_spec": interview_spec,
        "prior_feedback": prior_feedback,
        "iteration": str(iteration),
    }


def make_skill_caller(run_skill_fn, model: Optional[str] = "opus") -> SkillCaller:
    """
    Adapt `orchestrator.run_skill` into the plain-string caller the
    consensus loop expects. Imported lazily in the CLI to avoid a
    circular import in tests.
    """

    def _call(skill_name: str, args: dict[str, str]) -> str:
        result = run_skill_fn(skill_name, args, model=model)
        if not result.success:
            raise RuntimeError(
                f"Skill {skill_name} failed: {result.block_reason or 'unknown error'}"
            )
        return result.output or ""

    return _call
