"""Invoke tasks for dvx development."""

from invoke import task


@task
def tests(c, verbose=False):
    """Run all tests."""
    cmd = "pytest tests/"
    if verbose:
        cmd += " -v"
    c.run(cmd)


@task
def lint(c, fix=False):
    """Run ruff linter."""
    cmd = "ruff check src/ tests/"
    if fix:
        cmd += " --fix"
    c.run(cmd)


@task
def fmt(c):
    """Format code with ruff."""
    c.run("ruff format src/ tests/")


@task(pre=[lint, tests])
def check(c):
    """Run lint and tests."""
    pass
