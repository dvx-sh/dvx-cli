# Move dvx/ Contents to Project Root

## Problem

AI harnesses (like pi) cannot find the `.venv` directory because it's nested under `dvx/.venv/` instead of the project root. Flattening the structure so that all source files live at the repository root resolves this.

## Current Layout

```
dvx-cli/
├── install.sh
├── README.md
├── AGENTS.md
├── GOAL.md.example
├── LICENSE
├── .gitignore            # comprehensive root .gitignore
├── plans/
├── hooks/
├── .claude/, .github/, .dvx/, .omx/, .gstack/   # dotdirs
└── dvx/                  <-- nested payload
    ├── .gitignore        # only contains: .dvx/
    ├── pyproject.toml
    ├── tasks.py
    ├── uv.lock
    ├── bin/
    │   ├── dvx
    │   ├── setup
    │   └── dev-setup
    ├── src/
    ├── tests/
    └── plans/
```

## Target Layout

```
dvx-cli/
├── install.sh            # updated for flat layout
├── README.md
├── AGENTS.md             # updated paths
├── GOAL.md.example
├── LICENSE
├── .gitignore            # keep the comprehensive root one; drop dvx/.gitignore
├── pyproject.toml        # moved from dvx/ (package-dir src is still correct)
├── tasks.py
├── uv.lock
├── plans/                # merge dvx/plans/ into root plans/
├── hooks/
├── bin/
│   ├── dvx               # moved from dvx/bin/ (no changes needed; uses ~/.dvx at runtime)
│   ├── setup             # moved from dvx/bin/ (no changes needed; uses ~/.dvx at runtime)
│   └── dev-setup         # moved from dvx/bin/ (update SCRIPT_DIR to stay at bin/ parent)
├── src/                  # moved from dvx/src/
├── tests/                # moved from dvx/tests/
├── .claude/, .github/, .dvx/, .omx/, .gstack/
└── .venv/                # moved from dvx/.venv/ (or re-created by dev-setup)
```

## Tasks

### 1. Move source directories to root

- `mv dvx/src/ ./src`
- `mv dvx/tests/ ./tests`
- `mv dvx/bin/ ./bin`

### 2. Move standalone files to root

- `mv dvx/pyproject.toml ./pyproject.toml`
- `mv dvx/tasks.py ./tasks.py`
- `mv dvx/uv.lock ./uv.lock`

### 3. Merge dvx/plans/ into root plans/

- Copy any files from `dvx/plans/` into the existing `plans/` directory.
- Remove `dvx/plans/`.

### 4. Update `bin/dev-setup` for flat layout

The current script does:
```bash
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
```
This walks one level above `bin/` expecting to land in `dvx/`. After flattening, the parent of `bin/` **is** the repo root, so change it to:
```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# or equivalently:
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)/.."
```
Actually, `bin/dev-setup` currently does `$(dirname "$0")/..` which goes from `dvx/bin/` to `dvx/`. After flattening, `bin/` is at root, so it should do `$(dirname "$0")/..` to go from `bin/` to root. Wait — `dirname "bin/dev-setup"` is `bin/`, and `bin/../` is root. So the existing `$(cd "$(dirname "$0")/.." && pwd)` is actually **already correct** for the flat layout! No change needed.

### 5. Update `install.sh` for flat layout

The local-mode detection currently looks for a `dvx/` directory next to the script:
```bash
if [ -f "$SOURCE" ] && [ -d "$(cd "$(dirname "$SOURCE")" && pwd)/dvx" ]; then
```

And later:
```bash
DVX_PAYLOAD="${SRC_ROOT}/dvx"
```

After flattening, the payload **is** the repository root itself. Change the logic:

**Local detection** — instead of looking for `dvx/`, check for `pyproject.toml` next to the script:
```bash
LOCAL_ROOT=""
if [ -f "$SOURCE" ] && [ -f "$(cd "$(dirname "$SOURCE")" && pwd)/pyproject.toml" ]; then
    LOCAL_ROOT="$(cd "$(dirname "$SOURCE")" && pwd)"
fi
```

**Payload path** — the payload is `SRC_ROOT` directly, not `SRC_ROOT/dvx`:
```bash
DVX_PAYLOAD="$SRC_ROOT"
```

