"""T-0024: failing tests for verifier gate 6 -- coverage_delta.

The symbols imported below do not exist yet, so this module fails at
COLLECTION with ``ImportError``. That is the correct red state.

Section 7.5 measures **the lines the agent changed**, never the repository.
Both halves of that matter:

* A global threshold punishes an agent for coverage debt that predates it,
  which is unfixable inside its scope.
* A global threshold is also trivially gamed -- touch one line in a
  well-covered module and the repository average carries the change.

The denominator is ``changed_lines & (executed | missing)`` per file. Lines
coverage.py does not classify -- blank, comment, excluded -- drop out,
because counting them would penalise an agent for adding a docstring. The
ratio is 1.0 when the denominator is empty: a change with no measurable lines
is not a coverage regression.

``test_changed_line_ratio_exact`` is the AC-6 test, and it checks a ratio
computed by hand against a fixture small enough to verify by reading it.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from redgear.schemas import Claim, GateName, GateStatus, HarnessConfig, TaskNode, Verdict
from redgear.verifier import changed_line_ratio, coverage_delta_check, run_gates, run_harness

# The module the "agent" writes. Line numbers are load-bearing -- the expected
# ratio below is derived from them, so keep the numbering if you edit this.
#
#   1  def add(a, b):            executed
#   2      return a + b          executed
#   3  (blank)                   unclassified
#   4  (blank)                   unclassified
#   5  def classify(n):          executed
#   6      if n > 10:            executed
#   7          return "big"      MISSING   -- classify(5) never takes this branch
#   8      return "small"        executed
#   9  (blank)                   unclassified
#  10  (blank)                   unclassified
#  11 def never_called(x):       executed  -- the def line runs at import
#  12     return x               MISSING   -- the body never does
NEW_PKG = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def classify(n):\n"
    "    if n > 10:\n"
    '        return "big"\n'
    '    return "small"\n'
    "\n"
    "\n"
    "def never_called(x):\n"
    "    return x\n"
)

NEW_TESTS = (
    "from pkg import add, classify\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n"
    "\n"
    "\n"
    "def test_classify():\n"
    '    assert classify(5) == "small"\n'
)

# Lines 1-2 are unchanged, so the diff adds 3-12: ten changed lines.
#   classified  = {1, 2, 5, 6, 7, 8, 11, 12}
#   denominator = changed & classified = {5, 6, 7, 8, 11, 12}   -> 6
#   covered     = changed & executed   = {5, 6, 8, 11}          -> 4
EXPECTED_CONSIDERED = 6
EXPECTED_COVERED = 4
EXPECTED_RATIO = 4 / 6


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _harness(*, floor: float = 0.85, source: list[str] | None = None) -> HarnessConfig:
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=source if source is not None else ["src"],
        coverage_floor=floor,
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
            "writable_globs": ["src/**", "tests/**"],
            "creatable_globs": ["src/**", "tests/**"],
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


def _claim(repo: Path) -> Claim:
    return Claim(
        base_commit=_git(repo, "rev-parse", "HEAD").strip(),
        frozen_hashes={},
        allowed_tools=["Read", "Edit", "Write"],
        claimed_at=datetime.now(tz=UTC),
    )


def _apply_change(repo: Path) -> None:
    (repo / "src" / "pkg" / "__init__.py").write_text(NEW_PKG, encoding="utf-8")
    (repo / "tests" / "test_pkg.py").write_text(NEW_TESTS, encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-6: the ratio is computed over changed lines only, and matches by hand.
# ---------------------------------------------------------------------------


def test_changed_line_ratio_exact(python_repo: Path) -> None:
    """A real repository, a real coverage run, and a ratio worked out by
    reading the twelve-line fixture at the top of this module.

    Every intermediate is asserted as well as the final number. A bare ratio
    assertion tells you it broke; the intermediates tell you whether the diff
    parsing, the coverage parsing, or the arithmetic broke.
    """
    claim = _claim(python_repo)
    _apply_change(python_repo)

    run = run_harness(python_repo, harness=_harness(), with_coverage=True)
    assert run.coverage is not None, "no coverage data was produced"

    delta = changed_line_ratio(python_repo, claim.base_commit, run.coverage)

    assert delta.considered == EXPECTED_CONSIDERED, (
        f"denominator should be the 6 classified changed lines {{5,6,7,8,11,12}}, "
        f"got {delta.considered} (uncovered={delta.uncovered})"
    )
    assert delta.covered == EXPECTED_COVERED, (
        f"numerator should be the 4 executed changed lines {{5,6,8,11}}, got {delta.covered}"
    )
    assert delta.ratio == pytest.approx(EXPECTED_RATIO)

    # And the two lines that are genuinely uncovered are named, so the agent
    # knows which branch it failed to exercise.
    assert delta.uncovered.get("src/pkg/__init__.py") == [7, 12], (
        f"the uncovered changed lines must be named: {delta.uncovered}"
    )


def test_blank_lines_are_not_counted(python_repo: Path) -> None:
    """Lines 3, 4, 9 and 10 are blank and changed. Counting them would make
    the denominator 10 and the ratio 0.4 -- penalising the agent for
    formatting."""
    claim = _claim(python_repo)
    _apply_change(python_repo)

    run = run_harness(python_repo, harness=_harness(), with_coverage=True)
    assert run.coverage is not None
    delta = changed_line_ratio(python_repo, claim.base_commit, run.coverage)

    assert delta.considered == 6
    assert delta.considered != 10, "blank changed lines leaked into the denominator"


def test_files_outside_the_coverage_source_drop_out(python_repo: Path) -> None:
    """``tests/test_pkg.py`` changed too, and it carries no coverage data.

    Scoring it as wholly uncovered would mean every task that edits a test
    file fails the gate -- including every ``test_authoring`` task in the
    project.
    """
    claim = _claim(python_repo)
    _apply_change(python_repo)

    run = run_harness(python_repo, harness=_harness(), with_coverage=True)
    assert run.coverage is not None
    delta = changed_line_ratio(python_repo, claim.base_commit, run.coverage)

    assert "tests/test_pkg.py" not in delta.uncovered
    assert delta.considered == EXPECTED_CONSIDERED


def test_gate_fails_below_the_floor(python_repo: Path) -> None:
    claim = _claim(python_repo)
    _apply_change(python_repo)

    run = run_harness(python_repo, harness=_harness(floor=0.85), with_coverage=True)
    result = coverage_delta_check(
        python_repo, task=_task(), claim=claim, harness=_harness(floor=0.85), run=run
    )

    assert result.name == GateName.COVERAGE_DELTA
    assert result.status == GateStatus.FAILED
    assert any("uncovered" in reason or "coverage" in reason for reason in result.reasons)
    assert any("src/pkg/__init__.py:7" in reason for reason in result.reasons), (
        f"the uncovered lines must be named, not just the ratio: {result.reasons}"
    )


def test_gate_passes_at_or_above_the_floor(python_repo: Path) -> None:
    """0.666... clears a floor of 0.6. The comparison is >=, not >."""
    claim = _claim(python_repo)
    _apply_change(python_repo)

    harness = _harness(floor=0.6)
    run = run_harness(python_repo, harness=harness, with_coverage=True)
    result = coverage_delta_check(python_repo, task=_task(), claim=claim, harness=harness, run=run)

    assert result.status == GateStatus.PASSED


def test_empty_denominator_is_a_pass(python_repo: Path) -> None:
    """Section 7.5: "Ratio is 1.0 when the denominator is empty."

    Here the only change is a comment, so nothing measurable moved. A
    zero-denominator division would crash the gate on a legitimate change.
    """
    claim = _claim(python_repo)
    (python_repo / "src/pkg/__init__.py").write_text(
        "def add(a, b):\n    return a + b\n\n\n# a trailing comment\n", encoding="utf-8"
    )

    harness = _harness(floor=0.99)
    run = run_harness(python_repo, harness=harness, with_coverage=True)
    assert run.coverage is not None

    delta = changed_line_ratio(python_repo, claim.base_commit, run.coverage)
    assert delta.considered == 0
    assert delta.ratio == 1.0

    result = coverage_delta_check(python_repo, task=_task(), claim=claim, harness=harness, run=run)
    assert result.status == GateStatus.PASSED


def test_no_changes_at_all_is_a_pass(python_repo: Path) -> None:
    claim = _claim(python_repo)
    harness = _harness(floor=0.99)
    run = run_harness(python_repo, harness=harness, with_coverage=True)

    result = coverage_delta_check(python_repo, task=_task(), claim=claim, harness=harness, run=run)
    assert result.status == GateStatus.PASSED


def test_coverage_paths_are_posix_normalised(python_repo: Path) -> None:
    """coverage.py keys its JSON by **native** separators -- on Windows the
    module under test appears as ``src\\pkg\\__init__.py``. Git always emits
    forward slashes, so without normalisation the two sets never intersect
    and every ratio silently becomes 1.0.

    A gate that passes everything is indistinguishable from a gate that works
    until you look, which is why this is asserted rather than assumed.
    """
    claim = _claim(python_repo)
    _apply_change(python_repo)

    run = run_harness(python_repo, harness=_harness(), with_coverage=True)
    assert run.coverage is not None
    delta = changed_line_ratio(python_repo, claim.base_commit, run.coverage)

    for path in delta.uncovered:
        assert "\\" not in path, f"a native-separator path escaped normalisation: {path!r}"
    assert delta.considered > 0, "the changed set and the coverage data did not intersect at all"


def test_gate_is_skipped_for_scaffold(python_repo: Path) -> None:
    """Section 4.5: coverage_delta is one of the three gates a scaffold task
    does not apply -- you cannot meaningfully cover packaging metadata."""
    claim = _claim(python_repo)
    harness = _harness()
    run = run_harness(python_repo, harness=harness, with_coverage=True)

    result = coverage_delta_check(
        python_repo, task=_task(task_type="scaffold"), claim=claim, harness=harness, run=run
    )
    assert result.status == GateStatus.SKIPPED
    assert result.reasons, "a skipped gate must record why"


def test_gate_is_skipped_for_test_authoring(python_repo: Path) -> None:
    """A ``test_authoring`` task writes tests, not covered code. Its suite is
    red by design, so coverage measured from that run says nothing about the
    implementation that has not been written yet."""
    claim = _claim(python_repo)
    harness = _harness()
    run = run_harness(python_repo, harness=harness, with_coverage=True)

    result = coverage_delta_check(
        python_repo, task=_task(task_type="test_authoring"), claim=claim, harness=harness, run=run
    )
    assert result.status == GateStatus.SKIPPED
    assert result.reasons


# ---------------------------------------------------------------------------
# Regression: a skipped-as-not-applicable coverage_delta must not fail the
# aggregate verdict. Found by the first real end-to-end run against a live
# agent CLI -- a correct, fully in-scope test_authoring turn was rejected
# with every individual gate reporting "passed" and no gate named as the
# cause, because run_gates' verdict computation treated coverage_delta's
# legitimate not-applicable skip the same as a real gap in verification.
# ---------------------------------------------------------------------------


def test_a_correct_test_authoring_turn_passes_the_real_pipeline(python_repo: Path) -> None:
    """Every gate that runs for a valid test_authoring turn actually passes;
    coverage_delta is the one gate skipped by design (§4.5, §7.2); the
    aggregate verdict must be PASS, not FAIL with no gate named as the cause.
    """
    claim = _claim(python_repo)
    harness = _harness(floor=0.0)

    # Writable for test_authoring is tests/**; src/** stays untouched, so the
    # existing (correct) add() implementation makes this assertion false --
    # a real red, for the right reason, without touching frozen code.
    (python_repo / "tests" / "test_pkg.py").write_text(
        "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 999\n",
        encoding="utf-8",
    )

    proof = run_gates(
        python_repo,
        task=_task(task_type="test_authoring"),
        claim=claim,
        declared=["tests/test_pkg.py"],
        attempt=1,
        harness=harness,
        criteria=[],
    )

    by_name = {gate.name: gate for gate in proof.gates}
    assert by_name[GateName.TESTS_PASS].status == GateStatus.PASSED, proof.gates
    assert by_name[GateName.COVERAGE_DELTA].status == GateStatus.SKIPPED, proof.gates
    assert proof.verdict is Verdict.PASS, (
        f"a correct test_authoring turn was rejected with no gate actually failing: {proof.gates}"
    )
