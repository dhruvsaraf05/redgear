"""T-0024: failing tests for verifier gate 5 -- criteria_coverage.

The symbols imported below do not exist yet, so this module fails at
COLLECTION with ``ImportError``. That is the correct red state.

This is the gate that catches "implemented and fully tested" when the cited
test never ran.

It is worth being precise about why it is not redundant with gate 4. Gate 4
asks "did the suite pass?" -- a suite of one trivial test passes. Gate 5 asks
"did *these specific* tests, the ones the plan says verify this task, run and
pass?" An agent can satisfy gate 4 while never touching the behaviour the
task was about; it cannot satisfy gate 5 that way, because the criterion
names a pytest node id and the node id either appears in the report or it
does not.

Section 7.2 gives two distinct reasons, and the distinction is the whole
value: ``evidence_not_found`` means the test does not exist, and
``evidence_did_not_pass`` means it exists and is red. Collapsing them would
tell the agent to write a test that is already written.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from redgear.schemas import AcceptanceCriterion, GateName, GateStatus, HarnessConfig, TaskNode
from redgear.verifier import criteria_coverage_check, run_harness


def _harness() -> HarnessConfig:
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=["src"],
        coverage_floor=0.0,
        timeout_s=120,
    )


def _task(*, task_type: str = "implementation") -> TaskNode:
    payload: dict[str, Any] = {
        "id": "T-0099",
        "type": task_type,
        "title": "the task under verification",
        "state": "claimed",
        "spec_refs": ["FR-6"],
        "spec_hash": "sha256:" + "0" * 64,
        "depends_on": [],
        "scope": {
            "writable_globs": ["src/**"],
            "creatable_globs": ["src/**"],
            "frozen_globs": [],
        },
        "acceptance_criteria": [],
        "inherits_criteria_from": ["T-0098"] if task_type == "implementation" else [],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }
    return TaskNode.model_validate(payload)


def _criterion(cid: str, selector: str) -> AcceptanceCriterion:
    return AcceptanceCriterion.model_validate(
        {
            "id": cid,
            "statement": f"criterion {cid}",
            "verified_by": {"kind": "test", "selector": selector},
        }
    )


def _reason_kinds(result: Any) -> set[str]:
    return {reason.split(":", 1)[0].strip() for reason in result.reasons}


def _write_test(repo: Path, body: str) -> None:
    (repo / "tests" / "test_pkg.py").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-5: a criterion citing a nonexistent test node fails as evidence_not_found.
# ---------------------------------------------------------------------------


def test_missing_evidence_fails(python_repo: Path) -> None:
    """The headline case: the suite is green, and the gate still fails.

    This is exactly the shape of the lie the gate exists to catch -- an agent
    reports a task complete and cites a test that was never written. Gate 4
    is satisfied, because everything that *does* exist passes.
    """
    run = run_harness(python_repo, harness=_harness())
    assert run.report is not None
    assert run.report["summary"]["passed"] == 1, "precondition: the suite is green"

    result = criteria_coverage_check(
        _task(),
        [_criterion("AC-1", "tests/test_pkg.py::test_never_written")],
        run,
    )

    assert result.name == GateName.CRITERIA_COVERAGE
    assert result.status == GateStatus.FAILED
    assert "evidence_not_found" in _reason_kinds(result)
    assert any("test_never_written" in reason for reason in result.reasons), (
        f"the missing selector must be named: {result.reasons}"
    )
    assert any("AC-1" in reason for reason in result.reasons), (
        f"the criterion id must be named so the agent knows which one: {result.reasons}"
    )


def test_present_and_passing_evidence_passes(python_repo: Path) -> None:
    result = criteria_coverage_check(
        _task(),
        [_criterion("AC-1", "tests/test_pkg.py::test_add")],
        run_harness(python_repo, harness=_harness()),
    )
    assert result.status == GateStatus.PASSED


def test_failing_evidence_is_a_different_reason(python_repo: Path) -> None:
    """``evidence_did_not_pass`` is not ``evidence_not_found``.

    The tests exist; they are red. Telling the agent the test is missing
    would send it to write a second copy of a test that is already there.
    """
    _write_test(
        python_repo,
        "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n",
    )

    run = run_harness(python_repo, harness=_harness())
    result = criteria_coverage_check(
        _task(), [_criterion("AC-1", "tests/test_pkg.py::test_add")], run
    )

    assert result.status == GateStatus.FAILED
    kinds = _reason_kinds(result)
    assert "evidence_did_not_pass" in kinds
    assert "evidence_not_found" not in kinds, f"a red test is not a missing test: {result.reasons}"


def test_every_unmet_criterion_is_reported(python_repo: Path) -> None:
    """One item per attempt would burn the whole budget on a list the agent
    could have seen at once (the same argument as gate 1's)."""
    result = criteria_coverage_check(
        _task(),
        [
            _criterion("AC-1", "tests/test_pkg.py::test_add"),
            _criterion("AC-2", "tests/test_pkg.py::test_missing_one"),
            _criterion("AC-3", "tests/test_pkg.py::test_missing_two"),
        ],
        run_harness(python_repo, harness=_harness()),
    )

    assert result.status == GateStatus.FAILED
    assert any("AC-2" in reason for reason in result.reasons)
    assert any("AC-3" in reason for reason in result.reasons), (
        f"only the first unmet criterion was reported: {result.reasons}"
    )


def test_selector_matching_is_path_separator_agnostic(python_repo: Path) -> None:
    """A plan authored on Windows may spell the selector with backslashes.
    pytest node ids always use forward slashes, so a naive compare would call
    every criterion unmet."""
    result = criteria_coverage_check(
        _task(),
        [_criterion("AC-1", "tests\\test_pkg.py::test_add")],
        run_harness(python_repo, harness=_harness()),
    )
    assert result.status == GateStatus.PASSED, (
        f"backslash selector was not normalised: {result.reasons}"
    )


def test_no_criteria_passes_vacuously(python_repo: Path) -> None:
    """A task with nothing to prove has nothing unmet. Whether a task *should*
    carry criteria is a planning invariant (section 4.4), not this gate's
    business."""
    result = criteria_coverage_check(_task(), [], run_harness(python_repo, harness=_harness()))
    assert result.status == GateStatus.PASSED


def test_gate_is_skipped_for_scaffold(python_repo: Path) -> None:
    """Section 4.5: a scaffold task is verified by smoke checks;
    ``criteria_coverage`` is one of the three gates it does not apply."""
    result = criteria_coverage_check(
        _task(task_type="scaffold"),
        [_criterion("AC-1", "smoke::pip_install_editable")],
        run_harness(python_repo, harness=_harness()),
    )
    assert result.status == GateStatus.SKIPPED
    assert result.reasons, "a skipped gate must record why"


def test_test_authoring_requires_existence_not_passing(python_repo: Path) -> None:
    """For a ``test_authoring`` task the cited tests are *supposed* to be red
    -- gate 4 has already established that. Requiring them to pass here would
    contradict gate 4 outright and make the pair unsatisfiable.

    Existence is still checked, because "I wrote that test" is exactly the
    claim worth verifying at this phase.
    """
    _write_test(
        python_repo,
        "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n",
    )
    run = run_harness(python_repo, harness=_harness())

    present = criteria_coverage_check(
        _task(task_type="test_authoring"), [_criterion("AC-1", "tests/test_pkg.py::test_add")], run
    )
    assert present.status == GateStatus.PASSED, (
        f"a red test is the expected state for test_authoring: {present.reasons}"
    )

    absent = criteria_coverage_check(
        _task(task_type="test_authoring"),
        [_criterion("AC-1", "tests/test_pkg.py::test_never_written")],
        run,
    )
    assert absent.status == GateStatus.FAILED
    assert "evidence_not_found" in _reason_kinds(absent)
