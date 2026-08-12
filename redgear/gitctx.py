"""Read-only git interrogation.

Every verification gate is built on this. ``gitctx`` **never mutates the
repository** (G6): it reads state, and the human commits. The one
index-touching call permitted is ``git add -A -N`` (intent-to-add), justified
in section 7.4 -- it is reversible, creates no commit, and is the only way an
untracked file appears in a diff with its line content.

Two details are load-bearing and easy to get subtly wrong:

* **Intent-to-add.** ``git diff`` alone does not show untracked files. Without
  ``-N`` a brand-new file inside a frozen glob is invisible to the diff, and
  the G2 frozen-hash gate silently passes work it should have caught.
* **``--unified=0``.** With the default three lines of context, an edit to
  line 10 reports lines 7-13 as changed. Fed into the coverage-delta
  denominator (section 7.5) that turns a real coverage regression into a
  passing gate.

Paths are POSIX-relative on every platform, so a set computed on Windows
compares equal to one computed on Linux for the same tree.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from redgear.errors import NotAGitRepoError

#: A zero-context hunk header. The count is optional -- git omits it when it
#: is 1, and a parser that assumes the comma form silently drops every
#: single-line change.
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class ChangedFile:
    """One entry from the changed set.

    ``status`` is git's name-status letter: M(odified), A(dded), D(eleted).
    Renames are disabled at the call site (``--no-renames``) so a rename
    presents as a delete plus an add, which is what the scope gate needs to
    reason about -- a "rename" into a frozen glob is a new file there.
    """

    path: str
    status: str


def _run_git(repo_root: Path, *args: str) -> str:
    """Run one git command. ``shell=False``, fixed argv (section 11.1 rule 1)."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - resolved via PATH, fixed argv
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise NotAGitRepoError(
            "git is not available on PATH",
            detail={"repo_root": str(repo_root)},
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "not a git repository" in stderr.lower():
            raise NotAGitRepoError(
                f"{repo_root} is not a git repository",
                detail={"repo_root": str(repo_root), "stderr": stderr},
            ) from exc
        raise
    return result.stdout


#: Cache of directories already confirmed to be a repository root, so the
#: check costs one extra git call per repository per process rather than one
#: per command.
_VERIFIED_ROOTS: set[Path] = set()


def _git(repo_root: Path, *args: str) -> str:
    """Run a git command, but only if ``repo_root`` IS a repository root.

    Git resolves upward: run in a directory that merely *sits inside* a
    repository -- or inside one several levels up, which the system temp
    directory can be -- and every command silently succeeds against that
    outer repository. redgear would then diff, hash and report against a
    tree the user never pointed it at.

    Section 8.4 says refuse to start outside a git repository. "Outside"
    has to mean "not this repository's root", or the refusal is not a
    safety property at all.
    """
    resolved = repo_root.resolve()
    if resolved not in _VERIFIED_ROOTS:
        toplevel = _run_git(repo_root, "rev-parse", "--show-toplevel").strip()
        if not toplevel or Path(toplevel).resolve() != resolved:
            raise NotAGitRepoError(
                f"{repo_root} is not the root of a git repository",
                detail={
                    "repo_root": str(repo_root),
                    "enclosing_repository": toplevel or None,
                },
            )
        _VERIFIED_ROOTS.add(resolved)
    return _run_git(repo_root, *args)


def head_commit(repo_root: Path) -> str:
    """The current HEAD sha. The baseline every diff is taken against."""
    return _git(repo_root, "rev-parse", "HEAD").strip()


def _intent_to_add(repo_root: Path) -> None:
    """Make untracked files visible to ``git diff``.

    The ONLY index-touching call redgear makes (section 7.4). It records an
    intent to add, never content, so it creates no commit and is undone by
    ``git reset``. Without it the changed set is blind to new files, which is
    precisely the case G2 exists to catch.
    """
    _git(repo_root, "add", "-A", "-N")


def tracked_and_untracked(repo_root: Path) -> list[str]:
    """Every file in the working set: tracked plus untracked-not-ignored.

    This is what frozen globs are expanded against. Ignored files are
    excluded -- they are not part of the work and hashing them would make the
    frozen map depend on local build artifacts.
    """
    listing = _git(repo_root, "ls-files", "--cached", "--others", "--exclude-standard")
    return sorted({line.strip() for line in listing.splitlines() if line.strip()})


def changed_files(repo_root: Path, base_commit: str) -> list[ChangedFile]:
    """The real changed set since ``base_commit``.

    Computed from git, never from anything the agent claimed (G1). Untracked
    files are included via intent-to-add and report as additions.
    """
    _intent_to_add(repo_root)
    raw = _git(repo_root, "diff", "--name-status", "--no-renames", base_commit, "--")

    entries: list[ChangedFile] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        if not path:
            continue
        entries.append(ChangedFile(path=path.strip().replace("\\", "/"), status=status.strip()[:1]))
    return sorted(entries, key=lambda entry: entry.path)


def diff_patch(repo_root: Path, base_commit: str) -> str:
    """The unified diff since ``base_commit``, with **zero** context.

    ``--unified=0`` is mandatory: context lines would be counted as changed
    and inflate the coverage denominator. ``--no-color`` keeps ANSI escapes
    out of anything that later reaches a prompt (G7).
    """
    _intent_to_add(repo_root)
    return _git(
        repo_root,
        "diff",
        "--unified=0",
        "--no-color",
        "--no-renames",
        base_commit,
        "--",
    )


def changed_lines_from_patch(patch: str) -> dict[str, set[int]]:
    """Line numbers touched in the NEW revision, per file.

    New-file numbering is what coverage data is keyed by, so this is what the
    coverage-delta gate can intersect. A deleted file yields an empty set
    rather than being absent, so a caller can tell "no lines" from "not in
    the diff" -- a deletion must not be scored as uncovered lines.
    """
    result: dict[str, set[int]] = {}
    current: str | None = None

    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                # The file was deleted; keep `current` pointing at the old
                # name so its (empty) entry is still recorded below.
                continue
            current = target[2:] if target.startswith("b/") else target
            current = current.replace("\\", "/")
            result.setdefault(current, set())
        elif line.startswith("--- ") and line[4:].strip() != "/dev/null":
            # Remember the old name so a deletion still gets an entry.
            old = line[4:].strip()
            old = old[2:] if old.startswith("a/") else old
            result.setdefault(old.replace("\\", "/"), set())
        elif line.startswith("@@") and current is not None:
            match = _HUNK.match(line)
            if match is None:
                continue
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            result[current].update(range(start, start + count))

    return result


def dirty_paths(repo_root: Path) -> list[str]:
    """Paths making the working tree dirty, staged or not, including untracked.

    Section 8.4 refuses to start on a dirty tree; that refusal is only
    actionable if it names the files.
    """
    raw = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    paths: set[str] = set()
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # `git status` renders a rename as "old -> new"; the new name is what
        # exists on disk.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path.replace("\\", "/"))
    return sorted(paths)


def is_dirty(repo_root: Path) -> bool:
    """True if anything is uncommitted. Without a clean baseline the diff
    audit is fiction (section 8.4)."""
    return bool(dirty_paths(repo_root))
