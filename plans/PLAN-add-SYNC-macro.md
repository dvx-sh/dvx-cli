# PLAN: Add SYNC Macro

**Status:** Implemented (on branch `add-sync-and-stop-macros`, pending merge)
**Type:** Feature — new control macro for `dvx watch`
**Affects:** `dvx/src/goals.py`, `dvx/tests/test_goals.py`, `README.md`

---

## Problem

The MERGE macro merges the watch branch _into_ a remote target branch. This is useful for shipping work from a watch branch into `main`, `dev`, or another integration branch.

A complementary need: bring updates _from_ a remote branch _into_ the watch branch. When other developers push to `main` while a watch process is running, the watch branch diverges. The user currently has no way to ask the watcher to sync upstream changes in without interrupting the watch loop.

SYNC fills this gap. It fetches a remote branch (default: `origin/main`), merges it into the watch branch, and pushes the updated watch branch to origin. If the SYNC file is empty, the remote's default branch is used. If it contains text, that text names the branch to pull from.

## How MERGE Works (Reference)

The MERGE macro is the model SYNC follows. Key mechanics:

- **Control file**: `MERGE` dropped in `.dvx/todo/`. Empty = default branch; text = target branch name.
- **Queue priority**: claimed in `run_goal_watch` between items — after the in-flight item finishes and before the next queued one. It takes precedence over the queue (`if state.current is None and state.merge is None → claim_merge_request`).
- **State machine**: three statuses (`MERGE_STATUS_CLAIMED` → `MERGE_STATUS_LOCAL_MERGED` → `MERGE_STATUS_TARGET_PUSHED`), persisted in `state.merge`, crash-resumable.
- **Step 1 — `_step_merge_local`**: checkout watch branch, `git fetch <remote> <target>`, `git merge <target>` into watch branch. On conflict, invoke `resolve_merge_conflicts_with_claude`. Verify ancestry after resolution.
- **Step 2 — `_step_push_target`**: `git push <remote> <watch_branch>:refs/heads/<target>` (fast-forward the remote target to the watch branch tip). On non-fast-forward rejection, re-fetch and re-merge (up to `MERGE_PUSH_MAX_ATTEMPTS`).
- **Step 3 — `_push_watch_branch`**: `git push <remote> <watch_branch>`.
- **Claim validation**: checks remote exists, target branch is a valid git ref name, target differs from watch branch, target exists on remote, working tree is clean.
- **Conflict resolution prompt**: `resolve_merge_conflicts_with_claude` — tells Claude to resolve merge conflicts, stage, and commit.
- **Exclusion from scan**: `scan_goal_files` filters out `MERGE_FILE_NAME` from the goal queue.

SYNC mirrors this structure with different git operations.

## Design

### Control File

- **File name**: `SYNC`
- **Location**: `.dvx/todo/` (the watched directory)
- **Content**:
  - Empty (0 bytes): use the remote's default branch (same resolution as MERGE: `git symbolic-ref refs/remotes/origin/HEAD` → `git ls-remote --symref origin HEAD`)
  - Non-empty text: single branch name (e.g. `main`, `develop`) — override the default

### Sync Operations

The SYNC macro performs three git operations in sequence:

1. **Fetch and merge upstream into watch branch**
   - `git fetch origin <target>`
   - `git merge origin/<target>` into the watch branch
   - On conflict, invoke Claude Code to resolve (reuse the conflict resolution pattern from MERGE)
   - After resolution, verify `origin/<target>` is an ancestor of the watch branch

