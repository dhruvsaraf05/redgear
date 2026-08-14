"""T-0026: failing tests for the runner protocol and the deterministic fake.

``redgear/runner.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

Milestone 5 is deliberately placed *before* the gates and the loop. The
reasoning in section 12 is worth restating because it shapes what these tests
assert: build the fake first and every later component is written against a
caller that already exists; build it last and you write tests asserting
whatever the code happens to do.

So this file is really pinning down three seams:

* **The protocol is structural, not nominal** (NFR-8). The orchestrator is
  typed against ``Runner`` and imports no concrete runner. ``FakeRunner``
  satisfies it without inheriting from it, which is the property that lets the
  Claude Code adapter at T-0034 drop in unchanged.
* **A scenario's declaration is a claim, not the truth** (G1). The fake can
  lie about what it changed, because catching a lying agent is the entire
  product and a rig that only tells the truth cannot exercise it.
* **Nothing spawns and nothing dials out** (section 10.4, NFR-6). Asserted by
  breaking the primitives, not by inspection.
"""

from __future__ import annotations

import inspect
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fake_runner import ALL_SCENARIOS, SCENARIOS, FakeRunner, Scenario
from redgear.runner import Runner
from redgear.schemas import TurnOutcome, TurnResult


def _dispatch(runner: FakeRunner, cwd: Path) -> TurnResult:
    """The protocol call, with the arguments the orchestrator will pass."""
    return runner.dispatch(
        prompt="<the composed prompt>",
        allowed_tools=["Read", "Edit", "Write"],
        cwd=cwd,
        timeout_s=900,
        max_turns=25,
    )


# ---------------------------------------------------------------------------
# AC-1: the fake satisfies the protocol and applies canned changes.
# ---------------------------------------------------------------------------


def test_fake_satisfies_protocol(tmp_path: Path) -> None:
    """Structural conformance, then behaviour.

    ``isinstance`` against a runtime-checkable Protocol only proves the method
    *names* exist, so the signatures are compared explicitly. A fake whose
    ``dispatch`` took different parameters would satisfy ``isinstance`` and
    then fail the moment the orchestrator called it by keyword.
    """
    runner = FakeRunner(SCENARIOS["happy_implementation"])

    assert isinstance(runner, Runner), "FakeRunner does not structurally satisfy Runner"

    # Same parameters, same order, on the method the orchestrator actually calls.
    protocol_params = list(inspect.signature(Runner.dispatch).parameters)
    fake_params = list(inspect.signature(FakeRunner.dispatch).parameters)
    assert fake_params == protocol_params, (
        f"dispatch signature drifted from the protocol: {fake_params} != {protocol_params}"
    )
    assert list(inspect.signature(Runner.version).parameters) == list(
        inspect.signature(FakeRunner.version).parameters
    )

    # And it does the work: a canned patch really lands on disk.
    result = _dispatch(runner, tmp_path)

    written = tmp_path / "src" / "pkg" / "feature.py"
    assert written.is_file(), "the canned patch was not applied"
    assert "def feature()" in written.read_text(encoding="utf-8")
    assert isinstance(result, TurnResult)
    assert result.outcome is TurnOutcome.COMPLETED
    assert runner.version()


def test_protocol_is_satisfied_without_inheritance() -> None:
    """NFR-8: adapters conform **structurally**.

    The two assertions are the inversion that matters. ``issubclass`` against
    a runtime-checkable Protocol is itself a structural test and returns True
    here -- it is not evidence about inheritance. The nominal question has to
    be asked of the MRO, and the answer must be no: if conformance required
    inheriting, every adapter would have to import the package to satisfy the
    seam, and the seam would be nominal rather than structural.
    """
    assert issubclass(FakeRunner, Runner), "structural conformance"
    assert Runner not in FakeRunner.__mro__, "conformance must not come from inheritance"
    assert FakeRunner.__bases__ == (object,)


def test_dispatch_arguments_are_recorded(tmp_path: Path) -> None:
    """The orchestrator's own contract is asserted through this recording:
    that prompt 2 carries the failure excerpt, that ``allowed_tools`` never
    contains a bare ``Bash``, that budgets were passed through. Those tests
    (T-0030) have nowhere to look without it."""
    runner = FakeRunner(SCENARIOS["happy_implementation"])
    _dispatch(runner, tmp_path)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.prompt == "<the composed prompt>"
    assert call.allowed_tools == ("Read", "Edit", "Write")
    assert call.cwd == tmp_path
    assert call.timeout_s == 900
    assert call.max_turns == 25


