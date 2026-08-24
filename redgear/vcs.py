"""The only module that mutates git.

``gitctx`` reads; this module writes. The split is deliberate and structural,
the same shape as "``runner.py`` is the only module that spawns the agent CLI"
and "``state_engine.py`` is the only module that writes ``.redgear/``": one
privileged writer per mutable resource, so the question "what can change this?"
has a one-file answer.

Two operations, and only two:

* **commit** one verified task's work to the *local* repository (G6). Never a
  push, a rebase, a reset, a cherry-pick, or anything that rewrites history.
  Every commit is one verified task and is revertible with one command.
* **revert** the working tree to HEAD after a failed attempt, so the retry
  starts from the same clean tree the first attempt did.

**Why redgear commits at all.** ``scope_check`` diffs the working tree against
the claim's ``base_commit``. If nothing commits between tasks, ``base_commit``
never moves, and task N+1's diff contains task N's already-verified output --
so a legitimately verified predecessor looks like an out-of-scope write to its
own dependent, permanently, whatever the second agent does. A real two-task run
could not get past this. Committing between tasks is what makes ``base_commit``
a true baseline rather than a fiction.

**Why the revert is safe.** It is bounded by a precondition, not by hope: the
tree is asserted clean (excluding ``.redgear/``) immediately before every
claim, so every non-state path dirty at rejection time was written during that
turn. The revert is therefore "undo this turn", not "undo the tree". Drop the
per-claim assertion and this module becomes genuinely dangerous.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from redgear import gitctx
from redgear.errors import CommitFailedError
from redgear.paths import STATE_DIR_NAME, is_state_path
from redgear.schemas import GateStatus, Proof, TaskNode

#: Paths never committed. Everything else under `.redgear/` IS committed on
#: purpose -- the event log and the proof are the most important thing in the
#: commit, and work committed without the evidence for it is the split-brain
#: this project exists to prevent. These two are the exceptions because
#: neither is a record:
#:
#: * `locks/` holds the run lock, which is *live* for the whole run. A
#:   committed lock file makes every later checkout look like it has a stale
#:   one.
#: * `STOP` is a sentinel, not history.
#:
#: `.redgear/.gitignore` (written by ``state_engine.scaffold``) names the same
#: two, which covers the untracked case. This list covers the one .gitignore
#: cannot: a lock file *already tracked* in the target repo, where an ignore
#: rule has no effect -- which is not hypothetical, it is the state of the
#: repository the first real run was made against.
_COMMIT_EXCLUDED: tuple[str, ...] = (
    f"{STATE_DIR_NAME}/locks",
    f"{STATE_DIR_NAME}/STOP",
)

#: The revert excludes `.redgear/` **entirely**, and this is the single most
#: dangerous mistake available in this module: reverting the state directory
#: would delete the event log mid-run and destroy the audit trail redgear
#: exists to produce.
_REVERT_EXCLUDES: tuple[str, ...] = (f":(exclude){STATE_DIR_NAME}",)

#: Keep the subject inside the conventional limit. The title is human-authored
#: and human-approved (G7 trusts it), but a plan is free to carry a long one
#: and a 200-character subject line makes `git log --oneline` useless.
_MAX_SUBJECT = 72


@dataclass(frozen=True)
class CommitResult:
    """What one ``commit_verified_task`` actually did."""

    sha: str
    subject: str
    files: list[str]


@dataclass(frozen=True)
class RevertResult:
    """What one ``revert_working_tree`` actually discarded."""

    restored_to: str
    paths: list[str]


def _git_write(repo_root: Path, *args: str) -> str:
    """Run one mutating git command. ``shell=False``, fixed argv (rule 1).

    ``check=False`` so a failure becomes a structured ``CommitFailedError``
    carrying git's own stderr rather than a ``CalledProcessError`` traceback
    escaping to a user (section 11.2 rule 4).

    ``encoding="utf-8"`` for the reason ``gitctx._run_git`` documents at
    length: git emits UTF-8 regardless of host locale, and Python's default
    text-mode decode falls back to the locale codec, which fails *inside*
    subprocess's reader thread where ``check`` cannot see it.
    """
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607 - resolved via PATH, fixed argv
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise CommitFailedError(
            f"git {args[0]} failed in {repo_root}",
            detail={
                "argv": " ".join(("git", *args)),
                "exit_code": result.returncode,
                "stderr": (result.stderr or "").strip()[:2000],
            },
        )
    return result.stdout


def _subject(task: TaskNode) -> str:
    """``<task_id>: <title>``, one line, within the conventional width.

    The title is flattened before truncation: a newline in a subject would
    turn the rest of the trailer block into the commit *body* and silently
    lose the structure below.
    """
    flattened = " ".join(task.title.split())
    subject = f"{task.id}: {flattened}"
    if len(subject) > _MAX_SUBJECT:
        subject = subject[: _MAX_SUBJECT - 3].rstrip() + "..."
    return subject


def build_commit_message(
    task: TaskNode,
    *,
    attempt: int,
    run_id: str,
    proof: Proof,
    proof_dir: str,
) -> str:
    """The message for one verified task's commit.

    Structured and self-describing: a reader who finds this commit in
    ``git log`` a year later can reach the proof, the prompt, and the exact
    spec version it was verified against, without redgear installed.

    **Derived from the ``Proof``, never from the ``task_verified`` event.**
    That event's ``gates_passed`` field is a hardcoded list of every gate name
    (see docs/PROGRESS.md section 5) and would state that all six gates passed
    even where one was skipped. The proof is what actually happened.

    No signature, no ``Co-Authored-By``, no ``--author``. The human's git
    identity is correct: it is their repository and they are accountable for
    it. redgear's contribution is recorded in the trailers, where it can be
    read as a claim about verification rather than about authorship.
    """
    passed = [gate.name.value for gate in proof.gates if gate.status is GateStatus.PASSED]
    skipped = [gate.name.value for gate in proof.gates if gate.status is GateStatus.SKIPPED]

    lines = [
        _subject(task),
        "",
        f"Verified by redgear. Attempt {attempt} of {task.max_attempts}.",
        "",
        f"task: {task.id}",
        f"type: {task.type}",
        f"run: {run_id}",
        f"proof: {proof_dir}",
        f"gates: {' '.join(passed)}",
    ]
    if skipped:
        # Stated rather than padded into `gates:`. A gate that did not apply
        # to this task type did not pass, and a commit message that says it
        # did is a small lie in the one artifact meant to outlive the tooling.
        lines.append(f"skipped: {' '.join(skipped)} (not_applicable)")
    lines.append(f"spec: {task.spec_hash}")
    return "\n".join(lines) + "\n"


def staged_paths(repo_root: Path) -> list[str]:
    """Repo-relative paths currently staged, POSIX-separated."""
    raw = _git_write(repo_root, "diff", "--cached", "--name-only")
    return sorted(
        {line.strip().replace("\\", "/") for line in raw.splitlines() if line.strip()},
    )


def is_commit_excluded(path: str) -> bool:
    """Is this repo-relative path one of the two transient control files?"""
    normalised = path.replace("\\", "/").strip()
    for excluded in _COMMIT_EXCLUDED:
        if normalised == excluded or normalised.startswith(excluded + "/"):
            return True
    return False


def commit_verified_task(
    repo_root: Path,
    *,
    task: TaskNode,
    attempt: int,
    run_id: str,
    proof: Proof,
    proof_dir: str,
) -> CommitResult | None:
    """Commit one verified task's work. ``None`` when there was nothing to commit.

    **``--no-verify`` is deliberate and is documented in CLAUDE.md section 8.4
    and in the README**, because "redgear bypasses your hooks" must be
    something a user reads rather than discovers. Two reasons, and the first
    is a correctness one:

    * A hook that reformats (``ruff format``, ``prettier``, ``black``) mutates
      the tree *after* the proof was computed. The commit would then contain
      content no gate ever saw -- G1 violated by accident, which is the worst
      way to violate it.
    * redgear has already run the target repository's own configured lint and
      test commands as gates 3 and 4. The hook is re-running what was just
      run independently, and a failure there would deadlock the loop against a
      check that already passed.

    A blanket ``git add`` is safe **here specifically** because the scope gate
    already passed: a verified task has, by construction, changed only paths
    inside its declared scope. It is the gate that makes this safe, not the
    add.
    """
    before = gitctx.head_commit(repo_root)

    # A plain add, then unstage the exceptions -- deliberately NOT
    # `:(exclude)` pathspecs. Naming a gitignored path in a pathspec makes
    # `git add` fail outright ("The following paths are ignored ... use -f"),
    # so combining the ignore rule with an exclude pathspec breaks every
    # commit. The ignore rule covers the untracked case on its own; this
    # unstage covers the already-tracked one it cannot reach.
    _git_write(repo_root, "add", "-A", "--", ".")
    excluded = [path for path in staged_paths(repo_root) if is_commit_excluded(path)]
    if excluded:
        _git_write(repo_root, "restore", "--staged", "--", *excluded)

    files = staged_paths(repo_root)
    if not files:
        # Defensive: `mark_verified` has always just written the event log and
        # the projection, so this should be unreachable. `git commit` exits
        # non-zero on an empty index, and turning that into E_COMMIT_FAILED
        # would report an environment problem that is not one.
        return None

    message = build_commit_message(
        task, attempt=attempt, run_id=run_id, proof=proof, proof_dir=proof_dir
    )
    _git_write(repo_root, "commit", "--no-verify", "-m", message)

    after = gitctx.head_commit(repo_root)
    if after == before:
        raise CommitFailedError(
            "git reported success but HEAD did not move",
            detail={"repo_root": str(repo_root), "head": after, "task_id": task.id},
        )
    return CommitResult(sha=after, subject=_subject(task), files=files)


def revert_working_tree(repo_root: Path) -> RevertResult:
    """Restore the working tree to HEAD, discarding a failed attempt's writes.

    Everything under ``.redgear/`` is excluded, and ignored files are never
    removed (no ``-x``), so a virtualenv, a build directory and ``__pycache__``
    survive untouched.

    **The two commands must run in this order, and this is a trap.**
    ``gitctx``'s changed-set computation runs ``git add -A -N`` (intent-to-add,
    section 7.4) during verification, which puts untracked files *into the
    index*. ``git clean`` removes untracked files and therefore does not touch
    them. ``git restore --staged`` clears those index entries first, returning
    the files to genuinely-untracked, and only then can ``clean`` see them.
    Reverse the order and every file the agent created survives the revert --
    silently, and exactly on the path this exists to clean up.

    ``git restore`` rather than ``git checkout --`` for two reasons: it is the
    porcelain that handles the intent-to-add case correctly (``git checkout``
    on a ``-N`` entry errors or truncates the file to empty), and it keeps
    this module free of the verbs G6 forbids.
    """
    restored_to = gitctx.head_commit(repo_root)
    # Captured before the revert, because afterwards there is nothing left to
    # ask. A destructive operation that cannot say what it destroyed is not
    # auditable.
    discarded = [path for path in gitctx.dirty_paths(repo_root) if not is_state_path(path)]

    _git_write(repo_root, "restore", "--staged", "--worktree", "--", ".", *_REVERT_EXCLUDES)
    _git_write(repo_root, "clean", "-fd", "--", ".", *_REVERT_EXCLUDES)

    return RevertResult(restored_to=restored_to, paths=discarded)


def unexpected_dirt(repo_root: Path, *, dirty: Sequence[str] | None = None) -> list[str]:
    """Working-tree paths that are dirty for reasons that are not redgear's.

    ``.redgear/`` is excluded for the reason ``paths.is_state_path`` documents:
    the run lock, the event log, the projection and the persisted prompts all
    land during a run, and none of them is user work or agent work.

    Used at run start (section 8.4) *and* before every claim. The second call
    is what licenses the revert: if the tree is clean before the turn, then
    everything dirty after it belongs to the turn.
    """
    paths = gitctx.dirty_paths(repo_root) if dirty is None else dirty
    return [path for path in paths if not is_state_path(path)]
