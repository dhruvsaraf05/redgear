"""T-0022: failing tests for verifier gate 1 -- scope_check.

``redgear/verifier.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

This is G2 made mechanical. Every other agent wrapper *asks* an agent to stay
in scope; this is what actually checks, and it checks against real git state
rather than anything the agent said about itself.

Per section 10.4 these run on a real repository (the ``git_repo`` fixture) with
real files and a real diff. Nothing is mocked: a gate tested against a
fabricated diff string proves only that the fabrication was parsed.

The three failure reasons are distinct on purpose (section 7.2):

* ``out_of_scope_write`` -- a changed path matches no writable or creatable
  glob. The obvious one.
* ``undeclared_change`` -- redgear saw a change the agent did not declare.
* ``phantom_change`` -- the agent declared a file it never touched.

The last two matter more than they look. They can both fire while every path
is technically in scope, and they are how you catch an agent that has lost
track of its own edits -- which means it cannot be trusted about anything
else in the same submission.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from redgear.schemas import Claim, GateName, GateStatus, TaskNode, Verdict
from redgear.verifier import GATE_ORDER, run_gates, scope_check


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _task(
    *,
    writable: list[str],
    creatable: list[str] | None = None,
    frozen: list[str] | None = None,
) -> TaskNode:
    """An implementation node with the scope under test."""
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
            "creatable_globs": creatable if creatable is not None else writable,
            "frozen_globs": frozen if frozen is not None else [],
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


def _claim(repo: Path, frozen_hashes: dict[str, str] | None = None) -> Claim:
    return Claim(
        base_commit=_git(repo, "rev-parse", "HEAD").strip(),
        frozen_hashes=frozen_hashes or {},
        allowed_tools=["Read", "Edit", "Write"],
        claimed_at=datetime.now(tz=UTC),
    )


def _reason_kinds(result: Any) -> set[str]:
    """Reasons are ``"<kind>: <path>"`` so they stay greppable in a prompt."""
    return {reason.split(":", 1)[0].strip() for reason in result.reasons}


def _reason_paths(result: Any, kind: str) -> set[str]:
    return {
        reason.split(":", 1)[1].strip()
        for reason in result.reasons
        if reason.split(":", 1)[0].strip() == kind
    }


# ---------------------------------------------------------------------------
# AC-1: a write outside scope fails, naming EVERY offending path.
# ---------------------------------------------------------------------------


def test_out_of_scope_write_fails(git_repo: Path) -> None:
    """Naming every path at once matters: an agent shown one violation per
    attempt burns its whole budget on a list it could have seen in one go."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"], frozen=["tests/**"])

    # Three violations at once, plus one legitimate in-scope edit.
    (git_repo / "src" / "pkg" / "__init__.py").write_text("ok = 1\n", encoding="utf-8")
    (git_repo / "tests" / "test_pkg.py").write_text("nope = 1\n", encoding="utf-8")
    (git_repo / "README.md").write_text("nope\n", encoding="utf-8")
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "notes.md").write_text("nope\n", encoding="utf-8")

    declared = [
        "src/pkg/__init__.py",
        "tests/test_pkg.py",
        "README.md",
        "docs/notes.md",
    ]
    result = scope_check(git_repo, task=task, claim=claim, declared=declared)

    assert result.name == GateName.SCOPE_CHECK
    assert result.status == GateStatus.FAILED
    assert "out_of_scope_write" in _reason_kinds(result)

    offenders = _reason_paths(result, "out_of_scope_write")
    assert offenders == {"tests/test_pkg.py", "README.md", "docs/notes.md"}, (
        "every offending path must be reported, not just the first"
    )
    # The in-scope edit is not reported.
    assert "src/pkg/__init__.py" not in offenders


