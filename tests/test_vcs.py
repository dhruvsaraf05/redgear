"""Tests for ``redgear.vcs`` -- the only module that mutates git.

This module performs the one destructive operation in the whole engine, so
the failure paths are written first and are the substance of the file. The
happy path (a commit lands) is nearly trivial; what matters is that the
revert cannot reach past the turn it is undoing.

Three properties carry real weight and each has a test that would fail
loudly if it regressed:

* ``.redgear/`` survives a revert. Reverting the event log mid-run would
  destroy the audit trail redgear exists to produce.
* Ignored files survive a revert. ``git clean -fd`` without ``-x``; a
  virtualenv is not the agent's debris.
* An intent-to-add file is actually removed. ``gitctx`` stages untracked
  files with ``git add -A -N`` during verification, which hides them from
  ``git clean`` -- the unstage has to come first, and reversing the two
  commands silently defeats the whole revert.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from redgear import vcs
from redgear.errors import CommitFailedError
from redgear.schemas import (
    AcceptanceCriterion,
    GateName,
    GateResult,
    GateStatus,
    Proof,
    Scope,
    TaskNode,
    Verdict,
    VerifiedBy,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def _task(task_id: str = "T-0002", task_type: str = "implementation") -> TaskNode:
    return TaskNode.model_validate(
        {
            "id": task_id,
            "type": task_type,
            "title": "implement retry backoff",
            "state": "claimed",
            "spec_refs": ["FR-1"],
            "spec_hash": "sha256:97ee71867c3867b80290dfd89c89d4c1dcb8843a8271ba4052b00c60e61ab0c6",
            "depends_on": [],
            "scope": Scope(
                writable_globs=["src/**"],
                creatable_globs=["src/**"],
                frozen_globs=["tests/**"],
            ),
            "acceptance_criteria": [
                AcceptanceCriterion(
                    id="AC-1",
                    statement="it works",
                    verified_by=VerifiedBy(kind="test", selector="tests/test_pkg.py"),
                )
            ]
            if task_type != "implementation"
            else [],
            "inherits_criteria_from": ["T-0001"] if task_type == "implementation" else [],
            "attempts": 1,
            "max_attempts": 3,
            "claim": None,
            "prior_attempts": [],
            "verified_at": None,
            "proof_id": None,
            "escalation": None,
        }
    )


def _proof(*, skipped: GateName | None = None) -> Proof:
    gates = []
    for name in GateName:
        status = GateStatus.SKIPPED if name is skipped else GateStatus.PASSED
        gates.append(GateResult(name=name, status=status, reasons=[]))
    return Proof(
        task_id="T-0002",
        attempt=2,
        verdict=Verdict.PASS,
        gates=gates,
        computed_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# The revert: bounded, and bounded in the ways that matter.
# ---------------------------------------------------------------------------


def test_revert_restores_tracked_and_removes_untracked(git_repo: Path) -> None:
    """The base case: undo the turn."""
    (git_repo / "src" / "pkg" / "__init__.py").write_text("broken = 1\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "stray.py").write_text("debris = 2\n", encoding="utf-8")

    result = vcs.revert_working_tree(git_repo)

    assert (git_repo / "src" / "pkg" / "__init__.py").read_text(encoding="utf-8") == ""
    assert not (git_repo / "src" / "pkg" / "stray.py").exists()
    assert result.restored_to == _git(git_repo, "rev-parse", "HEAD").strip()
    assert set(result.paths) == {"src/pkg/__init__.py", "src/pkg/stray.py"}


def test_revert_never_touches_the_state_directory(git_repo: Path) -> None:
    """The most dangerous mistake available in this module. `.redgear/` holds
    the event log; reverting it mid-run would destroy the audit trail."""
    state = git_repo / ".redgear"
    (state / "runs").mkdir(parents=True)
    (state / "events.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")
    (state / "runs" / "prompt.txt").write_text("dispatched\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "stray.py").write_text("debris\n", encoding="utf-8")

    result = vcs.revert_working_tree(git_repo)

    assert (state / "events.jsonl").read_text(encoding="utf-8") == '{"seq": 0}\n'
    assert (state / "runs" / "prompt.txt").exists()
    assert not (git_repo / "src" / "pkg" / "stray.py").exists()
    assert all(not path.startswith(".redgear") for path in result.paths)


def test_revert_leaves_ignored_files_alone(git_repo: Path) -> None:
    """No `-x`. A virtualenv, a build directory and __pycache__ are not the
    agent's debris, and deleting a user's .venv would be unforgivable."""
    (git_repo / ".gitignore").write_text(".venv/\n*.log\n", encoding="utf-8")
    _git(git_repo, "add", ".gitignore")
    _git(git_repo, "commit", "-m", "ignore rules")

    venv = git_repo / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = somewhere\n", encoding="utf-8")
    (git_repo / "build.log").write_text("noise\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "stray.py").write_text("debris\n", encoding="utf-8")

    vcs.revert_working_tree(git_repo)

    assert (venv / "pyvenv.cfg").exists(), "the revert deleted an ignored directory"
    assert (git_repo / "build.log").exists()
    assert not (git_repo / "src" / "pkg" / "stray.py").exists()


def test_revert_removes_an_intent_to_add_file(git_repo: Path) -> None:
    """The ordering trap, pinned.

    ``gitctx`` runs ``git add -A -N`` during verification, which puts an
    untracked file INTO the index. ``git clean`` only removes untracked
    files, so it will not touch one -- the unstage has to happen first.
    Reverse the two commands in ``revert_working_tree`` and this test fails
    while every other test in this file still passes.
    """
    (git_repo / "src" / "pkg" / "stray.py").write_text("debris\n", encoding="utf-8")
    _git(git_repo, "add", "-A", "-N")
    # `git ls-files`, not `git diff --cached`: an intent-to-add entry is IN
    # the index but is deliberately absent from the cached diff, which is
    # part of why it is easy to miss that `git clean` cannot see it either.
    assert "src/pkg/stray.py" in _git(git_repo, "ls-files", "--", "src/pkg/stray.py")

    vcs.revert_working_tree(git_repo)

    assert not (git_repo / "src" / "pkg" / "stray.py").exists(), (
        "an intent-to-add file survived the revert; the unstage must precede the clean"
    )
    assert _git(git_repo, "status", "--porcelain").strip() == ""


def test_revert_does_not_move_head_or_rewrite_history(git_repo: Path) -> None:
    """G6 as amended: the revert restores a tree, it does not touch history."""
    head_before = _git(git_repo, "rev-parse", "HEAD").strip()
    log_before = _git(git_repo, "log", "--format=%H %T %P %s")

    (git_repo / "src" / "pkg" / "__init__.py").write_text("changed\n", encoding="utf-8")
    vcs.revert_working_tree(git_repo)

    assert _git(git_repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(git_repo, "log", "--format=%H %T %P %s") == log_before


def test_revert_of_a_clean_tree_is_a_no_op(git_repo: Path) -> None:
    """Reached whenever an attempt fails a gate without writing anything."""
    head_before = _git(git_repo, "rev-parse", "HEAD").strip()
    result = vcs.revert_working_tree(git_repo)
    assert result.paths == []
    assert _git(git_repo, "rev-parse", "HEAD").strip() == head_before


# ---------------------------------------------------------------------------
# The commit.
# ---------------------------------------------------------------------------


def test_commit_advances_head_and_cleans_the_tree(git_repo: Path) -> None:
    """The whole reason this module exists: HEAD has to move, or the next
    task's `base_commit` is the same stale baseline that made a real
    two-task run impossible."""
    before = _git(git_repo, "rev-parse", "HEAD").strip()
    (git_repo / "src" / "pkg" / "__init__.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )

    result = vcs.commit_verified_task(
        git_repo,
        task=_task(),
        attempt=2,
        run_id="run_20260822T004524033401",
        proof=_proof(),
        proof_dir=".redgear/runs/run_20260822T004524033401/iterations/0002/proof",
    )

    assert result is not None
    assert result.sha != before
    assert result.sha == _git(git_repo, "rev-parse", "HEAD").strip()
    assert _git(git_repo, "status", "--porcelain").strip() == ""
    assert "src/pkg/__init__.py" in result.files


def test_commit_excludes_the_lock_and_the_stop_sentinel(git_repo: Path) -> None:
    """A committed lock file makes every later checkout look like it has a
    stale one; STOP is a brake, not a record. Everything else under
    `.redgear/` IS committed -- the proof and the log are the point."""
    state = git_repo / ".redgear" / "locks"
    state.mkdir(parents=True)
    (state / "run.lock").write_text("held\n", encoding="utf-8")
    (git_repo / ".redgear" / "STOP").write_text("", encoding="utf-8")
    (git_repo / ".redgear" / "events.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")
    (git_repo / "src" / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    result = vcs.commit_verified_task(
        git_repo,
        task=_task(),
        attempt=1,
        run_id="run_x",
        proof=_proof(),
        proof_dir=".redgear/runs/run_x/iterations/0000/proof",
    )

    assert result is not None
    assert ".redgear/locks/run.lock" not in result.files
    assert ".redgear/STOP" not in result.files
    assert ".redgear/events.jsonl" in result.files, "the event log must be committed with the work"


def test_commit_uses_the_repository_identity_and_no_trailer_signature(git_repo: Path) -> None:
    """The human's git identity is correct: it is their repository and they
    are accountable for it. No signature, no Co-Authored-By."""
    (git_repo / "src" / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    vcs.commit_verified_task(
        git_repo,
        task=_task(),
        attempt=1,
        run_id="run_x",
        proof=_proof(),
        proof_dir="proof",
    )

    assert _git(git_repo, "log", "-1", "--format=%an").strip() == "redgear fixture"
    body = _git(git_repo, "log", "-1", "--format=%B")
    assert "Co-Authored-By" not in body
    assert "Signed-off-by" not in body


def test_commit_failure_is_structured_not_a_traceback(git_repo: Path) -> None:
    """Section 11.2 rule 4. A broken git identity is the failure a first-time
    user actually hits, and it must name itself rather than surface a
    CalledProcessError traceback.

    The identity is set to empty rather than *unset*: unsetting the local
    value falls through to the machine's global config, which on a developer
    workstation is set, so the commit would succeed and the test would prove
    nothing.
    """
    _git(git_repo, "config", "user.email", "")
    _git(git_repo, "config", "user.name", "")
    (git_repo / "src" / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(CommitFailedError) as excinfo:
        vcs.commit_verified_task(
            git_repo,
            task=_task(),
            attempt=1,
            run_id="run_x",
            proof=_proof(),
            proof_dir="proof",
        )
    assert excinfo.value.code == "E_COMMIT_FAILED"
    assert excinfo.value.detail["exit_code"]


def test_commit_bypasses_a_target_repo_hook(git_repo: Path) -> None:
    """`--no-verify`, and this is the test that documents why.

    A pre-commit hook that reformats would mutate the tree AFTER the proof
    was computed, so the commit would carry content no gate ever saw -- G1
    violated by accident. And redgear has already run the repository's own
    configured lint and test commands as gates 3 and 4, so a hook re-running
    them can only deadlock the loop against a check that already passed.

    This is stated in CLAUDE.md section 8.4 and in the README because
    "redgear bypasses your hooks" has to be something a user reads rather
    than discovers.
    """
    hooks = git_repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook refused'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(git_repo, "config", "core.hooksPath", ".githooks")

    (git_repo / "src" / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    result = vcs.commit_verified_task(
        git_repo,
        task=_task(),
        attempt=1,
        run_id="run_x",
        proof=_proof(),
        proof_dir="proof",
    )

    assert result is not None, "a target-repo pre-commit hook blocked a verified commit"
    assert _git(git_repo, "status", "--porcelain").strip() == ""


# ---------------------------------------------------------------------------
# The commit message.
# ---------------------------------------------------------------------------


def test_commit_message_is_structured_and_self_describing() -> None:
    message = vcs.build_commit_message(
        _task(),
        attempt=2,
        run_id="run_20260822T004524033401",
        proof=_proof(),
        proof_dir=".redgear/runs/run_20260822T004524033401/iterations/0002/proof",
    )

    lines = message.splitlines()
    assert lines[0] == "T-0002: implement retry backoff"
    assert lines[1] == ""
    assert lines[2] == "Verified by redgear. Attempt 2 of 3."
    assert "task: T-0002" in lines
    assert "type: implementation" in lines
    assert "run: run_20260822T004524033401" in lines
    assert ".redgear/runs/run_20260822T004524033401/iterations/0002/proof" in message
    assert "spec: sha256:97ee7186" in message
    assert "gates: scope_check frozen_hash_check lint" in message


def test_commit_message_states_a_skipped_gate_rather_than_padding_it() -> None:
    """A gate that did not apply did not pass. Listing it under `gates:`
    would be a small lie in the one artifact meant to outlive the tooling --
    and it is exactly the lie `task_verified.gates_passed` currently tells,
    which is why this message is built from the Proof instead."""
    message = vcs.build_commit_message(
        _task(task_type="test_authoring"),
        attempt=1,
        run_id="run_x",
        proof=_proof(skipped=GateName.COVERAGE_DELTA),
        proof_dir="proof",
    )

    assert "skipped: coverage_delta (not_applicable)" in message
    gates_line = next(line for line in message.splitlines() if line.startswith("gates: "))
    assert "coverage_delta" not in gates_line


def test_long_titles_do_not_produce_an_unusable_subject() -> None:
    task = _task().model_copy(update={"title": "implement " + "very " * 40 + "long thing"})
    subject = vcs.build_commit_message(
        task, attempt=1, run_id="r", proof=_proof(), proof_dir="p"
    ).splitlines()[0]
    assert len(subject) <= 72
    assert subject.startswith("T-0002: implement very")


def test_a_newline_in_a_title_cannot_break_the_trailer_block() -> None:
    """A subject carrying a newline would push the trailers into the body and
    silently lose the structure."""
    task = _task().model_copy(update={"title": "first line\nsecond line"})
    message = vcs.build_commit_message(task, attempt=1, run_id="r", proof=_proof(), proof_dir="p")
    assert message.splitlines()[0] == "T-0002: first line second line"


# ---------------------------------------------------------------------------
# The precondition the revert depends on.
# ---------------------------------------------------------------------------


def test_unexpected_dirt_ignores_the_state_directory(git_repo: Path) -> None:
    """redgear's own bookkeeping is neither user work nor agent work."""
    (git_repo / ".redgear").mkdir()
    (git_repo / ".redgear" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert vcs.unexpected_dirt(git_repo) == []

    (git_repo / "src" / "pkg" / "__init__.py").write_text("dirty\n", encoding="utf-8")
    assert vcs.unexpected_dirt(git_repo) == ["src/pkg/__init__.py"]
