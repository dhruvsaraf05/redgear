"""T-0024: failing tests for verifier gate 4 -- tests_pass.

The symbols imported below do not exist yet, so this module fails at
COLLECTION with ``ImportError``. That is the correct red state.

This gate is where redgear stops believing anyone. G1: "redgear runs pytest
itself and never believes a claim it can check." The verdict comes from the
JSON report on disk, never from stdout scraping and never from the agent's
own account of how it went.

Three things make it harder than it looks:

* **Polarity inverts.** For a ``test_authoring`` task the gate passes only
  when the target tests *fail*. Tests that already pass are a tautology
  (``tests_not_red``) -- the agent wrote assertions that were true before it
  started, which proves nothing about the implementation to come.
* **A collection error is not an assertion failure.** The correct agent
  response differs entirely: fix an import versus fix a behaviour. Reporting
  them the same way guarantees the wrong retry.
* **The runner nests.** This gate runs pytest inside a target repository
  while itself running under pytest. Everything the outer session leaks --
  ``PYTEST_ADDOPTS``, an ancestor ``pyproject.toml``, an ancestor
  ``conftest.py``, the cache directory -- has to be shut out explicitly.
  ``test_nested_runner_isolated`` is the regression guard for that, and every
  case it names was observed happening before it was written.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from redgear.errors import UnsafeHarnessCommandError
from redgear.schemas import Claim, GateName, GateStatus, HarnessConfig, TaskNode
from redgear.verifier import (
    GATE_ORDER,
    run_command,
    run_gates,
    run_harness,
    tests_pass_check,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _harness(*, timeout_s: int = 120) -> HarnessConfig:
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=["src"],
        coverage_floor=0.0,
        timeout_s=timeout_s,
    )


def _task(*, task_type: str = "implementation", writable: list[str] | None = None) -> TaskNode:
    payload: dict[str, Any] = {
        "id": "T-0099",
        "type": task_type,
        "title": "the task under verification",
        "state": "claimed",
        "spec_refs": ["FR-6"],
        "spec_hash": "sha256:" + "0" * 64,
        "depends_on": [],
        "scope": {
            "writable_globs": writable if writable is not None else ["src/**"],
            "creatable_globs": writable if writable is not None else ["src/**"],
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


def _reason_kinds(result: Any) -> set[str]:
    return {reason.split(":", 1)[0].strip() for reason in result.reasons}


def _write_test(repo: Path, body: str) -> None:
    (repo / "tests" / "test_pkg.py").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# The report is read from disk, never scraped from stdout.
# ---------------------------------------------------------------------------


def test_passing_suite_passes_for_implementation(python_repo: Path) -> None:
    run = run_harness(python_repo, harness=_harness())
    result = tests_pass_check(_task(), run)

    assert result.name == GateName.TESTS_PASS
    assert result.status == GateStatus.PASSED
    assert run.report is not None, "the JSON report is the evidence; stdout is not"
    assert run.report["summary"]["passed"] == 1


def test_failing_suite_fails_for_implementation(python_repo: Path) -> None:
    _write_test(
        python_repo, "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n"
    )

    run = run_harness(python_repo, harness=_harness())
    result = tests_pass_check(_task(), run)

    assert result.status == GateStatus.FAILED
    assert "test_failed" in _reason_kinds(result)
    assert any("test_add" in reason for reason in result.reasons), (
        f"the failing test must be named: {result.reasons}"
    )


# ---------------------------------------------------------------------------
# AC-3: a collection error is reported distinctly from an assertion failure.
# ---------------------------------------------------------------------------


def test_collection_error_distinct(python_repo: Path) -> None:
    """Section 7.2 gives collection failure its own reason "because the
    correct agent response differs entirely".

    An assertion failure means the code is wrong. A collection error means
    the suite never ran at all -- and an agent told only "tests failed" will
    go looking for a behavioural bug that does not exist.
    """
    _write_test(
        python_repo, "from pkg.nonexistent import gone\n\n\ndef test_x():\n    assert gone\n"
    )

    run = run_harness(python_repo, harness=_harness())
    result = tests_pass_check(_task(), run)

    assert result.status == GateStatus.FAILED
    kinds = _reason_kinds(result)
    assert "collection_error" in kinds, f"collection failure was not reported as such: {kinds}"
    assert "test_failed" not in kinds, (
        "a collection error must not be reported as an assertion failure -- they call for "
        f"different fixes: {result.reasons}"
    )
    assert any("tests/test_pkg.py" in reason for reason in result.reasons), (
        f"the uncollectable file must be named: {result.reasons}"
    )


def test_assertion_failure_is_not_reported_as_collection_error(python_repo: Path) -> None:
    """The converse direction of AC-3, so the two cannot collapse into one."""
    _write_test(
        python_repo, "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n"
    )

    result = tests_pass_check(_task(), run_harness(python_repo, harness=_harness()))

    assert "collection_error" not in _reason_kinds(result)


def test_collection_error_survives_absent_error_key(python_repo: Path) -> None:
    """When collection fails, pytest-json-report writes ``summary`` with no
    ``error`` key at all -- only ``total`` and ``collected``, both 0.

    Section 7.2 says to fail on "``failed > 0`` or ``error > 0``". Read
    literally against this report that is ``0 > 0 or 0 > 0`` -- false -- and a
    suite that never ran would sail through as a pass. Observed, not assumed.
    """
    _write_test(python_repo, "import totally_absent_module\n\n\ndef test_x():\n    assert True\n")

    run = run_harness(python_repo, harness=_harness())

    assert run.report is not None
    assert "error" not in run.report["summary"], (
        "this test encodes an observed report shape; if pytest-json-report now emits "
        "an `error` key the gate's collection handling should be revisited"
    )
    assert tests_pass_check(_task(), run).status == GateStatus.FAILED


# ---------------------------------------------------------------------------
# AC-4: polarity inverts for test_authoring.
# ---------------------------------------------------------------------------


def test_polarity_inverted_for_test_authoring(python_repo: Path) -> None:
    """A ``test_authoring`` task passes only when its tests FAIL.

    Both directions are asserted here, because an implementation that simply
    ignored task type would satisfy either one alone.
    """
    # Direction 1: tests fail -> the task succeeded.
    _write_test(
        python_repo, "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n"
    )
    red = tests_pass_check(
        _task(task_type="test_authoring"), run_harness(python_repo, harness=_harness())
    )
    assert red.status == GateStatus.PASSED, (
        f"a test_authoring task whose tests fail is a success: {red.reasons}"
    )

    # Direction 2: tests pass -> tautology, the task failed.
    _write_test(
        python_repo, "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    green = tests_pass_check(
        _task(task_type="test_authoring"), run_harness(python_repo, harness=_harness())
    )
    assert green.status == GateStatus.FAILED
    assert "tests_not_red" in _reason_kinds(green), (
        f"a test_authoring task whose tests already pass proves nothing: {green.reasons}"
    )


def test_missing_implementation_is_valid_red_for_test_authoring(python_repo: Path) -> None:
    """The bootstrap red state (PROGRESS.md section 6) is a *collection*
    error, not an assertion failure: the tests import a module that does not
    exist yet.

    Decision recorded here because the contract does not settle it: an
    ``ImportError``/``ModuleNotFoundError`` during collection **is** valid red
    for a ``test_authoring`` task. It is precisely the state the two-phase
    protocol tells the agent to leave behind.
    """
    _write_test(
        python_repo,
        "from pkg.not_written_yet import feature\n\n\ndef test_feature():\n    assert feature()\n",
    )

    result = tests_pass_check(
        _task(task_type="test_authoring"), run_harness(python_repo, harness=_harness())
    )

    assert result.status == GateStatus.PASSED, (
        f"a missing implementation is the expected red for test_authoring: {result.reasons}"
    )


def test_broken_test_file_is_not_valid_red_for_test_authoring(python_repo: Path) -> None:
    """The other half of that decision: a ``SyntaxError`` is **not** valid red.

    Both arrive as a collection error with identical report structure -- the
    only difference is the exception type inside ``longrepr``. Accepting both
    would let an agent satisfy a test_authoring task by writing a file that
    does not parse, which is the cheapest possible fake red.
    """
    _write_test(python_repo, "def test_broken(:\n    assert True\n")

    result = tests_pass_check(
        _task(task_type="test_authoring"), run_harness(python_repo, harness=_harness())
    )

    assert result.status == GateStatus.FAILED, (
        "a test file that does not parse is not a legitimate red state"
    )
    assert "invalid_red" in _reason_kinds(result), (
        f"a broken test file must be distinguished from a missing implementation: {result.reasons}"
    )


def test_empty_suite_is_not_valid_red_for_test_authoring(python_repo: Path) -> None:
    """Section 7.2: the gate passes only if the target tests "exist, collected,
    and failed". Deleting every test is not a red state, it is an absence."""
    _write_test(python_repo, "# no tests here at all\n")

    result = tests_pass_check(
        _task(task_type="test_authoring"), run_harness(python_repo, harness=_harness())
    )

    assert result.status == GateStatus.FAILED
    assert result.reasons