def test_scenarios_are_queued_and_the_last_repeats(tmp_path: Path) -> None:
    """Section 10.5's ``retry_then_succeed`` and ``exhausts_attempts`` are
    about a *sequence* of turns, not one turn. Both fall out of "next
    scenario, then the final one forever"."""
    runner = FakeRunner(
        SCENARIOS["writes_out_of_scope"],
        SCENARIOS["happy_implementation"],
    )

    first = _dispatch(runner, tmp_path)
    second = _dispatch(runner, tmp_path)
    third = _dispatch(runner, tmp_path)

    assert first.changed_files == ["README.md"]
    assert second.changed_files == ["src/pkg/feature.py"]
    assert third.changed_files == ["src/pkg/feature.py"], "the final scenario must repeat"
    assert len(runner.calls) == 3


def test_dispatch_is_deterministic(tmp_path: Path) -> None:
    """No clock, no randomness (section 10.5). Two runs of one scenario must
    be byte-identical, because G4's replay test compares event streams
    exactly and a varying duration would break it."""
    left = _dispatch(FakeRunner(SCENARIOS["happy_implementation"]), tmp_path / "a")
    right = _dispatch(FakeRunner(SCENARIOS["happy_implementation"]), tmp_path / "b")

    assert left.model_dump() == right.model_dump()


def test_a_runner_needs_at_least_one_scenario() -> None:
    with pytest.raises(ValueError, match="at least one scenario"):
        FakeRunner()


# ---------------------------------------------------------------------------
# AC-2: every declared scenario produces its documented outcome.
# ---------------------------------------------------------------------------

#: Literal expectations, written out rather than derived from the scenario
#: records. A test that recomputes its expectations from the same data the
#: implementation reads proves only that one function was called twice.
EXPECTED: dict[str, tuple[list[str], list[str], TurnOutcome]] = {
    # name: (files really on disk, files the agent declares, outcome)
    "happy_implementation": (
        ["src/pkg/feature.py"],
        ["src/pkg/feature.py"],
        TurnOutcome.COMPLETED,
    ),
    "happy_test_authoring": (
        ["tests/test_feature.py"],
        ["tests/test_feature.py"],
        TurnOutcome.COMPLETED,
    ),
    "writes_out_of_scope": (["README.md"], ["README.md"], TurnOutcome.COMPLETED),
    "adds_frozen_file": (
        ["tests/test_sneaky.py"],
        ["tests/test_sneaky.py"],
        TurnOutcome.COMPLETED,
    ),
    "touches_frozen_test": (
        ["tests/test_pkg.py"],
        ["tests/test_pkg.py"],
        TurnOutcome.COMPLETED,
    ),
    # The file is *deleted*, so nothing is expected on disk. The absence
    # itself is asserted by test_deleting_a_frozen_test_really_removes_it,
    # which this table cannot express.
    "deletes_frozen_test": ([], ["tests/test_pkg.py"], TurnOutcome.COMPLETED),
    "undeclared_change": (
        ["src/pkg/one.py", "src/pkg/two.py"],
        ["src/pkg/one.py"],
        TurnOutcome.COMPLETED,
    ),
    "phantom_change": (
        ["src/pkg/real.py"],
        ["src/pkg/ghost.py", "src/pkg/real.py"],
        TurnOutcome.COMPLETED,
    ),
    "lint_dirty": (["src/pkg/messy.py"], ["src/pkg/messy.py"], TurnOutcome.COMPLETED),
    "returns_blocked": ([], [], TurnOutcome.BLOCKED),
    "returns_scope_insufficient": ([], [], TurnOutcome.SCOPE_INSUFFICIENT),
    "malformed_output": ([], [], TurnOutcome.COMPLETED),
}


def test_all_scenarios_behave_as_declared(tmp_path: Path) -> None:
    """Every scenario in the registry, checked against a literal expectation.

    The two file lists are deliberately separate. Where they differ the
    scenario is modelling a *lying* agent, and that difference is the whole
    reason gate 1 cross-checks the claim against the real diff (G1).
    """
    for scenario in ALL_SCENARIOS:
        assert scenario.name in EXPECTED, (
            f"scenario {scenario.name!r} has no declared expectation; add one "
            "rather than letting a new behaviour go unasserted"
        )

        workspace = tmp_path / scenario.name
        workspace.mkdir()
        # `deletes_frozen_test` needs something to delete.
        seeded = workspace / "tests" / "test_pkg.py"
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

        result = _dispatch(FakeRunner(scenario), workspace)
        on_disk, declared, outcome = EXPECTED[scenario.name]

        assert result.outcome is outcome, f"{scenario.name}: wrong outcome"
        assert result.changed_files == declared, f"{scenario.name}: wrong declaration"
        for path in on_disk:
            assert (workspace / path).is_file(), f"{scenario.name}: {path} was not written"


