---
category: dvx
name: deslop
description: Clean up LLM-written prose artifacts in files changed during this dvx session
arguments:
  - name: changed_files
    description: Newline-separated list of files changed during this session — DO NOT widen beyond this set
    required: true
  - name: plan_file
    description: Path to the PLAN file (context only)
    required: false
---

# Deslop Pass

You are running the **deslop pass**: a final cleanup that removes LLM-written
prose artifacts from code. Functionality is not your concern — the finalize
gate already covered correctness. You are cleaning up the writing.

## Scope: changed files only

You may ONLY modify files in this list:

```
$ARGUMENTS.changed_files
```

Any edit outside this list is a violation. If nothing in the list needs
deslop, emit `[DESLOP_NOOP]` and stop. Do not widen scope to "related"
files.

## What to remove

1. **Obvious comments** that restate what identifier names already say.
   `# increment counter` above `counter += 1`. Remove.
2. **Task-referencing comments**: `# added for issue #123`, `# per the
   plan`, `# from the interview spec`. Remove.
3. **Planning artifacts**: `# TODO: consider ...`, `# design note: ...`,
   leftover scratchpad bullets. Remove unless they describe a non-obvious
   invariant.
4. **Redundant docstrings** that restate the signature. Remove — keep
   docstrings that add real info.
5. **Defensive validation on trusted inputs**: null checks and type checks
   on internal-only callers where the types are already guaranteed by
   the caller. Remove. Leave validation at system boundaries alone.
6. **Premature abstractions**: single-use helper classes, wrapper
   functions that just forward. Inline.
7. **Empty `__init__.py` re-exports** that exist only to flatten the
   import path without any filtering. Simplify.
8. **Verbose error messages** that repeat the exception type with no
   extra context. Tighten.

## What to leave alone

- Comments describing non-obvious invariants, workarounds, or domain
  knowledge (even if terse).
- Docstrings with real content — examples, behavior notes, edge cases.
- Validation at trust boundaries (user input, external APIs, RPC
  callers).
- Error messages that include runtime context (file paths, values).
- Project-voice stylistic choices that match the surrounding code.
- **Anything you are not sure about.** If removing it might matter, leave
  it.

## Rules of engagement

- One commit at the end, prefixed `cleanup:`. Describe the categories you
  touched ("cleanup: remove obvious comments and redundant docstrings"),
  not per-file minutiae.
- Do NOT touch tests. Test prose is intentionally verbose.
- Do NOT rename identifiers. Rename is a separate concern.
- Do NOT reorganize files. Deslop operates within file boundaries.
- After your edits, run the project's test command and include the
  result in your final report.

## Output contract

Finish with a markdown section on the last lines of your response:

```
## Deslop report

- Files modified: <count>
- Categories hit: <e.g. 1, 3, 5>
- Tests after cleanup: <PASS | FAIL | SKIPPED — with short reason>
- Rollback note: <empty, or "reverted changes in <file> because <reason>">
```

If no cleanup was warranted, emit `[DESLOP_NOOP]` at the very top and
skip the report — the orchestrator interprets a no-op as success.

## Standalone mode

If invoked as `/dvx:deslop` without the changed_files argument, ask the
user which files or diff range they want cleaned before touching
anything. Never guess the scope.
