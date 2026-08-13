"""T-0022: failing tests for verifier gate 2 -- frozen_hash_check.

``redgear/verifier.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

This gate is the mechanical heart of G2. It is the reason an implementation
agent cannot weaken a failing assertion to make the suite green: the test file
was SHA-256'd at claim time, and any difference at verification time fails
before lint or tests are even reached.

Three violation kinds, distinct because the correct agent response differs
for each (section 7.2):

* ``frozen_file_modified`` -- content changed.
* ``frozen_file_deleted`` -- the file is gone. Deleting a failing test is the
  crudest way to make a suite green.
* ``frozen_file_added`` -- a NEW file appeared inside a frozen glob. This is
  the one a naive implementation misses: an untracked file is invisible to
  ``git diff`` and absent from the recorded digest map, so a check that only
  re-hashes the recorded paths walks straight past it.

Real repository, real files, real digests -- section 10.4. Nothing mocked.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from redgear.hashing import digest_map
from redgear.paths import match_glob
from redgear.schemas import Claim, GateName, GateStatus, TaskNode
from redgear.verifier import GATE_ORDER, frozen_hash_check, run_gates


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _task(*, writable: list[str], frozen: list[str]) -> TaskNode:
    payload: dict[str, Any] = {
        "id": "T-0099",
        "type": "implementation",
        "title": "the task under verification",
        "state": "claimed",
        "spec_refs": ["FR-1"],
        "spec_hash": "sha256:" + "0" * 64,
        "depends_on": [],
        "scope": {
            "writable_globs": writable,
            "creatable_globs": writable,
            "frozen_globs": frozen,
        },
        "acceptance_criteria": [],
        "inherits_criteria_from": ["T-0098"],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }
    return TaskNode.model_validate(payload)


def _claim_now(repo: Path, frozen_globs: list[str]) -> Claim:
    """Snapshot the frozen set exactly as state_engine.claim_task does.

    Built from the same primitives the write path uses -- ``git ls-files``
    over tracked *and* untracked files, then ``hashing.digest_map`` -- so
    that what this gate compares against is genuinely what claim time would
    have recorded.
    """
    listing = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard")
    files = [line.strip() for line in listing.splitlines() if line.strip()]
    matched = sorted(p for p in files if any(match_glob(p, g) for g in frozen_globs))
    return Claim(
        base_commit=_git(repo, "rev-parse", "HEAD").strip(),
        frozen_hashes=digest_map(repo, matched),
        allowed_tools=["Read", "Edit", "Write"],
        claimed_at=datetime.now(tz=UTC),
    )


def _kinds(result: Any) -> set[str]:
    return {reason.split(":", 1)[0].strip() for reason in result.reasons}


def _paths(result: Any, kind: str) -> set[str]:
    return {
        reason.split(":", 1)[1].strip()
        for reason in result.reasons
        if reason.split(":", 1)[0].strip() == kind
    }


# ---------------------------------------------------------------------------
# AC-4: modification, deletion and addition each fail distinctly.
# ---------------------------------------------------------------------------


def test_all_frozen_violation_kinds(git_repo: Path) -> None:
    """Each kind is reported under its own name, because the correct agent
    response differs: restore the content, restore the file, or delete the
    one it should never have created."""
    frozen = ["tests/**"]

    # --- baseline: an untouched frozen set passes ---
    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)
    clean = frozen_hash_check(git_repo, task=task, claim=claim)
    assert clean.name == GateName.FROZEN_HASH_CHECK
    assert clean.status == GateStatus.PASSED
    assert clean.reasons == []

    # --- modified ---
    (git_repo / "tests" / "test_pkg.py").write_text("assert False\n", encoding="utf-8")
    modified = frozen_hash_check(git_repo, task=task, claim=claim)
    assert modified.status == GateStatus.FAILED
    assert _kinds(modified) == {"frozen_file_modified"}
    assert _paths(modified, "frozen_file_modified") == {"tests/test_pkg.py"}

    # --- deleted ---
    (git_repo / "tests" / "test_pkg.py").unlink()
    deleted = frozen_hash_check(git_repo, task=task, claim=claim)
    assert deleted.status == GateStatus.FAILED
    assert _kinds(deleted) == {"frozen_file_deleted"}
    assert _paths(deleted, "frozen_file_deleted") == {"tests/test_pkg.py"}

    # --- added ---
    # Restore the file first, then re-snapshot, so this case isolates the
    # addition rather than compounding the deletion above.
    (git_repo / "tests" / "test_pkg.py").write_bytes(
        b"def test_placeholder() -> None:\n    assert True\n"
    )
    restored = _claim_now(git_repo, frozen)
    (git_repo / "tests" / "test_smuggled.py").write_text("assert True\n", encoding="utf-8")
    added = frozen_hash_check(git_repo, task=task, claim=restored)
    assert added.status == GateStatus.FAILED
    assert _kinds(added) == {"frozen_file_added"}
    assert _paths(added, "frozen_file_added") == {"tests/test_smuggled.py"}


def test_added_file_is_caught_even_though_untracked(git_repo: Path) -> None:
    """The failure mode this gate exists to catch, stated plainly.

    A brand-new file is untracked, so it is absent from the recorded digest
    map. An implementation that only re-hashes the recorded paths finds every
    one of them unchanged and passes -- while a new test file sits inside the
    frozen glob. Section 7.2 is explicit: re-expand the globs against tracked
    PLUS untracked files.
    """
    frozen = ["tests/**"]
    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)

    new_file = git_repo / "tests" / "test_agent_wrote_this.py"
    new_file.write_text("def test_always_passes():\n    assert True\n", encoding="utf-8")

    # It really is untracked -- that is the whole point.
    status = _git(git_repo, "status", "--porcelain")
    assert "test_agent_wrote_this.py" in status
    assert "??" in status

    # Every recorded digest is still correct...
    for path, recorded in claim.frozen_hashes.items():
        actual = "sha256:" + hashlib.sha256((git_repo / path).read_bytes()).hexdigest()
        assert actual == recorded

    # ...and the gate still fails, because the glob was re-expanded.
    result = frozen_hash_check(git_repo, task=task, claim=claim)
    assert result.status == GateStatus.FAILED
    assert _paths(result, "frozen_file_added") == {"tests/test_agent_wrote_this.py"}


def test_edit_outside_the_frozen_glob_is_ignored(git_repo: Path) -> None:
    """The gate is about the frozen set only. A legitimate edit inside the
    writable scope must not trip it, or every passing task would fail."""
    frozen = ["tests/**"]
    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)

    (git_repo / "src" / "pkg" / "__init__.py").write_text("edited = True\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "brand_new.py").write_text("new = True\n", encoding="utf-8")

    result = frozen_hash_check(git_repo, task=task, claim=claim)
    assert result.status == GateStatus.PASSED
    assert result.reasons == []


def test_empty_frozen_globs_passes(git_repo: Path) -> None:
    """A scaffold task typically freezes nothing (section 4.5). Freezing
    nothing must pass, not fail vacuously."""
    task = _task(writable=["**"], frozen=[])
    claim = _claim_now(git_repo, [])
    (git_repo / "anything.txt").write_text("whatever\n", encoding="utf-8")

    result = frozen_hash_check(git_repo, task=task, claim=claim)
    assert result.status == GateStatus.PASSED


# ---------------------------------------------------------------------------
# AC-5: every violation reported, not only the first.
# ---------------------------------------------------------------------------


def test_all_violations_reported(git_repo: Path) -> None:
    """Section 7.2: "Report every violation, not just the first."

    All three kinds at once, several of each. An agent shown one violation
    per attempt spends its whole budget discovering a list it could have been
    handed in one go.
    """
    frozen = ["tests/**"]

    # Widen the frozen set so there is enough to violate.
    for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py"):
        (git_repo / "tests" / name).write_text(f"# {name}\nassert True\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "more tests")

    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)
    assert len(claim.frozen_hashes) == 5  # test_pkg.py + the four above

    # Two modified, two deleted, two added.
    (git_repo / "tests" / "test_a.py").write_text("# tampered\n", encoding="utf-8")
    (git_repo / "tests" / "test_b.py").write_text("# tampered\n", encoding="utf-8")
    (git_repo / "tests" / "test_c.py").unlink()
    (git_repo / "tests" / "test_d.py").unlink()
    (git_repo / "tests" / "test_new_1.py").write_text("assert True\n", encoding="utf-8")
    (git_repo / "tests" / "test_new_2.py").write_text("assert True\n", encoding="utf-8")

    result = frozen_hash_check(git_repo, task=task, claim=claim)

    assert result.status == GateStatus.FAILED
    assert _kinds(result) == {
        "frozen_file_modified",
        "frozen_file_deleted",
        "frozen_file_added",
    }
    assert _paths(result, "frozen_file_modified") == {"tests/test_a.py", "tests/test_b.py"}
    assert _paths(result, "frozen_file_deleted") == {"tests/test_c.py", "tests/test_d.py"}
    assert _paths(result, "frozen_file_added") == {"tests/test_new_1.py", "tests/test_new_2.py"}
    assert len(result.reasons) == 6, f"expected all six violations, got {result.reasons}"

    # Reported in a stable order, so two runs of the same failure produce the
    # same prompt (the prompt engine snapshots these).
    assert result.reasons == frozen_hash_check(git_repo, task=task, claim=claim).reasons


def test_deleted_frozen_file_is_reported_not_crashed(git_repo: Path) -> None:
    """Regression: deleting a frozen file must REPORT, never raise.

    `git ls-files --cached` still lists a tracked file after it has been
    deleted from the working tree. Feeding that expansion straight into
    ``digest_map`` raised ``FileNotFoundError`` -- so gate 2 crashed on the
    single most likely way an agent fakes a green suite: deleting the test
    that fails.

    A crash here is worse than a miss. The orchestrator would surface an
    engine error rather than a gate verdict, the agent would get no
    corrective excerpt naming the deleted file, and the deletion itself would
    go unreported. Named explicitly so the fix cannot silently regress.
    """
    frozen = ["tests/**"]
    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)

    target = git_repo / "tests" / "test_pkg.py"
    assert "tests/test_pkg.py" in claim.frozen_hashes
    target.unlink()

    # The file is gone from disk but still tracked -- the exact shape that
    # used to crash.
    assert not target.exists()
    assert "tests/test_pkg.py" in _git(git_repo, "ls-files", "--cached")

    result = frozen_hash_check(git_repo, task=task, claim=claim)

    assert result.status == GateStatus.FAILED
    assert _paths(result, "frozen_file_deleted") == {"tests/test_pkg.py"}
    # Reported as a deletion specifically -- not as a modification, because
    # the correct agent response differs (restore the file, not its content).
    assert _kinds(result) == {"frozen_file_deleted"}

    # And it is reachable through the full pipeline, not just the gate
    # function, since that is how the orchestrator will call it.
    proof = run_gates(git_repo, task=task, claim=claim, declared=[], attempt=1)
    by_name = {gate.name: gate for gate in proof.gates}
    assert by_name[GateName.SCOPE_CHECK].status == GateStatus.FAILED
    assert by_name[GateName.FROZEN_HASH_CHECK].status == GateStatus.SKIPPED


def test_digest_comparison_is_byte_exact_not_line_ending_normalised(git_repo: Path) -> None:
    """A CRLF/LF difference is a real content change and must be caught.

    If digests were computed in text mode, Python's universal-newline
    translation would make a CRLF file and an LF file hash identically -- so
    an agent could rewrite a frozen test's line endings, or a Windows tool
    could do it silently, and the gate would see nothing. It would also make
    a digest taken on Windows disagree with the same file on Linux, failing
    the gate for every Linux user for no reason.
    """
    frozen = ["tests/**"]
    target = git_repo / "tests" / "test_pkg.py"
    target.write_bytes(b"def test_x():\n    assert True\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "lf version")

    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)
    assert frozen_hash_check(git_repo, task=task, claim=claim).status == GateStatus.PASSED

    # Same logical content, CRLF line endings.
    target.write_bytes(b"def test_x():\r\n    assert True\r\n")

    result = frozen_hash_check(git_repo, task=task, claim=claim)
    assert result.status == GateStatus.FAILED, (
        "a CRLF rewrite of a frozen file was not detected -- digests are "
        "being computed in text mode"
    )
    assert _paths(result, "frozen_file_modified") == {"tests/test_pkg.py"}


def test_modifying_a_frozen_file_fails_gate_1_and_skips_gate_2(git_repo: Path) -> None:
    """Gate 2 is defence in depth, NOT a second chance at gate 1.

    For a validly-scoped task, touching a frozen path can never reach gate 2:

    * gate 1 checks every changed path against ``writable_globs`` (a
      modification is checked against writable; ``creatable`` governs
      additions);
    * §4.4 invariant 7 guarantees ``frozen ∩ writable`` is empty;
    * therefore a modified frozen file is *always* also an
      ``out_of_scope_write``, gate 1 fails, and the pipeline short-circuits
      before gate 2 runs.

    This test previously asserted the opposite -- that gate 1 would pass so
    gate 2 could fail alone. That arrangement is impossible under a valid
    scope, and asserting it hid the real design: gate 2 earns its place by
    catching what gate 1's glob logic *cannot* see, namely a newly created
    file inside a frozen glob (``test_added_file_is_caught_even_though_untracked``)
    and a deleted frozen file (``test_deleted_frozen_file_is_reported_not_crashed``),
    neither of which depends on gate 1 passing.
    """
    frozen = ["tests/**"]
    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)

    # A legitimate in-scope edit...
    (git_repo / "src" / "pkg" / "__init__.py").write_text("ok = 1\n", encoding="utf-8")
    # ...and a frozen test quietly weakened alongside it.
    (git_repo / "tests" / "test_pkg.py").write_text("# deleted the assertion\n", encoding="utf-8")

    proof = run_gates(
        git_repo,
        task=task,
        claim=claim,
        declared=["src/pkg/__init__.py", "tests/test_pkg.py"],
        attempt=1,
    )

    by_name = {gate.name: gate for gate in proof.gates}

    # Gate 1 catches it, naming the frozen path as an out-of-scope write.
    scope_gate = by_name[GateName.SCOPE_CHECK]
    assert scope_gate.status == GateStatus.FAILED
    assert _paths(scope_gate, "out_of_scope_write") == {"tests/test_pkg.py"}
    # The legitimate edit is not blamed.
    assert "src/pkg/__init__.py" not in _paths(scope_gate, "out_of_scope_write")

    # Gate 2 never ran -- recorded skipped, not passed and not omitted.
    assert by_name[GateName.FROZEN_HASH_CHECK].status == GateStatus.SKIPPED
    assert by_name[GateName.FROZEN_HASH_CHECK].reasons, "a skipped gate must say why"

    # The full contracted list still appears, in order (section 7.1).
    assert [gate.name for gate in proof.gates] == list(GATE_ORDER)
    for later in GATE_ORDER[1:]:
        assert by_name[later].status == GateStatus.SKIPPED


def test_frozen_check_does_not_mutate_the_repository(git_repo: Path) -> None:
    """The gate reads. It must not commit, stage content, or move HEAD --
    G6, and the same guard gitctx carries."""
    frozen = ["tests/**"]
    claim = _claim_now(git_repo, frozen)
    task = _task(writable=["src/**"], frozen=frozen)

    head_before = _git(git_repo, "rev-parse", "HEAD").strip()
    log_before = _git(git_repo, "log", "--format=%H %T %s")

    (git_repo / "tests" / "test_pkg.py").write_text("tampered\n", encoding="utf-8")
    frozen_hash_check(git_repo, task=task, claim=claim)

    assert _git(git_repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(git_repo, "log", "--format=%H %T %s") == log_before
    # The tampering is still present -- nothing was reverted underneath the user.
    assert (git_repo / "tests" / "test_pkg.py").read_text(encoding="utf-8") == "tampered\n"