# ---------------------------------------------------------------------------
# AC-7: a timeout is a recorded gate failure, never an escaping exception.
# ---------------------------------------------------------------------------


def test_timeout_is_gate_failure(python_repo: Path) -> None:
    """Section 7.3: "A timeout is a gate failure with reason ``timeout``, not
    an exception that kills the run."

    An escaping ``TimeoutExpired`` would abort the whole run over one slow
    suite, losing the proof and the lease along with it.
    """
    _write_test(
        python_repo,
        "import time\n\n\ndef test_slow():\n    time.sleep(60)\n",
    )

    run = run_harness(python_repo, harness=_harness(timeout_s=2))
    result = tests_pass_check(_task(), run)

    assert run.timed_out is True
    assert result.status == GateStatus.FAILED
    assert "timeout" in _reason_kinds(result), (
        f"a timeout must be named as such, not reported as a test failure: {result.reasons}"
    )


def test_run_command_returns_a_result_on_timeout(python_repo: Path) -> None:
    """The primitive underneath: no exception crosses the boundary."""
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=python_repo,
        timeout_s=2,
    )
    assert result.timed_out is True
    assert result.exit_code != 0


def test_run_command_rejects_parent_traversal(python_repo: Path) -> None:
    """Section 7.3: "Reject any configured ``cmd`` containing ``..``"."""
    with pytest.raises(UnsafeHarnessCommandError):
        run_command([sys.executable, "../evil.py"], cwd=python_repo, timeout_s=5)


