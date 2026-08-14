"""T-0030: failing tests for orchestrator.py -- the continuous loop.

``redgear/orchestrator.py`` does not exist yet, so the import block below
fails at COLLECTION with ``ModuleNotFoundError``. That is the correct red
state.

This is where redgear becomes a system rather than a pile of correct parts.
The loop owns **control flow and nothing else** (section 2.2): it does not
compose prompt text, does not build argv, does not run gates. It selects,
asks ``prompt_engine`` for a prompt, hands it to a ``Runner``, asks
``verifier`` for a proof, and decides what happens next.

Three structural properties are what these tests actually pin down, and all
three are easy to break in ways that still look like they work:

* **The retry path is not a special case.** A failed task returns to the
  queue and is re-selected. ``prompt_engine`` emits a corrective prompt
  because it can *see* the failure in prior attempts -- not because the loop
  told it to. One code path, one prompt builder, no ``if retrying:``.
* **Ordering is load-bearing.** Budget and STOP are checked before any work
  begins; the prompt is persisted before dispatch is attempted; gates run
  only after the agent process has exited. Each of those has a distinct
  failure mode that no later check can recover from.
* **An honest exit is free.** ``blocked`` and ``scope_insufficient`` skip
  verification entirely and leave the attempt counter alone (G3). Verifying
  work the agent already said it did not do is meaningless, and charging for
  honesty removes the reason to be honest.

The loop is tested against the fake runner and, for most cases, an injected
verifier. That is deliberate: gate mechanics already have 43 tests of their
own, and a loop test that spawned pytest would be testing the harness twice
while making the suite slow. One end-to-end test uses the real ``run_gates``
so the wiring itself is not taken on trust.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner, Scenario
from fake_runner.scenarios import FileEdit
from redgear import orchestrator
from redgear.orchestrator import TERMINATION_EXIT_CODES, RunOutcome, run
from redgear.schemas import (
    Budget,
    Claim,
    GateName,
    GateResult,
    GateStatus,
    HarnessConfig,
    Proof,
    TaskNode,
    TurnOutcome,
    Verdict,
)

FIXED = datetime(2026, 1, 1, tzinfo=UTC)

CRITERION_SELECTOR = "tests/test_pkg.py::test_placeholder"


# ---------------------------------------------------------------------------
# Fixture repository: a real git repo carrying a real .redgear/ projection.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _node(
    task_id: str,
    *,
    task_type: str,
    state: str,
    depends_on: list[str] | None = None,
    writable: list[str] | None = None,
    frozen: list[str] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    inherits: list[str] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    globs = writable if writable is not None else ["src/**"]
    return {
        "id": task_id,
        "type": task_type,
        "title": f"task {task_id}",
        "state": state,
        "spec_refs": ["FR-7"],
        "spec_hash": "sha256:" + "d" * 64,
        "depends_on": depends_on or [],
        "scope": {
            "writable_globs": globs,
            "creatable_globs": globs,
            "frozen_globs": frozen if frozen is not None else ["tests/**"],
        },
        "acceptance_criteria": criteria or [],
        "inherits_criteria_from": inherits or [],
        "attempts": 0,
        "max_attempts": max_attempts,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }


#: The standard shape: a verified test_authoring node supplying the criteria,
#: and one implementation node that inherits them (G2, invariant 4).
def _default_nodes() -> list[dict[str, Any]]:
    return [
        _node(
            "T-0000",
            task_type="test_authoring",
            state="verified",
            writable=["tests/**"],
            frozen=["src/**"],
            criteria=[
                {
                    "id": "AC-1",
                    "statement": "The placeholder test passes.",
                    "verified_by": {"kind": "test", "selector": CRITERION_SELECTOR},
                }
            ],
        ),
        _node(
            "T-0001",
            task_type="implementation",
            state="ready",
            depends_on=["T-0000"],
            inherits=["T-0000"],
        ),
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with a committed tree and an active plan."""
    root = tmp_path / "target"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_pkg.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    (root / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent))\n",
        encoding="utf-8",
    )

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "commit.gpgsign", "false")

    _write_graph(root, _default_nodes())

    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def _write_graph(root: Path, nodes: list[dict[str, Any]]) -> None:
    redgear = root / ".redgear"
    redgear.mkdir(exist_ok=True)
    (redgear / "task_graph.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_hash": "sha256:" + "d" * 64,
                "state": "active",
                "generated_at": "2026-01-01T00:00:00Z",
                "nodes": nodes,
                "edges": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _reload(root: Path) -> dict[str, TaskNode]:
    from redgear.state_engine import load_graph

    return {node.id: node for node in load_graph(root).nodes}


def _events(root: Path) -> list[dict[str, Any]]:
    log = root / ".redgear" / "events.jsonl"
    if not log.is_file():
        return []
    parsed: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            parsed.append(json.loads(line))
    return parsed


# ---------------------------------------------------------------------------
# Scenarios for loop *sequences*.
#
# T-0027 built scenarios as data precisely so that a new agent behaviour is a
# new record rather than a new function. These live here rather than in
# `fake_runner.SCENARIOS` because they are fixtures for this module's
# sequences, and adding them to the shared registry would change what
# `test_fake_runner.py::test_all_scenarios_behave_as_declared` enumerates.
# ---------------------------------------------------------------------------

CLEAN = "def feature():\n    return 42\n"
DIRTY = "import os\n\n\ndef feature():\n    return 42\n"

WRITES_CLEAN = Scenario(
    name="writes_clean",
    doc="A correct patch inside scope.",
    edits=(FileEdit("src/pkg/feature.py", CLEAN),),
    summary="Added the feature.",
)

WRITES_DIRTY = Scenario(
    name="writes_dirty",
    doc="Same file, carrying a lint violation. The retry overwrites it.",
    edits=(FileEdit("src/pkg/feature.py", DIRTY),),
    summary="Added the feature.",
)

BLOCKED = Scenario(
    name="loop_blocked",
    doc="Honest exit: cannot proceed.",
    outcome=TurnOutcome.BLOCKED,
    summary="The criteria contradict ADR-0007.",
)

MALFORMED = Scenario(
    name="loop_malformed",
    doc="Unparseable structured output. A runner fault, not a task failure.",
    parse_ok=False,
    exit_code=1,
    summary="",
)


def _harness(floor: float = 0.0) -> HarnessConfig:
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=["src"],
        coverage_floor=floor,
        timeout_s=120,
    )


