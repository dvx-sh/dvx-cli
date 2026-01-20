# PLAN: Create DVX Skills for Claude Code (Phase 1 - Core)

**Status**: Complete
**Prepared**: 2025-01-10
**Repo**: dvx-cli

---

## Executive Summary

Create `/dvx:*` Claude Code skills that replace prompt files. This is the minimal viable skill system:

1. **Convert prompt files to skills** - Same behavior, new format
2. **Install skills with pip** - `pip install dvx` sets up everything
3. **Basic interactive skills** - `/dvx:help`, `/dvx:status`

Phase 2 (separate plan) will add GSD-inspired features like `/dvx:pause`, `/dvx:resume`, `/dvx:map`, `/dvx:verify`, etc.

---

## What We're Building

### Core Skills (7 orchestration + 2 interactive = 9 total)

**Orchestration Skills** (replace prompt files, invoked by `dvx run`):
| Skill | Replaces |
|-------|----------|
| `/dvx:implement` | `implementer.md` |
| `/dvx:implement-fix` | `implementer-fix.md` |
| `/dvx:review` | `reviewer.md` |
| `/dvx:escalate` | `escalater.md` |
| `/dvx:split-task` | `task-splitter.md` |
| `/dvx:polish` | `polisher.md` |
| `/dvx:finalize` | `finalizer.md` |

**Interactive Skills** (for humans in Claude Code):
| Skill | Purpose |
|-------|---------|
| `/dvx:help` | List all skills |
| `/dvx:status` | Show current state |

### What's Deferred to Phase 2

These GSD-inspired features will come later:
- `/dvx:progress` - Smart status + routing
- `/dvx:pause` / `/dvx:resume` - Session handoff
- `/dvx:map` - Codebase analysis
- `/dvx:verify` - User acceptance testing
- `/dvx:triage` - FIX file review

---

## Design

### Skill File Format

```markdown
---
category: dvx
name: implement
description: Implement a single task from a plan file
arguments:
  - name: task_id
    description: The task ID to implement
    required: true
  - name: plan_file
    description: Path to the PLAN file
    required: true
---

# DVX Implement

You are implementing task $ARGUMENTS.task_id from $ARGUMENTS.plan_file.

[... rest of prompt ...]
```

### How the Orchestrator Invokes Skills

```python
def run_skill(skill_name: str, args: dict[str, str], model: str = None) -> SessionResult:
    """Invoke a /dvx:* skill."""
    arg_str = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in args.items())
    prompt = f"/dvx:{skill_name} {arg_str}"
    return run_claude(prompt, model=model)
```

### Installation

```bash
pip install dvx
```

This:
1. Installs the `dvx` CLI
2. Copies skills to `~/.claude/commands/dvx/`
3. Skills immediately work as `/dvx:*` in Claude Code

---

## Implementation Plan

### [x] Task 1: Create Skills Directory

Set up the skills directory structure.

**Create:**
- `src/skills/` directory
- `src/skills/_template.md` showing the format

### [x] Task 2: Port Orchestration Skills

Convert all 7 prompt files to skill format.

**For each skill:**
1. Add YAML frontmatter (category, name, description, arguments)
2. Replace `.format()` placeholders with `$ARGUMENTS.name` syntax
3. Keep all output markers identical

**Files:**
- `src/skills/implement.md`
- `src/skills/implement-fix.md`
- `src/skills/review.md`
- `src/skills/escalate.md`
- `src/skills/split-task.md`
- `src/skills/polish.md`
- `src/skills/finalize.md`

### [x] Task 3: Create Help and Status Skills

Basic interactive skills for humans.

**Files:**
- `src/skills/help.md` - Lists all /dvx:* skills
- `src/skills/status.md` - Shows .dvx/ state

### [x] Task 4: Add run_skill() to Orchestrator

New function to invoke skills instead of loading prompts.

**Changes to `src/orchestrator.py`:**
- Add `run_skill(name, args, model)` function
- Update `run_implementer()` → uses `/dvx:implement`
- Update `run_reviewer()` → uses `/dvx:review`
- Update all other `run_*()` functions
- Remove `load_prompt()` function

### [x] Task 5: Add Skill Installation

Install skills when dvx is installed.

**Approach:** On first `dvx` run, check if `~/.claude/commands/dvx/` exists. If not, copy skills from package.

**Files:**
- `src/cli.py` - Add `ensure_skills_installed()` function
- `pyproject.toml` - Include skills in package data

### [x] Task 6: Remove Prompt Files

Clean up old architecture.

**Delete:**
- `prompts/` directory and all files

### [x] Task 7: Update README

Document the new skill-based architecture.

**Add:**
- Skills are installed automatically
- Available `/dvx:*` commands
- How to use skills interactively

---

## Success Criteria

1. `pip install dvx` installs skills to `~/.claude/commands/dvx/`
2. `dvx run` works exactly as before (uses skills internally)
3. `/dvx:help` shows all available skills
4. `/dvx:status` shows orchestration state
5. All existing tests pass
6. Prompt files are deleted

---

## Skill Reference (Phase 1)

| Skill | Description | Invoked By |
|-------|-------------|------------|
| `/dvx:implement` | Implement a task | CLI |
| `/dvx:implement-fix` | Fix based on feedback | CLI |
| `/dvx:review` | Review implementation | CLI |
| `/dvx:escalate` | Decide escalation | CLI |
| `/dvx:split-task` | Analyze complexity | CLI |
| `/dvx:polish` | Holistic review | CLI |
| `/dvx:finalize` | Final quality gate | CLI |
| `/dvx:help` | List all skills | Human |
| `/dvx:status` | Show state | Human |

---

## Credits

Design influenced by [get-shit-done](https://github.com/glittercowboy/get-shit-done) by TÂCHES (MIT License).

---

## Phase 2 Preview

After Phase 1 is working, a separate PLAN will add:
- `/dvx:progress` - Intelligent status + next action routing
- `/dvx:pause` / `/dvx:resume` - Session handoff files
- `/dvx:map` - Codebase structure analysis
- `/dvx:verify` - Guided user acceptance testing
- `/dvx:triage` - FIX file prioritization
- `/dvx:plan` - Interactive plan creation
