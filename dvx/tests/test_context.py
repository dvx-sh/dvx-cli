"""Tests for the context snapshot module."""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from context import (
    TIMESTAMP_FORMAT,
    _timestamp,
    ensure_context_dir,
    get_context_dir,
    load_latest,
    load_latest_content,
    slug_from,
    slug_from_plan_file,
    snapshot_template,
    write,
)


class TestSlugFrom:
    def test_lowercases_and_hyphenates(self):
        assert slug_from("Build A User Auth System") == "build-a-user-auth-system"

    def test_collapses_special_chars(self):
        assert slug_from("Add /healthz endpoint to Flask app") == "add-healthz-endpoint-to-flask-app"

    def test_strips_edges(self):
        assert slug_from("   ---hello world---   ") == "hello-world"

    def test_truncates(self):
        long = "a" * 120
        result = slug_from(long)
        assert len(result) <= 60
        assert result == "a" * 60

    def test_truncates_and_trims_trailing_dash(self):
        # A dash that would land exactly at the cap should be stripped.
        text = ("a" * 59) + "-more"  # 64 chars total
        result = slug_from(text)
        assert result == "a" * 59
        assert not result.endswith("-")

    def test_empty_input_falls_back(self):
        assert slug_from("") == "snapshot"
        assert slug_from("   ") == "snapshot"
        assert slug_from("***") == "snapshot"


class TestSlugFromPlanFile:
    def test_strips_plan_prefix(self):
        assert slug_from_plan_file("PLAN-user-auth.md") == "user-auth"

    def test_handles_path(self):
        assert slug_from_plan_file("plans/PLAN-healthz-endpoint.md") == "healthz-endpoint"

    def test_no_prefix(self):
        assert slug_from_plan_file("notes.md") == "notes"

    def test_case_insensitive_prefix(self):
        assert slug_from_plan_file("plan-foo.md") == "foo"


class TestTimestamp:
    def test_format(self):
        fixed = datetime(2026, 4, 18, 9, 30, 15, tzinfo=timezone.utc)
        stamp = _timestamp(fixed)
        assert stamp == "20260418T093015Z"

    def test_format_roundtrip(self):
        fixed = datetime(2026, 4, 18, 9, 30, 15, tzinfo=timezone.utc)
        stamp = _timestamp(fixed)
        datetime.strptime(stamp, TIMESTAMP_FORMAT)

    def test_naive_treated_as_utc(self):
        naive = datetime(2026, 4, 18, 9, 30, 15)
        assert _timestamp(naive) == "20260418T093015Z"


class TestDirectoryHandling:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_context_dir_does_not_create(self):
        context_dir = get_context_dir(self.temp_dir)
        assert context_dir == Path(self.temp_dir) / ".dvx" / "context"
        assert not context_dir.exists()

    def test_ensure_creates_on_demand(self):
        context_dir = ensure_context_dir(self.temp_dir)
        assert context_dir.exists()
        assert context_dir.is_dir()


class TestWriteAndLoad:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_write_creates_file(self):
        fixed = datetime(2026, 4, 18, 9, 30, 15, tzinfo=timezone.utc)
        path = write("user-auth", "# hello\n", project_dir=self.temp_dir, now=fixed)
        assert path.exists()
        assert path.name == "user-auth-20260418T093015Z.md"
        assert path.read_text() == "# hello\n"

    def test_write_falls_back_to_snapshot_slug(self):
        path = write("", "body", project_dir=self.temp_dir)
        assert path.name.startswith("snapshot-")

    def test_load_latest_none_when_missing(self):
        assert load_latest("user-auth", project_dir=self.temp_dir) is None
        assert load_latest_content("user-auth", project_dir=self.temp_dir) is None

    def test_load_latest_picks_newest(self):
        earlier = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        later = datetime(2026, 4, 18, 9, 30, 15, tzinfo=timezone.utc)

        write("user-auth", "first", project_dir=self.temp_dir, now=earlier)
        write("user-auth", "second", project_dir=self.temp_dir, now=later)

        latest = load_latest("user-auth", project_dir=self.temp_dir)
        assert latest is not None
        assert latest.name == "user-auth-20260418T093015Z.md"
        assert load_latest_content("user-auth", project_dir=self.temp_dir) == "second"

    def test_load_latest_ignores_other_slugs(self):
        stamp = datetime(2026, 4, 18, 9, 30, 15, tzinfo=timezone.utc)
        write("user-auth", "auth", project_dir=self.temp_dir, now=stamp)
        write("healthz", "health", project_dir=self.temp_dir, now=stamp)

        assert load_latest_content("user-auth", project_dir=self.temp_dir) == "auth"
        assert load_latest_content("healthz", project_dir=self.temp_dir) == "health"
        assert load_latest("missing", project_dir=self.temp_dir) is None


class TestSnapshotTemplate:
    def test_required_sections_present(self):
        body = snapshot_template("Build feature X")
        for heading in (
            "## Task statement",
            "## Desired outcome",
            "## Known facts / evidence",
            "## Constraints",
            "## Unknowns / open questions",
            "## Likely codebase touchpoints",
            "## Decision boundaries",
        ):
            assert heading in body
        assert "Build feature X" in body

    def test_empty_fields_filled_in(self):
        body = snapshot_template("Task")
        assert "(none captured)" in body
