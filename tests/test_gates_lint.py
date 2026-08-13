"""T-0024: failing tests for verifier gate 3 -- lint.

The symbols imported below do not exist yet, so this module fails at
COLLECTION with ``ImportError``. That is the correct red state.

Gate 3 is the first gate that *executes a tool*. Everything before it reads
git; from here on the harness shells out, which brings section 7.3 into play:
``shell=False``, an argument vector from configuration only, a scrubbed
environment, and a timeout that is a recorded failure rather than an escaping
exception.

The gate's one interesting judgement is **whose fault a violation is**.
Section 7.2: "Filter to the task's writable scope: a pre-existing violation
elsewhere is not this agent's failure." A gate that failed an agent for lint
it was never permitted to touch would be unfixable -- the agent cannot edit
the offending file, so every retry burns an attempt on a violation outside
its scope. That is the ``preexisting_lint`` scenario in section 10.5, and it
must **pass**.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from redgear.schemas import Claim, GateName, GateStatus, HarnessConfig, TaskNode
from redgear.verifier import GATE_ORDER, lint_check, run_gates


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _harness() -> HarnessConfig:
    """``ruff`` is not necessarily on PATH -- in this project's venv it is
    reachable only as ``python -m ruff``. Configuration carries the exact
    argv (section 7.3); nothing is inferred at call time."""
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=["src"],
        coverage_floor=0.0,
        timeout_s=120,
    )


def _task(
    *,
    writable: list[str],
    creatable: list[str] | None = None,
    frozen: list[str] | None = None,
    task_type: str = "implementation",
) -> TaskNode:
    payload: dict[str, Any] = {
        "id": "T-0099",
        "type": task_type,
        "title": "the task under verification",
        "state": "claimed",
        "spec_refs": ["FR-6"],
        "spec_hash": "sha256:" + "0" * 64,
        "depends_on": [],
        "scope": {
            "writable_globs": writable,
            "creatable_globs": creatable if creatable is not None else writable,
            "frozen_globs": frozen if frozen is not None else [],
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


def _reason_kinds(result: Any) -> set[str]:
    return {reason.split(":", 1)[0].strip() for reason in result.reasons}


# ---------------------------------------------------------------------------
# AC-1: a violation inside scope fails, and the suite never runs.
# ---------------------------------------------------------------------------


def test_in_scope_violation_short_circuits(python_repo: Path) -> None:
    """Section 7.1 short-circuits on first failure. Lint is cheap and tests
    are expensive, so a lint failure must stop the pipeline *before* pytest
    runs -- and the proof has to show that it did, by recording the later
    gates as skipped rather than omitting them."""
    # `import os` unused (F401) in a file the task is allowed to write.
    (python_repo / "src/pkg/__init__.py").write_text(
        "import os\n\n\ndef add(a, b):\n    return a + b\n", encoding="utf-8"
    )

    proof = run_gates(
        python_repo,
        task=_task(writable=["src/**"]),
        claim=_claim(python_repo),
        declared=["src/pkg/__init__.py"],
        attempt=1,
        harness=_harness(),
    )

    by_name = {gate.name: gate for gate in proof.gates}
    assert by_name[GateName.LINT].status == GateStatus.FAILED
    assert "lint_violation" in _reason_kinds(by_name[GateName.LINT])
    assert any("F401" in reason for reason in by_name[GateName.LINT].reasons), (
        f"the rule code must be named so the agent can fix it: {by_name[GateName.LINT].reasons}"
    )

    # The whole point: the expensive gates never ran.
    for later in GATE_ORDER[3:]:
        assert by_name[later].status == GateStatus.SKIPPED, (
            f"{later} ran even though lint failed; the pipeline did not short-circuit"
        )
        assert by_name[later].reasons, "a skipped gate must record why"


def test_lint_violation_names_file_and_line(python_repo: Path) -> None:
    """Section 5.5: a reason without a location is not actionable, and a
    vague failure guarantees an identical retry."""
    (python_repo / "src/pkg/__init__.py").write_text(
        "import os\n\n\ndef add(a, b):\n    return a + b\n", encoding="utf-8"
    )

    result = lint_check(python_repo, task=_task(writable=["src/**"]), harness=_harness())

    assert result.name == GateName.LINT
    assert result.status == GateStatus.FAILED
    violations = [r for r in result.reasons if r.startswith("lint_violation")]
    assert violations, result.reasons
    assert any("src/pkg/__init__.py:1" in reason for reason in violations), (
        f"no reason names the offending file and line: {violations}"
    )
    # Paths are repo-relative: ruff emits absolute native paths, which leak
    # the user's home directory into a prompt and waste tokens (section 5.4).
    for reason in violations:
        assert ":\\" not in reason and not reason.split(":", 1)[1].strip().startswith("/"), (
            f"absolute path leaked into a reason: {reason!r}"
        )


def test_clean_scope_passes(python_repo: Path) -> None:
    result = lint_check(python_repo, task=_task(writable=["src/**"]), harness=_harness())
    assert result.status == GateStatus.PASSED


# ---------------------------------------------------------------------------
# AC-2: a pre-existing violation outside scope is not this agent's failure.
# ---------------------------------------------------------------------------


def test_out_of_scope_violation_ignored(python_repo: Path) -> None:
    """The ``preexisting_lint`` scenario (section 10.5): a violation the agent
    was never permitted to touch must not fail it.

    Failing here would be unfixable by construction -- the file is outside the
    write scope, so the agent cannot correct it, and every retry would burn an
    attempt on someone else's mess.
    """
    # Dirty file OUTSIDE the writable scope.
    (python_repo / "legacy.py").write_text("import os\nimport sys\n", encoding="utf-8")
    # The file the task may actually write is clean.
    (python_repo / "src/pkg/__init__.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )

    result = lint_check(python_repo, task=_task(writable=["src/**"]), harness=_harness())

    assert result.status == GateStatus.PASSED, (
        f"a violation outside the task scope failed the gate: {result.reasons}"
    )
    assert not any(r.startswith("lint_violation") for r in result.reasons)


def test_out_of_scope_violations_are_noted_not_silent(python_repo: Path) -> None:
    """Section 10.5 says this scenario "passes **with a note**".

    Silence would be wrong: the run is proceeding past real lint debt, and a
    proof that does not mention it reads as a clean repository.
    """
    (python_repo / "legacy.py").write_text("import os\nimport sys\n", encoding="utf-8")

    result = lint_check(python_repo, task=_task(writable=["src/**"]), harness=_harness())

    assert result.status == GateStatus.PASSED
    assert result.reasons, "passing while ignoring real violations must still be recorded"
    assert "ignored_out_of_scope" in _reason_kinds(result)


def test_only_in_scope_violations_counted_when_both_present(python_repo: Path) -> None:
    """Both kinds at once: the in-scope one fails the gate, and the
    out-of-scope one is neither counted against the agent nor hidden."""
    (python_repo / "legacy.py").write_text("import os\nimport sys\n", encoding="utf-8")
    (python_repo / "src/pkg/__init__.py").write_text(
        "import json\n\n\ndef add(a, b):\n    return a + b\n", encoding="utf-8"
    )

    result = lint_check(python_repo, task=_task(writable=["src/**"]), harness=_harness())

    assert result.status == GateStatus.FAILED
    violations = [r for r in result.reasons if r.startswith("lint_violation")]
    assert violations
    assert all("legacy.py" not in reason for reason in violations), (
        f"an out-of-scope violation was counted against the agent: {violations}"
    )


def test_creatable_paths_count_as_in_scope(python_repo: Path) -> None:
    """A file the task was permitted to *create* is as much its
    responsibility as one it was permitted to modify."""
    (python_repo / "brand_new.py").write_text("import os\n", encoding="utf-8")

    task = _task(writable=["src/**"], creatable=["src/**", "brand_new.py"])
    result = lint_check(python_repo, task=task, harness=_harness())

    assert result.status == GateStatus.FAILED
    assert any("brand_new.py" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# Reporting discipline: section 7.2 caps the mapped violations at 20.
# ---------------------------------------------------------------------------


def test_violations_capped_with_true_count(python_repo: Path) -> None:
    """Section 5.5 rule 4: when truncated, the agent must still learn the real
    count. Facing 1 problem and facing 40 call for different responses."""
    body = "".join(f"import mod{i}\n" for i in range(30))
    (python_repo / "src/pkg/__init__.py").write_text(body, encoding="utf-8")

    result = lint_check(python_repo, task=_task(writable=["src/**"]), harness=_harness())

    assert result.status == GateStatus.FAILED
    violations = [r for r in result.reasons if r.startswith("lint_violation")]
    assert len(violations) <= 20, "section 7.2 maps at most the first 20 violations"
    assert any("more" in r.lower() for r in result.reasons), (
        f"truncation must state the true total: {result.reasons}"
    )
