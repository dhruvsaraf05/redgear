"""T-0004: failing tests for redgear.paths -- glob matching and .redgear/
layout resolution.

``redgear/paths.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

This module decides what an agent is allowed to write (CLAUDE.md section
1.4 G2, section 4.4 invariant 7), so its edge cases are security-relevant:
an absolute path or a ``..``-escape that gets normalised into "matching" a
writable glob would let an agent write outside its granted scope with the
verifier gate never seeing a violation. Every test below that touches an
edge case is defending against that specific failure mode, not being
thorough for its own sake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from redgear.errors import ScopeOverlapError
from redgear.paths import (
    adr_index_path,
    assert_no_scope_overlap,
    config_path,
    events_path,
    find_scope_overlaps,
    match_glob,
    redgear_dir,
    run_lock_path,
    spec_path,
    stop_path,
    task_graph_path,
    task_lock_path,
)

# ---------------------------------------------------------------------------
# AC-2: recursive glob matching handles nested paths, never matches absolute
# paths.
# ---------------------------------------------------------------------------


def test_glob_recursion_and_relativity() -> None:
    """`**` recurses arbitrarily deep; absolute paths never match."""
    assert match_glob("redgear/__init__.py", "redgear/**") is True
    assert match_glob("redgear/a/b/c/d.py", "redgear/**") is True
    assert match_glob("redgear/a/b/c/d/e/f.py", "redgear/**") is True
    assert match_glob("tests/test_x.py", "redgear/**") is False

    # Absolute paths never match a relative glob, even when the tail of the
    # absolute path looks exactly like something the glob would otherwise
    # accept.
    assert match_glob("/redgear/__init__.py", "redgear/**") is False
    assert match_glob("/etc/passwd", "**") is False
    assert match_glob("C:/Users/x/redgear/__init__.py", "redgear/**") is False
    assert match_glob("C:\\Users\\x\\redgear\\__init__.py", "redgear/**") is False


def test_star_does_not_cross_separator() -> None:
    """`*` matches within one path segment only."""
    assert match_glob("redgear/foo.py", "redgear/*.py") is True
    assert match_glob("redgear/sub/foo.py", "redgear/*.py") is False
    assert match_glob("redgear/sub/foo.py", "redgear/**/*.py") is True
    assert match_glob("redgear/foo.py", "redgear/**/*.py") is True


def test_exact_literal_pattern_matches_only_itself() -> None:
    assert match_glob("pyproject.toml", "pyproject.toml") is True
    assert match_glob("pyproject.toml.bak", "pyproject.toml") is False
    assert match_glob("sub/pyproject.toml", "pyproject.toml") is False


def test_matching_is_posix_and_platform_independent() -> None:
    """A candidate path arriving with OS-native (backslash) separators --
    as `str(Path(...))` produces on Windows -- must still match a
    POSIX-style glob. A test that only passes with backslashes, or only
    with forward slashes, is a bug: this repo's dev machine is Windows, so
    both directions are exercised here rather than assumed."""
    assert match_glob("redgear\\a\\b.py", "redgear/**") is True
    assert match_glob("redgear/a/b.py", "redgear/**") is True
    assert match_glob(str(Path("redgear") / "a" / "b.py"), "redgear/**") is True


def test_dotdot_escape_rejected_outright() -> None:
    """A path escaping the repo root via `..` is rejected outright, not
    normalised into something that happens to match."""
    assert match_glob("../outside/foo.py", "**") is False
    assert match_glob("redgear/../../../etc/passwd", "redgear/**") is False
    assert match_glob("redgear/..", "redgear/**") is False
    assert match_glob("..", "**") is False
    # A literal directory component that merely contains ".." as a
    # substring (not the segment itself) is not a traversal and must not
    # be rejected by an overly blunt substring check.
    assert match_glob("redgear/..foo/bar.py", "redgear/**") is True


def test_empty_pattern_list_matches_nothing() -> None:
    assert match_glob("anything.py", "") is False


# ---------------------------------------------------------------------------
# AC-3: overlapping writable and frozen globs are detected over a tracked
# file list.
# ---------------------------------------------------------------------------


def test_scope_overlap_detected() -> None:
    """The exact T-0001 shape: a scaffold's writable globs include two
    redgear/** marker files; if redgear/** were ALSO frozen (as it is for
    every test_authoring/implementation node elsewhere in the graph), that
    is precisely the overlap CLAUDE.md section 4.4 invariant 7 forbids."""
    writable = [
        "pyproject.toml",
        ".gitignore",
        ".gitattributes",
        ".pre-commit-config.yaml",
        ".github/**",
        "LICENSE",
        "README.md",
        "redgear/__init__.py",
        "redgear/py.typed",
        "tests/conftest.py",
        "tests/test_smoke.py",
        "docs/**",
    ]
    frozen = ["redgear/**"]
    tracked_files = [
        "pyproject.toml",
        "redgear/__init__.py",
        "redgear/py.typed",
        "redgear/schemas.py",
        "tests/conftest.py",
    ]

    overlaps = find_scope_overlaps(writable, frozen, tracked_files)

    assert overlaps == ["redgear/__init__.py", "redgear/py.typed"]

    with pytest.raises(ScopeOverlapError) as excinfo:
        assert_no_scope_overlap(writable, frozen, tracked_files)
    assert excinfo.value.detail["overlaps"] == ["redgear/__init__.py", "redgear/py.typed"]


def test_scope_overlap_clean_case_is_actually_clean() -> None:
    """The real, current T-0001 node: writable includes the two marker
    files, frozen_globs is empty. No overlap."""
    writable = ["redgear/__init__.py", "redgear/py.typed", "tests/conftest.py"]
    frozen: list[str] = []
    tracked_files = ["redgear/__init__.py", "redgear/py.typed", "redgear/schemas.py"]

    assert find_scope_overlaps(writable, frozen, tracked_files) == []


def test_scope_overlap_respects_empty_glob_lists() -> None:
    assert find_scope_overlaps([], ["redgear/**"], ["redgear/schemas.py"]) == []
    assert find_scope_overlaps(["redgear/**"], [], ["redgear/schemas.py"]) == []
    assert find_scope_overlaps([], [], []) == []


def test_file_matching_multiple_writable_globs_is_fine() -> None:
    """A file matching two different writable patterns is not an overlap
    -- overlap is specifically writable-vs-frozen, not within one list."""
    writable = ["tests/**", "tests/conftest.py"]
    frozen: list[str] = []
    tracked_files = ["tests/conftest.py"]

    assert find_scope_overlaps(writable, frozen, tracked_files) == []


def test_scope_overlap_reports_every_violation_not_only_first() -> None:
    """CLAUDE.md section 7.2: 'Report every violation, not just the
    first.' find_scope_overlaps is the computation the frozen_hash_check
    gate will eventually call, so it must not short-circuit internally."""
    writable = ["redgear/**"]
    frozen = ["redgear/**"]
    tracked_files = ["redgear/a.py", "redgear/b.py", "redgear/c.py"]

    overlaps = find_scope_overlaps(writable, frozen, tracked_files)

    assert overlaps == ["redgear/a.py", "redgear/b.py", "redgear/c.py"]


# ---------------------------------------------------------------------------
# .redgear/ layout resolution -- CLAUDE.md section 2.3, verbatim.
# ---------------------------------------------------------------------------


def test_redgear_layout_matches_section_2_3(tmp_path: Path) -> None:
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()

    assert redgear_dir(repo_root) == repo_root / ".redgear"
    assert config_path(repo_root) == repo_root / ".redgear" / "config.json"
    assert spec_path(repo_root) == repo_root / ".redgear" / "spec" / "spec.json"
    assert task_graph_path(repo_root) == repo_root / ".redgear" / "task_graph.json"
    assert adr_index_path(repo_root) == repo_root / ".redgear" / "adrs" / "index.json"
    assert events_path(repo_root) == repo_root / ".redgear" / "events.jsonl"
    assert stop_path(repo_root) == repo_root / ".redgear" / "STOP"
    assert run_lock_path(repo_root) == repo_root / ".redgear" / "locks" / "run.lock"
    assert task_lock_path(repo_root, "T-0042") == (repo_root / ".redgear" / "locks" / "T-0042.lock")


def test_redgear_layout_resolves_against_a_real_directory(tmp_path: Path) -> None:
    """Uses tmp_path for actual filesystem work -- never the real
    .redgear/ directory of this repository."""
    repo_root = tmp_path / "target-repo"
    (repo_root / ".redgear" / "spec").mkdir(parents=True)
    (repo_root / ".redgear" / "spec" / "spec.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )

    resolved = spec_path(repo_root)
    assert resolved.exists()
    assert resolved.is_relative_to(repo_root)
    assert json.loads(resolved.read_text(encoding="utf-8")) == {"schema_version": 1}
