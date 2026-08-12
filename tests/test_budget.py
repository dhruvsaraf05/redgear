"""T-0018: failing tests for redgear.budget -- G6, bounded autonomy.

``redgear/budget.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

This is what makes an unattended loop with file-write and shell access a safe
object rather than a dangerous one. It is bounded **by construction**, not by
good intentions:

* Caps are evaluated **before** each iteration begins, never mid-turn. A cap
  that fires halfway through a turn leaves the agent's edits half-applied and
  the projection describing a state that never existed.
* ``max_consecutive_failures`` is the runaway detector. Distinct tasks each
  failing in a row means something systemic is broken -- wrong interpreter,
  missing dependency, bad plan -- and continuing burns money for nothing.
* The STOP sentinel ends the run with **success**. Stopping deliberately is
  not an error, and reporting it as one trains users to ignore exit codes.
* Signals abort the turn, terminate the process **tree**, and release locks.
  Killing only the direct child orphans whatever the agent spawned.

``Budget`` itself already exists in schemas.py (T-0003, section 8.1). This
module is the policy over it, not a redefinition.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from redgear.budget import (
    EXIT_CODES,
    RunCounters,
    budget_exhausted,
    clear_stop,
    request_stop,
    stop_requested,
    terminate_process_tree,
)
from redgear.paths import stop_path
from redgear.schemas import Budget

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    (root / ".redgear").mkdir(parents=True)
    return root


def _counters(**kwargs: object) -> RunCounters:
    defaults: dict[str, object] = {
        "iterations": 0,
        "consecutive_failures": 0,
        "started_at": datetime.now(tz=UTC),
    }
    defaults.update(kwargs)
    return RunCounters(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-1: each cap terminates the run cleanly.
# ---------------------------------------------------------------------------


def test_each_cap_terminates_run() -> None:
    """Iteration, wall clock and consecutive-failure caps each fire, each
    names itself, and each ends the run as ``budget_exhausted``."""
    budget = Budget(
        max_iterations=5,
        max_wall_clock_s=60,
        max_consecutive_failures=3,
    )
    started = datetime.now(tz=UTC)

    # Nothing exhausted yet.
    assert budget_exhausted(budget, _counters(started_at=started), now=started) is None

    # --- iteration cap ---
    verdict = budget_exhausted(budget, _counters(iterations=5, started_at=started), now=started)
    assert verdict is not None
    assert verdict.reason == "budget_exhausted"
    assert verdict.cap == "max_iterations"
    assert "5" in verdict.detail

    # One below the cap is still fine -- the check is >=, not >.
    assert (
        budget_exhausted(budget, _counters(iterations=4, started_at=started), now=started) is None
    )

    # --- wall clock cap ---
    verdict = budget_exhausted(
        budget,
        _counters(started_at=started),
        now=started + timedelta(seconds=61),
    )
    assert verdict is not None
    assert verdict.cap == "max_wall_clock_s"

    assert (
        budget_exhausted(budget, _counters(started_at=started), now=started + timedelta(seconds=59))
        is None
    )

    # --- consecutive failure cap: the runaway detector ---
    verdict = budget_exhausted(
        budget, _counters(consecutive_failures=3, started_at=started), now=started
    )
    assert verdict is not None
    assert verdict.cap == "max_consecutive_failures"

    assert (
        budget_exhausted(budget, _counters(consecutive_failures=2, started_at=started), now=started)
        is None
    )


def test_termination_reasons_map_to_distinct_exit_codes() -> None:
    """Section 4.3. Distinct codes are how a script or CI job tells "stopped
    on purpose" from "the engine broke"."""
    assert EXIT_CODES == {
        "complete": 0,
        "stopped": 0,
        "blocked": 2,
        "budget_exhausted": 3,
        "runner_error": 4,
        "engine_error": 5,
    }
    # Every non-zero code is unique, so a caller can discriminate.
    non_zero = [code for code in EXIT_CODES.values() if code != 0]
    assert len(non_zero) == len(set(non_zero))


# ---------------------------------------------------------------------------
# AC-2: caps are checked at the iteration boundary, never mid-turn.
# ---------------------------------------------------------------------------


