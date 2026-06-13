"""
Watched work queue processing for `dvx watch`.

Watches a work directory (default .dvx/todo/) and processes files one at a
time: each file gets its own working branch, is handed to the selected agent,
and is merged back into the branch that was active when the watcher started,
which is then pushed to its remote (when one exists) so remote reviewers see
the work.

GOAL*.md files use Claude Code's dedicated /goal flow for Claude models and a
Codex exec prompt for GPT models. Every other regular file uses the current dvx
run loop, with the watched input copied to a stable .dvx/watch/ snapshot before
the run starts so the live inbox file is never part of the work branch commits.

The watched directory lives under .dvx/ so it is created by dvx itself and
is already gitignored - other sessions can queue work simply by dropping files
there. Watcher state lives separately in
.dvx/watch/state.json (outside the watched directory) and is written atomically
(temp file + os.replace), so the watcher can be killed or crash at any
point and `dvx watch` will recover and continue from the last completed
step.

The watched directory also accepts one control file: MERGE. Dropping it asks
the watcher to merge the watch branch into a remote branch - empty file
means the remote's default branch; otherwise the file holds a single branch
name (e.g. "dev"). The merge runs between items: after the in-flight item
finishes (if any) and before the next queued item starts. The remote target
is brought into the watch branch first (the selected agent resolves conflicts), then
the target is fast-forwarded to the watch branch tip - never force-pushed -
so if another process advances the target mid-merge, the push is rejected
and the watcher re-fetches and re-merges instead of clobbering it.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from claude_session import (
    SessionResult,
    is_gpt_model,
)
from claude_session import (
    resolve_agent_model as resolve_claude_model,
)
from claude_session import (
    run_agent as run_claude,
)
from state import ensure_dvx_dir, get_dvx_dir

logger = logging.getLogger(__name__)

# Watcher state lives in .dvx/watch/, NOT in the watched .dvx/todo/ inbox.
GOALS_STATE_NAME = "watch"
GOALS_STATE_FILE = "state.json"
CURRENT_GOAL_CONTENT_FILE = "current-goal.md"
RUN_ITEM_CONTENT_DIR = "run-items"
QUEUED_GOAL_SNAPSHOT_DIR = "queued-goals"
QUEUED_GOAL_SNAPSHOT_MANIFEST = "manifest.json"
WATCH_FILE_GLOB = "*"
ITEM_TYPE_GOAL = "goal"
ITEM_TYPE_RUN = "run"
GOAL_TIMEOUT_SECONDS = 4 * 60 * 60
# The built-in /goal command caps its condition argument; passing whole goal
# files inline trips it, so the condition references the snapshot file instead.
GOAL_CONDITION_MAX_CHARS = 4000
GOAL_REJECTION_SIGNATURE = "Goal condition is limited to"
COMMIT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_TODO_DIR = ".dvx/todo"
# Backward-compatible internal alias for callers importing the old constant.
DEFAULT_GOALS_DIR = DEFAULT_TODO_DIR
DEFAULT_POLL_INTERVAL = 2.0

# Control file in the watched directory that requests a merge of the watch
# branch into a remote branch. Empty file = the remote's default branch;
# otherwise the file contains a single branch name (e.g. "dev").
MERGE_FILE_NAME = "MERGE"
# How many times the remote target may advance mid-merge (rejecting our
# fast-forward push) before the watcher gives up and asks to be re-run.
MERGE_PUSH_MAX_ATTEMPTS = 3

# Step statuses for the current goal. Each status names the step that has
# COMPLETED; recovery re-enters the state machine at the next step. Every
# step is safe to re-run if the process died after doing the work but
# before recording the status.
STATUS_CLAIMED = "claimed"            # popped from queue, content snapshotted
STATUS_BRANCHED = "branched"          # working branch exists and is checked out
STATUS_RAN = "ran"                    # Claude Code finished the /goal session
STATUS_GOAL_DELETED = "goal_deleted"  # watched file removed from todo dir
STATUS_COMMITTED = "committed"        # all changes committed on working branch
STATUS_MERGED = "merged"              # working branch merged into watch branch
RUN_PREREQ_STATUSES = {STATUS_CLAIMED, STATUS_BRANCHED}

# Step statuses for a pending merge request (MERGE control file). Same
# convention as goal statuses: each names the step that has COMPLETED.
MERGE_STATUS_CLAIMED = "claimed"            # MERGE file consumed, target recorded
MERGE_STATUS_LOCAL_MERGED = "local_merged"  # remote target merged into watch branch
MERGE_STATUS_TARGET_PUSHED = "target_pushed"  # remote target fast-forwarded to watch tip


@dataclass
class GoalState:
    """Persistent watcher state. One instance per project."""
    watch_branch: str
    goals_dir: str
    queue: list[str] = field(default_factory=list)
    current: Optional[dict] = None
    merge: Optional[dict] = None
    blocked: Optional[dict] = None
    completed: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GoalState":
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def goals_state_dir(project_dir: Optional[str] = None) -> Path:
    return get_dvx_dir(GOALS_STATE_NAME, project_dir)


def goals_state_file(project_dir: Optional[str] = None) -> Path:
    return goals_state_dir(project_dir) / GOALS_STATE_FILE


def load_goal_state(project_dir: Optional[str] = None) -> Optional[GoalState]:
    state_file = goals_state_file(project_dir)
    if not state_file.exists():
        return None
    try:
        return GoalState.from_dict(json.loads(state_file.read_text()))
    except Exception as e:
        logger.error(f"Error loading goal state: {e}")
        return None


def save_goal_state(state: GoalState, project_dir: Optional[str] = None) -> None:
    """Write state atomically so a crash never leaves a torn state file."""
    ensure_dvx_dir(GOALS_STATE_NAME, project_dir)
    state.updated_at = datetime.now().isoformat()
    state_file = goals_state_file(project_dir)
    tmp_file = state_file.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(state.to_dict(), indent=2))
    os.replace(tmp_file, state_file)


def clear_goal_state(project_dir: Optional[str] = None) -> bool:
    """
    Remove all watch-processing state. The watched directory itself is left
    untouched so a subsequent `dvx watch` re-discovers the work files.

    Returns True if there was state to clear.
    """
    state_dir = goals_state_dir(project_dir)
    if not state_dir.exists():
        return False
    shutil.rmtree(state_dir)
    return True


def current_goal_content_file(project_dir: Optional[str] = None) -> Path:
    return goals_state_dir(project_dir) / CURRENT_GOAL_CONTENT_FILE


def _new_run_item_content_file(
    goal_file_name: str,
    content: str,
    project_dir: Optional[str] = None,
) -> Path:
    """Create a unique per-item snapshot path for the dvx run state namespace."""
    branch_slug = branch_name_for_goal(goal_file_name)
    nonce = datetime.now().isoformat()
    digest = _content_sha256(f"{goal_file_name}\0{content}\0{nonce}")[:12]
    suffix = Path(goal_file_name).suffix or ".md"
    return goals_state_dir(project_dir) / RUN_ITEM_CONTENT_DIR / f"{branch_slug}-{digest}{suffix}"


def _claim_content_file(
    goal_file_name: str,
    item_type: str,
    content: str,
    project_dir: Optional[str] = None,
) -> Path:
    if item_type == ITEM_TYPE_GOAL:
        return current_goal_content_file(project_dir)
    return _new_run_item_content_file(goal_file_name, content, project_dir)


def _current_content_file(state: GoalState, project_dir: Optional[str] = None) -> Path:
    stored = (state.current or {}).get("content_file")
    if stored:
        return Path(stored)
    return current_goal_content_file(project_dir)


def queued_goal_snapshot_dir(project_dir: Optional[str] = None) -> Path:
    return goals_state_dir(project_dir) / QUEUED_GOAL_SNAPSHOT_DIR


def queued_goal_snapshot_manifest_file(project_dir: Optional[str] = None) -> Path:
    return queued_goal_snapshot_dir(project_dir) / QUEUED_GOAL_SNAPSHOT_MANIFEST


def queued_goal_snapshot_file(goal_file_name: str, project_dir: Optional[str] = None) -> Path:
    return queued_goal_snapshot_dir(project_dir) / goal_file_name


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _git_error(result: subprocess.CompletedProcess, action: str) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
    return f"Failed to {action}: {detail}"


def branch_exists(branch: str) -> bool:
    return _git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def _branch_ref(branch: str) -> str:
    return f"refs/heads/{branch}"


def _branch_tip(branch: str) -> Optional[str]:
    result = _git(["rev-parse", "--verify", _branch_ref(branch)])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _branch_creation_marker(goal_file: str, branch: str) -> str:
    return f"dvx watch: create {branch} for {goal_file}"


def _branch_reflog_has_marker(branch: str, marker: str) -> bool:
    result = _git(["reflog", "show", "--format=%gs", _branch_ref(branch)])
    if result.returncode != 0:
        return False
    return marker in result.stdout.splitlines()


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    result = _git(["merge-base", "--is-ancestor", ancestor, descendant])
    return result.returncode == 0


def _merge_in_progress() -> bool:
    return _git(["rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode == 0


def _remote_name() -> tuple[Optional[str], str]:
    """The remote to talk to: origin when present, else the first remote.

    Returns (remote, error); (None, "") means no remote is configured.
    """
    result = _git(["remote"])
    if result.returncode != 0:
        return None, _git_error(result, "list remotes")
    remotes = result.stdout.split()
    if not remotes:
        return None, ""
    return ("origin" if "origin" in remotes else remotes[0]), ""


def _default_remote_branch(remote: str) -> tuple[Optional[str], str]:
    """Resolve the remote's default branch (its HEAD), e.g. main."""
    result = _git(["symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"])
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = f"refs/remotes/{remote}/"
        if ref.startswith(prefix):
            return ref[len(prefix):], ""
    result = _git(["ls-remote", "--symref", remote, "HEAD"])
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "ref:" and parts[2] == "HEAD":
                if parts[1].startswith("refs/heads/"):
                    return parts[1][len("refs/heads/"):], ""
    return None, (
        f"Cannot determine the default branch of remote {remote}; "
        f"run `git remote set-head {remote} --auto` or name the target branch "
        f"in the {MERGE_FILE_NAME} file"
    )


