"""Shared pytest fixtures.

Per CLAUDE.md section 10.4, verification-gate tests run against a real git
repository rather than mocked subprocess calls. The ``git_repo`` fixture
creates one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, initialised git repository with one commit and a minimal layout.

    Layout::

        repo/
            src/
                pkg/
                    __init__.py
            tests/
                test_pkg.py

    ``user.email``, ``user.name``, and ``commit.gpgsign=false`` are set as
    local config so the fixture commits cleanly on any machine, regardless of
    the host's global git configuration.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    _run_git("init", "-b", "main", cwd=repo)
    _run_git("config", "user.email", "redgear-fixture@example.invalid", cwd=repo)
    _run_git("config", "user.name", "redgear fixture", cwd=repo)
    _run_git("config", "commit.gpgsign", "false", cwd=repo)

    src_pkg = repo / "src" / "pkg"
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text("", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_pkg.py").write_text(
        "def test_placeholder() -> None:\n    assert True\n",
        encoding="utf-8",
    )

    _run_git("add", "-A", cwd=repo)
    _run_git("commit", "-m", "initial commit", cwd=repo)

    return repo
