"""G6 -- bounded autonomy: caps, the STOP sentinel, signal handling.

An unattended loop that spawns an agent with file-write and shell access is a
genuinely dangerous object. This module is what bounds it **by construction**
rather than by good intentions.

Three shapes matter more than the arithmetic:

* ``budget_exhausted`` is a **pure predicate** over recorded counters. It
  holds no process handle, opens no file and sleeps on nothing, so it is
  structurally incapable of interrupting a turn in progress. The
  "checked before each iteration, never mid-turn" rule is therefore enforced
  by what the function *can* do, not by remembering where to call it.
* Stopping deliberately exits **0**. A STOP that reported failure would train
  users to ignore exit codes, which is how a real failure later goes unseen.
* Termination kills the process **tree**. An agent CLI spawns its own
  children; reaping only the direct child leaves grandchildren holding file
  handles after the run has reported itself finished.

``Budget`` lives in schemas.py (section 8.1). This module is policy over it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from redgear.paths import stop_path
from redgear.schemas import Budget

#: Section 4.3. Distinct non-zero codes are how a script or CI job tells
#: "stopped on purpose" from "the engine broke".
EXIT_CODES: dict[str, int] = {
    "complete": 0,
    "stopped": 0,
    "blocked": 2,
    "budget_exhausted": 3,
    "runner_error": 4,
    "engine_error": 5,
}

#: Section 8.3, and the shell convention (128 + signal number) that every CI
#: system already interprets correctly.
SIGNAL_EXIT_CODES: dict[str, int] = {"SIGINT": 130, "SIGTERM": 143}

#: Grace period between asking a process tree to stop and forcing it.
_TERMINATE_GRACE_S = 5


@dataclass(frozen=True)
class RunCounters:
    """What the loop has done so far. Immutable; each update returns a copy.

    Immutability is deliberate: the budget check must see a consistent
    snapshot, and a mutable counter shared with a running turn could change
    between two reads of the same predicate.
    """

    iterations: int
    consecutive_failures: int
    started_at: datetime

    def with_failure(self) -> RunCounters:
        return replace(
            self,
            iterations=self.iterations + 1,
            consecutive_failures=self.consecutive_failures + 1,
        )

    def with_success(self) -> RunCounters:
        """A success resets the runaway detector.

        The cap counts failures *in a row*: intermittent failure is normal
        (that is what retries are for), while an unbroken streak means
        something systemic is wrong.
        """
        return replace(self, iterations=self.iterations + 1, consecutive_failures=0)


@dataclass(frozen=True)
class BudgetVerdict:
    """Which cap fired, in terms a status line can print verbatim."""

    reason: str
    cap: str
    detail: str


def budget_exhausted(budget: Budget, counters: RunCounters, now: datetime) -> BudgetVerdict | None:
    """Has any cap been reached? Returns the verdict, or None to continue.

    A pure function of its arguments -- no I/O, no clock of its own, no
    process handles. Call it at the top of an iteration; it cannot fire
    anywhere else because it cannot *do* anything anywhere else.

    Comparisons are ``>=``: a cap of 5 iterations permits iterations 0..4 and
    stops before starting the sixth.
    """
    if counters.iterations >= budget.max_iterations:
        return BudgetVerdict(
            reason="budget_exhausted",
            cap="max_iterations",
            detail=(f"{counters.iterations} iterations completed, cap is {budget.max_iterations}"),
        )

    elapsed = (now - counters.started_at).total_seconds()
    if elapsed >= budget.max_wall_clock_s:
        return BudgetVerdict(
            reason="budget_exhausted",
            cap="max_wall_clock_s",
            detail=f"{elapsed:.0f}s elapsed, cap is {budget.max_wall_clock_s}s",
        )

    if counters.consecutive_failures >= budget.max_consecutive_failures:
        return BudgetVerdict(
            reason="budget_exhausted",
            cap="max_consecutive_failures",
            detail=(
                f"{counters.consecutive_failures} consecutive failures, "
                f"cap is {budget.max_consecutive_failures}. Distinct tasks failing "
                "in a row usually means the environment or the plan is wrong."
            ),
        )

    return None


# ---------------------------------------------------------------------------
# The STOP sentinel
# ---------------------------------------------------------------------------


def stop_requested(repo_root: Path) -> bool:
    """Is `.redgear/STOP` present? Checked before every iteration (section 8.3)."""
    return stop_path(repo_root).exists()


def request_stop(repo_root: Path) -> None:
    """Create the sentinel. Idempotent -- a user unsure whether `redgear stop`
    took effect must be able to simply run it again."""
    path = stop_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Created by `redgear stop`. The run ends at the next iteration "
        "boundary with exit code 0. Delete this file to allow runs again.\n",
        encoding="utf-8",
    )


def clear_stop(repo_root: Path) -> None:
    """Remove the sentinel. Idempotent for the same reason."""
    stop_path(repo_root).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Process-tree termination and signals
# ---------------------------------------------------------------------------


def terminate_process_tree(process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    """Kill a process and everything it spawned.

    Never raises. This runs from signal handlers and ``finally`` blocks, where
    an exception would mask the failure that triggered the cleanup.

    Platform note: Windows has no process groups usable here, so this shells
    out to ``taskkill /T``, which walks the tree by parent id. POSIX uses
    ``killpg`` on the group the child was started in. Both are best-effort --
    a grandchild that has already reparented cannot be found by either.

    **Callers must spawn with ``start_new_session=True``.** This is a real
    precondition, not a suggestion, and it is invisible on Windows. ``killpg``
    signals a process *group*: a child spawned without a new session inherits
    redgear's own group, so killing it on timeout signals redgear too. That
    failed exactly one way -- a timeout test under CI sent SIGTERM to the
    pytest process running it, and the job died with no useful output while
    the Windows ``taskkill`` path stayed green.
    """
    if process.poll() is not None:
        return

    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)

    try:
        process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_TERMINATE_GRACE_S)


def signal_exit_code(signal_name: str) -> int:
    """128 + signal number, per section 8.3."""
    return SIGNAL_EXIT_CODES[signal_name]