def _branch_matches_creation_claim(branch: str, claim: Optional[dict]) -> bool:
    if not claim:
        return False
    base_oid = claim.get("base_oid")
    marker = claim.get("reflog_marker")
    if not base_oid or not marker:
        return False
    return _branch_tip(branch) == base_oid and _branch_reflog_has_marker(branch, marker)


def _branch_has_watcher_ownership(state: GoalState) -> bool:
    if state.current.get("branch_created_by_watcher") is not True:
        return False
    branch = state.current["branch"]
    claim = state.current.get("branch_creation")
    if not claim:
        return False
    base_oid = claim.get("base_oid")
    marker = claim.get("reflog_marker")
    if not base_oid or not marker:
        return False
    return _branch_reflog_has_marker(branch, marker) and _is_ancestor(base_oid, branch)


def _branch_ownership_error(state: GoalState) -> str:
    return f"Watched item branch no longer has validated watcher ownership: {state.current['branch']}"


def checkout(branch: str) -> tuple[bool, str]:
    result = _git(["checkout", branch])
    if result.returncode != 0:
        return False, _git_error(result, f"checkout {branch}")
    return True, ""


def branch_name_for_goal(goal_file_name: str) -> str:
    """Derive a working branch name: GOAL-add-feature-x.md -> goal-add-feature-x."""
    stem = Path(goal_file_name).stem.lower()
    slug = re.sub(r"[^a-z0-9._/-]+", "-", stem).strip("-.")
    result = _git(["check-ref-format", "--branch", slug])
    if result.returncode != 0 or not slug:
        raise ValueError(f"Cannot derive a valid branch name from: {goal_file_name}")
    return slug


