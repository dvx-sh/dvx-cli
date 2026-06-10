# PROPOSAL: Bug-Fix Workflow with Workaround Detection

**Status:** Proposed
**Type:** Workflow extension
**Affects:** `src/skills/`, `src/orchestrator.py`, `src/cli.py`

---

## Problem

dvx's current consensus loop (Planner -> Architect -> Critic) and execution loop (Implement -> Review -> Fix -> Commit) are designed for **feature implementation**. They assume:

1. The task description names a thing to *build*.
2. Viable options exist at planning time and can be enumerated from prior knowledge.
3. The implementer's job is to translate a known design into code.
4. The reviewer's job is to verify the code matches the design and meets quality bars.

These assumptions break for **bug-fix and build-failure tasks**, where:

1. The task description names a *symptom* (a broken build, a failing test, an error message).
2. Viable fixes are not knowable at planning time. They require investigation in the repo to surface.
3. The implementer's job includes diagnosis, not just translation.
4. The reviewer cannot tell whether a small diff is a root-cause fix or a symptom-mitigation hack just by looking at the diff.

The failure mode this enables: when an implementer hits a non-trivial bug, it tends to escalate through workarounds (bump dependency versions, relax strict checks, defer errors to runtime) rather than stopping to enumerate options. Each workaround "fixes" the symptom enough to pass review, so no gate trips. The user discovers the hack later, often when the deferred problem surfaces in production.

## Motivating Pattern

A characteristic instance:

- **Symptom**: iOS framework fails to link with `Undefined symbols: SwiftProtobuf.Google_Protobuf_Empty`.
- **Root cause**: an upstream-generated SPM package omits `swift-protobuf` from its `Package.swift` dependencies despite referencing it in sources.
- **Tempting workarounds (in escalating severity)**:
  1. Bump the upstream package version hoping a fixed manifest landed (silent failure if not).
  2. Pin to a specific version range (treats a transitive bug as your own version constraint).
  3. Set `OTHER_LDFLAGS = -Wl,-undefined,dynamic_lookup` to defer unresolved symbols to runtime (passes link, breaks at app launch).
- **Cleanest fix**: patch the checked-out `Package.swift` between SPM resolve and SPM build, then build with `-disableAutomaticPackageResolution` to prevent re-fetching.

An implementer that lands #3 produces a passing build and a small diff. A standard reviewer approves it. The deferred-error linker flag then ships, and the failure surfaces only when the dynamically-resolved symbol is needed at runtime.

## Gap Analysis Against Current dvx

Walking the current skill set against this kind of task:

- **`create-plan.md`** asks for "complete code examples with types and functions" in each phase. For a bug fix you do not have the solution yet; you have a stack trace.
- **`interview.md`** clarifies *intent* ambiguity (scope, non-goals, success criteria). It does not clarify *root-cause* ambiguity.
- **`consensus-planner.md`** requires >= 2 Viable Options with pros/cons/effort/risk. This is the closest thing to a forcing function for ranked alternatives, but it runs once at planning time and the planner can only enumerate options it already knows about, not options that require investigation to surface.
- **`implement.md`** has `[NEEDS_SPLIT]`, `[ALREADY_COMPLETE]`, and `[BLOCKED]` exits, but no "this requires diagnosis before coding" exit and no requirement to enumerate options before writing the fix.
- **`review.md`** checks correctness, project standards, code quality, over-engineering, tests, security, and performance. It does not check for symptom-mitigation patterns.
- **`escalate.md`** runs only when a session signals a problem. Workarounds do not signal; they confidently produce a passing result, and the escalater never engages.

Net: nothing in the current loop catches a confidently-applied workaround.

## Proposed Changes

Four changes, ordered by impact-to-effort ratio.

### 1. New skill: `diagnose.md` — produce a ranked-options ISSUE artifact

A new skill that takes a symptom description and produces a `plans/ISSUE-<slug>.md` artifact with this structure:

