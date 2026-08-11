"""Glob matching and `.redgear/` layout resolution.

This module decides what an agent is allowed to write (CLAUDE.md G2, section
4.4 invariant 7), so its edge cases are security-relevant rather than
cosmetic. Two rules carry that weight:

* **POSIX semantics regardless of host.** Candidate paths are normalised to
  forward slashes before matching. Development happens on Windows and CI runs
  on Linux; a matcher that agreed with a glob only on one of them would let a
  scope violation through on the other.
* **Escapes are rejected, never normalised.** An absolute path, or a path
  containing a `..` segment, is refused outright rather than resolved into
  something that happens to sit inside a writable glob. Resolving first would
  let `redgear/../../../etc/passwd` be judged against `redgear/**`.

The `..` check is exact: it compares whole path segments, so `..foo` -- a
perfectly legal filename -- is not mistaken for a traversal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from redgear.errors import ScopeOverlapError

#: A leading drive letter, e.g. `C:/Users/...`, after separator normalisation.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

#: Cache compiled patterns: the verifier matches every changed path against
#: every glob in a task's scope, so the same handful of patterns recur.
_COMPILED: dict[str, re.Pattern[str]] = {}


def _to_posix(path: str) -> str:
    """Normalise separators without resolving anything.

    Deliberately not ``Path.resolve()`` or ``os.path.normpath``: both would
    collapse `..` segments, which is precisely the escape this module must
    detect rather than silently rewrite.
    """
    return path.replace("\\", "/")


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or bool(_WINDOWS_DRIVE.match(path))


def _has_traversal(path: str) -> bool:
    """True if any whole segment is `..`. `..foo` is a filename, not a escape."""
    return any(segment == ".." for segment in path.split("/"))


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob into an anchored regex.

    `**/` spans zero or more directories, `**` spans anything, `*` and `?`
    stay inside a single segment. Everything else is matched literally.
    """
    compiled = _COMPILED.get(pattern)
    if compiled is not None:
        return compiled

    parts: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if pattern.startswith("**/", index):
            # Zero or more leading directories, so `a/**/*.py` matches both
            # `a/b.py` and `a/b/c.py`.
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif char == "*":
            parts.append("[^/]*")
            index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(char))
            index += 1

    compiled = re.compile("".join(parts) + r"\Z")
    _COMPILED[pattern] = compiled
    return compiled


def match_glob(path: str, pattern: str) -> bool:
    """True if a repo-relative path matches a glob.

    Returns False -- never raises -- for an empty pattern, an absolute path,
    or a path containing a `..` segment. A gate asking "is this write in
    scope?" wants a verdict, and the safe verdict for a malformed path is
    "not in scope".
    """
    if not pattern:
        return False

    candidate = _to_posix(path)
    if not candidate:
        return False
    if _is_absolute(candidate) or _has_traversal(candidate):
        return False

    return _glob_to_regex(_to_posix(pattern)).match(candidate) is not None


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    """True if the path matches at least one glob in the list."""
    return any(match_glob(path, pattern) for pattern in patterns)


def find_scope_overlaps(
    writable_globs: Sequence[str],
    frozen_globs: Sequence[str],
    tracked_files: Sequence[str],
) -> list[str]:
    """Every tracked file matched by both a writable and a frozen glob.

    Section 4.4 invariant 7. Overlap is specifically writable-against-frozen:
    a file matching two different writable patterns is not a violation.

    Reports every offending path, not just the first (section 7.2), and keeps
    the caller's ordering so the report is stable.
    """
    overlaps: list[str] = []
    for tracked in tracked_files:
        if matches_any(tracked, writable_globs) and matches_any(tracked, frozen_globs):
            overlaps.append(tracked)
    return overlaps


def assert_no_scope_overlap(
    writable_globs: Sequence[str],
    frozen_globs: Sequence[str],
    tracked_files: Sequence[str],
) -> None:
    """Raise ``ScopeOverlapError`` if any tracked file is both writable and frozen."""
    overlaps = find_scope_overlaps(writable_globs, frozen_globs, tracked_files)
    if overlaps:
        raise ScopeOverlapError(
            "writable and frozen globs overlap over the tracked file list",
            detail={"overlaps": list(overlaps)},
        )


# ---------------------------------------------------------------------------
# `.redgear/` layout -- CLAUDE.md section 2.3, verbatim.
# ---------------------------------------------------------------------------


def redgear_dir(repo_root: Path) -> Path:
    return repo_root / ".redgear"


def config_path(repo_root: Path) -> Path:
    return redgear_dir(repo_root) / "config.json"


def spec_path(repo_root: Path) -> Path:
    return redgear_dir(repo_root) / "spec" / "spec.json"


def task_graph_path(repo_root: Path) -> Path:
    return redgear_dir(repo_root) / "task_graph.json"


def adr_index_path(repo_root: Path) -> Path:
    return redgear_dir(repo_root) / "adrs" / "index.json"


def events_path(repo_root: Path) -> Path:
    return redgear_dir(repo_root) / "events.jsonl"


def stop_path(repo_root: Path) -> Path:
    """The G6 kill switch. Absent unless a stop has been requested."""
    return redgear_dir(repo_root) / "STOP"


def run_lock_path(repo_root: Path) -> Path:
    return redgear_dir(repo_root) / "locks" / "run.lock"


def task_lock_path(repo_root: Path, task_id: str) -> Path:
    return redgear_dir(repo_root) / "locks" / f"{task_id}.lock"