def test_caps_checked_at_iteration_boundary() -> None:
    """``budget_exhausted`` is a pure predicate over recorded counters.

    It takes no process, opens no file and cannot interrupt anything, so it
    is structurally incapable of firing mid-turn -- the guarantee is enforced
    by the shape of the function, not by remembering where to call it.
    """
    import inspect

    from redgear import budget as budget_module

    signature = inspect.signature(budget_module.budget_exhausted)
    assert list(signature.parameters) == ["budget", "counters", "now"]

    source = inspect.getsource(budget_module.budget_exhausted)
    for forbidden in ("subprocess", "Popen", "kill", "terminate", "open(", "sleep"):
        assert forbidden not in source, (
            f"budget_exhausted touches {forbidden!r}; it must be a pure predicate "
            "that cannot interrupt a turn in progress"
        )

    # Same inputs, same answer, no matter how often it is asked.
    budget = Budget(max_iterations=2)
    counters = _counters(iterations=2)
    now = datetime.now(tz=UTC)
    first = budget_exhausted(budget, counters, now=now)
    for _ in range(5):
        assert budget_exhausted(budget, counters, now=now) == first


def test_counters_record_consecutive_not_total_failures() -> None:
    """The runaway detector counts failures *in a row*. A success in between
    resets it, because intermittent failure is not systemic failure."""
    counters = _counters()
    counters = counters.with_failure()
    counters = counters.with_failure()
    assert counters.consecutive_failures == 2

    counters = counters.with_success()
    assert counters.consecutive_failures == 0

    counters = counters.with_failure()
    assert counters.consecutive_failures == 1

    # Iterations accumulate regardless of outcome.
    assert counters.iterations == 4


# ---------------------------------------------------------------------------
# AC-3: the STOP sentinel.
# ---------------------------------------------------------------------------


def test_stop_sentinel_honoured(repo: Path) -> None:
    """Presence ends the run at the next boundary with SUCCESS status.

    Stopping deliberately is not a failure. Reporting it as one trains users
    to ignore exit codes, which is how a real failure later gets missed.
    """
    assert stop_requested(repo) is False

    request_stop(repo)
    assert stop_path(repo).exists()
    assert stop_requested(repo) is True

    # Success, not failure.
    assert EXIT_CODES["stopped"] == 0

    # A stop is honoured even when the budget has room left.
    budget = Budget(max_iterations=50)
    assert budget_exhausted(budget, _counters(iterations=1), now=datetime.now(tz=UTC)) is None
    assert stop_requested(repo) is True

    clear_stop(repo)
    assert stop_requested(repo) is False
    assert not stop_path(repo).exists()


def test_request_stop_is_idempotent(repo: Path) -> None:
    """`redgear stop` run twice must not fail the second time -- a user who
    is unsure whether it took should be able to just run it again."""
    request_stop(repo)
    request_stop(repo)
    assert stop_requested(repo) is True
    clear_stop(repo)
    clear_stop(repo)
    assert stop_requested(repo) is False


# ---------------------------------------------------------------------------
# AC-4: signals abort the turn, kill the tree, release locks.
# ---------------------------------------------------------------------------


def test_signal_aborts_and_releases(repo: Path) -> None:
    """Terminating the process TREE, not just the direct child.

    An agent CLI spawns its own subprocesses. Killing only the child leaves
    grandchildren holding file handles and burning CPU after the run has
    reported itself finished.

    Uses a real process tree, not a mock: a mocked kill proves only that a
    function was called, and the failure mode here is that the OS did not
    actually reap anything.
    """
    marker = repo / "grandchild_alive.txt"
    grandchild_code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        "for _ in range(200):\n"
        "    p.write_text(str(time.time()))\n"
        "    time.sleep(0.05)\n"
    )
    child_code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
        "time.sleep(30)\n"
    )

    parent = subprocess.Popen(
        [sys.executable, "-c", child_code, grandchild_code, str(marker)],
        cwd=REPO_ROOT,
    )
    try:
        # Wait for the grandchild to prove it is running.
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "grandchild never started; the test cannot prove anything"

        terminate_process_tree(parent)

        assert parent.poll() is not None, "the direct child survived termination"

        # The grandchild must stop touching the marker.
        time.sleep(0.5)
        settled = marker.read_text(encoding="utf-8")
        time.sleep(0.5)
        assert marker.read_text(encoding="utf-8") == settled, (
            "the grandchild is still running -- only the direct child was killed"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_terminate_process_tree_is_safe_on_a_dead_process() -> None:
    """Termination runs from a signal handler and a finally block, so it must
    never raise on an already-exited process -- that would mask the original
    failure with a secondary one."""
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=10)
    terminate_process_tree(finished)
    terminate_process_tree(finished)


def test_signal_exit_codes_follow_convention() -> None:
    """Section 8.3: SIGINT exits 130, SIGTERM exits 143 -- 128 + signal
    number, the shell convention every CI system already understands."""
    from redgear.budget import SIGNAL_EXIT_CODES

    assert SIGNAL_EXIT_CODES["SIGINT"] == 130
    assert SIGNAL_EXIT_CODES["SIGTERM"] == 143