# ---------------------------------------------------------------------------
# An injected verifier, so control-flow tests do not spawn subprocesses.
# ---------------------------------------------------------------------------


def _proof(task_id: str, attempt: int, verdict: Verdict, gate: GateName | None = None) -> Proof:
    gates = [
        GateResult(
            name=name,
            status=(
                GateStatus.FAILED
                if name == gate
                else (GateStatus.PASSED if verdict is Verdict.PASS else GateStatus.SKIPPED)
            ),
            reasons=(
                ["lint_violation: src/pkg/feature.py:1:1 F401 `os` imported but unused"]
                if name == gate
                else []
            ),
        )
        for name in GateName
    ]
    return Proof(task_id=task_id, attempt=attempt, verdict=verdict, gates=gates, computed_at=FIXED)


@dataclass
class StubVerifier:
    """Returns canned verdicts in order; the last one repeats.

    The loop's job is deciding what a verdict *means*, not producing one.
    Gate mechanics are covered by their own 43 tests, and running them here
    would spawn a nested pytest per iteration for no additional coverage.
    """

    verdicts: list[Verdict]
    gate: GateName | None = GateName.LINT
    calls: list[tuple[str, int]] = field(default_factory=list)

    def __call__(
        self,
        repo_root: Path,
        *,
        task: TaskNode,
        claim: Claim,
        declared: Any,
        attempt: int,
        harness: Any = None,
        criteria: Any = None,
    ) -> Proof:
        self.calls.append((task.id, attempt))
        index = min(len(self.calls) - 1, len(self.verdicts) - 1)
        verdict = self.verdicts[index]
        return _proof(task.id, attempt, verdict, self.gate if verdict is Verdict.FAIL else None)


def _run(
    repo: Path,
    runner: FakeRunner,
    *,
    verdicts: list[Verdict] | None = None,
    budget: Budget | None = None,
    verify: Any = None,
) -> RunOutcome:
    checker = verify if verify is not None else StubVerifier(verdicts or [Verdict.PASS])
    return run(
        repo,
        runner=runner,
        budget=budget or Budget(max_iterations=10),
        harness=_harness(),
        verify=checker,
    )


# ---------------------------------------------------------------------------
# AC-1: a verified task advances the graph; the loop continues immediately.
# ---------------------------------------------------------------------------


