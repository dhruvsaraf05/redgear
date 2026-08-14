"""A ``Runner`` that applies a canned patch instead of spawning an agent.

Section 10.5: "It never spawns a process: it applies a canned patch to the
working tree and returns a canned ``TurnResult``."

Two design points are worth stating because the orchestrator depends on them.

**Scenarios are queued, and the last one repeats.** Several section 10.5
scenarios are about a *sequence* of turns rather than a single one --
``retry_then_succeed`` needs fail-then-pass, ``exhausts_attempts`` needs the
same failure three times. Modelling a dispatch as "the next scenario, or the
final one forever" covers both without the orchestrator's tests needing to
know anything about how the fake is wired.

**Every dispatch is recorded.** ``FakeRunner.calls`` is what lets a test assert
things the loop is actually responsible for: that prompt 2 contains the failure
excerpt from attempt 1, that ``allowed_tools`` was derived from the task's
scope and never contains a bare ``Bash``, that the prompt was persisted before
dispatch. Without the recording those assertions have nowhere to look.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from redgear.schemas import TurnResult
from .scenarios import Scenario

#: Fixed, because section 10.5's determinism requirement means no clock and no
#: randomness anywhere in this module. A duration that varied would make a
#: recorded event stream differ between runs, and G4's replay test compares
#: byte-for-byte.
_FAKE_DURATION_MS = 0
_FAKE_COST_USD = 0.0


@dataclass(frozen=True)
class DispatchCall:
    """Exactly what the orchestrator handed the runner, kept for assertions."""

    prompt: str
    allowed_tools: tuple[str, ...]
    cwd: Path
    timeout_s: int
    max_turns: int


def apply_scenario(scenario: Scenario, cwd: Path) -> TurnResult:
    """Apply the canned patch and build the canned result.

    Writing happens first so that a scenario which both edits files *and*
    reports ``blocked`` behaves like a real agent that got part-way and then
    stopped -- the working tree is dirty and the outcome is honest, which is a
    combination the orchestrator has to handle.
    """
    for edit in scenario.edits:
        target = cwd / edit.path
        if edit.is_delete:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the content byte-identical across platforms. Without
        # it Python would translate "\n" to "\r\n" on Windows, and a frozen
        # file's digest would differ from the same scenario's on Linux.
        target.write_text(edit.content or "", encoding="utf-8", newline="")

    return TurnResult(
        outcome=scenario.outcome,
        summary=scenario.summary,
        # A CLAIM, deliberately computed from the scenario's declaration rather
        # than from what was written (G1). This is what lets a scenario lie.
        changed_files=list(scenario.declared_files),
        known_gaps=list(scenario.known_gaps),
        blocker_category=scenario.blocker_category,
        blocker_detail=scenario.blocker_detail,
        exit_code=scenario.exit_code,
        session_id=f"fake-session-{scenario.name}",
        num_turns=1,
        duration_ms=_FAKE_DURATION_MS,
        cost_usd_estimate=_FAKE_COST_USD,
        raw_stdout_path=f"<fake-runner:{scenario.name}>",
        parse_ok=scenario.parse_ok,
    )


class FakeRunner:
    """The section 10.5 test rig. Satisfies ``redgear.runner.Runner``.

    A plain class rather than a dataclass: the constructor takes ``*scenarios``
    varargs, which a generated ``__init__`` cannot express.
    """

    def __init__(self, *scenarios: Scenario, agent_version: str = "fake-runner/1.0") -> None:
        if not scenarios:
            raise ValueError("a FakeRunner needs at least one scenario")
        self.scenarios: tuple[Scenario, ...] = tuple(scenarios)
        self.agent_version = agent_version
        self.calls: list[DispatchCall] = []

    def dispatch(
        self,
        prompt: str,
        allowed_tools: list[str],
        cwd: Path,
        timeout_s: int,
        max_turns: int,
    ) -> TurnResult:
        self.calls.append(
            DispatchCall(
                prompt=prompt,
                allowed_tools=tuple(allowed_tools),
                cwd=cwd,
                timeout_s=timeout_s,
                max_turns=max_turns,
            )
        )
        # The queue is consumed in order and the last entry repeats, so a
        # single-scenario runner answers every dispatch the same way while a
        # multi-scenario one plays out a retry sequence.
        index = min(len(self.calls) - 1, len(self.scenarios) - 1)
        return apply_scenario(self.scenarios[index], cwd)

    def version(self) -> str:
        return self.agent_version
