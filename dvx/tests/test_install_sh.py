"""Tests for the root install.sh installer modes.

Each test runs install.sh against an isolated HOME (tmp_path) and a minimal
fake checkout whose dvx/bin/setup is a stub that records its arguments in
$HOME/.dvx/setup-ran — the real setup (venv + pip install) is never run.
"""

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


def write_payload(root: Path) -> None:
    """Create a minimal fake dvx/ payload under root."""
    (root / "dvx" / "bin").mkdir(parents=True)
    setup = root / "dvx" / "bin" / "setup"
    setup.write_text('#!/bin/bash\nmkdir -p "$HOME/.dvx"\necho "$@" > "$HOME/.dvx/setup-ran"\n')
    setup.chmod(0o755)
    dvx_bin = root / "dvx" / "bin" / "dvx"
    dvx_bin.write_text("#!/bin/bash\n")
    dvx_bin.chmod(0o755)
    (root / "dvx" / "src" / "skills").mkdir(parents=True)
    (root / "dvx" / "src" / "cli.py").write_text("")
    (root / "dvx" / "src" / "skills" / "demo.md").write_text("# demo skill\n")
    (root / "dvx" / "pyproject.toml").write_text('[project]\nname = "dvx"\n')


@pytest.fixture
def home(tmp_path):
    """Isolated HOME directory."""
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def fake_checkout(tmp_path):
    """Fake repo checkout containing install.sh and a stub dvx/ payload."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    write_payload(checkout)
    script = checkout / "install.sh"
    shutil.copy(INSTALL_SH, script)
    script.chmod(0o755)
    return checkout


@pytest.fixture
def remote_tarball(tmp_path):
    """file:// URL to a tarball fixture shaped like the GitHub main archive."""
    src = tmp_path / "tarball-src" / "dvx-cli-main"
    src.mkdir(parents=True)
    write_payload(src)
    tar_path = tmp_path / "dvx-cli-main.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(src, arcname="dvx-cli-main")
    return f"file://{tar_path}"


def run_install(checkout: Path, home: Path, *args: str, env_extra: dict | None = None):
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(checkout / "install.sh"), *args],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
    )


class TestLocalMode:
    def test_local_flag_installs_payload_and_runs_setup(self, fake_checkout, home):
        result = run_install(fake_checkout, home, "--local")
        assert result.returncode == 0, result.stderr
        assert "local clone" in result.stdout
        assert (home / ".dvx" / "bin" / "setup").exists()
        assert (home / ".dvx" / "src" / "cli.py").exists()
        assert (home / ".dvx" / "setup-ran").exists()
        assert (home / ".claude" / "commands" / "dvx" / "demo.md").exists()

    def test_no_flags_auto_detects_local(self, fake_checkout, home):
        result = run_install(fake_checkout, home)
        assert result.returncode == 0, result.stderr
        assert "local clone" in result.stdout
        assert (home / ".dvx" / "setup-ran").exists()

    def test_local_without_payload_errors(self, tmp_path, home):
        bare = tmp_path / "bare"
        bare.mkdir()
        script = bare / "install.sh"
        shutil.copy(INSTALL_SH, script)
        script.chmod(0o755)
        result = run_install(bare, home, "--local")
        assert result.returncode != 0
        assert "Error" in result.stderr
        assert not (home / ".dvx").exists()

    def test_dev_flag_forwarded_to_setup(self, fake_checkout, home):
        result = run_install(fake_checkout, home, "--local", "--dev")
        assert result.returncode == 0, result.stderr
        assert (home / ".dvx" / "setup-ran").read_text().strip() == "--dev"


class TestRemoteMode:
    def test_remote_flag_installs_from_tarball(self, fake_checkout, home, remote_tarball):
        result = run_install(
            fake_checkout,
            home,
            "--remote",
            env_extra={"DVX_REPO_TARBALL": remote_tarball},
        )
        assert result.returncode == 0, result.stderr
        assert "local clone" not in result.stdout
        assert "Downloading" in result.stdout
        assert (home / ".dvx" / "src" / "cli.py").exists()
        assert (home / ".dvx" / "setup-ran").exists()

    def test_piped_execution_chooses_remote(self, tmp_path, home, remote_tarball):
        workdir = tmp_path / "empty"
        workdir.mkdir()
        env = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "DVX_REPO_TARBALL": remote_tarball,
        }
        with open(INSTALL_SH) as script:
            result = subprocess.run(
                ["bash"],
                stdin=script,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
            )
        assert result.returncode == 0, result.stderr
        assert "local clone" not in result.stdout
        assert "Downloading" in result.stdout
        assert (home / ".dvx" / "setup-ran").exists()


class TestFlagParsing:
    def test_help_prints_usage_and_installs_nothing(self, fake_checkout, home):
        result = run_install(fake_checkout, home, "--help")
        assert result.returncode == 0
        assert "Usage" in result.stdout
        assert "--local" in result.stdout
        assert "--remote" in result.stdout
        assert "--dev" in result.stdout
        assert not (home / ".dvx").exists()

    def test_short_help_flag(self, fake_checkout, home):
        result = run_install(fake_checkout, home, "-h")
        assert result.returncode == 0
        assert "Usage" in result.stdout
        assert not (home / ".dvx").exists()

    def test_unknown_flag_errors_and_installs_nothing(self, fake_checkout, home):
        result = run_install(fake_checkout, home, "--bogus")
        assert result.returncode != 0
        assert "unknown option" in result.stderr
        assert "Usage" in result.stderr
        assert not (home / ".dvx").exists()

    def test_local_and_remote_conflict(self, fake_checkout, home):
        result = run_install(fake_checkout, home, "--local", "--remote")
        assert result.returncode != 0
        assert "mutually exclusive" in result.stderr
        assert not (home / ".dvx").exists()