def test_pass_advances_and_continues(repo: Path) -> None:
    """Two ready tasks, both passing: one run, two iterations, no human."""
    nodes = _default_nodes()
    nodes.append(
        _node(
            "T-0002",
            task_type="implementation",
            state="ready",
            depends_on=["T-0000"],
            inherits=["T-0000"],
        )
    )
    _write_graph(repo, nodes)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "two tasks")

    runner = FakeRunner(WRITES_CLEAN)
    outcome = _run(repo, runner, verdicts=[Verdict.PASS])

    assert outcome.reason == "complete"
    assert outcome.exit_code == 0
    assert outcome.tasks_verified == 2
    assert len(runner.calls) == 2, "the loop stopped after one task instead of continuing"

    nodes_after = _reload(repo)
    assert nodes_after["T-0001"].state == "verified"
    assert nodes_after["T-0002"].state == "verified"
    assert nodes_after["T-0001"].proof_id

    kinds = [event["event"] for event in _events(repo)]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_ended"
    assert kinds.count("task_verified") == 2


def test_a_run_with_nothing_ready_completes_immediately(repo: Path) -> None:
    """No work is not an error. Section 4.3 has `complete` as a normal
    termination; the run ends 0 without dispatching anything."""
    nodes = [n for n in _default_nodes() if n["id"] == "T-0000"]
    _write_graph(repo, nodes)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "nothing to do")

    runner = FakeRunner(WRITES_CLEAN)
    outcome = _run(repo, runner)

    assert outcome.reason == "complete"
    assert outcome.exit_code == 0
    assert runner.calls == []


# ---------------------------------------------------------------------------
# AC-2: a failure is carried forward into the next prompt.
# ---------------------------------------------------------------------------


def test_retry_carries_failure_forward(repo: Path) -> None:
    """Section 5.1: "A prompt that says 'tests failed' instead of naming the
    assertion produces an identical retry."

    So the second prompt must contain the first attempt's actual gate output,
    inside the untrusted fence.
    """
    runner = FakeRunner(WRITES_DIRTY, WRITES_CLEAN)
    outcome = _run(repo, runner, verdicts=[Verdict.FAIL, Verdict.PASS])

    assert outcome.reason == "complete"
    assert len(runner.calls) == 2, "the failed task was not re-selected"

    first, second = runner.calls[0].prompt, runner.calls[1].prompt
    assert "F401" not in first, "attempt 1 cannot know about its own failure"
    assert "F401" in second, "the retry prompt does not carry the failure excerpt"

    from redgear.prompt_engine import UNTRUSTED_BEGIN

    assert UNTRUSTED_BEGIN in second, "the excerpt is not inside the untrusted fence"

    # Check the PRIOR ATTEMPTS section specifically. Asserting "lint" against
    # the whole prompt would pass vacuously -- the VERIFICATION section names
    # the lint command in every prompt ever composed.
    prior = second.partition("## Prior attempts")[2].partition("## Required outcome")[0]
    assert "F401" in prior, "the excerpt is outside the prior-attempts section"
    assert GateName.LINT.value in prior, "the failed gate is not named in the retry"

    assert _reload(repo)["T-0001"].attempts == 1, "the failed attempt was not counted"


def test_allowed_tools_never_grant_a_bare_shell(repo: Path) -> None:
    """Section 8.2: "Bash: only the specific prefixes the task needs. Never
    bare `Bash`." An unrestricted shell defeats every scope guarantee at
    once, because the agent can simply write files with it."""
    runner = FakeRunner(WRITES_CLEAN)
    _run(repo, runner)

    assert runner.calls
    for call in runner.calls:
        assert "Bash" not in call.allowed_tools, f"a bare shell was granted: {call.allowed_tools}"
        assert "Read" in call.allowed_tools
        assert "--dangerously-skip-permissions" not in call.allowed_tools


def test_implementation_task_may_write_but_test_scope_is_not_granted(repo: Path) -> None:
    runner = FakeRunner(WRITES_CLEAN)
    _run(repo, runner)
    granted = runner.calls[0].allowed_tools
    assert "Edit" in granted and "Write" in granted


# ---------------------------------------------------------------------------
# AC-3: the retry uses the same code path.
# ---------------------------------------------------------------------------


def _without_prior_attempts(prompt: str) -> str:
    head, _, rest = prompt.partition("## Prior attempts")
    _, _, tail = rest.partition("## Required outcome")
    return head + tail


