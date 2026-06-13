Bug Report — dvx finalizer false-blocked TASK-01 on a verdict-tag placement violation

Summary

The run BLOCKED is spurious. The finalizer actually reached an [APPROVED] verdict — the implementation passed every check. The block was triggered purely by an output-format contract violation: the finalizer emitted conversational preamble before the verdict tag, so dvx's first-line parser couldn't find it.

What happened

- Task: task-01-ddr-sensitive-policy-27f6c110c838.md, Task 7 ("Write acceptance tests"), finalize phase, attempt 1/3.
- dvx blocked with: "Finalizer output did not start with a recognized verdict tag ([APPROVED] / [SUGGESTIONS] / [ISSUES] / [CRITICAL])."
- state.json: phase: "blocked", finalize_verdict: "BLOCKED", finalize_iterations: 1.

Root cause

The finalize contract (.claude/commands/dvx/finalize.md:128-132) is explicit:

▎ Verdict tag placement: the verdict tag MUST be the first non-empty line of your response. The orchestrator parses the first line to decide next steps — anything else is treated as a parse error and the run is blocked for human review.

The finalizer's actual output began with:

I now have a complete picture. Let me summarize my findings.

I reviewed all 6 commits, the full diff (27 files, ~4100 lines), and verified:
- Build: clean ...
...
[APPROVED]      <-- line 25, should have been line 1

The [APPROVED] tag was present but on line 25, behind ~11 lines of preamble. dvx only inspects the first non-empty line, found prose, and blocked.

Impact

- None on the code. The finalizench is clean: go build, go vet,golangci-lint (0 issues), unit tests (models/services/mcp/...), and integration tests
(repository RLS/immutability/dat028 & 033 apply, allow-record,block-before-egress, fail-closed, MCP parity) all pass. Verdict was APPROVED with one
non-blocking note.
- The block is a false positive caused entirely by tag placement.

The one substantive note the finalizer raised (carry forward, non-blocking)

Migrations on this branch are numbered 028 and 033, leaving a 029–032 gap. The runner
(pkg/database/migrations.go) traskips version <= current, so alater sibling task that introduces a migration numbered 029–032 and deploys incrementally onto
an env already at v33 would haveped. The finalizer deliberatelydid not renumber (rename could collide with a concurrent branch's allocation). Action:
downstream tasks (TASK-02/03/04/ion numbers ≥ 034.

Recommended fix

Two layers — fix the immediate brrence:

1. Unblock now (no code change):r-formatting failure mode, not an implementation defect. Re-run the finalize step; the branch is mergeable as-is. (dvx run
.../task-01-ddr-sensitive-policy)
2. Prevent recurrence (finalizer-side): The finalizer must output the verdict tag as the first
non-empty line with zero preambl analysis recap before the tag.All narrative belongs after the tag, under ## Summary. Optionally, harden dvx's parser to scan
for a verdict tag anywhere in thprose block) rather than failing on a non-tag first line, since the verdict was unambiguously present.

Verification after unblock

- make check (lint + test + build) on task-01-ddr-sensitive-policy.
- Confirm finalize emits [APPROV


Output from process:

```
Task 7: Write acceptance tests (unit, repository, integration, MCP, audit)
------------------------------------------------------------
  Implementing task 7...
Implementing task 7
Running implementer for task 7: Write acceptance tests (unit, repository, integration, MCP, audit)
Reviewing implementation...
Running reviewer...
  Committing changes...
Running implementer commit...
  Task 7 complete!

============================================================
FINALIZING
============================================================
All 7 tasks completed!

Running final review...
  Final review (attempt 1/3)...
Running finalizer for /home/damon/src/github.com/ekaya-inc/ekaya-engine/.dvx/watch/run-items/task-01-ddr-sensitive-policy-27f6c110c838.md
Blocked: Finalizer output did not start with a recognized verdict tag ([APPROVED] / [SUGGESTIONS] / [ISSUES] / [CRITICAL]).
Wrote blocked context to /home/damon/src/github.com/ekaya-inc/ekaya-engine/.dvx/task-01-ddr-sensitive-policy-27f6c110c838.md/blocked-context.md

============================================================
BLOCKED
============================================================
Reason: Finalizer output did not start with a recognized verdict tag ([APPROVED] / [SUGGESTIONS] / [ISSUES] / [CRITICAL]).

Context written to: /home/damon/src/github.com/ekaya-inc/ekaya-engine/.dvx/task-01-ddr-sensitive-policy-27f6c110c838.md/blocked-context.md

Run `dvx run /home/damon/src/github.com/ekaya-inc/ekaya-engine/.dvx/watch/run-items/task-01-ddr-sensitive-policy-27f6c110c838.md` and when resolved type `/exit` - dvx will continue.

dvx is blocked on /home/damon/src/github.com/ekaya-inc/ekaya-engine/.dvx/watch/run-items/task-01-ddr-sensitive-policy-27f6c110c838.md.
Error: Claude Code failed: dvx run exited with status 1
State preserved - re-run `dvx watch` to retry from the failed step.
```