def _git_root() -> Optional[Path]:
    result = _git(["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _repo_relative_path(path: str) -> str:
    raw = Path(path).expanduser()
    root = _git_root()
    resolved = raw.resolve() if raw.is_absolute() else (Path.cwd() / raw).resolve()
    if root is not None:
        try:
            return resolved.relative_to(root).as_posix().rstrip("/")
        except ValueError:
            return resolved.as_posix().rstrip("/")
    return resolved.as_posix().rstrip("/")


def _exclude_prefixes(goals_dir: str) -> list[str]:
    """Path prefixes that must never be committed: the watch inbox and dvx state."""
    goals_prefix = _repo_relative_path(goals_dir)
    prefixes = []
    if goals_prefix:
        prefixes.append(goals_prefix.rstrip("/") + "/")
    prefixes.append(".dvx/")
    return prefixes


def _matches_excluded_path(path: str, prefix: str) -> bool:
    clean_path = path.rstrip("/")
    clean_prefix = prefix.rstrip("/")
    return clean_path == clean_prefix or path.startswith(prefix)


def _non_excluded_paths(paths: list[str], prefixes: list[str]) -> list[str]:
    return [path for path in paths if not any(_matches_excluded_path(path, p) for p in prefixes)]


def _dirty_paths(goals_dir: str) -> list[str]:
    """List uncommitted paths, excluding the watched directory and .dvx state."""
    result = _git(["status", "--porcelain=v1", "-z"])
    if result.returncode != 0:
        raise RuntimeError(_git_error(result, "read git status"))

    prefixes = _exclude_prefixes(goals_dir)
    paths = []
    entries = [entry for entry in result.stdout.split("\0") if entry]
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry.strip():
            continue
        status = entry[:2]
        entry_paths = [entry[3:]]
        if "R" in status or "C" in status:
            if i < len(entries):
                entry_paths.append(entries[i])
                i += 1
        paths.extend(_non_excluded_paths(entry_paths, prefixes))
    return paths


def _ensure_branch_creation_claim(
    state: GoalState,
    project_dir: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    claim = state.current.get("branch_creation")
    if claim:
        return claim, ""

    result = _git(["rev-parse", "--verify", f"{state.watch_branch}^{{commit}}"])
    if result.returncode != 0:
        return None, _git_error(result, f"resolve watch branch {state.watch_branch}")

    claim = {
        "base_oid": result.stdout.strip(),
        "reflog_marker": _branch_creation_marker(
            state.current["goal_file"],
            state.current["branch"],
        ),
        "started_at": datetime.now().isoformat(),
    }
    state.current["branch_creation"] = claim
    save_goal_state(state, project_dir)
    return claim, ""


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------

def build_goal_prompt(goal_content: str, project_dir: Optional[str] = None) -> str:
    """
    Build the /goal prompt for a goal session.

    The /goal condition is capped at GOAL_CONDITION_MAX_CHARS, so the full goal
    content is never passed inline. Instead the condition points at the snapshot
    file the watcher wrote at claim time; the session reads it as its full
    instructions. The snapshot is (re)written here so the referenced path is
    always valid, even when recovering from partial state.
    """
    ensure_dvx_dir(GOALS_STATE_NAME, project_dir)
    content_file = current_goal_content_file(project_dir)
    content_file.write_text(goal_content)
    return (
        f"/goal Complete the goal specified in the file {content_file}. "
        "Read that file first - it is the complete instruction set: requirements, "
        "scope limits, decisions already made, verification commands, and rules. "
        "The goal is met only when every requirement in that file is implemented "
        "and every verification command in it passes. Do not modify anything "
        "under .dvx/ (including that file)."
    )


def build_codex_goal_prompt(goal_content: str, project_dir: Optional[str] = None) -> str:
    """
    Build a Codex exec prompt for a watched GOAL file.

    Codex exec is already non-interactive, so do not rely on Claude Code's
    `/goal` slash command. The prompt explicitly asks Codex to plan, implement,
    self-review, and verify before finishing.
    """
    ensure_dvx_dir(GOALS_STATE_NAME, project_dir)
    content_file = current_goal_content_file(project_dir)
    content_file.write_text(goal_content)
    return (
        f"Complete the goal specified in the file `{content_file}`.\n\n"
        "Workflow requirements:\n"
        "1. Read that file first; it is the complete instruction set: requirements, "
        "scope limits, decisions already made, verification commands, and rules.\n"
        "2. Make a concise plan, implement the full requested change, review your "
        "own diff, and run the smallest verification that proves correctness.\n"
        "3. Continue fixing issues until the goal requirements are met and the "
        "verification commands pass, or report a concrete blocker.\n"
        "4. Do not modify anything under `.dvx/` including the goal snapshot file.\n"
        "5. Do not ask for user input; use safe, reversible defaults when needed."
    )


def run_goal_with_claude(
    goal_content: str,
    cwd: Optional[str] = None,
    project_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> SessionResult:
    """Run the selected agent for the goal contents."""
    selected_model = resolve_claude_model(model)
    if is_gpt_model(selected_model):
        prompt = build_codex_goal_prompt(goal_content, project_dir)
    else:
        prompt = build_goal_prompt(goal_content, project_dir)
        condition_len = len(prompt) - len("/goal ")
        if condition_len > GOAL_CONDITION_MAX_CHARS:
            return SessionResult(
                output="",
                session_id=None,
                success=False,
                blocked=True,
                block_reason=(
                    f"goal condition is {condition_len} chars, over the /goal limit "
                    f"of {GOAL_CONDITION_MAX_CHARS}"
                ),
            )
    return run_claude(
        prompt=prompt,
        cwd=cwd,
        model=selected_model,
        timeout=GOAL_TIMEOUT_SECONDS,
    )


def run_item_with_orchestrator(
    plan_file: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> SessionResult:
    """Run the current dvx run flow for a watched non-GOAL input."""
    previous_cwd = Path.cwd()
    try:
        if cwd is not None:
            os.chdir(cwd)
        from orchestrator import run_orchestrator

        exit_code = run_orchestrator(plan_file, model=resolve_claude_model(model))
    finally:
        if cwd is not None:
            os.chdir(previous_cwd)

    return SessionResult(
        output=f"dvx run exited with status {exit_code}",
        session_id=None,
        success=exit_code == 0,
        block_reason=None if exit_code == 0 else f"dvx run exited with status {exit_code}",
    )


def commit_logical_groups_with_claude(
    goals_dir: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> SessionResult:
    """Ask Claude Code to commit all outstanding changes in logical groups."""
    prompt = (
        "Commit all outstanding changes in this repository into logical groups.\n"
        "- Inspect `git status` and `git diff` to understand the changes.\n"
        "- Create one commit per logical group with a clear message explaining why.\n"
        f"- Do NOT commit anything under `{goals_dir}` or `.dvx/`.\n"
        "- Do not push.\n"
        "- When finished, the working tree (other than those excluded paths) must be clean."
    )
    return run_claude(
        prompt=prompt,
        cwd=cwd,
        model=resolve_claude_model(model),
        timeout=COMMIT_TIMEOUT_SECONDS,
    )


def resolve_merge_conflicts_with_claude(
    target: str,
    goals_dir: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> SessionResult:
    """Ask Claude Code to resolve an in-progress merge and conclude it."""
    prompt = (
        f"A `git merge` of {target} into this branch stopped on conflicts.\n"
        "- Run `git status` to see the conflicted files.\n"
        "- Resolve every conflict, preserving the intent of both sides.\n"
        "- Stage the resolved files and conclude the merge with `git commit` "
        "(keep the default merge commit message).\n"
        f"- Do NOT push. Do NOT modify anything under `{goals_dir}` or `.dvx/`.\n"
        "- When finished, `git status` must report a clean working tree with "
        "no merge in progress."
    )
    return run_claude(
        prompt=prompt,
        cwd=cwd,
        model=resolve_claude_model(model),
        timeout=COMMIT_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def item_type_for_file(file_name: str) -> str:
    """Classify watched inputs: GOAL*.md uses /goal; every other file uses dvx run."""
    path = Path(file_name)
    if path.name.startswith("GOAL") and path.suffix == ".md":
        return ITEM_TYPE_GOAL
    return ITEM_TYPE_RUN


def scan_goal_files(goals_dir: Path) -> list[str]:
    """List watched work file names oldest-first by mtime, then name."""
    if not goals_dir.exists():
        return []
    files = [
        p
        for p in goals_dir.glob(WATCH_FILE_GLOB)
        if p.is_file() and p.name != MERGE_FILE_NAME
    ]
    files.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
    return [p.name for p in files]


def _watch_queue_sort_key(goals_dir: Path, file_name: str) -> tuple[int, int, str]:
    path = goals_dir / file_name
    try:
        return (0, path.stat().st_mtime_ns, file_name)
    except FileNotFoundError:
        # Stale queue entries should be claimed and failed before real work so
        # they cannot sit in saved state indefinitely.
        return (-1, 0, file_name)


def _snapshot_queued_goal(
    state: GoalState,
    goal_file_name: str,
    project_dir: Optional[str] = None,
) -> dict:
    snapshot_file = queued_goal_snapshot_file(goal_file_name, project_dir)
    goal_path = Path(state.goals_dir) / goal_file_name
    if not goal_path.exists():
        return {"goal_file": goal_file_name, "existed": False}
    content = goal_path.read_text()
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(content)
    return {
        "goal_file": goal_file_name,
        "existed": True,
        "sha256": _content_sha256(content),
    }


def _clear_queued_goal_snapshots(project_dir: Optional[str] = None) -> None:
    shutil.rmtree(queued_goal_snapshot_dir(project_dir), ignore_errors=True)


def _snapshot_queued_goals(state: GoalState, project_dir: Optional[str] = None) -> list[dict]:
    if not state.queue:
        return []

    entries = []
    for goal_file_name in state.queue:
        entries.append(_snapshot_queued_goal(state, goal_file_name, project_dir))
    manifest = queued_goal_snapshot_manifest_file(project_dir)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"goals": entries}, indent=2))
    return entries


def _queued_goal_snapshot_entries(project_dir: Optional[str] = None) -> tuple[list[dict], str]:
    manifest = queued_goal_snapshot_manifest_file(project_dir)
    if not manifest.exists():
        return [], ""
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError as e:
        return [], f"Queued item snapshot manifest is unreadable: {e}"
    return data.get("goals", []), ""


def _guard_holder(state: GoalState) -> Optional[dict]:
    """The active operation (current goal or pending merge) that owns the guard."""
    return state.current or state.merge


def _queued_goal_guard_failure(
    state: GoalState,
    project_dir: Optional[str],
    message: str,
) -> tuple[bool, str]:
    holder = _guard_holder(state)
    if holder and holder.get("queued_goal_guard"):
        save_goal_state(state, project_dir)
    return False, message


def _verify_queued_goal_snapshots(
    state: GoalState,
    project_dir: Optional[str] = None,
) -> tuple[bool, str]:
    holder = _guard_holder(state)
    active_guard = holder.get("queued_goal_guard") if holder else None
    manifest = queued_goal_snapshot_manifest_file(project_dir)
    if active_guard and not manifest.exists():
        return _queued_goal_guard_failure(state, project_dir, (
            "Queued item snapshot manifest is missing while a watcher runner guard is active. "
            f"Live files were preserved; clean snapshots remain in {queued_goal_snapshot_dir(project_dir)}."
        ))

    if not queued_goal_snapshot_manifest_file(project_dir).exists():
        _clear_queued_goal_snapshots(project_dir)
        return True, ""

    entries, error = _queued_goal_snapshot_entries(project_dir)
    if error:
        if active_guard:
            return _queued_goal_guard_failure(state, project_dir, error)
        return False, error
    if active_guard and entries != active_guard.get("goals", []):
        return _queued_goal_guard_failure(state, project_dir, (
            "Queued item snapshot manifest changed while a watcher runner guard is active. "
            f"Live files were preserved; clean snapshots remain in {queued_goal_snapshot_dir(project_dir)}."
        ))
    if not entries:
        return True, ""

    conflicts = []
    for entry in entries:
        goal_file_name = entry["goal_file"]
        goal_path = Path(state.goals_dir) / goal_file_name
        if not entry.get("existed", False):
            if goal_path.exists():
                conflicts.append(f"{goal_file_name} was recreated")
            continue

        snapshot_file = queued_goal_snapshot_file(goal_file_name, project_dir)
        if not snapshot_file.exists():
            conflicts.append(f"{goal_file_name} snapshot is missing")
            continue

        expected_hash = entry.get("sha256")
        if not expected_hash:
            conflicts.append(f"{goal_file_name} snapshot hash is missing")
            continue

        snapshot_hash = _content_sha256(snapshot_file.read_text())
        if snapshot_hash != expected_hash:
            conflicts.append(f"{goal_file_name} snapshot was modified")
            continue

        if not goal_path.exists():
            conflicts.append(f"{goal_file_name} was deleted")
        elif _content_sha256(goal_path.read_text()) != expected_hash:
            conflicts.append(f"{goal_file_name} was modified")

    if conflicts:
        message = (
            "Queued items changed while a watcher runner was active: "
            f"{conflicts}. Live files were preserved; clean snapshots remain in "
            f"{queued_goal_snapshot_dir(project_dir)}."
        )
        if active_guard:
            return _queued_goal_guard_failure(state, project_dir, message)
        return False, message

    _clear_queued_goal_snapshots(project_dir)
    if active_guard:
        holder.pop("queued_goal_guard", None)
        save_goal_state(state, project_dir)
    return True, ""


def _begin_queued_goal_guard(state: GoalState, project_dir: Optional[str] = None) -> tuple[bool, str]:
    ok, error = _verify_queued_goal_snapshots(state, project_dir)
    if not ok:
        return False, error
    entries = _snapshot_queued_goals(state, project_dir)
    holder = _guard_holder(state)
    if entries and holder:
        holder["queued_goal_guard"] = {
            "goals": entries,
            "started_at": datetime.now().isoformat(),
        }
        save_goal_state(state, project_dir)
    return True, ""


def _finish_queued_goal_guard(state: GoalState, project_dir: Optional[str] = None) -> tuple[bool, str]:
    return _verify_queued_goal_snapshots(state, project_dir)


def _delete_queued_goal_snapshot(goal_file_name: str, project_dir: Optional[str] = None) -> None:
    queued_goal_snapshot_file(goal_file_name, project_dir).unlink(missing_ok=True)


def enqueue_new_goals(state: GoalState, project_dir: Optional[str] = None) -> list[str]:
    """Add newly arrived watched files to the queue. Returns the added names."""
    known = set(state.queue)
    if state.current:
        known.add(state.current["goal_file"])
    known.update(f["goal_file"] for f in state.failed if "goal_file" in f)

    goals_dir = Path(state.goals_dir)
    added = [name for name in scan_goal_files(goals_dir) if name not in known]
    original_queue = list(state.queue)
    if added:
        state.queue.extend(added)
    if state.queue:
        state.queue.sort(key=lambda name: _watch_queue_sort_key(goals_dir, name))
    if added or state.queue != original_queue:
        save_goal_state(state, project_dir)
    return added


def claim_next_goal(state: GoalState, project_dir: Optional[str] = None) -> Optional[dict]:
    """
    Atomically claim the next watched file in the queue as the current item.

    Reviews the watched file (it must exist and be non-empty) and copies the
    current contents into .dvx/watch/ so the run step can recover even if the
    watched file is later deleted. Unusable files are recorded in state.failed
    and skipped.
    """
    if state.current:
        return state.current

    while state.queue:
        name = state.queue[0]
        goal_path = Path(state.goals_dir) / name
        item_type = item_type_for_file(name)

        content = goal_path.read_text() if goal_path.exists() else ""
        reason = None
        branch = None
        if not content.strip():
            reason = "missing" if not goal_path.exists() else "empty watched file"
        else:
            try:
                branch = branch_name_for_goal(name)
            except ValueError as e:
                reason = str(e)
            else:
                if branch_exists(branch):
                    reason = f"branch already exists: {branch}"

        if reason:
            state.queue.pop(0)
            state.failed.append({
                "goal_file": name,
                "item_type": item_type,
                "reason": reason,
                "at": datetime.now().isoformat(),
            })
            _delete_queued_goal_snapshot(name, project_dir)
            save_goal_state(state, project_dir)
            logger.warning(f"Skipping goal {name}: {reason}")
            continue

        ensure_dvx_dir(GOALS_STATE_NAME, project_dir)
        try:
            dirty_baseline = _dirty_paths(state.goals_dir)
        except RuntimeError as e:
            logger.error(f"Cannot claim goal {name}: {e}")
            return None
        if dirty_baseline:
            state.blocked = {
                "goal_file": name,
                "reason": "working tree is dirty outside the watched directory and .dvx",
                "dirty_paths": dirty_baseline,
                "at": datetime.now().isoformat(),
            }
            save_goal_state(state, project_dir)
            logger.warning(
                f"Blocking goal {name}: working tree is dirty outside the watched directory "
                "and .dvx: "
                f"{dirty_baseline}"
            )
            return None
        content_file = _claim_content_file(name, item_type, content, project_dir)
        content_file.parent.mkdir(parents=True, exist_ok=True)
        content_file.write_text(content)
        state.queue.pop(0)
        _delete_queued_goal_snapshot(name, project_dir)
        state.current = {
            "goal_file": name,
            "item_type": item_type,
            "content_file": str(content_file),
            "branch": branch,
            "status": STATUS_CLAIMED,
            "branch_created_by_watcher": False,
            "dirty_baseline": dirty_baseline,
            "started_at": datetime.now().isoformat(),
        }
        state.blocked = None
        save_goal_state(state, project_dir)
        return state.current

    return None


# ---------------------------------------------------------------------------
# Goal processing state machine
# ---------------------------------------------------------------------------

def _step_create_branch(state: GoalState, project_dir: Optional[str] = None) -> tuple[bool, str]:
    branch = state.current["branch"]
    if branch_exists(branch):
        # Recovering from a crash after the branch was created.
        if state.current.get("branch_created_by_watcher") is True:
            if not _branch_has_watcher_ownership(state):
                return False, _branch_ownership_error(state)
            return checkout(branch)
        if _branch_matches_creation_claim(branch, state.current.get("branch_creation")):
            state.current["branch_created_by_watcher"] = True
            save_goal_state(state, project_dir)
            return checkout(branch)
        return False, f"Branch already exists and is not owned by this watcher: {branch}"

    claim, error = _ensure_branch_creation_claim(state, project_dir)
    if claim is None:
        return False, error

    result = _git([
        "update-ref",
        "--create-reflog",
        "-m",
        claim["reflog_marker"],
        _branch_ref(branch),
        claim["base_oid"],
        "",
    ])
    if result.returncode != 0:
        if branch_exists(branch) and _branch_matches_creation_claim(branch, claim):
            state.current["branch_created_by_watcher"] = True
            save_goal_state(state, project_dir)
            return checkout(branch)
        if branch_exists(branch):
            return False, f"Branch already exists and is not owned by this watcher: {branch}"
        return False, _git_error(result, f"create branch {branch}")
    state.current["branch_created_by_watcher"] = True
    save_goal_state(state, project_dir)
    return checkout(branch)


def _checkout_owned_goal_branch(state: GoalState) -> tuple[bool, str]:
    branch = state.current["branch"]
    if not branch_exists(branch):
        return False, f"Watch item branch does not exist: {branch}"
    if not _branch_has_watcher_ownership(state):
        return False, _branch_ownership_error(state)
    return checkout(branch)


def _current_item_type(state: GoalState) -> str:
    return state.current.get("item_type") or item_type_for_file(state.current["goal_file"])


def _validate_runner_commits(state: GoalState) -> tuple[bool, str]:
    base_oid = (state.current.get("branch_creation") or {}).get("base_oid")
    if not base_oid:
        return True, ""
    return _validate_commits_did_not_include_excluded_paths(state, base_oid)


def _migrate_legacy_run_content_file(
    state: GoalState,
    project_dir: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Rebind pre-upgrade non-GOAL watch state from the shared snapshot to a
    per-item run snapshot before `dvx run` starts.
    """
    if not state.current or state.current.get("content_file"):
        return True, ""
    if _current_item_type(state) != ITEM_TYPE_RUN:
        return True, ""
    if state.current.get("status") not in RUN_PREREQ_STATUSES:
        return True, ""

    legacy_file = current_goal_content_file(project_dir)
    used_legacy_file = legacy_file.exists()
    if used_legacy_file:
        content = legacy_file.read_text()
    else:
        goal_path = Path(state.goals_dir) / state.current["goal_file"]
        if not goal_path.exists():
            return False, f"Watched file contents unavailable for {state.current['goal_file']}"
        content = goal_path.read_text()

    content_file = _new_run_item_content_file(
        state.current["goal_file"],
        content,
        project_dir,
    )
    content_file.parent.mkdir(parents=True, exist_ok=True)
    content_file.write_text(content)
    if used_legacy_file:
        legacy_file.unlink(missing_ok=True)
    state.current["content_file"] = str(content_file)
    save_goal_state(state, project_dir)
    return True, ""


def _step_run_claude(
    state: GoalState,
    claude_runner: Callable[[str], SessionResult],
    run_runner: Callable[[str], SessionResult],
    project_dir: Optional[str] = None,
) -> tuple[bool, str]:
    ok, error = _checkout_owned_goal_branch(state)
    if not ok:
        return False, error

    item_type = _current_item_type(state)
    content_file = _current_content_file(state, project_dir)
    if content_file.exists():
        content = content_file.read_text()
    else:
        goal_path = Path(state.goals_dir) / state.current["goal_file"]
        if not goal_path.exists():
            return False, f"Watched file contents unavailable for {state.current['goal_file']}"
        content = goal_path.read_text()
        ensure_dvx_dir(GOALS_STATE_NAME, project_dir)
        content_file.parent.mkdir(parents=True, exist_ok=True)
        content_file.write_text(content)

    ok, error = _begin_queued_goal_guard(state, project_dir)
    if not ok:
        return False, error
    guard_ok = True
    guard_error = ""
    try:
        if item_type == ITEM_TYPE_GOAL:
            result = claude_runner(content)
        else:
            result = run_runner(str(content_file))
    finally:
        guard_ok, guard_error = _finish_queued_goal_guard(state, project_dir)
    if not guard_ok:
        return False, guard_error
    if not result.success:
        return False, f"Agent session failed: {result.block_reason or 'session did not succeed'}"
    if item_type == ITEM_TYPE_GOAL:
        if GOAL_REJECTION_SIGNATURE in result.output:
            return False, f"/goal rejected the goal: {result.output.strip()[:300]}"
        if result.result_event_seen is False:
            # A cleanly finished session always emits a result event; without one
            # the session was cut short (rate limit, crash) and the goal cannot be
            # trusted as done.
            return False, "Goal session was truncated before finishing (no result event)"
        if result.tool_use_count == 0:
            # A goal session that never used a single tool cannot have done any
            # work - treat it as a failure instead of silently completing.
            summary = result.output.strip()[:300] or "<no output>"
            return False, f"Goal session ended without doing any work (no tool use). Output: {summary}"
    ok, error = _validate_runner_commits(state)
    if not ok:
        return False, error
    return True, ""


def _step_delete_goal_file(state: GoalState) -> tuple[bool, str]:
    goal_path = Path(state.goals_dir) / state.current["goal_file"]
    goal_path.unlink(missing_ok=True)
    return True, ""


def _fallback_commit(state: GoalState) -> tuple[bool, str]:
    """Single catch-all commit when the logical-groups session left work behind."""
    result = _git(["add", "-A"])
    if result.returncode != 0:
        return False, _git_error(result, "stage changes")

    for prefix in _exclude_prefixes(state.goals_dir):
        _git(["reset", "-q", "--", prefix])

    staged = _git(["diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return True, ""
    if staged.returncode != 1:
        return False, _git_error(staged, "inspect staged changes")

    item_type = _current_item_type(state)
    result = _git(["commit", "-m", f"{item_type}: apply changes for {state.current['goal_file']}"])
    if result.returncode != 0:
        return False, _git_error(result, "commit goal changes")
    return True, ""


def _commit_touched_paths_since(base_oid: str) -> tuple[Optional[list[str]], str]:
    result = _git(["log", "--format=", "--name-only", "-z", f"{base_oid}..HEAD"])
    if result.returncode != 0:
        return None, _git_error(result, "inspect committed paths")
    return [path for path in result.stdout.split("\0") if path], ""


def _excluded_committed_paths_since(state: GoalState, base_oid: str) -> tuple[Optional[list[str]], str]:
    touched_paths, error = _commit_touched_paths_since(base_oid)
    if touched_paths is None:
        return None, error
    prefixes = _exclude_prefixes(state.goals_dir)
    return [
        path
        for path in touched_paths
        if any(_matches_excluded_path(path, prefix) for prefix in prefixes)
    ], ""


def _validate_commits_did_not_include_excluded_paths(
    state: GoalState,
    base_oid: str,
) -> tuple[bool, str]:
    excluded_paths, error = _excluded_committed_paths_since(state, base_oid)
    if excluded_paths is None:
        return False, error
    if not excluded_paths:
        return True, ""

    rollback = _git(["reset", "--mixed", "--quiet", base_oid])
    if rollback.returncode != 0:
        return False, (
            f"Commit step included excluded paths {excluded_paths}; "
            f"{_git_error(rollback, 'roll back unsafe commit')}"
        )
    return False, f"Commit step included excluded paths: {excluded_paths}"


def _commit_validation_base(
    state: GoalState,
    project_dir: Optional[str] = None,
) -> tuple[Optional[str], str]:
    base_oid = state.current.get("commit_validation_base")
    if base_oid:
        return base_oid, ""

    base_oid = _branch_tip(state.current["branch"])
    if base_oid is None:
        return None, f"Cannot resolve branch tip for {state.current['branch']}"
    state.current["commit_validation_base"] = base_oid
    save_goal_state(state, project_dir)
    return base_oid, ""


def _step_commit(
    state: GoalState,
    commit_runner: Callable[[str], SessionResult],
    project_dir: Optional[str] = None,
) -> tuple[bool, str]:
    ok, error = _checkout_owned_goal_branch(state)
    if not ok:
        return False, error

    base_oid = state.current.get("commit_validation_base")
    if base_oid:
        ok, error = _validate_commits_did_not_include_excluded_paths(state, base_oid)
        if not ok:
            return False, error

    ok, error = _verify_queued_goal_snapshots(state, project_dir)
    if not ok:
        return False, error

    try:
        dirty_paths = _dirty_paths(state.goals_dir)
        dirty_baseline = state.current.get("dirty_baseline", [])
        baseline_still_dirty = [path for path in dirty_baseline if path in dirty_paths]
        if baseline_still_dirty:
            return False, f"Pre-existing dirty paths remain before goal commit: {baseline_still_dirty}"
        if not dirty_paths:
            if base_oid:
                state.current.pop("commit_validation_base", None)
            return True, ""
    except RuntimeError as e:
        return False, str(e)

    base_oid, error = _commit_validation_base(state, project_dir)
    if base_oid is None:
        return False, error

    ok, error = _begin_queued_goal_guard(state, project_dir)
    if not ok:
        return False, error
    guard_ok = True
    guard_error = ""
    try:
        result = commit_runner(state.goals_dir)
    finally:
        guard_ok, guard_error = _finish_queued_goal_guard(state, project_dir)
    if not guard_ok:
        return False, guard_error
    if not result.success:
        logger.warning("Logical-groups commit session failed; using fallback commit")

    ok, error = _validate_commits_did_not_include_excluded_paths(state, base_oid)
    if not ok:
        return False, error

    if _dirty_paths(state.goals_dir):
        ok, error = _fallback_commit(state)
        if not ok:
            return False, error
        ok, error = _validate_commits_did_not_include_excluded_paths(state, base_oid)
        if not ok:
            return False, error

    remaining = _dirty_paths(state.goals_dir)
    if remaining:
        return False, f"Working tree still dirty after commit step: {remaining}"
    state.current.pop("commit_validation_base", None)
    return True, ""


def _push_watch_branch(state: GoalState) -> tuple[bool, str]:
    """
    Push the watch branch so remote reviewers see merged goal work.

    Repos without a remote (local-only use, tests) skip the push. A push
    failure fails the step loudly: the merge step is safe to re-run (merging
    an already-merged branch is a no-op), so a retry just pushes again.
    """
    remote, error = _remote_name()
    if error:
        return False, error
    if remote is None:
        logger.info("No git remote configured; skipping push")
        return True, ""
    result = _git(["push", remote, state.watch_branch])
    if result.returncode != 0:
        return False, _git_error(result, f"push {state.watch_branch} to {remote}")
    logger.info(f"Pushed {state.watch_branch} to {remote}")
    return True, ""


def _step_merge(state: GoalState) -> tuple[bool, str]:
    branch = state.current["branch"]
    if not branch_exists(branch):
        return False, f"Watch item branch does not exist: {branch}"
    if not _branch_has_watcher_ownership(state):
        return False, _branch_ownership_error(state)

    ok, error = checkout(state.watch_branch)
    if not ok:
        return False, error

    message = f"Merge branch '{branch}' ({_current_item_type(state)}: {state.current['goal_file']})"
    result = _git(["merge", "--no-ff", "-m", message, branch])
    if result.returncode != 0:
        _git(["merge", "--abort"])
        return False, _git_error(result, f"merge {branch} into {state.watch_branch}")

    return _push_watch_branch(state)


def _step_finish(state: GoalState, project_dir: Optional[str] = None) -> tuple[bool, str]:
    branch = state.current["branch"]
    if branch_exists(branch):
        if not _branch_has_watcher_ownership(state):
            return False, f"Refusing to delete branch without watcher ownership: {branch}"
        if not _is_ancestor(branch, state.watch_branch):
            return False, f"Watch item branch is not fully merged into {state.watch_branch}: {branch}"
        ok, error = checkout(state.watch_branch)
        if not ok:
            return False, error
        result = _git(["branch", "-D", branch])
        if result.returncode != 0:
            return False, _git_error(result, f"delete branch {branch}")
    else:
        ok, error = checkout(state.watch_branch)
        if not ok:
            return False, error

    _current_content_file(state, project_dir).unlink(missing_ok=True)
    state.completed.append({
        "goal_file": state.current["goal_file"],
        "item_type": _current_item_type(state),
        "branch": branch,
        "finished_at": datetime.now().isoformat(),
    })
    state.current = None
    save_goal_state(state, project_dir)
    return True, ""


def process_current_goal(
    state: GoalState,
    claude_runner: Optional[Callable[[str], SessionResult]] = None,
    run_runner: Optional[Callable[[str], SessionResult]] = None,
    commit_runner: Optional[Callable[[str], SessionResult]] = None,
    project_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Drive the current watched item through its remaining steps.

    The status field records the last COMPLETED step, so this can resume
    after a crash at any point. Returns (ok, error).
    """
    if claude_runner is None:
        def claude_runner(content):
            return run_goal_with_claude(content, project_dir=project_dir, model=model)
    if run_runner is None:
        def run_runner(plan_file):
            return run_item_with_orchestrator(plan_file, cwd=project_dir, model=model)
    if commit_runner is None:
        def commit_runner(goals_dir):
            return commit_logical_groups_with_claude(goals_dir, model=model)
    ok, error = _migrate_legacy_run_content_file(state, project_dir)
    if not ok:
        return False, error

    transitions = {
        STATUS_CLAIMED: (lambda: _step_create_branch(state, project_dir), STATUS_BRANCHED),
        STATUS_BRANCHED: (
            lambda: _step_run_claude(state, claude_runner, run_runner, project_dir),
            STATUS_RAN,
        ),
        STATUS_RAN: (lambda: _step_delete_goal_file(state), STATUS_GOAL_DELETED),
        STATUS_GOAL_DELETED: (lambda: _step_commit(state, commit_runner, project_dir), STATUS_COMMITTED),
        STATUS_COMMITTED: (lambda: _step_merge(state), STATUS_MERGED),
    }

    while state.current:
        status = state.current["status"]
        if status == STATUS_MERGED:
            return _step_finish(state, project_dir)

        if status not in transitions:
            return False, f"Unknown goal status: {status}"

        step, next_status = transitions[status]
        logger.info(f"[goal:{state.current['goal_file']}] step after '{status}'")
        ok, error = step()
        if not ok:
            return False, error
        state.current["status"] = next_status
        save_goal_state(state, project_dir)

    return True, ""


# ---------------------------------------------------------------------------
# Merge requests (MERGE control file)
# ---------------------------------------------------------------------------

def merge_request_file(state: GoalState) -> Path:
    return Path(state.goals_dir) / MERGE_FILE_NAME


def claim_merge_request(
    state: GoalState,
    project_dir: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """
    Claim a MERGE control file as the pending merge request.

    Validates the request fully before consuming the file: a remote must be
    configured, the target must be a real remote branch (or resolvable as the
    remote's default branch for an empty file), and it must differ from the
    watch branch. Unusable requests are recorded in state.failed and the file
    is deleted so they don't loop. A dirty working tree blocks the claim the
    same way it blocks goals; the file is left in place to retry.

    Returns (merge_state_or_None, error).
    """
    if state.merge:
        return state.merge, ""

    merge_file = merge_request_file(state)
    if not merge_file.exists():
        # A merge-blocked record is stale once its MERGE file is gone.
        if state.blocked and state.blocked.get("merge_file"):
            state.blocked = None
            save_goal_state(state, project_dir)
        return None, ""

    def reject(reason: str) -> tuple[None, str]:
        merge_file.unlink(missing_ok=True)
        state.blocked = None
        state.failed.append({
            "merge_file": MERGE_FILE_NAME,
            "reason": reason,
            "at": datetime.now().isoformat(),
        })
        save_goal_state(state, project_dir)
        logger.warning(f"Rejecting merge request: {reason}")
        return None, ""

    remote, error = _remote_name()
    if error:
        return None, error
    if remote is None:
        return reject("merge request requires a git remote")

    text = merge_file.read_text().strip()
    if text:
        if len(text.split()) != 1:
            return reject(
                f"{MERGE_FILE_NAME} file must be empty (default branch) or "
                "contain a single branch name"
            )
        target = text
        if _git(["check-ref-format", "--branch", target]).returncode != 0:
            return reject(f"invalid target branch name: {target}")
    else:
        target, error = _default_remote_branch(remote)
        if target is None:
            return None, error

    if target == state.watch_branch:
        return reject(f"target branch equals the watch branch: {target}")

    result = _git(["ls-remote", "--heads", remote, target])
    if result.returncode != 0:
        return None, _git_error(result, f"look up {target} on {remote}")
    if not result.stdout.strip():
        return reject(f"branch not found on {remote}: {target}")

    try:
        dirty = _dirty_paths(state.goals_dir)
    except RuntimeError as e:
        return None, str(e)
    if dirty:
        state.blocked = {
            "merge_file": MERGE_FILE_NAME,
            "reason": "working tree is dirty outside the watched directory and .dvx",
            "dirty_paths": dirty,
            "at": datetime.now().isoformat(),
        }
        save_goal_state(state, project_dir)
        logger.warning(
            "Blocking merge request: working tree is dirty outside the watched directory "
            f"and .dvx: {dirty}"
        )
        return None, ""

    state.merge = {
        "merge_file": MERGE_FILE_NAME,
        "remote": remote,
        "target": target,
        "status": MERGE_STATUS_CLAIMED,
        "push_attempts": 0,
        "requested_at": datetime.now().isoformat(),
    }
    state.blocked = None
    save_goal_state(state, project_dir)
    # Deleting after the save means a crash here re-runs an already-recorded
    # merge - harmless, since a repeated merge is a no-op fast-forward.
    merge_file.unlink(missing_ok=True)
    return state.merge, ""


def _step_merge_local(
    state: GoalState,
    merge_runner: Callable[[str], SessionResult],
    project_dir: Optional[str] = None,
) -> tuple[bool, str]:
    """Bring the remote target into the watch branch, resolving conflicts via Claude."""
    merge = state.merge
    remote, target = merge["remote"], merge["target"]

    # A crash during a previous attempt can leave a merge in progress;
    # the working tree was clean at claim time, so aborting loses nothing
    # that this step won't redo.
    if _merge_in_progress():
        _git(["merge", "--abort"])
    ok, error = checkout(state.watch_branch)
    if not ok:
        return False, error

    result = _git(["fetch", remote, target])
    if result.returncode != 0:
        return False, _git_error(result, f"fetch {target} from {remote}")

    result = _git(["rev-parse", "--verify", f"refs/remotes/{remote}/{target}^{{commit}}"])
    if result.returncode != 0:
        return False, f"Remote branch missing after fetch: {remote}/{target}"
    target_oid = result.stdout.strip()
    merge["target_oid"] = target_oid
    save_goal_state(state, project_dir)

    if _is_ancestor(target_oid, state.watch_branch):
        return True, ""

    message = f"Merge {remote}/{target} into {state.watch_branch} (dvx merge request)"
    result = _git(["merge", "-m", message, target_oid])
    if result.returncode == 0:
        return True, ""
    if not _merge_in_progress():
        return False, _git_error(result, f"merge {remote}/{target} into {state.watch_branch}")

    ok, error = _begin_queued_goal_guard(state, project_dir)
    if not ok:
        _git(["merge", "--abort"])
        return False, error
    guard_ok = True
    guard_error = ""
    try:
        session = merge_runner(f"{remote}/{target}")
    finally:
        guard_ok, guard_error = _finish_queued_goal_guard(state, project_dir)
    if not guard_ok:
        _git(["merge", "--abort"])
        return False, guard_error
    if not session.success:
        _git(["merge", "--abort"])
        return False, (
            f"Conflict resolution failed: {session.block_reason or 'session did not succeed'}"
        )
    if _merge_in_progress() or _git(["ls-files", "-u"]).stdout.strip():
        _git(["merge", "--abort"])
        return False, f"Merge conflicts with {remote}/{target} were not fully resolved"

    head_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if head_branch != state.watch_branch:
        return False, (
            f"Conflict resolution left HEAD on {head_branch}, expected {state.watch_branch}"
        )
    if not _is_ancestor(target_oid, state.watch_branch):
        return False, f"Conflict resolution did not complete the merge of {remote}/{target}"
    try:
        remaining = _dirty_paths(state.goals_dir)
    except RuntimeError as e:
        return False, str(e)
    if remaining:
        return False, f"Working tree still dirty after conflict resolution: {remaining}"
    return True, ""


def _step_push_target(state: GoalState) -> tuple[bool, str, bool]:
    """
    Fast-forward the remote target branch to the watch branch tip.

    Returns (ok, error, remote_moved). remote_moved means the push was
    rejected because the target advanced after the local merge - the race
    the retry loop exists for. The push is never forced.
    """
    merge = state.merge
    remote, target = merge["remote"], merge["target"]
    result = _git(["push", remote, f"{state.watch_branch}:refs/heads/{target}"])
    if result.returncode == 0:
        return True, "", False
    detail = (result.stderr or "") + (result.stdout or "")
    remote_moved = (
        "non-fast-forward" in detail
        or "fetch first" in detail
        or "[rejected]" in detail
    )
    error = _git_error(result, f"push {state.watch_branch} to {remote}/{target}")
    return False, error, remote_moved


def process_merge_request(
    state: GoalState,
    merge_runner: Optional[Callable[[str], SessionResult]] = None,
    project_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Drive the pending merge request through its remaining steps.

    Mirrors process_current_goal: the status field records the last completed
    step, so a crash resumes where it left off. When the remote target
    advances between the local merge and the push (another process merged
    first), the request loops back to re-fetch and re-merge, up to
    MERGE_PUSH_MAX_ATTEMPTS times per run.
    """
    if merge_runner is None:
        def merge_runner(target):
            return resolve_merge_conflicts_with_claude(target, state.goals_dir, model=model)

    while state.merge:
        status = state.merge["status"]
        remote, target = state.merge["remote"], state.merge["target"]
        logger.info(f"[merge:{remote}/{target}] step after '{status}'")

        if status == MERGE_STATUS_CLAIMED:
            ok, error = _step_merge_local(state, merge_runner, project_dir)
            if not ok:
                return False, error
            state.merge["status"] = MERGE_STATUS_LOCAL_MERGED
            save_goal_state(state, project_dir)
        elif status == MERGE_STATUS_LOCAL_MERGED:
            ok, error, remote_moved = _step_push_target(state)
            if ok:
                state.merge["status"] = MERGE_STATUS_TARGET_PUSHED
                save_goal_state(state, project_dir)
                continue
            if not remote_moved:
                return False, error
            attempts = state.merge.get("push_attempts", 0) + 1
            state.merge["push_attempts"] = attempts
            state.merge["status"] = MERGE_STATUS_CLAIMED
            save_goal_state(state, project_dir)
            if attempts >= MERGE_PUSH_MAX_ATTEMPTS:
                state.merge["push_attempts"] = 0
                save_goal_state(state, project_dir)
                return False, (
                    f"{error} ({remote}/{target} kept advancing during the merge; "
                    "re-run `dvx watch` to try again)"
                )
            logger.info(
                f"{remote}/{target} advanced during the merge; re-fetching and merging again"
            )
        elif status == MERGE_STATUS_TARGET_PUSHED:
            ok, error = _push_watch_branch(state)
            if not ok:
                return False, error
            state.completed.append({
                "merge_file": state.merge.get("merge_file", MERGE_FILE_NAME),
                "merged_into": f"{remote}/{target}",
                "finished_at": datetime.now().isoformat(),
            })
            state.merge = None
            save_goal_state(state, project_dir)
        else:
            return False, f"Unknown merge status: {status}"

    return True, ""


def _blocked_dirty_paths_changed(state: GoalState) -> tuple[bool, str]:
    if not state.blocked:
        return True, ""
    try:
        dirty_paths = _dirty_paths(state.goals_dir)
    except RuntimeError as e:
        return False, str(e)
    return sorted(dirty_paths) != sorted(state.blocked.get("dirty_paths", [])), ""


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def _has_resumable_work(state: GoalState) -> bool:
    """
    True if saved state contains work a new watcher run must continue.

    Completed/failed history does not count: an idle state file carries
    nothing worth recovering, and its saved watch branch goes stale as soon
    as the user switches branches.
    """
    return bool(state.current or state.merge or state.blocked or state.queue)


def run_goal_watch(
    start_branch: str,
    goals_dir: str = DEFAULT_TODO_DIR,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    once: bool = False,
    claude_runner: Optional[Callable[[str], SessionResult]] = None,
    run_runner: Optional[Callable[[str], SessionResult]] = None,
    commit_runner: Optional[Callable[[str], SessionResult]] = None,
    merge_runner: Optional[Callable[[str], SessionResult]] = None,
    project_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> int:
    """
    Watch the work directory and process queued files until interrupted.

    Saved state is recovered only when it has resumable work (an in-flight,
    blocked, or queued goal, or a pending merge request) — an idle state file
    from a previous run is discarded so each watch starts from the branch it
    was launched on. An in-flight item resumes at the step after the last one
    recorded, then the queue continues. A MERGE control file in the watched
    directory is claimed between items and processed before the next queued
    item. With once=True, returns as soon as there is no pending work (used by
    tests).
    """
    Path(goals_dir).mkdir(parents=True, exist_ok=True)

    state = load_goal_state(project_dir)
    if state is not None and not _has_resumable_work(state):
        clear_goal_state(project_dir)
        state = None
    if state is None:
        state = GoalState(watch_branch=start_branch, goals_dir=goals_dir)
        save_goal_state(state, project_dir)
        print(f"Watch branch: {state.watch_branch}")
    else:
        if not branch_exists(state.watch_branch):
            print(
                "Error: saved goal state has pending work, but its watch branch "
                f"'{state.watch_branch}' no longer exists."
            )
            print(
                "Recreate the branch to resume, or run `dvx clear` to discard "
                "the saved state and start fresh."
            )
            return 1
        print(f"Recovered goal state (watch branch: {state.watch_branch})")
        if state.current:
            print(
                f"Resuming goal {state.current['goal_file']} "
                f"after step '{state.current['status']}'"
            )
        if state.merge:
            print(
                f"Resuming merge into {state.merge['remote']}/{state.merge['target']} "
                f"after step '{state.merge['status']}'"
            )
        if state.goals_dir != goals_dir:
            print(f"Using watched directory from saved state: {state.goals_dir}")

    watching_notice_printed = False

    while True:
        added = enqueue_new_goals(state, project_dir)
        if added:
            watching_notice_printed = False
        for name in added:
            print(f"Queued {item_type_for_file(name)} item: {name}")

        should_claim = True
        if state.current is None and state.merge is None and state.blocked:
            should_claim, error = _blocked_dirty_paths_changed(state)
            if error:
                print(f"Error: {error}")
                return 1

        # A MERGE request runs between work items and takes precedence over the
        # queue: claim it before claiming the next item.
        if state.current is None and state.merge is None and should_claim:
            claimed_merge, error = claim_merge_request(state, project_dir)
            if error:
                print(f"Error: {error}")
                return 1
            if claimed_merge:
                watching_notice_printed = False
                print(
                    f"Merge requested: {state.watch_branch} -> "
                    f"{claimed_merge['remote']}/{claimed_merge['target']}"
                )
            elif state.blocked and state.blocked.get("merge_file"):
                print(f"Blocked merge request: {state.blocked['reason']}")
                print(f"Dirty paths: {state.blocked['dirty_paths']}")

        if state.current is None and state.merge:
            merged_into = f"{state.merge['remote']}/{state.merge['target']}"
            ok, error = process_merge_request(
                state, merge_runner=merge_runner, project_dir=project_dir, model=model
            )
            if not ok:
                print(f"Error: {error}")
                print("State preserved - re-run `dvx watch` to retry from the failed step.")
                return 1
            print(f"Merged {state.watch_branch} into {merged_into} and pushed.")
            watching_notice_printed = False
            continue

        merge_blocked = bool(state.blocked and state.blocked.get("merge_file"))
        if state.current is None and should_claim and not merge_blocked:
            claimed = claim_next_goal(state, project_dir)
            if claimed:
                watching_notice_printed = False
                print(
                    f"Working on {claimed.get('item_type', item_type_for_file(claimed['goal_file']))} "
                    f"item: {claimed['goal_file']} (branch: {claimed['branch']})"
                )
            elif state.blocked:
                print(f"Blocked item {state.blocked['goal_file']}: {state.blocked['reason']}")
                print(f"Dirty paths: {state.blocked['dirty_paths']}")

        if state.current:
            ok, error = process_current_goal(
                state,
                claude_runner=claude_runner,
                run_runner=run_runner,
                commit_runner=commit_runner,
                project_dir=project_dir,
                model=model,
            )
            if not ok:
                print(f"Error: {error}")
                print("State preserved - re-run `dvx watch` to retry from the failed step.")
                return 1
            print(f"Item complete and merged into {state.watch_branch}.")
            watching_notice_printed = False
            continue

        if state.blocked:
            if once:
                return 1
            time.sleep(poll_interval)
            continue

        if not watching_notice_printed:
            print(f"Watching for work files in: {state.goals_dir}")
            watching_notice_printed = True

        if once:
            return 0
        time.sleep(poll_interval)