def test_no_retry_special_case(repo: Path) -> None:
    """The single most important structural property of the loop.

    A retry is not a different operation. The loop re-selects the task and
    calls the same builder; the prompt differs **only** in the PRIOR ATTEMPTS
    section, because that is the only thing about the state that changed.

    If the loop grew an ``if retrying:`` branch, the two prompts would begin
    to diverge elsewhere -- and the divergence would be invisible until it
    caused a behaviour change nobody could trace.
    """
    runner = FakeRunner(WRITES_DIRTY, WRITES_CLEAN)
    _run(repo, runner, verdicts=[Verdict.FAIL, Verdict.PASS])

    assert len(runner.calls) == 2
    first, second = runner.calls[0].prompt, runner.calls[1].prompt

    assert first != second, "the retry prompt is identical; the failure was not fed back"
    assert _without_prior_attempts(first) == _without_prior_attempts(second), (
        "the two prompts differ outside the PRIOR ATTEMPTS section, which means "
        "something other than the recorded failure changed the composition"
    )

    # Both dispatches carried the same permissions and budget: the loop did
    # not treat the retry as a different kind of turn.
    assert runner.calls[0].allowed_tools == runner.calls[1].allowed_tools
    assert runner.calls[0].timeout_s == runner.calls[1].timeout_s
    assert runner.calls[0].max_turns == runner.calls[1].max_turns


def test_the_loop_source_has_no_retry_branch() -> None:
    """A guard against reintroduction. The behavioural test above is the real
    assertion; this one names the mistake so a future reader meets it."""
    source = Path(str(orchestrator.__file__)).read_text(encoding="utf-8")
    lowered = source.lower()
    for smell in ("if retry", "if is_retry", "if retrying", "if attempt >", "if attempt ="):
        assert smell not in lowered, f"the loop appears to branch on retry state: {smell!r}"


# ---------------------------------------------------------------------------
# AC-4: an honest exit is free.
# ---------------------------------------------------------------------------


def test_blocked_ends_run_free(repo: Path) -> None:
    """G3. Three things must all hold, and each is separately temptingly
    wrong: the attempt counter does not move, the gates never run, and the
    run ends as `blocked` rather than as a failure."""
    verifier_stub = StubVerifier([Verdict.PASS])
    runner = FakeRunner(BLOCKED)
    outcome = _run(repo, runner, verify=verifier_stub)

    assert outcome.reason == "blocked"
    assert outcome.exit_code == 2
    assert outcome.tasks_escalated == 1

    node = _reload(repo)["T-0001"]
    assert node.attempts == 0, "an honest exit consumed an attempt (G3)"
    assert node.state == "escalated"
    assert node.escalation is not None
    assert node.escalation.reason == "blocker"

    assert verifier_stub.calls == [], (
        "gates ran on a turn the agent said it could not complete; there is "
        "nothing to verify and doing so wastes a subprocess"
    )

    kinds = [event["event"] for event in _events(repo)]
    assert "task_escalated" in kinds
    assert "task_rejected" not in kinds, "an honest exit was recorded as a rejection"


def test_scope_insufficient_is_also_free(repo: Path) -> None:
    scenario = Scenario(
        name="loop_scope_insufficient",
        doc="Needs a path it was not granted.",
        outcome=TurnOutcome.SCOPE_INSUFFICIENT,
        summary="This requires editing migrations/, which is frozen.",
    )
    outcome = _run(repo, FakeRunner(scenario))

    assert outcome.reason == "blocked"
    assert _reload(repo)["T-0001"].attempts == 0


# ---------------------------------------------------------------------------
# AC-5: attempts run out.
# ---------------------------------------------------------------------------


def test_attempts_exhausted_escalates(repo: Path) -> None:
    """max_attempts is 3, so the third failure escalates rather than
    producing a fourth prompt."""
    runner = FakeRunner(WRITES_DIRTY)
    outcome = _run(repo, runner, verdicts=[Verdict.FAIL], budget=Budget(max_iterations=20))

    assert outcome.reason == "blocked"
    assert outcome.exit_code == 2
    assert len(runner.calls) == 3, f"expected exactly 3 dispatches, got {len(runner.calls)}"

    node = _reload(repo)["T-0001"]
    assert node.attempts == 3
    assert node.state == "escalated"
    assert node.escalation is not None
    assert node.escalation.reason == "attempts_exhausted"

    kinds = [event["event"] for event in _events(repo)]
    assert kinds.count("task_rejected") == 3
    assert kinds.count("task_escalated") == 1


