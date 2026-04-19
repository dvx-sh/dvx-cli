"""
Context snapshot management for dvx.

A snapshot is a markdown file under `.dvx/context/{slug}-{YYYYMMDDTHHMMSSZ}.md`
capturing task statement, desired outcome, known facts, constraints, unknowns,
likely codebase touchpoints, and decision boundaries. Snapshots are optional
grounding input for skills.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from state import get_dvx_root

logger = logging.getLogger(__name__)

CONTEXT_DIR_NAME = "context"
MAX_SLUG_LEN = 60
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def slug_from(text: str) -> str:
    """
    Derive a lowercase ASCII-safe slug from free-form text.

    - Lowercases
    - Replaces non-alphanumeric runs with a single dash
    - Strips leading/trailing dashes
    - Caps at MAX_SLUG_LEN chars
    - Falls back to "snapshot" if the result is empty
    """
    lowered = text.lower()
    ascii_safe = re.sub(r"[^a-z0-9]+", "-", lowered)
    trimmed = ascii_safe.strip("-")
    if not trimmed:
        return "snapshot"
    return trimmed[:MAX_SLUG_LEN].rstrip("-") or "snapshot"


def get_context_dir(project_dir: Optional[str] = None) -> Path:
    """Return the `.dvx/context/` directory for a project (may not exist)."""
    return get_dvx_root(project_dir) / CONTEXT_DIR_NAME


def ensure_context_dir(project_dir: Optional[str] = None) -> Path:
    """Create `.dvx/context/` on demand and return its path."""
    context_dir = get_context_dir(project_dir)
    context_dir.mkdir(parents=True, exist_ok=True)
    return context_dir


def _timestamp(now: Optional[datetime] = None) -> str:
    """Return a UTC ISO 8601-ish timestamp suitable for filenames."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def write(
    slug: str,
    content: str,
    project_dir: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Path:
    """
    Write a snapshot for `slug` with the given content.

    The filename is `{slug}-{timestamp}.md` and the parent directory is
    created on demand. Returns the path written to.
    """
    if not slug:
        slug = "snapshot"

    context_dir = ensure_context_dir(project_dir)
    filename = f"{slug}-{_timestamp(now)}.md"
    path = context_dir / filename
    path.write_text(content)
    logger.info(f"Wrote context snapshot to {path}")
    return path


def load_latest(slug: str, project_dir: Optional[str] = None) -> Optional[Path]:
    """
    Return the newest snapshot file matching `{slug}-*.md`, or None.

    Because the timestamp suffix is ISO 8601 (lex-sortable), picking the
    lexicographic max is equivalent to picking the most recent write.
    """
    context_dir = get_context_dir(project_dir)
    if not context_dir.exists():
        return None

    candidates = sorted(context_dir.glob(f"{slug}-*.md"))
    if not candidates:
        return None
    return candidates[-1]


def load_latest_content(slug: str, project_dir: Optional[str] = None) -> Optional[str]:
    """Return the content of the latest snapshot for `slug`, or None."""
    path = load_latest(slug, project_dir)
    if path is None:
        return None
    try:
        return path.read_text()
    except OSError as exc:
        logger.warning(f"Failed to read snapshot {path}: {exc}")
        return None


def slug_from_plan_file(plan_file: str) -> str:
    """
    Derive a canonical slug from a plan filename.

    Strips the PLAN- prefix and the .md extension so a plan called
    `PLAN-user-auth.md` maps to the slug `user-auth`, matching the
    filename a human would recognize.
    """
    name = Path(plan_file).stem
    if name.upper().startswith("PLAN-"):
        name = name[len("PLAN-"):]
    return slug_from(name)


def snapshot_template(
    task_statement: str,
    desired_outcome: str = "",
    known_facts: str = "",
    constraints: str = "",
    unknowns: str = "",
    touchpoints: str = "",
    decision_boundaries: str = "",
) -> str:
    """Return a well-formed snapshot body for the required section set."""
    return (
        "# Context Snapshot\n\n"
        "## Task statement\n\n"
        f"{task_statement.strip() or '(to be filled in)'}\n\n"
        "## Desired outcome\n\n"
        f"{desired_outcome.strip() or '(to be filled in)'}\n\n"
        "## Known facts / evidence\n\n"
        f"{known_facts.strip() or '(none captured)'}\n\n"
        "## Constraints\n\n"
        f"{constraints.strip() or '(none captured)'}\n\n"
        "## Unknowns / open questions\n\n"
        f"{unknowns.strip() or '(none captured)'}\n\n"
        "## Likely codebase touchpoints\n\n"
        f"{touchpoints.strip() or '(none captured)'}\n\n"
        "## Decision boundaries\n\n"
        f"{decision_boundaries.strip() or '(none captured)'}\n"
    )