**Copy step** — avoid copying `dvx/` (it will be gone) and dotdirs like `.venv`, `.cache`, `.pytest_cache`, `.ruff_cache`:
```bash
# Copy all tracked content into ~/.dvx, excluding .venv and cache dirs
for item in "$DVX_PAYLOAD"/*; do
    base=$(basename "$item")
    case "$base" in
        dvx) continue ;;  # will not exist after flattening, keep for safety
    esac
    cp -r "$item" "$DVX_HOME/"
done
```

Or more simply, since `DVX_PAYLOAD` is now the root and the only top-level items are `pyproject.toml`, `tasks.py`, `uv.lock`, `bin/`, `src/`, `tests/`, `plans/`:
```bash
rm -rf "$DVX_HOME/src"
cp -r "$DVX_PAYLOAD/src" "$DVX_HOME/"
cp -r "$DVX_PAYLOAD/bin" "$DVX_HOME/"
cp -r "$DVX_PAYLOAD/tests" "$DVX_HOME/"
for f in pyproject.toml tasks.py uv.lock; do
    [ -f "$DVX_PAYLOAD/$f" ] && cp "$DVX_PAYLOAD/$f" "$DVX_HOME/"
done
```

Also update all documentation strings in `install.sh` that reference "dvx/ payload" to "repository root" or similar.

### 6. Update `AGENTS.md` (repo root)

Update all path references from `dvx/...` to the flat equivalents:
- `dvx/src/skills/` → `src/skills/`
- `dvx/src/claude_session.py` → `src/claude_session.py`
- `dvx/src/cli.py` → `src/cli.py`
- `dvx/src/orchestrator.py` → `src/orchestrator.py`
- `dvx/src/plan_parser.py` → `src/plan_parser.py`
- `dvx/src/state.py` → `src/state.py`
- `dvx/bin/dvx` → `bin/dvx`
- `dvx/bin/setup` → `bin/setup`
- `dvx/bin/dev-setup` → `bin/dev-setup`
- `dvx/tests/` → `tests/`
- "detects the dvx/ payload" → "detects the repository root"
- "Copies dvx/ into ~/.dvx/" → "Copies the project files into ~/.dvx/"
- `dvx/bin/dev-setup` → `bin/dev-setup`

References to `~/.dvx/` and `.dvx/` (the runtime directories) should remain unchanged.

### 7. Update `README.md` if needed

The README references to `.dvx/` and `~/.dvx/` are runtime paths and should remain unchanged. The manual install section references `dvx-cli` as the clone directory name — no change needed. Verify no references to `dvx/` as a source directory path exist.

### 8. Handle `.gitignore`

- Keep the comprehensive root `.gitignore` as-is (it already includes everything).
- Delete `dvx/.gitignore` (it only contained `.dvx/` which is already ignored by the root file).
- Also remove `dvx/.cache`, `dvx/.pytest_cache`, `dvx/.ruff_cache` (cached data, not needed).

### 9. Remove the `dvx/` directory

After all moves are complete, remove the now-empty `dvx/` directory:
```bash
rmdir dvx/   # or rm -rf dvx/ if anything lingers
```

### 10. Verify

- `bin/dev-setup` still works (creates `.venv` at repo root).
- `pip install -e .` works from repo root.
- `source .venv/bin/activate && invoke tests` passes.
- `source .venv/bin/activate && invoke lint` passes.
- `./install.sh --local` works (installs to `~/.dvx/` correctly).
- `~/.dvx/bin/dvx --help` works after install.

### 11. Update remote install tarball extraction

In `install.sh`, the remote download extracts `dvx-cli-<branch>/`. After flattening, the payload files are at that directory root (not under a `dvx/` subdirectory). The `DVX_PAYLOAD="$SRC_ROOT"` change handles this.

## Notes

- `pyproject.toml` `package-dir = {"" = "src"}` is still correct after the move since `src/` is at the project root and `pyproject.toml` is also at the root.
- `bin/dvx` and `bin/setup` reference `~/.dvx/` (the installed location), not the source tree. They require no changes.
- The installed layout at `~/.dvx/` remains: `~/.dvx/bin/`, `~/.dvx/src/`, `~/.dvx/.venv/` — this is unchanged.