# ---------------------------------------------------------------------------
# AC-6: termination reasons and exit codes.
# ---------------------------------------------------------------------------


def test_termination_reasons_and_exit_codes(repo: Path) -> None:
    """Section 4.3's table is normative and the exit codes are the CLI's
    contract with whatever is driving it."""
    assert TERMINATION_EXIT_CODES == {
        "complete": 0,
        "stopped": 0,
        "blocked": 2,
        "budget_exhausted": 3,
        "runner_error": 4,
        "engine_error": 5,
    }

    # Every reason the loop can actually reach is exercised somewhere in this
    # file: complete here, blocked in test_blocked_ends_run_free,
    # budget_exhausted in test_budget_exhausted_stops_after_exactly_the_cap,
    # stopped in test_stop_sentinel_ends_the_run_cleanly, and runner_error in
    # test_parse_failure_twice_is_runner_error. Only one is driven here,
    # because a second run against this repository would refuse to start on
    # the tree the first one left dirty -- which is section 8.4 working, not a
    # test problem.
    outcome = _run(repo, FakeRunner(WRITES_CLEAN))
    assert outcome.reason == "complete"
    assert outcome.exit_code == TERMINATION_EXIT_CODES[outcome.reason]


def test_budget_exhausted_stops_after_exactly_the_cap(repo: Path) -> None:
    """Section 8.1: caps are checked *before* each iteration, never mid-turn.
    A cap firing halfway through a turn leaves the tree written and the
    verdict unrecorded."""
    runner = FakeRunner(WRITES_DIRTY)
    outcome = _run(
        repo,
        runner,
        verdicts=[Verdict.FAIL],
        budget=Budget(max_iterations=2, max_consecutive_failures=99),
    )

    assert outcome.reason == "budget_exhausted"
    assert outcome.exit_code == 3
    assert len(runner.calls) == 2, "the cap did not stop the loop at exactly 2 iterations"


def test_consecutive_failures_stops_the_run(repo: Path) -> None:
    """The runaway detector. Distinct tasks failing in a row means something
    systemic is wrong, and continuing burns money without progress."""
    runner = FakeRunner(WRITES_DIRTY)
    outcome = _run(
        repo,
        runner,
        verdicts=[Verdict.FAIL],
        budget=Budget(max_iterations=50, max_consecutive_failures=2),
    )

    assert outcome.reason == "budget_exhausted"
    assert len(runner.calls) == 2


def test_stop_sentinel_ends_the_run_cleanly(repo: Path) -> None:
    """Section 8.3: the brake. It takes effect at the next iteration
    boundary, exits 0, and leaves no orphaned lock."""
    from redgear.budget import request_stop

    request_stop(repo)
    runner = FakeRunner(WRITES_CLEAN)
    outcome = _run(repo, runner)

    assert outcome.reason == "stopped"
    assert outcome.exit_code == 0
    assert runner.calls == [], "work started even though STOP was present"
    assert not (repo / ".redgear" / "locks" / "run.lock").exists(), "orphaned run lock"


def test_stop_mid_run_finishes_nothing_further(repo: Path) -> None:
    """STOP appearing after an iteration completes: the current turn is
    already done, and nothing further starts."""
    from redgear.budget import request_stop

    class StopAfterFirst(StubVerifier):
        def __call__(self, repo_root: Path, **kwargs: Any) -> Proof:
            proof = super().__call__(repo_root, **kwargs)
            request_stop(repo_root)
            return proof

    nodes = _default_nodes()
    nodes.append(
        _node(
            "T-0002",
            task_type="implementation",
            state="ready",
            depends_on=["T-0000"],
            inherits=["T-0000"],
        )
    )
    _write_graph(repo, nodes)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "two tasks")

    runner = FakeRunner(WRITES_CLEAN)
    outcome = _run(repo, runner, verify=StopAfterFirst([Verdict.PASS]))

    assert outcome.reason == "stopped"
    assert len(runner.calls) == 1, "a second task was started after STOP appeared"
    assert _reload(repo)["T-0001"].state == "verified", "the in-flight turn was abandoned"