- **Observed symptom**: exact error text, reproduction steps, environment details.
- **What was tried**: with commit SHA attribution if any commits exist for this symptom on the current branch.
- **Investigation findings**: file paths and line numbers of relevant code, citations.
- **Viable options** (>= 2): each with effort (S/M/L), risk (low/med/high), reversibility (easy/hard), and an explicit rejection rationale for any option ruled out.
- **Recommended option**: with explicit reasoning.
- **Open questions for human**: anything the agent could not resolve.

The artifact format mirrors `consensus-planner.md`'s Viable Options section but is scoped for diagnostic work rather than design work. The artifact is the input to a subsequent `dvx run` against the recommended option, OR is handed to a human for selection.

**Trigger conditions** (orchestrator-side):
- The task title contains diagnostic keywords (`fix`, `debug`, `broken`, `failing`, `error`, `investigate`, `crash`, `regression`).
- The implementer outputs `[NEEDS_INVESTIGATION]`.
- The escalater determines a flagged situation is bug-shaped.

**Standalone mode** (Claude Code direct invocation): infer the symptom from the user's message, run the investigation, write the ISSUE file, and offer to invoke `/dvx:implement` against the recommended option.

### 2. Add workaround-detection to `review.md`

Extend the review checklist with a "Symptom-Mitigation vs Root-Cause" section that flags:

- Linker flags or build settings that defer errors to runtime (`-undefined`, `dynamic_lookup`, `--allow-unresolved-symbols`).
- Broad `try/except: pass`, `catch (Exception _) {}`, or `_ = result` that swallow errors without rethrowing.
- Disabling strict mode or relaxing type checks to make code compile.
- Pinning to an older version to avoid a known-broken newer one without a corresponding ADR consequence note.
- Patches that change error-handling behavior without changing underlying logic.
- Comments or commit messages containing `workaround`, `hack`, `temporary`, `for now`, or `TODO: fix properly`.
- Configuration changes that make tests pass by reducing strictness (raising a threshold, skipping a test).

When any pattern is detected, the reviewer must:
- Output a new `[WORKAROUND_DETECTED]` tag with a list of which patterns matched.
- Require either (a) an explicit ADR Consequence note in the commit body explaining why root-cause is impractical, OR (b) escalation via `evaluate_trigger`.

### 3. Fix-mode branch in `implement.md`

When the task is bug-shaped (same trigger conditions as `diagnose`), the implementer must produce a `[VIABLE_OPTIONS]` block before writing code. Block shape:

```
[VIABLE_OPTIONS]

## Symptom
<one-line description>

## Options considered
1. <option name> — Effort: S/M/L | Risk: low/med/high | Reversibility: easy/hard
   Pros: ...
   Cons: ...
2. ...

## Chosen
<option N> because <reason>

## Why not the alternatives
- Option 1: ...
- Option 2: ...
```

The reviewer is then required to verify the chosen option is the cleanest viable one, not merely that the diff functions. If a non-recommended option is chosen, the implementer must include an ADR Consequence note in the commit body.

### 4. Orchestrator-side: detect linear-escalation thrashing

Current `increment_iteration` only counts review-loop bounces (review fails -> fix -> re-review). It does not detect the failure mode where each commit succeeds review but the *sequence* of commits shows escalating workarounds.

Add a thrashing detector that triggers the escalater proactively when any of these signals fire:

- The same file is modified in N consecutive task commits (default N=3).
- Commit messages contain reversal language: `actually`, `instead`, `revert`, `another approach`, `try again`.
- Diff trajectory shows strictly growing scope (file count or line count increasing across N commits).
- A commit reverts a substantial portion of a recent commit by the same task.

Implementation lives in `src/orchestrator.py` alongside `get_change_stats`. New function `detect_thrashing(plan_file, task) -> tuple[bool, str]` runs after each commit-phase. When it returns `True`, the orchestrator calls `evaluate_trigger` with source `orchestrator` and reason `Thrashing detected: <signals>`, allowing the escalater to decide whether to continue or block.

