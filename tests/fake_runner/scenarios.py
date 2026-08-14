"""Agent behaviours, declared as data.

CLAUDE.md section 2.2 sketches this module as "one function per agent
behaviour". It is written as **one frozen record per behaviour** instead, and
the difference is load-bearing rather than stylistic.

Section 10.5 requires roughly two dozen distinct behaviours, and T-0030 needs
all of them at once. As functions they would be two dozen near-identical bodies
differing in a path string and an enum, and every new assertion the
orchestrator needs -- "what did it declare?", "did it lie?" -- would mean
editing all of them. As data, a behaviour is a row: the files it writes, what
it claims in ``changed_files``, the outcome it reports, and where those two
disagree.

The lie surface is the interesting part, and it is deliberately explicit.
``omits_from_declaration`` and ``declares_extra`` are how a scenario models an
agent that has lost track of its own edits -- the ``undeclared_change`` and
``phantom_change`` failures gate 1 exists to catch. Encoding them as *fields*
rather than as bespoke functions means the fake cannot accidentally tell the
truth: the declaration is computed from the same record the patch is.

Nothing here reads a clock, a random source, or the environment. Two runs of
the same scenario produce byte-identical results, which is the property the
whole loop's testability rests on.
"""

from __future__ import annotations

from dataclasses import dataclass

from redgear.schemas import BlockerCategory, TurnOutcome


@dataclass(frozen=True)
class FileEdit:
    """One file the agent touches.

    ``content is None`` means **delete**. That case matters more than it
    looks: deleting a frozen test is the crudest way to make a failing suite
    green, and it is the one violation gate 2 catches that gate 1's glob logic
    cannot see.
    """

    path: str
    content: str | None = None

    @property
    def is_delete(self) -> bool:
        return self.content is None


@dataclass(frozen=True)
class Scenario:
    """One agent behaviour, fully declared.

    ``name`` is the key used in ``SCENARIOS`` and the id pytest shows when a
    parametrised case fails, so it is worth keeping it the same wording
    section 10.5 uses.
    """

    name: str
    doc: str
    edits: tuple[FileEdit, ...] = ()
    outcome: TurnOutcome = TurnOutcome.COMPLETED
    summary: str = "fake agent turn"
    known_gaps: tuple[str, ...] = ()
    blocker_category: BlockerCategory | None = None
    blocker_detail: str | None = None
    exit_code: int = 0
    parse_ok: bool = True

    #: Paths the agent claims to have changed but never touched -> phantom_change.
    declares_extra: tuple[str, ...] = ()
    #: Paths the agent really changed but leaves out of its report -> undeclared_change.
    omits_from_declaration: tuple[str, ...] = ()

    @property
    def touched_paths(self) -> tuple[str, ...]:
        """Every path the patch really touches, written or deleted."""
        return tuple(edit.path for edit in self.edits)

    @property
    def declared_files(self) -> tuple[str, ...]:
        """What the agent *says* it changed -- a claim, never the truth (G1).

        Sorted so a scenario's declaration is stable across runs; the gate
        compares sets, but a proof and a prompt both render this list.
        """
        omitted = set(self.omits_from_declaration)
        declared = {path for path in self.touched_paths if path not in omitted}
        declared.update(self.declares_extra)
        return tuple(sorted(declared))


_FEATURE = "def feature():\n    return 42\n"
_MESSY = "import os\n\n\ndef messy():\n    return 1\n"


SCENARIOS: dict[str, Scenario] = {
    scenario.name: scenario
    for scenario in (
        # --- honest, in-scope work ------------------------------------------
        Scenario(
            name="happy_implementation",
            doc="A correct patch entirely inside the granted scope.",
            edits=(FileEdit("src/pkg/feature.py", _FEATURE),),
            summary="Added the feature module.",
        ),
        Scenario(
            name="happy_test_authoring",
            doc="Writes a test that fails for the declared reason.",
            edits=(
                FileEdit(
                    "tests/test_feature.py",
                    "from pkg.feature import feature\n\n\ndef test_feature():\n"
                    "    assert feature() == 42\n",
                ),
            ),
            summary="Wrote the failing test for the feature.",
        ),
        # --- scope violations -------------------------------------------------
        Scenario(
            name="writes_out_of_scope",
            doc="Edits a path the task was never granted.",
            edits=(FileEdit("README.md", "edited without permission\n"),),
            summary="Updated the readme.",
        ),
        Scenario(
            name="touches_frozen_test",
            doc="An implementation task editing tests/** -- G2's core violation.",
            edits=(FileEdit("tests/test_pkg.py", "def test_placeholder():\n    assert True\n"),),
            summary="Adjusted the test so it passes.",
        ),
        Scenario(
            name="adds_frozen_file",
            doc="Creates a new file inside a frozen glob; absent from the claim-time digest map.",
            edits=(FileEdit("tests/test_sneaky.py", "def test_sneaky():\n    assert True\n"),),
            summary="Added a test.",
        ),
        Scenario(
            name="deletes_frozen_test",
            doc="Deletes a frozen test -- the crudest way to make a red suite green.",
            edits=(FileEdit("tests/test_pkg.py", None),),
            summary="Removed an obsolete test.",
        ),
        # --- dishonest bookkeeping -------------------------------------------
        Scenario(
            name="undeclared_change",
            doc="Edits two files in scope and reports only one.",
            edits=(
                FileEdit("src/pkg/one.py", "one = 1\n"),
                FileEdit("src/pkg/two.py", "two = 2\n"),
            ),
            omits_from_declaration=("src/pkg/two.py",),
            summary="Added the first module.",
        ),
        Scenario(
            name="phantom_change",
            doc="Reports a file it never touched.",
            edits=(FileEdit("src/pkg/real.py", "real = 1\n"),),
            declares_extra=("src/pkg/ghost.py",),
            summary="Added both modules.",
        ),
        # --- work that fails a later gate ------------------------------------
        Scenario(
            name="lint_dirty",
            doc="In-scope code carrying a lint violation; the suite never runs.",
            edits=(FileEdit("src/pkg/messy.py", _MESSY),),
            summary="Added the module.",
        ),
        # --- honest exit (G3) -------------------------------------------------
        Scenario(
            name="returns_blocked",
            doc="Cannot proceed honestly. Costs no attempt and pauses the run.",
            outcome=TurnOutcome.BLOCKED,
            summary="The task contradicts an ADR rule and I cannot resolve it.",
            blocker_category=BlockerCategory.CONTRADICTORY_RULES,
            blocker_detail=(
                "ADR-0007 requires integer minor units; the acceptance criterion "
                "asks for a float. Both cannot hold."
            ),
        ),
        Scenario(
            name="returns_scope_insufficient",
            doc="The task needs a path the agent was not granted.",
            outcome=TurnOutcome.SCOPE_INSUFFICIENT,
            summary="Implementing this requires editing migrations/, which is frozen.",
            blocker_category=BlockerCategory.AMBIGUOUS_TASK,
            blocker_detail="migrations/0004_add_column.py must change for the test to pass.",
        ),
        # --- integration failure ----------------------------------------------
        Scenario(
            name="malformed_output",
            doc="No parseable structured result. A runner-level fault, not a task failure.",
            outcome=TurnOutcome.COMPLETED,
            summary="",
            exit_code=1,
            parse_ok=False,
        ),
    )
}

#: Stable ordering so a parametrised run reports cases in a predictable order.
ALL_SCENARIOS: tuple[Scenario, ...] = tuple(SCENARIOS[name] for name in sorted(SCENARIOS))
