"""The agent-CLI seam.

``runner.py`` is the only module permitted to spawn an agent CLI (section 2.2).
This file defines the *shape* of that capability; the Claude Code adapter
arrives at T-0034 and the deterministic fake lives in ``tests/fake_runner/``.

**Why a protocol rather than a base class.** NFR-8 requires that agent-CLI
specifics stay confined to a single adapter behind a stable interface, and
section 2.4 forbids branching on agent identity anywhere outside this module.
A ``Protocol`` gets that without inheritance: an adapter conforms by having the
right methods, so it never has to import redgear to satisfy the seam, and
``orchestrator.py`` can be typed against ``Runner`` while importing no concrete
implementation at all.

This is also the one place section 11.3's ban on speculative abstraction is
deliberately not applied, and the justification is specific: the second
implementation already exists. The fake runner ships on day one and is the
primary test harness for every milestone after this one, so the seam is being
drawn between two real callers rather than in anticipation of one.

**What an adapter must provide** (section 2.4). A CLI that cannot do all four
is not supportable, and the right response is to say so rather than to
half-support it:

1. send a single prompt non-interactively,
2. constrain tool permissions,
3. cap the number of turns,
4. return a machine-readable result.

Those map onto ``dispatch``'s parameters one for one. Note what is absent:
there is no way to ask a runner what it did, no callback, and no streaming
handle. A turn is one prompt in, one result out, process exits (section 1.2),
and the agent CLI is a stateless worker holding no memory between turns.

**The result is a claim, not a verdict** (G1). ``TurnResult.changed_files`` and
``outcome`` are what the agent *says*. Every one of them is cross-checked
against real git state by ``verifier.py`` after the process has exited. No
field an adapter populates may influence a gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from redgear.schemas import TurnResult


@runtime_checkable
class Runner(Protocol):
    """One agent CLI, reduced to what the orchestrator needs of it.

    ``@runtime_checkable`` so a test can assert conformance with
    ``isinstance``. Be aware of what that buys: it checks method *names* only,
    never signatures. Signature conformance is a static property, enforced by
    ``mypy --strict`` at every call site and asserted explicitly in
    ``tests/test_fake_runner.py``.
    """

    def dispatch(
        self,
        prompt: str,
        allowed_tools: list[str],
        cwd: Path,
        timeout_s: int,
        max_turns: int,
    ) -> TurnResult:
        """Run one turn: one prompt in, one result out, process exits.

        :param prompt: The complete composed prompt. Passed as a single argv
            element, never interpolated into a shell string (section 6.5) --
            a prompt containing backticks must not be able to execute
            anything.
        :param allowed_tools: The permission allowlist derived from the task's
            scope. Never a bare ``Bash`` and never
            ``--dangerously-skip-permissions`` (section 8.2).
        :param cwd: The target repository root, resolved and asserted to be
            inside the repository.
        :param timeout_s: Wall-clock ceiling. On expiry the adapter terminates
            the process tree -- not just the direct child -- and the turn
            counts as a failed attempt (section 6.5).
        :param max_turns: Ceiling on agentic turns within this one dispatch.

        Implementations must not raise on a *task* failure: a turn that went
        badly is still a ``TurnResult``, because the orchestrator decides what
        a failure means and needs the record either way. Raising is reserved
        for the adapter itself being broken -- the CLI missing, or output that
        could not be parsed twice running (section 6.4 rule 4).
        """
        ...

    def version(self) -> str:
        """The installed agent CLI's version string.

        Surfaced by ``redgear doctor``. Adapter flags change between releases
        of a third-party CLI, so being able to see the installed version is
        what makes drift diagnosable rather than mysterious (section 2.4).
        """
        ...
