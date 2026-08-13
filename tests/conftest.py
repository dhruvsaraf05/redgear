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


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """A real git repository that can actually be linted, tested and measured.

    ``git_repo`` is deliberately inert -- it exists to be diffed. Gates 3-6
    execute real tools against real code, so they need a repository where
    ``ruff``, ``pytest`` and ``coverage`` all have something to say. Layout::

        repo/
            conftest.py                 -- puts src/ on sys.path
            src/pkg/__init__.py         -- the module under test
            tests/test_pkg.py           -- its tests

    The source lives under ``src/`` for two reasons that are not cosmetic.
    ``coverage --source`` takes packages and directories, **not file paths**:
    pointed at a bare ``pkg.py`` it collects nothing at all and reports "No
    data was collected". And keeping the tests outside the measured tree is
    what lets a changed test file legitimately drop out of the coverage-delta
    denominator instead of scoring as uncovered.

    The nested path also means coverage.py keys its JSON by
    ``src\\pkg\\__init__.py`` on Windows, so the POSIX-normalisation the gate
    has to do is actually exercised rather than trivially satisfied.

    The repository's own ``conftest.py`` is what makes ``from pkg import ...``
    resolve. Relying on pytest's implicit ``sys.path`` insertion instead would
    couple the fixture to the import mode, and the whole point of these tests
    is that the nested runner's configuration is pinned rather than inferred.

    Deliberately carries **no** ``pyproject.toml``. That is the dangerous
    shape: with no config of its own, a naive nested pytest walks up out of
    the repository and adopts whatever it finds above -- which is exactly what
    ``test_nested_runner_isolated`` pins down.
    """
    repo = tmp_path / "pyrepo"
    repo.mkdir()

    _run_git("init", "-b", "main", cwd=repo)
    _run_git("config", "user.email", "redgear-fixture@example.invalid", cwd=repo)
    _run_git("config", "user.name", "redgear fixture", cwd=repo)
    _run_git("config", "commit.gpgsign", "false", cwd=repo)

    (repo / "conftest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        'sys.path.insert(0, str(Path(__file__).parent / "src"))\n',
        encoding="utf-8",
    )
    src_pkg = repo / "src" / "pkg"
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_pkg.py").write_text(
        "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    _run_git("add", "-A", cwd=repo)
    _run_git("commit", "-m", "initial commit", cwd=repo)

    return repo