def test_in_scope_changes_pass(git_repo: Path) -> None:
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])

    (git_repo / "src" / "pkg" / "__init__.py").write_text("ok = 1\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "extra.py").write_text("ok = 2\n", encoding="utf-8")

    result = scope_check(
        git_repo,
        task=task,
        claim=claim,
        declared=["src/pkg/__init__.py", "src/pkg/extra.py"],
    )
    assert result.status == GateStatus.PASSED
    assert result.reasons == []


def test_no_changes_at_all_passes(git_repo: Path) -> None:
    """An agent that changed nothing violated no scope. Whether doing nothing
    is acceptable is the tests_pass gate's problem, not this one's."""
    result = scope_check(
        git_repo, task=_task(writable=["src/**"]), claim=_claim(git_repo), declared=[]
    )
    assert result.status == GateStatus.PASSED


def test_new_file_must_match_creatable_not_merely_writable(git_repo: Path) -> None:
    """Section 7.2: "new files must match ``creatable_globs`` specifically".

    A task permitted to modify existing files is not automatically permitted
    to add new ones -- a task scoped to edit one module should not be able to
    invent a second one beside it.
    """
    claim = _claim(git_repo)
    task = _task(writable=["src/**"], creatable=["src/pkg/allowed_new.py"])

    # Modifying an existing file inside writable -- fine.
    (git_repo / "src" / "pkg" / "__init__.py").write_text("edited = 1\n", encoding="utf-8")
    # Creating the one permitted new file -- fine.
    (git_repo / "src" / "pkg" / "allowed_new.py").write_text("new = 1\n", encoding="utf-8")
    # Creating a file that is writable but NOT creatable -- a violation.
    (git_repo / "src" / "pkg" / "sneaky.py").write_text("new = 2\n", encoding="utf-8")

    result = scope_check(
        git_repo,
        task=task,
        claim=claim,
        declared=[
            "src/pkg/__init__.py",
            "src/pkg/allowed_new.py",
            "src/pkg/sneaky.py",
        ],
    )

    assert result.status == GateStatus.FAILED
    offenders = _reason_paths(result, "out_of_scope_write")
    assert offenders == {"src/pkg/sneaky.py"}


# ---------------------------------------------------------------------------
# AC-2: a change redgear saw but the agent did not declare.
# ---------------------------------------------------------------------------


def test_undeclared_change_fails(git_repo: Path) -> None:
    """Every path here is inside scope, so only the declaration mismatch can
    fail it. An agent that has lost track of its own edits cannot be trusted
    about the rest of its submission."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])

    (git_repo / "src" / "pkg" / "__init__.py").write_text("a = 1\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "forgotten.py").write_text("b = 2\n", encoding="utf-8")

    result = scope_check(
        git_repo,
        task=task,
        claim=claim,
        declared=["src/pkg/__init__.py"],  # forgotten.py omitted
    )

    assert result.status == GateStatus.FAILED
    assert "undeclared_change" in _reason_kinds(result)
    assert _reason_paths(result, "undeclared_change") == {"src/pkg/forgotten.py"}
    # In scope, so this must NOT also be reported as out of scope.
    assert "out_of_scope_write" not in _reason_kinds(result)


# ---------------------------------------------------------------------------
# AC-3: a file the agent declared but never touched.
# ---------------------------------------------------------------------------


def test_phantom_change_fails(git_repo: Path) -> None:
    """The claim is cross-checked against the real diff, never trusted (G1)."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])

    (git_repo / "src" / "pkg" / "__init__.py").write_text("a = 1\n", encoding="utf-8")

    result = scope_check(
        git_repo,
        task=task,
        claim=claim,
        declared=["src/pkg/__init__.py", "src/pkg/imaginary.py"],
    )

    assert result.status == GateStatus.FAILED
    assert "phantom_change" in _reason_kinds(result)
    assert _reason_paths(result, "phantom_change") == {"src/pkg/imaginary.py"}


def test_undeclared_and_phantom_reported_together(git_repo: Path) -> None:
    """Both directions of the mismatch surface in one pass, so a corrective
    prompt can carry the whole picture."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])

    (git_repo / "src" / "pkg" / "real.py").write_text("a = 1\n", encoding="utf-8")

    result = scope_check(git_repo, task=task, claim=claim, declared=["src/pkg/ghost.py"])

    assert result.status == GateStatus.FAILED
    kinds = _reason_kinds(result)
    assert "undeclared_change" in kinds
    assert "phantom_change" in kinds
    assert _reason_paths(result, "undeclared_change") == {"src/pkg/real.py"}
    assert _reason_paths(result, "phantom_change") == {"src/pkg/ghost.py"}


def test_declared_paths_are_compared_posix_normalised(git_repo: Path) -> None:
    """An agent on Windows may declare ``src\\pkg\\x.py``. Git always emits
    POSIX separators, so a naive string compare would call every Windows
    declaration a phantom AND every real edit undeclared."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])

    (git_repo / "src" / "pkg" / "__init__.py").write_text("a = 1\n", encoding="utf-8")

    result = scope_check(git_repo, task=task, claim=claim, declared=["src\\pkg\\__init__.py"])
    assert result.status == GateStatus.PASSED, (
        f"backslash declaration was not normalised: {result.reasons}"
    )


# ---------------------------------------------------------------------------
# AC-6: gates after a failure are recorded skipped, never omitted.
# ---------------------------------------------------------------------------


def test_later_gates_marked_skipped(git_repo: Path) -> None:
    """Section 7.1: the proof must show the full contract including what was
    not reached. An omitted gate is indistinguishable from a gate that does
    not exist, which makes a proof unreadable as evidence."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"], frozen=["tests/**"])

    (git_repo / "README.md").write_text("out of scope\n", encoding="utf-8")

    proof = run_gates(git_repo, task=task, claim=claim, declared=["README.md"], attempt=1)

    assert proof.verdict == Verdict.FAIL
    assert proof.task_id == "T-0099"
    assert proof.attempt == 1

    # The full contracted list, in order, nothing omitted.
    assert [gate.name for gate in proof.gates] == list(GATE_ORDER)
    assert len(proof.gates) == 6

    by_name = {gate.name: gate for gate in proof.gates}
    assert by_name[GateName.SCOPE_CHECK].status == GateStatus.FAILED
    for later in GATE_ORDER[1:]:
        assert by_name[later].status == GateStatus.SKIPPED, (
            f"{later} should be skipped after an earlier failure"
        )

    # A skipped gate says why, so the proof reads as evidence rather than a
    # blank.
    assert by_name[GateName.LINT].reasons, "a skipped gate must record why"


def test_gate_order_matches_section_7_1() -> None:
    """The order is normative -- cheap structural checks run before expensive
    ones, and the frozen-hash audit runs before anything executes code."""
    assert GATE_ORDER == [
        GateName.SCOPE_CHECK,
        GateName.FROZEN_HASH_CHECK,
        GateName.LINT,
        GateName.TESTS_PASS,
        GateName.CRITERIA_COVERAGE,
        GateName.COVERAGE_DELTA,
    ]


def test_gates_three_to_six_are_not_stubbed(git_repo: Path) -> None:
    """Gates 3-6 arrive at T-0025. Until then they must be reported as not
    run -- never as passing. A stub that always passes is worse than an
    absent gate, because a proof would show a green it never earned."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])
    (git_repo / "src" / "pkg" / "__init__.py").write_text("a = 1\n", encoding="utf-8")

    proof = run_gates(
        git_repo,
        task=task,
        claim=claim,
        declared=["src/pkg/__init__.py"],
        attempt=1,
    )

    by_name = {gate.name: gate for gate in proof.gates}
    # Gates 1-2 genuinely ran and passed.
    assert by_name[GateName.SCOPE_CHECK].status == GateStatus.PASSED
    assert by_name[GateName.FROZEN_HASH_CHECK].status == GateStatus.PASSED
    # Gates 3-6 are not implemented, so they are skipped, not passed.
    for pending in GATE_ORDER[2:]:
        assert by_name[pending].status == GateStatus.SKIPPED
        assert by_name[pending].status != GateStatus.PASSED

    # And because they never ran, the overall verdict cannot be a pass.
    assert proof.verdict == Verdict.FAIL, "a proof whose gates did not all run must not report PASS"


def test_proof_is_serialisable(git_repo: Path) -> None:
    """The proof is written to `.redgear/runs/.../proof/verdict.json`, so it
    must round-trip through JSON without loss."""
    from redgear.schemas import Proof

    claim = _claim(git_repo)
    proof = run_gates(
        git_repo, task=_task(writable=["src/**"]), claim=claim, declared=[], attempt=2
    )
    dumped = proof.model_dump(mode="json")
    assert Proof.model_validate(dumped) == proof
    assert isinstance(dumped["computed_at"], str)


def test_scope_check_never_trusts_the_declaration_for_the_changed_set(
    git_repo: Path,
) -> None:
    """G1: the changed set is recomputed from git. A declaration that omits
    everything must not make the gate see an empty diff."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"], frozen=[])

    (git_repo / "README.md").write_text("out of scope\n", encoding="utf-8")

    result = scope_check(git_repo, task=task, claim=claim, declared=[])

    assert result.status == GateStatus.FAILED
    # Both the scope violation and the missing declaration are found, even
    # though the agent declared nothing at all.
    assert "out_of_scope_write" in _reason_kinds(result)
    assert "undeclared_change" in _reason_kinds(result)


@pytest.mark.parametrize("kind", ["out_of_scope_write", "undeclared_change", "phantom_change"])
def test_every_reason_names_a_path(git_repo: Path, kind: str) -> None:
    """A reason without a path is not actionable. Section 5.5 exists because
    a vague failure guarantees an identical retry."""
    claim = _claim(git_repo)
    task = _task(writable=["src/**"])

    (git_repo / "README.md").write_text("x\n", encoding="utf-8")
    (git_repo / "src" / "pkg" / "undeclared.py").write_text("y\n", encoding="utf-8")

    result = scope_check(
        git_repo, task=task, claim=claim, declared=["README.md", "src/pkg/ghost.py"]
    )
    for reason in result.reasons:
        assert ":" in reason, f"reason is not '<kind>: <path>' shaped: {reason!r}"
        assert reason.split(":", 1)[1].strip(), f"reason names no path: {reason!r}"
    assert kind in _reason_kinds(result)
