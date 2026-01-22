# PLAN: Refactor Embedded Prompts to Skills

## Goal

Move embedded prompts from Python code to skill files where manual invocation is valuable. Keep process-specific prompts embedded when they serve automation only.

---

## Current State

### Already Skills (invoked via `run_skill()`)
| Skill | Used By | Manual Use Case |
|-------|---------|-----------------|
| `implement.md` | orchestrator | Debug implementation |
| `review.md` | orchestrator | Manual code review |
| `polish.md` | orchestrator | Manual polish pass |
| `finalize.md` | orchestrator | Manual final check |
| `escalate.md` | orchestrator | Debug escalation |
| `split-task.md` | orchestrator | Manual task splitting |
| `implement-fix.md` | orchestrator | Debug fix application |
| `status.md` | cli | Check orchestration status |
| `help.md` | cli | Show available commands |

### Embedded Prompts (candidates for extraction)

| Location | Purpose | Manual Value? |
|----------|---------|---------------|
| `cli.py:145` | Create new plan | **HIGH** - `/dvx:create-plan` |
| `cli.py:125` | Update existing plan | **HIGH** - `/dvx:update-plan` |
| `cli.py:337` | Resolve blocked state | **MEDIUM** - `/dvx:resolve-blocked` |
| `orchestrator.py:1057` | Commit after task | **HIGH** - `/dvx:commit-task` |
| `orchestrator.py:722` | Apply polish suggestions | **LOW** - tightly coupled to polish output |
| `orchestrator.py:876` | Address finalizer issues | **LOW** - tightly coupled to finalizer output |
| `orchestrator.py:1674` | Add missing tests | **MEDIUM** - `/dvx:add-tests` |
| `plan_parser.py:261` | Parse plan to JSON | **NONE** - returns structured data for code |
| `plan_parser.py:180` | Compress oversized plan | **NONE** - automated maintenance |

---

## Design Decisions

### Extract to Skills (HIGH + MEDIUM value)

1. **`create-plan.md`** - Create implementation plan from requirements
   - Arguments: `requirements`, `output_file`
   - Manual: `/dvx:create-plan "Add dark mode support"`

2. **`update-plan.md`** - Update existing plan with new requirements
   - Arguments: `plan_file`, `changes`
   - Manual: `/dvx:update-plan plans/PLAN-foo.md "Add error handling"`

3. **`commit-task.md`** - Commit completed task with proper message
   - Arguments: `task_id`, `task_title`, `plan_file`
   - Manual: `/dvx:commit-task 2.1 "Add login form" plans/PLAN-auth.md`

4. **`add-tests.md`** - Add missing tests for implementation
   - Arguments: `task_id`, `task_title`, `reviewer_notes`
   - Manual: `/dvx:add-tests 1 "API endpoint" "Missing edge case tests"`

5. **`resolve-blocked.md`** - Help resolve blocked orchestration
   - Arguments: `plan_file`, `blocked_reason`, `context`
   - Manual: `/dvx:resolve-blocked` (interactive)

### Keep Embedded (LOW/NONE value)

1. **Apply polish** (`orchestrator.py:722`) - Receives structured polish output, tightly coupled
2. **Address finalizer issues** (`orchestrator.py:876`) - Receives structured issue list, tightly coupled
3. **Parse plan** (`plan_parser.py:261`) - Returns JSON for orchestrator, no human use case
4. **Compress plan** (`plan_parser.py:180`) - Automated maintenance, already commits

---

## Implementation Tasks

### Phase 1: Create New Skills

1. [ ] Create `src/skills/create-plan.md`
   - Extract from `cli.py:145`
   - Add arguments: `requirements`, `output_file` (optional)
   - Include plan structure guidelines

2. [ ] Create `src/skills/update-plan.md`
   - Extract from `cli.py:125`
   - Add arguments: `plan_file`, `changes`
   - Include update guidelines (preserve structure, add tasks)

3. [ ] Create `src/skills/commit-task.md`
   - Extract from `orchestrator.py:1057`
   - Add arguments: `task_id`, `task_title`, `plan_file`
   - Include lean commit guidelines (already in implement.md)

4. [ ] Create `src/skills/add-tests.md`
   - Extract from `orchestrator.py:1674`
   - Add arguments: `task_id`, `task_title`, `reviewer_notes`
   - Include test writing guidelines

5. [ ] Create `src/skills/resolve-blocked.md`
   - Extract from `cli.py:337`
   - Add arguments: `plan_file`, `blocked_reason`, `context`
   - Include resolution strategies

### Phase 2: Update Code to Use Skills

6. [ ] Update `cli.py` to use `run_skill("create-plan", ...)`
   - Replace embedded prompt at line 145
   - Handle argument passing

7. [ ] Update `cli.py` to use `run_skill("update-plan", ...)`
   - Replace embedded prompt at line 125
   - Handle argument passing

8. [ ] Update `orchestrator.py` to use `run_skill("commit-task", ...)`
   - Replace `run_implementer_commit()` prompt at line 1057
   - Keep function wrapper for orchestrator integration

9. [ ] Update `orchestrator.py` to use `run_skill("add-tests", ...)`
   - Replace embedded prompt at line 1674
   - Keep function wrapper for orchestrator integration

10. [ ] Update `cli.py` to use `run_skill("resolve-blocked", ...)`
    - Replace embedded prompt at line 337
    - Handle interactive resolution

### Phase 3: Documentation

11. [ ] Update `help.md` to list new manual skills
12. [ ] Add examples to each new skill's description

---

## Verification

- [ ] All existing tests pass
- [ ] `dvx plan "test requirement"` works (uses create-plan skill)
- [ ] `dvx run` completes full cycle (uses commit-task skill)
- [ ] Manual skill invocation works: `/dvx:create-plan`, `/dvx:commit-task`

---

## Files to Create

- `src/skills/create-plan.md`
- `src/skills/update-plan.md`
- `src/skills/commit-task.md`
- `src/skills/add-tests.md`
- `src/skills/resolve-blocked.md`

## Files to Modify

- `src/cli.py` - Use skills instead of embedded prompts
- `src/orchestrator.py` - Use skills instead of embedded prompts
- `src/skills/help.md` - Document new skills
