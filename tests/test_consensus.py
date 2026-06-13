"""Tests for the consensus planning loop."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from consensus import (
    ARCHITECT_TAG_CONCERNS,
    ARCHITECT_TAG_PASS,
    CRITIC_TAG_APPROVE,
    CRITIC_TAG_ITERATE,
    CRITIC_TAG_REJECT,
    MAX_ITERATIONS,
    REQUIRED_PLAN_SECTIONS,
    VERDICT_UNPARSED,
    critic_suggested_reject,
    extract_plan_body,
    parse_architect_verdict,
    parse_critic_verdict,
    run_consensus,
    validate_plan,
)


class TestParseArchitectVerdict:
    def test_pass(self):
        assert parse_architect_verdict("[ARCH_PASS] looks fine") == ARCHITECT_TAG_PASS

    def test_concerns(self):
        assert parse_architect_verdict("[ARCH_CONCERNS] see below") == ARCHITECT_TAG_CONCERNS

    def test_missing(self):
        assert parse_architect_verdict("no tag here") == VERDICT_UNPARSED

    def test_case_insensitive(self):
        assert parse_architect_verdict("[arch_pass]") == ARCHITECT_TAG_PASS


class TestParseCriticVerdict:
    def test_approve(self):
        assert parse_critic_verdict("[APPROVE]\nbody") == CRITIC_TAG_APPROVE

    def test_iterate(self):
        assert parse_critic_verdict("[ITERATE] revise") == CRITIC_TAG_ITERATE

    def test_reject(self):
        assert parse_critic_verdict("[REJECT] ship it later") == CRITIC_TAG_REJECT

    def test_unparsed(self):
        assert parse_critic_verdict("no tag") == VERDICT_UNPARSED


class TestValidatePlan:
    def _full_plan(self) -> str:
        sections = "\n\n".join(f"{s}\nbody" for s in REQUIRED_PLAN_SECTIONS)
        return f"# Plan: thing\n\n{sections}\n"

    def test_full_plan_has_no_missing(self):
        assert validate_plan(self._full_plan()) == []

    def test_missing_listed(self):
        body = "# Plan: X\n\n## Principles\n\n- P1"
        missing = validate_plan(body)
        assert "## Principles" not in missing
        assert "## Decision Drivers" in missing
        assert "## ADR" in missing


class TestExtractPlanBody:
    def test_strips_markdown_fence(self):
        wrapped = (
            "Here is the plan:\n"
            "```markdown\n"
            "# Plan: foo\n\n## Principles\n- P1\n"
            "```\n"
        )
        body = extract_plan_body(wrapped)
        assert body.startswith("# Plan: foo")
        assert "```" not in body

    def test_finds_raw_plan(self):
        raw = "Preamble.\n\n# Plan: foo\n\n## Principles\n- P1\n"
        assert extract_plan_body(raw).startswith("# Plan: foo")

    def test_falls_back_to_input(self):
        raw = "not a plan at all"
        assert extract_plan_body(raw) == "not a plan at all"


class _FakeCaller:
    """A scripted SkillCaller for deterministic loop testing."""

    def __init__(self, scripted: list[dict[str, str]]):
        # scripted is a list of per-iteration responses keyed by skill name.
        self.scripted = scripted
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.iteration = 0

    def __call__(self, skill_name: str, args: dict[str, str]) -> str:
        self.calls.append((skill_name, args))
        try:
            out = self.scripted[self.iteration][skill_name]
        except (IndexError, KeyError):
            raise AssertionError(
                f"No scripted response for skill={skill_name} iteration={self.iteration}"
            )
        # Advance iteration after the critic call (the last of the trio).
        if skill_name == "critic":
            self.iteration += 1
        return out


def _plan_with_all_sections(suffix: str = "") -> str:
    sections = "\n\n".join(f"{s}\nbody{suffix}" for s in REQUIRED_PLAN_SECTIONS)
    return f"# Plan: foo\n\n{sections}\n"


class TestRunConsensus:
    def test_approves_first_round(self):
        caller = _FakeCaller(
            [
                {
                    "consensus-planner": _plan_with_all_sections(),
                    "architect": "[ARCH_PASS]\nno concerns",
                    "critic": "[APPROVE]\nall good",
                }
            ]
        )
        result = run_consensus("build X", caller)
        assert result.approved is True
        assert len(result.iterations) == 1
        assert result.iterations[0].architect_verdict == ARCHITECT_TAG_PASS
        assert result.iterations[0].critic_verdict == CRITIC_TAG_APPROVE

    def test_iterates_until_approve(self):
        caller = _FakeCaller(
            [
                {
                    "consensus-planner": _plan_with_all_sections(" v1"),
                    "architect": "[ARCH_CONCERNS]\nfix X",
                    "critic": "[ITERATE]\n1. Fix X",
                },
                {
                    "consensus-planner": _plan_with_all_sections(" v2"),
                    "architect": "[ARCH_PASS]",
                    "critic": "[APPROVE]",
                },
            ]
        )
        result = run_consensus("build X", caller)
        assert result.approved is True
        assert len(result.iterations) == 2
        # Planner in round 2 should have received prior feedback.
        round_two_planner = [c for c in caller.calls if c[0] == "consensus-planner"][1]
        assert "ITERATE" in round_two_planner[1]["prior_feedback"]

    def test_enforces_sequential_order(self):
        """Architect MUST be called before Critic each iteration."""
        caller = _FakeCaller(
            [
                {
                    "consensus-planner": _plan_with_all_sections(),
                    "architect": "[ARCH_PASS]",
                    "critic": "[APPROVE]",
                }
            ]
        )
        run_consensus("build X", caller)
        skills_called_in_order = [c[0] for c in caller.calls]
        assert skills_called_in_order == ["consensus-planner", "architect", "critic"]

    def test_caps_iterations_and_returns_best(self):
        iterations_scripted = [
            {
                "consensus-planner": _plan_with_all_sections(f" v{i+1}"),
                "architect": "[ARCH_CONCERNS]",
                "critic": "[ITERATE]\n1. still wrong",
            }
            for i in range(MAX_ITERATIONS)
        ]
        caller = _FakeCaller(iterations_scripted)
        result = run_consensus("build X", caller)
        assert result.approved is False
        assert len(result.iterations) == MAX_ITERATIONS
        assert result.final_plan.startswith("> **Consensus not reached")
        assert result.stopped_reason.startswith("max-iterations-")

    def test_reject_also_loops(self):
        caller = _FakeCaller(
            [
                {
                    "consensus-planner": _plan_with_all_sections(" v1"),
                    "architect": "[ARCH_CONCERNS]",
                    "critic": "[REJECT]\nfundamental rework",
                },
                {
                    "consensus-planner": _plan_with_all_sections(" v2"),
                    "architect": "[ARCH_PASS]",
                    "critic": "[APPROVE]",
                },
            ]
        )
        result = run_consensus("build X", caller)
        assert result.approved is True
        assert result.iterations[0].critic_verdict == CRITIC_TAG_REJECT
        assert critic_suggested_reject(result) is False  # final is approve

    def test_non_approve_propagates_feedback(self):
        caller = _FakeCaller(
            [
                {
                    "consensus-planner": _plan_with_all_sections(" v1"),
                    "architect": "[ARCH_CONCERNS]\nMissing verification steps",
                    "critic": "[ITERATE]\n1. Add verification steps",
                },
                {
                    "consensus-planner": _plan_with_all_sections(" v2"),
                    "architect": "[ARCH_PASS]",
                    "critic": "[APPROVE]",
                },
            ]
        )
        run_consensus("build X", caller)
        second_planner_call = [c for c in caller.calls if c[0] == "consensus-planner"][1]
        feedback = second_planner_call[1]["prior_feedback"]
        assert "Missing verification steps" in feedback
        assert "Add verification steps" in feedback

    def test_critic_suggested_reject_tracks_last_iteration(self):
        caller = _FakeCaller(
            [
                {
                    "consensus-planner": _plan_with_all_sections(" v1"),
                    "architect": "[ARCH_CONCERNS]",
                    "critic": "[REJECT]\nbad",
                }
            ]
            + [
                {
                    "consensus-planner": _plan_with_all_sections(f" v{i}"),
                    "architect": "[ARCH_CONCERNS]",
                    "critic": "[REJECT]\nstill bad",
                }
                for i in range(2, MAX_ITERATIONS + 1)
            ]
        )
        result = run_consensus("build X", caller)
        assert result.approved is False
        assert critic_suggested_reject(result) is True