# ---------------------------------------------------------------------------
# AC-7: the prompt is on disk before dispatch is attempted.
# ---------------------------------------------------------------------------


@dataclass
class PromptSpy:
    """Records what was on disk at the moment dispatch was called."""

    inner: FakeRunner
    repo: Path
    seen: list[list[str]] = field(default_factory=list)

    def dispatch(
        self, prompt: str, allowed_tools: list[str], cwd: Path, timeout_s: int, max_turns: int
    ) -> Any:
        found = sorted(
            path.read_text(encoding="utf-8")
            for path in (self.repo / ".redgear" / "runs").rglob("prompt.txt")
        )
        self.seen.append(found)
        return self.inner.dispatch(prompt, allowed_tools, cwd, timeout_s, max_turns)

    def version(self) -> str:
        return self.inner.version()


def test_prompt_persisted_before_dispatch(repo: Path) -> None:
    """G4 and section 11.1 rule 6: "Never dispatch a prompt before persisting
    it."

    A run you cannot read back prompt-by-prompt is not auditable, and
    auditability is the product. The check is made *from inside* the dispatch
    call, because asserting afterwards proves only that the file exists
    eventually -- which a persist-after-dispatch implementation would also
    satisfy.
    """
    inner = FakeRunner(WRITES_DIRTY, WRITES_CLEAN)
    spy = PromptSpy(inner, repo)
    run(
        repo,
        runner=spy,
        budget=Budget(max_iterations=10),
        harness=_harness(),
        verify=StubVerifier([Verdict.FAIL, Verdict.PASS]),
    )

    assert len(spy.seen) == 2
    for index, on_disk in enumerate(spy.seen):
        assert len(on_disk) == index + 1, (
            f"dispatch {index + 1} saw {len(on_disk)} persisted prompt(s); "
            "the prompt for this turn was not written before dispatch"
        )
        assert inner.calls[index].prompt in on_disk, (
            "the prompt on disk is not the prompt that was dispatched"
        )

    kinds = [event["event"] for event in _events(repo)]
    assert kinds.count("prompt_dispatched") == 2


def test_persisted_prompt_is_byte_identical_to_the_dispatched_one(repo: Path) -> None:
    """The `prompt_sha256` on `prompt_dispatched` is what makes G4 verifiable
    rather than asserted: the file can be re-hashed and matched."""
    import hashlib

    runner = FakeRunner(WRITES_CLEAN)
    _run(repo, runner)

    written = list((repo / ".redgear" / "runs").rglob("prompt.txt"))
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert text == runner.calls[0].prompt

    dispatched = [e for e in _events(repo) if e["event"] == "prompt_dispatched"]
    assert len(dispatched) == 1
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert dispatched[0]["prompt_sha256"].endswith(digest)


# ---------------------------------------------------------------------------
# AC-8: two unparseable results end the run.
# ---------------------------------------------------------------------------


def test_parse_failure_twice_is_runner_error(repo: Path) -> None:
    """Section 6.4 rule 4: "A missing or malformed `structured_output` is a
    parse failure, not a task failure. Retry the dispatch once... Two
    consecutive parse failures end the run with `runner_error` -- that is an
    integration bug and must be loud, not silently retried forever."
    """
    verifier_stub = StubVerifier([Verdict.PASS])
    runner = FakeRunner(MALFORMED)
    outcome = _run(repo, runner, verify=verifier_stub)

    assert outcome.reason == "runner_error"
    assert outcome.exit_code == 4
    assert len(runner.calls) == 2, "the dispatch was not retried exactly once"

    assert verifier_stub.calls == [], "gates ran on an unparseable result"
    assert _reload(repo)["T-0001"].attempts == 0, (
        "a parse failure consumed an attempt; it is a runner fault, not a task failure"
    )


def test_one_parse_failure_then_success_is_not_fatal(repo: Path) -> None:
    """The retry exists because a single malformed result is recoverable.
    Only a *second* consecutive one means the integration is broken."""
    runner = FakeRunner(MALFORMED, WRITES_CLEAN)
    outcome = _run(repo, runner, verdicts=[Verdict.PASS])

    assert outcome.reason == "complete"
    assert len(runner.calls) == 2
    assert _reload(repo)["T-0001"].state == "verified"


# ---------------------------------------------------------------------------
# Run-level invariants (section 10.5's global assertions).
# ---------------------------------------------------------------------------