## Implementation Tasks

- [ ] Add `src/skills/diagnose.md` with the ISSUE-artifact format defined above.
- [ ] Add `is_bug_shaped(task) -> bool` helper to `src/orchestrator.py` for the diagnostic-keyword classifier (used by both `implement` and `diagnose` triggers).
- [ ] Add `dvx diagnose <symptom>` as a top-level command in `src/cli.py` that runs the diagnose skill standalone and writes `plans/ISSUE-<slug>.md`.
- [ ] Update `src/skills/review.md` with the Symptom-Mitigation section and the `[WORKAROUND_DETECTED]` tag.
- [ ] Extend `parse_review_result` in `src/orchestrator.py` to surface a `workaround_detected` boolean and matched-pattern list.
- [ ] Update `_run_orchestrator_inner` to route to `evaluate_trigger` when `workaround_detected` is True without a corresponding ADR Consequence note in the commit body.
- [ ] Update `src/skills/implement.md` to add the bug-shaped detection branch and require a `[VIABLE_OPTIONS]` block when triggered.
- [ ] Add `parse_viable_options(output) -> dict | None` to `src/orchestrator.py` returning the structured options block or `None`.
- [ ] Reject the implementer output and re-prompt once if `is_bug_shaped(task)` is True and `parse_viable_options` returns `None`.
- [ ] Add `detect_thrashing(plan_file, task) -> tuple[bool, str]` to `src/orchestrator.py` implementing the signals listed in change #4.
- [ ] Wire `detect_thrashing` into the commit-phase exit path in `_run_orchestrator_inner`.
- [ ] Update `src/skills/help.md` and `README.md` to document the new skill and command.
- [ ] Add unit tests in `dvx/tests/` covering `is_bug_shaped`, `parse_viable_options`, `detect_thrashing`, and the workaround-detection branch of `parse_review_result`.

## Acceptance Criteria

1. `dvx diagnose "<symptom>"` produces a well-formed `plans/ISSUE-<slug>.md` containing >= 2 ranked options with explicit rejection rationale for unselected ones.
2. A review of a diff that adds `OTHER_LDFLAGS = -Wl,-undefined,dynamic_lookup` (or any equivalent symptom-mitigation pattern from the list) emits `[WORKAROUND_DETECTED]` and routes through the escalater.
3. An implementer invoked on a task whose title contains a diagnostic keyword refuses to write code until it has emitted a `[VIABLE_OPTIONS]` block.
4. After three consecutive task commits modifying the same file with growing line counts, the orchestrator engages the escalater with `Thrashing detected` as the reason.
5. All existing dvx tests continue to pass; new tests cover each of the additions above.

## Out of Scope

- Changing the consensus loop (Planner -> Architect -> Critic) for feature work. The proposal targets bug/diagnostic flows specifically; feature flows remain unchanged.
- Modifying `interview.md`. Intent clarification is a separate axis from root-cause investigation.
- Persistent diagnostic memory across sessions (remembering that a specific upstream package omits a specific dependency so the next session does not re-investigate). That belongs to a separate working-memory system outside dvx.
- Auto-applying the fix recommended by `diagnose`. The proposal keeps human-or-agent judgment in the loop between diagnosis and implementation.

## Open Questions

1. Should `diagnose` be a standalone skill, or a mode of `interview`? Standalone keeps responsibilities clean; merging avoids skill proliferation.
2. Should `[WORKAROUND_DETECTED]` always escalate, or should it allow inline acceptance via an ADR Consequence note without going through the escalater? Always-escalate is safer; inline is faster.
3. Should the bug-shaped classifier be keyword-based, model-based (ask Claude "is this task diagnostic in nature?"), or both? Keyword is cheap and deterministic; model is more accurate but adds latency.
4. Should the thrashing detector look across tasks, not just within a single task? A pattern of small tasks each adding to the same file might be benign refactoring or might be thrashing-with-task-splits.
