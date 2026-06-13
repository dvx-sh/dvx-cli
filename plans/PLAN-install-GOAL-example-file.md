# PLAN: Install GOAL.md.example with dvx Instead of Curl-Downloading

**Status:** Planned
**Type:** UX improvement — ship the goal template as an installed file
**Affects:** `install.sh`, `README.md`

---

## Problem

The README.md install prompt asks the AI agent to:

```
curl -s https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/GOAL.md.example -o GOAL.md.example
```

This means every new user's AI agent hits GitHub at install time to download a file that is already part of the dvx distribution. It's an unnecessary network call, and it scatters the template into the user's project directory instead of keeping it with the installed tool.

## Design

### Ship GOAL.md.example as an Installed File

Place `GOAL.md.example` into `~/.dvx/` alongside the installed source tree and bin directory. The canonical location after installation will be:

```
~/.dvx/GOAL.md.example
```

### Update install.sh

Add a copy step in the "Copy files" section of `install.sh`, after `cp -r "$DVX_PAYLOAD/"* "$DVX_HOME/"`. Since `GOAL.md.example` lives at the repo root (not inside `dvx/`), it needs an explicit copy:

```bash
# Copy the goal template so AI agents can reference it without curl
cp "${SRC_ROOT}/GOAL.md.example" "$DVX_HOME/GOAL.md.example"
```

This runs in both local and remote modes because `SRC_ROOT` is set for both paths.

### Update README.md Install Prompt

Replace the curl step with a reference to the installed file:

**Before:**
```
Install dvx for me:

1. Run: curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
2. Download the goal template into this project:
   curl -s https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/GOAL.md.example -o GOAL.md.example
3. Make sure ~/.dvx/bin is on my PATH...
```

**After:**
```
Install dvx for me:

1. Run: curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
2. Make sure ~/.dvx/bin is on my PATH...
```

The "Queue work" section already references `GOAL.md.example` by name. It should be updated to point agents to the installed copy:

**Before:**
```
Read GOAL.md.example, then create a well-scoped GOAL*.md file in .dvx/todo/ for this task:
```

**After:**
```
Read ~/.dvx/GOAL.md.example, then create a well-scoped GOAL*.md file in .dvx/todo/ for this task:
```

## Implementation Tasks

- [ ] Add `cp "${SRC_ROOT}/GOAL.md.example" "$DVX_HOME/GOAL.md.example"` to `install.sh` in the "Copy files" section (after the `cp -r` of the dvx payload, before the chmod step)
- [ ] In `README.md`, remove the "Download the goal template" step (step 2) from the install prompt and renumber step 3 to step 2
- [ ] In `README.md`, update the "Queue work" section to reference `~/.dvx/GOAL.md.example` instead of `GOAL.md.example`
- [ ] In `README.md`, update any other references to `GOAL.md.example` (without a path) to `~/.dvx/GOAL.md.example`
- [ ] Verify no tests or other files reference the curl-download pattern

## Acceptance Criteria

1. After running `install.sh` (local or remote), `~/.dvx/GOAL.md.example` exists and matches the repo's `GOAL.md.example`.
2. The README.md install prompt no longer asks the agent to curl-download `GOAL.md.example`.
3. The README.md "Queue work" section references `~/.dvx/GOAL.md.example`.
4. The install prompt is now a single-step install (just the curl | bash, then PATH).

## Out of Scope

- Making `GOAL.md.example` available as a `dvx` subcommand (e.g., `dvx goal-template`). That's a separate feature.
- Copying the file into the project directory during installation. The agent or user copies it as needed from `~/.dvx/`.