# ---------------------------------------------------------------------------
# AC-8: the nested runner is isolated from the outer session.
# ---------------------------------------------------------------------------


def test_nested_runner_isolated(python_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every clause here corresponds to an inheritance that was **observed**
    happening, not one that was guessed at.

    The child pytest runs inside a target repository while this process is
    itself under pytest. Left alone it walks up out of the repository looking
    for configuration, honours the outer session's environment, and drops a
    cache directory in the user's tree.

    Note in particular that ``--rootdir`` alone does **not** fix the config
    hijack -- it pins the reported rootdir while ``configfile`` still resolves
    to the ancestor. Only pinning the config file with ``-c`` does.
    """
    # An ancestor config that would deselect everything if it were honoured.
    (python_repo.parent / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-k this_matches_nothing"\n'
        'testpaths = ["nonexistent"]\n',
        encoding="utf-8",
    )
    # An ancestor conftest that would corrupt collection if it were loaded.
    (python_repo.parent / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n    items.clear()\n",
        encoding="utf-8",
    )
    # An outer-session variable that silently rewrites the child's argv.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k this_matches_nothing")

    run = run_harness(python_repo, harness=_harness())

    assert run.report is not None, "the nested run produced no report at all"
    assert run.report["summary"].get("collected") == 1, (
        "the child did not collect the repository's own test -- it inherited the outer "
        f"session's configuration: summary={run.report['summary']}"
    )
    assert run.report["summary"].get("passed") == 1

    # The target repository is the user's tree; the harness does not litter it.
    assert not (python_repo / ".pytest_cache").exists(), (
        "-p no:cacheprovider is missing: the harness wrote a cache into the target repo"
    )

    # The isolation is explicit in the argv, so it is auditable in argv.json
    # rather than being a property someone has to re-derive.
    argv = " ".join(run.argv)
    assert "no:cacheprovider" in argv
    assert "--confcutdir" in argv, "an ancestor conftest.py is loaded without --confcutdir"
    assert "-c" in run.argv, "without -c the child adopts an ancestor config file"


def test_harness_env_is_scrubbed_of_outer_session_state(python_repo: Path) -> None:
    """Section 7.3's scrubbed environment, and G5: a malicious test in a
    target repository must not be able to read the user's credentials out of
    ``os.environ``."""
    from redgear.verifier import harness_env

    env = harness_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "PYTEST_ADDOPTS" not in env
    assert "PYTEST_CURRENT_TEST" not in env
    assert env.get("PYTHONHASHSEED") == "0"
    assert env.get("NO_COLOR") == "1"


def test_harness_env_is_sufficient_to_start_python(python_repo: Path) -> None:
    """A scrubbed environment that cannot launch the interpreter is not a
    safety measure, it is a broken harness.

    On Windows, omitting ``SYSTEMROOT`` makes Python fail to initialise
    Winsock (``OSError: [WinError 10106]``) and pytest dies with an
    INTERNALERROR before collecting anything. Section 7.3's allowlist does not
    mention it; this asserts the harness supplies whatever the platform needs.
    """
    result = run_command(
        [sys.executable, "-c", "import asyncio, socket; print('ok')"],
        cwd=python_repo,
        timeout_s=60,
    )
    assert result.exit_code == 0, (
        f"the scrubbed environment cannot start the interpreter: {result.stderr[-400:]}"
    )
    assert "ok" in result.stdout


def test_report_file_is_written_inside_the_target_repo(python_repo: Path) -> None:
    """The outer session's own report must not be clobbered by the inner run."""
    run = run_harness(python_repo, harness=_harness())
    assert run.report is not None
    assert any("--json-report-file" in arg for arg in run.argv)
    for arg in run.argv:
        if arg.startswith("--json-report-file"):
            written = Path(arg.split("=", 1)[1])
            assert python_repo.resolve() in written.resolve().parents, (
                f"the nested report was written outside the target repo: {written}"
            )


# ---------------------------------------------------------------------------
# Pipeline placement.
# ---------------------------------------------------------------------------


def test_failing_tests_skip_the_later_gates(python_repo: Path) -> None:
    """Section 7.1 short-circuits: criteria and coverage never run once the
    suite is red, and the proof records them as skipped rather than omitting
    them."""
    _write_test(
        python_repo, "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 99\n"
    )

    proof = run_gates(
        python_repo,
        task=_task(writable=["tests/**"]),
        claim=_claim(python_repo),
        declared=["tests/test_pkg.py"],
        attempt=1,
        harness=_harness(),
    )

    by_name = {gate.name: gate for gate in proof.gates}
    assert by_name[GateName.TESTS_PASS].status == GateStatus.FAILED
    for later in GATE_ORDER[4:]:
        assert by_name[later].status == GateStatus.SKIPPED
        assert by_name[later].reasons
