# PLAN: Add STOP Macro

**Status:** Implemented (on branch `add-sync-and-stop-macros`, pending merge)
**Type:** Feature — new control macro for `dvx watch`
**Affects:** `dvx/src/goals.py`, `dvx/tests/test_goals.py`, `README.md`

---

## Problem

There is no graceful way to tell `dvx watch` to finish its current work and exit. The only option today is `Ctrl+C`, which interrupts in the middle of processing and can leave state inconsistent or require a recovery restart.

A STOP macro fills this gap. When a `STOP` file is dropped in `.dvx/todo/`, the watcher lets any in-flight goal finish (including a pending merge or sync), then exits cleanly.

### Macro File Deletion

As a general rule for **all** control macros: the macro file is deleted when consumed. This applies to:

- **MERGE** — already deletes its file in `claim_merge_request` (on both success and validation failure).
- **STOP** — this plan. The STOP file is deleted when detected.
- **SYNC** — future macro (covered by PLAN-add-SYNC-macro.md). Must follow the same delete-on-consume pattern.

The contract is: a macro file is a one-shot signal. Once consumed, it is gone. If the user wants to trigger it again, they drop a new file. This plan includes auditing MERGE and documenting the pattern so SYNC (and any future macro) follows it.

## How MERGE Works (Reference)

The MERGE macro is the primary reference for control-file mechanics in the watch loop:

- **Control file**: `MERGE` dropped in `.dvx/todo/`. Empty = default branch; text = target branch name.
- **Queue priority**: claimed between items — after the in-flight item finishes and before the next queued one.
- **File deletion**: the MERGE file is deleted when `claim_merge_request` consumes it (or on validation failure).
- **Exclusion from scan**: `scan_goal_files` filters out `MERGE_FILE_NAME` so it never enters the goal queue.

STOP follows the same placement and claim pattern but with no multi-step processing — it is a single-action, single-pass macro.

## Design

### Control File

