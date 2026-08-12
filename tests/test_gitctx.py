"""T-0020: failing tests for redgear.gitctx -- read-only git interrogation.

``redgear/gitctx.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

Phase C begins here: every verification gate is built on this being correct.
Per section 10.4 and this node's own graph note, these run against a **real
git repository** (the ``git_repo`` fixture in conftest.py) and do **not** mock
subprocess. The gates are about real git state and real exit codes; a mocked
git proves only that the mock behaves like the mock.

Three details are load-bearing and easy to get subtly wrong:

* **Untracked files need ``git add -A -N``** (intent-to-add) to appear in a
  diff at all. Without it a brand-new file under a frozen glob is invisible,
  and the G2 frozen-hash gate silently passes work it should have caught.
  This is the only index-touching call redgear makes; it must never commit.
* **``--unified=0``** is required. Context lines would be counted as changed
  and silently inflate the coverage-delta denominator (section 7.4), turning
  a real coverage regression into a passing gate.
* **Dirty-tree detection reports the paths**, not a boolean. "Refuses to
  start on a dirty tree" is only actionable if it says which files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from redgear.gitctx import (
    changed_files,
    changed_lines_from_patch,
    diff_patch,
    dirty_paths,
    head_commit,
    is_dirty,
    tracked_and_untracked,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------------------
# AC-1: the changed set covers modified, added, deleted and untracked.
# ---------------------------------------------------------------------------


def test_changed_set_all_kinds(git_repo: Path) -> None:
    """All four kinds, in one diff, against a real baseline commit.

    Untracked is the one that breaks naive implementations: `git diff` alone
    does not show it, so a gate built on the naive version would let an agent
    create a brand-new file inside a frozen glob undetected.
    """
    base = _git(git_repo, "rev-parse", "HEAD").strip()

    # modified
    (git_repo / "src" / "pkg" / "__init__.py").write_text("VERSION = 2\n", encoding="utf-8")
    # deleted
    (git_repo / "tests" / "test_pkg.py").unlink()
    # added, then staged -- a normal `git add`
    (git_repo / "src" / "pkg" / "added.py").write_text("x = 1\n", encoding="utf-8")
    _git(git_repo, "add", "src/pkg/added.py")
    # untracked, never staged
    (git_repo / "src" / "pkg" / "untracked.py").write_text("y = 2\n", encoding="utf-8")

    result = changed_files(git_repo, base)
    paths = {entry.path for entry in result}

    assert "src/pkg/__init__.py" in paths, "modified file missing"
    assert "tests/test_pkg.py" in paths, "deleted file missing"
    assert "src/pkg/added.py" in paths, "added file missing"
    assert "src/pkg/untracked.py" in paths, "UNTRACKED file missing -- G2 would be blind"

    by_path = {entry.path: entry for entry in result}
    assert by_path["src/pkg/__init__.py"].status == "M"
    assert by_path["tests/test_pkg.py"].status == "D"
    assert by_path["src/pkg/added.py"].status == "A"
    assert by_path["src/pkg/untracked.py"].status == "A"

    # POSIX separators regardless of host, so a Windows run and a Linux run
    # produce comparable sets.
    assert all("\\" not in path for path in paths)


def test_no_changes_yields_empty_set(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD").strip()
    assert changed_files(git_repo, base) == []


def test_tracked_and_untracked_listing(git_repo: Path) -> None:
    """The file list the frozen-hash gate expands globs against. It must
    include untracked-but-not-ignored files for the same reason the diff
    must."""
    (git_repo / "src" / "pkg" / "fresh.py").write_text("z = 3\n", encoding="utf-8")
    (git_repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (git_repo / "ignored.py").write_text("nope\n", encoding="utf-8")

    listing = tracked_and_untracked(git_repo)

    assert "src/pkg/__init__.py" in listing
    assert "src/pkg/fresh.py" in listing
    assert "ignored.py" not in listing, "an ignored file is not part of the working set"


# ---------------------------------------------------------------------------
# AC-2: untracked files appear via intent-to-add, without committing.
# ---------------------------------------------------------------------------


def test_untracked_visible_without_commit(git_repo: Path) -> None:
    """`git add -A -N` makes untracked content visible as additions with full
    line data. It is the only index-touching call redgear makes, and it must
    never create a commit."""
    base = _git(git_repo, "rev-parse", "HEAD").strip()
    commits_before = _git(git_repo, "rev-list", "--count", "HEAD").strip()

    (git_repo / "src" / "pkg" / "brand_new.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )

    patch = diff_patch(git_repo, base)

    assert "brand_new.py" in patch, "intent-to-add did not expose the untracked file"
    assert "+def f():" in patch, "untracked file appeared without its line content"

    # No commit was created.
    assert _git(git_repo, "rev-list", "--count", "HEAD").strip() == commits_before
    assert _git(git_repo, "rev-parse", "HEAD").strip() == base

    # And the file is still not committed -- intent-to-add is reversible.
    status = _git(git_repo, "status", "--porcelain")
    assert "brand_new.py" in status


# ---------------------------------------------------------------------------
# AC-3: changed line numbers from zero-context hunks.
# ---------------------------------------------------------------------------


def test_changed_lines_zero_context(git_repo: Path) -> None:
    """Parsed from the NEW file's line numbers, with no context inflation.

    With the default 3 lines of context, an edit to line 10 would report
    lines 7-13 as changed. Fed into the coverage-delta denominator that turns
    a real regression into a pass, which is why section 7.4 mandates
    ``--unified=0``.
    """
    target = git_repo / "src" / "pkg" / "wide.py"
    target.write_text("\n".join(f"line_{i} = {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    base = _commit_all(git_repo, "add wide file")

    # Change exactly one line in the middle of a large file.
    lines = target.read_text(encoding="utf-8").splitlines()
    lines[9] = "line_10 = 999"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    patch = diff_patch(git_repo, base)
    changed = changed_lines_from_patch(patch)

    assert "src/pkg/wide.py" in changed
    assert changed["src/pkg/wide.py"] == {10}, (
        "expected exactly line 10; context lines were counted as changed"
    )


def test_changed_lines_multiple_hunks_and_files(git_repo: Path) -> None:
    first = git_repo / "src" / "pkg" / "a.py"
    second = git_repo / "src" / "pkg" / "b.py"
    body = "\n".join(f"v{i} = {i}" for i in range(1, 31)) + "\n"
    first.write_text(body, encoding="utf-8")
    second.write_text(body, encoding="utf-8")
    base = _commit_all(git_repo, "add two files")

    a_lines = first.read_text(encoding="utf-8").splitlines()
    a_lines[2] = "v3 = 300"
    a_lines[20] = "v21 = 2100"
    first.write_text("\n".join(a_lines) + "\n", encoding="utf-8")

    b_lines = second.read_text(encoding="utf-8").splitlines()
    b_lines[0] = "v1 = 100"
    second.write_text("\n".join(b_lines) + "\n", encoding="utf-8")

    changed = changed_lines_from_patch(diff_patch(git_repo, base))

    assert changed["src/pkg/a.py"] == {3, 21}
    assert changed["src/pkg/b.py"] == {1}


def test_changed_lines_for_a_pure_deletion_is_empty(git_repo: Path) -> None:
    """A deleted file has no lines in the new revision. The denominator is
    over NEW-file lines, so a deletion contributes nothing rather than
    contributing zero-coverage lines that would fail the gate unfairly."""
    target = git_repo / "src" / "pkg" / "doomed.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    base = _commit_all(git_repo, "add doomed")

    target.unlink()
    changed = changed_lines_from_patch(diff_patch(git_repo, base))

    assert changed.get("src/pkg/doomed.py", set()) == set()


def test_changed_lines_parser_handles_hunk_header_forms() -> None:
    """A hunk header may omit the count when it is 1. A parser that assumes
    the comma form silently drops every single-line change."""
    patch = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -5 +5 @@\n"
        "-old\n"
        "+new\n"
        "@@ -10,0 +11,3 @@\n"
        "+one\n"
        "+two\n"
        "+three\n"
    )
    assert changed_lines_from_patch(patch) == {"x.py": {5, 11, 12, 13}}


# ---------------------------------------------------------------------------
# AC-4: a dirty tree is detected and its paths reported.
# ---------------------------------------------------------------------------


def test_dirty_tree_detected(git_repo: Path) -> None:
    """Section 8.4 refuses to start on a dirty tree. That refusal is only
    actionable if it names the files -- "the tree is dirty" leaves the user
    running `git status` themselves."""
    assert is_dirty(git_repo) is False
    assert dirty_paths(git_repo) == []

    (git_repo / "src" / "pkg" / "__init__.py").write_text("changed = True\n", encoding="utf-8")
    assert is_dirty(git_repo) is True
    assert "src/pkg/__init__.py" in dirty_paths(git_repo)

    # An untracked file also makes the tree dirty: without a clean baseline
    # the diff audit is fiction.
    _commit_all(git_repo, "settle")
    assert is_dirty(git_repo) is False

    (git_repo / "stray.txt").write_text("stray\n", encoding="utf-8")
    assert is_dirty(git_repo) is True
    assert "stray.txt" in dirty_paths(git_repo)


def test_head_commit_is_the_real_sha(git_repo: Path) -> None:
    expected = _git(git_repo, "rev-parse", "HEAD").strip()
    assert head_commit(git_repo) == expected
    assert len(head_commit(git_repo)) == 40


# ---------------------------------------------------------------------------
# AC-5: history is never mutated.
# ---------------------------------------------------------------------------


def test_history_never_mutated(git_repo: Path) -> None:
    """G6: redgear reads git state; the human commits.

    Exercises every read path against a dirty tree and asserts the commit
    graph is byte-identical afterwards.
    """
    base = _git(git_repo, "rev-parse", "HEAD").strip()
    log_before = _git(git_repo, "log", "--format=%H %T %P %s")
    reflog_before = _git(git_repo, "reflog", "--format=%H %gs")
    branch_before = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    # Make the tree dirty in every way, then run every read path over it.
    (git_repo / "src" / "pkg" / "__init__.py").write_text("m = 1\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "new.py").write_text("n = 2\n", encoding="utf-8")
    (git_repo / "tests" / "test_pkg.py").unlink()

    changed_files(git_repo, base)
    diff_patch(git_repo, base)
    changed_lines_from_patch(diff_patch(git_repo, base))
    tracked_and_untracked(git_repo)
    is_dirty(git_repo)
    dirty_paths(git_repo)
    head_commit(git_repo)

    assert _git(git_repo, "log", "--format=%H %T %P %s") == log_before, "history changed"
    assert _git(git_repo, "reflog", "--format=%H %gs") == reflog_before, "reflog changed"
    assert _git(git_repo, "rev-parse", "HEAD").strip() == base, "HEAD moved"
    assert _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch_before

    # The working tree still holds the uncommitted edits -- nothing was
    # stashed, reset or checked out from under the user.
    assert (git_repo / "src" / "pkg" / "__init__.py").read_text(encoding="utf-8") == "m = 1\n"
    assert (git_repo / "src" / "pkg" / "new.py").exists()
    assert not (git_repo / "tests" / "test_pkg.py").exists()


def test_gitctx_module_issues_no_write_commands() -> None:
    """Structural guard: the only index-touching command permitted is
    ``git add -A -N`` (section 7.4). Anything that writes history must not
    appear in the source at all."""
    import inspect

    from redgear import gitctx

    source = inspect.getsource(gitctx)
    for forbidden in ('"commit"', '"rebase"', '"reset"', '"checkout"', '"merge"', '"push"'):
        assert forbidden not in source, f"gitctx references a history-mutating command: {forbidden}"


def test_not_a_repo_is_reported(tmp_path: Path) -> None:
    """Section 8.4 refuses to run outside a git repository."""
    from redgear.errors import NotAGitRepoError

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(NotAGitRepoError) as excinfo:
        head_commit(plain)
    assert excinfo.value.code == "E_NOT_A_REPO"