2. **Push watch branch to origin**
   - `git push origin <watch_branch>`
   - Unlike MERGE, this pushes the watch branch _to itself_ (not to the target)
   - On non-fast-forward rejection, re-fetch and re-merge (same retry loop as MERGE's `_step_push_target`)

3. **Done** — return watch loop to normal processing

Key difference from MERGE: MERGE pushes `watch_branch:refs/heads/<target>` (advancing the target branch). SYNC pushes just `watch_branch` (advancing the watch branch itself).

### State Machine

Mirror MERGE's three-step status chain, with SYNC-specific statuses:

```python
SYNC_STATUS_CLAIMED = "claimed"           # SYNC file consumed, target recorded
SYNC_STATUS_LOCAL_MERGED = "local_merged" # upstream merged into watch branch
SYNC_STATUS_WATCH_PUSHED = "watch_pushed" # watch branch pushed to origin
```

The `GoalState` dataclass gains a new field:

```python
sync: Optional[dict] = None
```

The sync dict mirrors merge's structure:

```python
{
    "sync_file": "SYNC",
    "remote": "origin",
    "target": "main",
    "status": SYNC_STATUS_CLAIMED,
    "push_attempts": 0,
    "requested_at": "...",
}
```

### Watch Loop Integration

Insert SYNC handling into `run_goal_watch` alongside MERGE, with **equal precedence** (both run before queue items, both wait for in-flight items to finish):

```python
# Current: claim_merge_request, then process_merge_request
# New: claim_merge_request, claim_sync_request, process_merge_request, process_sync_request

if state.current is None and state.merge is None and state.sync is None and should_claim:
    claimed_merge, error = claim_merge_request(state, project_dir)
    # ... existing merge claim logic ...
    if not claimed_merge:
        claimed_sync, error = claim_sync_request(state, project_dir)
        # ... sync claim logic ...
```

The precedence chain: MERGE → SYNC → queue items. Both MERGE and SYNC are "control macros" that jump ahead of queued goals.

### Guard Integration

SYNC's local merge step invokes Claude for conflict resolution, which means the queued-goal guard must wrap the session (same as MERGE). The `_begin_queued_goal_guard` / `_finish_queued_goal_guard` pair already checks `_guard_holder(state)` which returns `state.current or state.merge` — extend it to `state.current or state.merge or state.sync`.

### Conflict Resolution

Reuse `resolve_merge_conflicts_with_claude` with the target as `origin/<target>`. The existing prompt ("A `git merge` of {target} into this branch stopped on conflicts...") applies equally to SYNC's merge. No new prompt needed.

### Exclusion from Scan

`scan_goal_files` already filters out `MERGE_FILE_NAME`. Add `SYNC_FILE_NAME` to the exclusion list so it never enters the goal queue.

## Implementation Tasks

- [ ] Add `SYNC_FILE_NAME = "SYNC"` constant to `goals.py`
- [ ] Add `SYNC_STATUS_CLAIMED`, `SYNC_STATUS_LOCAL_MERGED`, `SYNC_STATUS_WATCH_PUSHED` constants
- [ ] Add `sync: Optional[dict] = None` field to `GoalState` dataclass
- [ ] Add `SYNC_PUSH_MAX_ATTEMPTS = 3` constant (same as `MERGE_PUSH_MAX_ATTEMPTS`)
- [ ] Add `sync_request_file(state)` helper returning `Path(state.goals_dir) / SYNC_FILE_NAME`
- [ ] Add `claim_sync_request(state, project_dir)` — mirrors `claim_merge_request` but validates the sync target branch (must exist on remote, valid ref name, not equal to watch branch). Empty file resolves to default branch. On validation failure, delete file and record in `state.failed`. On dirty tree, set `state.blocked` and leave file in place.
- [ ] Add `_step_sync_local(state, merge_runner, project_dir)` — mirrors `_step_merge_local`: checkout watch branch, fetch target, merge `origin/<target>` into watch branch, resolve conflicts via Claude if needed, verify ancestry.
- [ ] Add `_step_push_watch(state)` — mirrors `_step_push_target` but pushes `watch_branch` to `origin/<watch_branch>` instead of `watch_branch:refs/heads/<target>`. Returns `(ok, error, remote_moved)` with same non-fast-forward detection.
- [ ] Add `process_sync_request(state, merge_runner, project_dir, model)` — state machine mirroring `process_merge_request`: `CLAIMED → _step_sync_local → LOCAL_MERGED → _step_push_watch → WATCH_PUSHED`. On non-ff rejection, reset to `CLAIMED` and retry (up to `SYNC_PUSH_MAX_ATTEMPTS`). On success, append to `state.completed`, clear `state.sync`.
- [ ] Update `_guard_holder(state)` to include `state.sync` in the OR chain
- [ ] Update `scan_goal_files` to exclude `SYNC_FILE_NAME` alongside `MERGE_FILE_NAME`
- [ ] Update `_has_resumable_work(state)` to include `state.sync` in the check
- [ ] Update `run_goal_watch` watch loop to:
  - Claim SYNC requests after MERGE claim (when neither `current`, `merge`, nor `sync` is set)
  - Process `state.sync` when set and `state.current` is None (after merge processing, before queue item claiming)
  - Handle `state.blocked` with `sync_file` key (analogous to `merge_file`)
  - Print appropriate status messages for sync claim, block, and completion
- [ ] Update `clear_goal_state` behavior: already removes entire `.dvx/watch/` directory, so no changes needed
- [ ] Update module docstring to document the SYNC control file alongside MERGE
- [ ] Update `cmd_watch` docstring in `cli.py` to document SYNC
- [ ] Update `README.md` — add a "Sync the watch branch" section alongside "Merge the watch branch"

## Acceptance Criteria

1. Dropped `SYNC` file (empty) causes the watcher to fetch `origin/main` (or default branch), merge it into the watch branch, and push the watch branch.
2. `SYNC` file containing a branch name (e.g. `develop`) overrides the default and syncs from that branch.
3. SYNC runs between queued items — after the in-flight item finishes and before the next one starts — and takes precedence over the queue.
4. SYNC and MERGE can coexist: if both files are dropped, MERGE is claimed first, then SYNC is claimed and processed after.
5. Conflict resolution during the merge step invokes Claude Code and resumes cleanly after resolution.
6. Non-fast-forward push rejection triggers re-fetch and re-merge (up to `SYNC_PUSH_MAX_ATTEMPTS`).
7. Crashed SYNC state recovers on `dvx watch` restart (state persisted through `state.sync`).
8. Dirty working tree blocks SYNC claim (file left in place, reported as blocked).
9. Invalid branch names and missing remotes are rejected with clear error messages.
10. All existing tests pass; new tests cover SYNC claim, processing, conflict resolution, push retry, crash recovery, and precedence with MERGE.

## Test Plan

New test class `TestSyncRequest` alongside `TestMergeRequest` in `test_goals.py`:

- `test_empty_sync_file_fetches_default_branch` — empty file resolves to remote default
- `test_named_target_sync_merges_into_watch_branch` — `main` in file merges `origin/main` into watch
- `test_sync_pushes_watch_branch_not_target` — verify push target is `watch_branch`, not `refs/heads/<target>`
- `test_sync_conflicts_resolved_by_merge_runner` — Claude resolves conflicts, merge completes
- `test_sync_non_ff_push_refetch_and_remerge` — push rejected, re-fetch, re-merge succeeds
- `test_sync_max_push_attempts_exhausted` — after 3 rejections, gives up with message
- `test_sync_takes_precedence_over_queued_goals` — queued goal starts after SYNC completes
- `test_merge_precedence_over_sync` — MERGE claimed before SYNC when both files present
- `test_sync_claim_invalid_branch_rejected` — bad branch name → deleted + recorded in failed
- `test_sync_claim_no_remote_rejected` — no remote configured → deleted + failed
- `test_sync_claim_dirty_tree_blocked` — dirty paths → blocked state, file left
- `test_sync_recovers_from_local_merged_status` — crash after merge, resume from push
- `test_sync_excluded_from_goal_scan` — `scan_goal_files` skips `SYNC` file
- `test_has_resumable_work_includes_sync` — `state.sync` prevents state discard

## Out of Scope

- Forcing a push when the watch branch is behind. SYNC uses the same non-force push as MERGE; if the user force-pushed the watch branch elsewhere, they'll need to handle that manually.
- Syncing without a git remote. SYNC requires a remote (like MERGE). Local-only repos cannot use it.
- Syncing multiple branches at once. One SYNC file, one branch. Drop another SYNC file after completion for a second branch.
- Auto-sync on a schedule. SYNC is triggered by dropping a file, not by time or divergence detection.

## Open Questions

1. Should SYNC and MERGE share the same `MERGE_PUSH_MAX_ATTEMPTS` / `SYNC_PUSH_MAX_ATTEMPTS`, or should we use a single shared constant? Keeping them separate avoids coupling; a shared constant reduces duplication. Recommendation: keep separate for now (same value, independent tuning).
2. If both MERGE and SYNC files exist and MERGE fails, should SYNC still be claimed? Currently, MERGE failure returns from the watch loop with error code 1. The user re-runs `dvx watch`, at which point SYNC would be claimed (MERGE file is already consumed). Recommendation: this is the natural behavior — don't change it.
3. Should SYNC validate that the target branch name differs from the watch branch? Merging the watch branch into itself is a no-op. MERGE rejects this. Recommendation: yes, follow MERGE's pattern.