- **File name**: `STOP`
- **Location**: `.dvx/todo/` (the watched directory)
- **Content**: ignored (the file's presence is the signal; content is not parsed)

### Stop Behavior

When the STOP file is detected:

1. **If a goal is in-flight** (`state.current` is set): let it complete normally, then exit after cleanup.
2. **If a merge is in-flight** (`state.merge` is set): let it complete normally, then exit after cleanup.
3. **If a sync is in-flight** (`state.sync` is set): let it complete normally, then exit after cleanup.
4. **If nothing is in-flight**: exit immediately (cleanly).

In all cases, the STOP file is deleted before exit. The state file is saved so the user can resume later if desired — no partial work is abandoned.

### Watch Loop Integration

Insert STOP handling into `run_goal_watch` at the same priority tier as MERGE (before queue items, after in-flight items finish), but with **lowest control-precedence**. The precedence chain:

```
MERGE → SYNC → STOP → queue items
```

STOP is checked after MERGE and SYNC claim/processing. If STOP is the only pending macro, it is consumed and the loop exits. If MERGE or SYNC is also pending, those run first and STOP is consumed after they complete.

The check is inserted at the "nothing is running" gate — after any in-flight operation completes and before claiming the next work item. If STOP is found, delete the file, save state, and `return 0` (clean exit code).

```python
# Between items, after in-flight goal finishes:
# 1. claim_merge_request (highest control priority)
# 2. claim_sync_request (future)
# 3. check for STOP file (lowest control priority)
# 4. claim_next_goal (queue items)
```

STOP must also be respected **after** a MERGE or SYNC completes (not only between queue items). After any control macro finishes processing and the loop is about to `continue`, check for STOP before resuming to the next cycle.

### Implementation Sketch

```python
STOP_FILE_NAME = "STOP"

def stop_request_file(state: GoalState) -> Path:
    """Path to the STOP control file in the watched directory."""
    return Path(state.goals_dir) / STOP_FILE_NAME

def check_stop_request(state: GoalState) -> bool:
    """
    Check for a STOP file. If present, delete it and signal exit.
    Returns True if STOP was found and consumed (caller should return/exit).
    """
    stop_path = stop_request_file(state)
    if stop_path.is_file():
        stop_path.unlink()
        return True
    return False
```

The check is inserted at these points in `run_goal_watch`:

1. **Between items** (same gate as MERGE/SYNC claim): after in-flight goal finishes, after MERGE/SYNC claims, before `claim_next_goal`.
2. **After MERGE processing completes**: before the `continue` that resumes the loop.
3. **After SYNC processing completes**: before the `continue` that resumes the loop.

### Macro File Deletion Contract

This plan formalizes the pattern: **every control macro deletes its file when consumed**. The implementation tasks include:

- Verifying MERGE always deletes its file (success and failure paths).
- Implementing STOP file deletion in `check_stop_request`.
- Documenting the pattern in module-level comments so SYNC (and future macros) follow it.

The SYNC plan (PLAN-add-SYNC-macro.md) should be cross-referenced to ensure SYNC's `claim_sync_request` deletes the file, matching the contract established here.

### No State Persistence Needed

Unlike MERGE (which has a multi-step state machine), STOP does not need persisted state. If the process crashes after consuming STOP, the file is already gone and there is nothing to recover. The regular `state.current` and `state.merge` recovery paths handle any in-flight work independently.

### Guard Integration

STOP does not invoke Claude or git operations, so no guard changes are needed. It is a passive check that returns before any external process is started.

### Exclusion from Scan

`scan_goal_files` already filters out `MERGE_FILE_NAME`. Add `STOP_FILE_NAME` to the exclusion list so it never enters the goal queue.

## Implementation Tasks

- [ ] Add `STOP_FILE_NAME = "STOP"` constant to `goals.py`
- [ ] Add `stop_request_file(state)` helper returning `Path(state.goals_dir) / STOP_FILE_NAME`
- [ ] Add `check_stop_request(state)` — checks if STOP file exists, deletes it, returns `True` if found (consumed). Pure file system operation, no state mutation.
- [ ] Update `scan_goal_files` to exclude `STOP_FILE_NAME` alongside `MERGE_FILE_NAME`
- [ ] Update `run_goal_watch` watch loop to:
  - Between queue items (after in-flight goal finishes, after MERGE/SYNC claims, before `claim_next_goal`), call `check_stop_request(state)`. If True, save state and `return 0`.
  - After MERGE processing completes (before the `continue`), call `check_stop_request(state)`. If True, save state and `return 0`.
  - After SYNC processing completes (before the `continue`, when SYNC is implemented), call `check_stop_request(state)`. If True, save state and `return 0`.
  - Print `"Stop requested — exiting after current work."` when STOP is consumed.
- [ ] Audit MERGE file deletion: verify `claim_merge_request` always deletes the MERGE file (on both success and validation failure). Add assertions or comments confirming the contract.
- [ ] Add module-level comment documenting the macro file deletion contract: "Control macro files (MERGE, SYNC, STOP) are deleted when consumed by their respective claim functions. A macro file is a one-shot signal — once deleted, drop a new file to trigger again."
- [ ] Update module docstring to document the STOP control file alongside MERGE
- [ ] Update `cmd_watch` docstring in `cli.py` to document STOP
- [ ] Update `README.md` — add a "Stop the watch loop" section alongside "Merge the watch branch"
- [ ] Ensure `save_goal_state(state, project_dir)` is called before clean exit so state is preserved for resumption

## Acceptance Criteria

1. Dropping a `STOP` file in `.dvx/todo/` causes the watcher to exit cleanly after current work completes.
2. If a goal is in-flight, it runs to completion before the watcher exits.
3. If a merge is in-flight, it runs to completion before the watcher exits.
4. If a sync is in-flight (when implemented), it runs to completion before the watcher exits.
5. If nothing is in-flight, the watcher exits immediately.
6. The STOP file is deleted after consumption.
7. Exit code is `0` (clean exit), distinguishing it from crash/error exits.
8. State is saved before exit so the user can resume later.
9. If STOP is dropped while MERGE is processing, MERGE completes first, then the watcher exits (not the next queue item).
10. STOP is excluded from `scan_goal_files` (never enters the goal queue).
11. The macro file deletion contract is documented and verified for MERGE.
12. All existing tests pass; new tests cover STOP claim, exit timing, file deletion, and interaction with MERGE.

## Test Plan

New test class `TestStopRequest` in `test_goals.py`:

- `test_stop_file_detected_and_deleted` — STOP file is found, consumed, and deleted
- `test_stop_exits_immediately_when_idle` — no in-flight work → immediate `return 0`
- `test_stop_waits_for_in_flight_goal` — goal completes, then exit (mock runner finishes, then STOP is consumed)
- `test_stop_waits_for_in_flight_merge` — merge completes, then exit (STOP found after merge, not before next queue item)
- `test_stop_excluded_from_goal_scan` — `scan_goal_files` skips `STOP` file
- `test_stop_file_content_ignored` — STOP file with any content behaves the same
- `test_stop_preserves_state_before_exit` — state file is saved after STOP consumption
- `test_stop_returns_zero_exit_code` — exit code is `0` (clean)
- `test_no_stop_file_normal_loop_continues` — absent STOP → loop continues as normal
- `test_merge_deletes_file_on_success` — MERGE file is gone after successful claim
- `test_merge_deletes_file_on_validation_failure` — MERGE file is gone after bad branch name rejection
- `test_stop_after_merge_completes` — STOP file dropped during merge → consumed after merge finishes, before next queue item

## Out of Scope

- Graceful shutdown signal (SIGTERM/SIGINT). STOP is file-based only; OS signals are handled by Python's default interrupt behavior.
- Forced/abort exit. STOP waits for current work to finish. If the user needs immediate termination, `Ctrl+C` is still available.
- STOP from outside the watched directory. The STOP file must be in `.dvx/todo/`.
- Combining STOP with other file content. The STOP file is a signal only; its content is ignored.
- SYNC implementation. SYNC is covered by PLAN-add-SYNC-macro.md; this plan only ensures STOP checks for `state.sync` and that SYNC follows the delete-on-consume contract.

## Open Questions

1. Should STOP print a message when it is *detected* during in-flight work (e.g. "Stop requested — will exit after current goal"), or only on the way out? Recommendation: print on detection so the user knows the request was received.
2. Should STOP be a persistent flag (if the file reappears during a long merge, does it still trigger after the merge ends)? With the delete-on-consume pattern, the answer is no — the file must be present at the check point. This matches MERGE's behavior. Recommendation: delete on consume, no persistence.