def test_deleting_a_frozen_test_really_removes_it(tmp_path: Path) -> None:
    """Handled separately because its expectation is an *absence*, which the
    table above cannot express. This is the violation gate 2 catches that
    gate 1's glob logic cannot see."""
    victim = tmp_path / "tests" / "test_pkg.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

    result = _dispatch(FakeRunner(SCENARIOS["deletes_frozen_test"]), tmp_path)

    assert not victim.exists(), "the scenario claims a deletion it did not perform"
    assert result.changed_files == ["tests/test_pkg.py"]


def test_touching_a_frozen_test_rewrites_it(tmp_path: Path) -> None:
    victim = tmp_path / "tests" / "test_pkg.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("def test_real():\n    assert compute() == 42\n", encoding="utf-8")

    _dispatch(FakeRunner(SCENARIOS["touches_frozen_test"]), tmp_path)

    assert "assert True" in victim.read_text(encoding="utf-8"), (
        "the frozen test was not actually weakened, so gate 2 would have nothing to catch"
    )


def test_lying_scenarios_really_disagree_with_the_disk(tmp_path: Path) -> None:
    """The property that makes this rig useful: for these two scenarios the
    claim and the truth must NOT match. A fake that always told the truth
    could not exercise the gate that catches an agent which does not."""
    undeclared = _dispatch(FakeRunner(SCENARIOS["undeclared_change"]), tmp_path / "u")
    assert "src/pkg/two.py" not in undeclared.changed_files
    assert (tmp_path / "u" / "src" / "pkg" / "two.py").is_file()

    phantom = _dispatch(FakeRunner(SCENARIOS["phantom_change"]), tmp_path / "p")
    assert "src/pkg/ghost.py" in phantom.changed_files
    assert not (tmp_path / "p" / "src" / "pkg" / "ghost.py").exists()


def test_blocked_carries_a_category_and_detail() -> None:
    """G3: ``blocked`` is a normal outcome, not an error -- but it has to be
    actionable, because a human is about to read it and intervene."""
    scenario = SCENARIOS["returns_blocked"]
    assert scenario.outcome is TurnOutcome.BLOCKED
    assert scenario.blocker_category is not None
    assert scenario.blocker_detail


def test_malformed_output_is_a_parse_failure_not_a_task_failure(tmp_path: Path) -> None:
    """Section 6.4 rule 4: a missing structured result is a runner-level
    fault. The orchestrator retries the dispatch once and then ends the run
    with ``runner_error`` -- it must not be mistaken for a failed task."""
    result = _dispatch(FakeRunner(SCENARIOS["malformed_output"]), tmp_path)
    assert result.parse_ok is False
    assert result.exit_code != 0


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
def test_every_scenario_is_documented(scenario: Scenario) -> None:
    """A scenario nobody can explain is a scenario nobody can debug when it
    starts failing three milestones from now."""
    assert scenario.doc.strip(), f"{scenario.name} has no doc"
    assert scenario.name.strip()


# ---------------------------------------------------------------------------
# AC-3: no test path spawns a process or opens a network connection.
# ---------------------------------------------------------------------------


def test_no_subprocess_or_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted by breaking the primitives, not by reading the source.

    Section 10.4 forbids a test from calling a real agent CLI, and G5 forbids
    the package from opening a connection at all. An inspection-based check
    would pass forever while an indirect import quietly reintroduced either.
    So every spawn and connect entry point is replaced with something that
    raises, and then every scenario is run.

    The patching happens inside the test body on purpose: the fixtures above
    it are allowed to use git, and only the fake's own behaviour is under
    scrutiny here.
    """
    calls: list[str] = []

    def forbidden(name: str) -> Any:
        def boom(*args: object, **kwargs: object) -> Any:
            calls.append(name)
            raise AssertionError(f"the fake runner reached {name}, which section 10.4 forbids")

        return boom

    for attribute in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, attribute, forbidden(f"subprocess.{attribute}"))
    for attribute in ("system", "popen", "execv", "spawnv"):
        if hasattr(os, attribute):
            monkeypatch.setattr(os, attribute, forbidden(f"os.{attribute}"))
    monkeypatch.setattr(socket, "socket", forbidden("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))

    for scenario in ALL_SCENARIOS:
        workspace = tmp_path / scenario.name
        workspace.mkdir()
        seeded = workspace / "tests" / "test_pkg.py"
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

        result = _dispatch(FakeRunner(scenario), workspace)
        assert isinstance(result, TurnResult)

    assert calls == [], f"forbidden primitives were reached: {calls}"


def test_the_fake_imports_no_network_module() -> None:
    """Belt and braces alongside the behavioural check above, and the same
    rule section 10.3 greps for across the package."""
    import fake_runner
    from fake_runner import runner as fake_runner_module
    from fake_runner import scenarios as scenarios_module

    for module in (fake_runner, fake_runner_module, scenarios_module):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        for banned in ("import socket", "import httpx", "import urllib", "import requests"):
            assert banned not in source, f"{module.__name__} contains {banned!r}"