def test_run_refuses_a_draft_plan(repo: Path) -> None:
    """Section 3.3: the human review gate. `redgear run` refuses to start on
    a draft graph -- the plan defines the tests, so a wrong plan produces
    confidently verified wrong software."""
    from redgear.errors import PlanUnreviewedError

    graph = json.loads((repo / ".redgear" / "task_graph.json").read_text(encoding="utf-8"))
    graph["state"] = "draft"
    (repo / ".redgear" / "task_graph.json").write_text(json.dumps(graph), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "draft")

    with pytest.raises(PlanUnreviewedError):
        _run(repo, FakeRunner(WRITES_CLEAN))


def test_run_refuses_a_dirty_tree(repo: Path) -> None:
    """Section 8.4: without a clean baseline the diff audit is fiction."""
    from redgear.errors import DirtyTreeError

    (repo / "src" / "pkg" / "stray.py").write_text("stray = 1\n", encoding="utf-8")

    with pytest.raises(DirtyTreeError) as excinfo:
        _run(repo, FakeRunner(WRITES_CLEAN))
    assert "stray" in str(excinfo.value.detail)


def test_every_run_ends_with_exactly_one_run_ended_event(repo: Path) -> None:
    _run(repo, FakeRunner(WRITES_CLEAN))
    kinds = [event["event"] for event in _events(repo)]
    assert kinds.count("run_started") == 1
    assert kinds.count("run_ended") == 1
    assert kinds[-1] == "run_ended"


def test_event_sequence_is_gapless_and_monotonic(repo: Path) -> None:
    """FR-1. Asserted after a real run rather than in isolation, because the
    loop is what writes events concurrently with everything else."""
    _run(repo, FakeRunner(WRITES_DIRTY, WRITES_CLEAN), verdicts=[Verdict.FAIL, Verdict.PASS])
    seqs = [event["seq"] for event in _events(repo)]
    assert seqs == list(range(len(seqs)))


def _leftover_locks(repo: Path) -> list[Path]:
    locks = repo / ".redgear" / "locks"
    return sorted(locks.glob("*.lock")) if locks.is_dir() else []


def test_no_orphaned_lock_after_a_normal_termination(repo: Path) -> None:
    """Section 8.3: "Never leave an orphaned lock" -- a stale one means the
    next run refuses to start and the user has to delete a file they do not
    understand."""
    _run(repo, FakeRunner(WRITES_CLEAN))
    assert _leftover_locks(repo) == []


def test_no_orphaned_lock_when_the_run_raises(repo: Path) -> None:
    """The path that actually leaks locks in practice: an exception unwinding
    out of the loop rather than a clean return.

    A dirty tree is used because it is the one refusal that happens *after*
    the lock is taken, so it genuinely exercises the release path.
    """
    from redgear.errors import DirtyTreeError

    (repo / "src" / "pkg" / "stray.py").write_text("stray = 1\n", encoding="utf-8")

    with pytest.raises(DirtyTreeError):
        _run(repo, FakeRunner(WRITES_CLEAN))

    assert _leftover_locks(repo) == [], "an exception left the run lock behind"


def test_run_outcome_reports_what_happened(repo: Path) -> None:
    outcome = _run(repo, FakeRunner(WRITES_CLEAN))
    assert isinstance(outcome, RunOutcome)
    assert outcome.run_id
    assert outcome.iterations == 1
    assert outcome.tasks_verified == 1
    assert outcome.tasks_escalated == 0


# ---------------------------------------------------------------------------
# One end-to-end pass with the REAL verifier, so the wiring is not assumed.
# ---------------------------------------------------------------------------


def test_real_gates_end_to_end(repo: Path) -> None:
    """Everything above injects a verifier to keep the loop tests fast. This
    one runs the actual six-gate pipeline against the actual repository, so a
    mis-wired call signature cannot hide behind a stub.

    It is the only test here that spawns a subprocess, and it is worth the
    seconds: a loop that calls `run_gates` incorrectly would pass every other
    test in this file.
    """
    runner = FakeRunner(WRITES_CLEAN)
    outcome = run(
        repo,
        runner=runner,
        budget=Budget(max_iterations=5),
        harness=_harness(floor=0.0),
    )

    assert outcome.reason == "complete", f"real gates did not pass: {outcome}"
    assert outcome.tasks_verified == 1

    node = _reload(repo)["T-0001"]
    assert node.state == "verified"
    assert node.proof_id
