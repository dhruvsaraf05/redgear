"""T-0016: failing tests for redgear.locks -- run lock and task leases.

``redgear/locks.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

Two locks, different lifetimes:

* **The run lock** is held for a whole ``redgear run``. A second run in the
  same repository is refused (``E_RUN_LOCKED``), because two loops dispatching
  into one working tree would interleave edits and make every diff audit
  fiction.
* **A task lease** is held for one task, expires on a clock, and is reaped if
  its holder dies. Expiry is what stops a crashed run from wedging a task
  forever.

The property that actually bites users is AC-4: **no orphan locks**. A stale
lock means the next run refuses to start and the human has to delete a file
they do not understand. Where an orphan is genuinely unavoidable -- a
``SIGKILL`` leaves no chance to clean up -- the guarantee is instead that the
lock is self-describing and expirable, so it can be diagnosed and reaped
rather than puzzled over.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from redgear.errors import RunLockedError
from redgear.events import replay as replay_events
from redgear.locks import (
    acquire_task_lease,
    lease_is_expired,
    read_lock_file,
    reap_expired_leases,
    release_task_lease,
    run_lock,
    task_lease,
)
from redgear.paths import events_path, run_lock_path, task_lock_path

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A bare target repository -- only `.redgear/` matters for locking."""
    root = tmp_path / "target"
    (root / ".redgear" / "locks").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# AC-1: concurrent claims on one task -- exactly one winner.
# ---------------------------------------------------------------------------


def test_exactly_one_claim_wins(repo: Path) -> None:
    """Real threads, real contention. Exactly one acquires; the rest are
    refused. A lock that is never contended proves nothing."""
    winners: list[str] = []
    refused: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(worker_id: int) -> None:
        barrier.wait()  # maximise the overlap
        try:
            lease = acquire_task_lease(repo, "T-0042", holder=f"worker-{worker_id}")
            winners.append(lease.holder)
        except BaseException as exc:
            refused.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(refused) == 7
    assert all(isinstance(exc, RunLockedError) for exc in refused), (
        f"a refusal was not a RunLockedError: {[type(e) for e in refused]}"
    )

    # The lock file names its holder, so a human can see who has it.
    info = read_lock_file(task_lock_path(repo, "T-0042"))
    assert info is not None
    assert info.holder == winners[0]


def test_lock_file_is_self_describing(repo: Path) -> None:
    """A human diagnosing a stale lock must be able to read it: who holds it,
    when it was taken, when it expires."""
    before = datetime.now(tz=UTC)
    with task_lease(repo, "T-0007", holder="run_01J8X", ttl_seconds=900) as lease:
        path = task_lock_path(repo, "T-0007")
        assert path.exists()

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["holder"] == "run_01J8X"
        assert raw["task_id"] == "T-0007"
        assert "acquired_at" in raw and "expires_at" in raw and "token" in raw

        info = read_lock_file(path)
        assert info is not None
        assert info.holder == "run_01J8X"
        assert info.token == lease.token
        assert info.expires_at > before + timedelta(seconds=800)


def test_release_allows_reacquisition(repo: Path) -> None:
    first = acquire_task_lease(repo, "T-0001", holder="a")
    with pytest.raises(RunLockedError):
        acquire_task_lease(repo, "T-0001", holder="b")

    release_task_lease(repo, "T-0001", token=first.token)
    second = acquire_task_lease(repo, "T-0001", holder="b")
    assert second.holder == "b"
    assert second.token != first.token


def test_release_with_wrong_token_refuses(repo: Path) -> None:
    """A stale holder must not be able to release someone else's lease --
    that would hand the task to two runners at once."""
    lease = acquire_task_lease(repo, "T-0001", holder="a")
    with pytest.raises(RunLockedError):
        # S106: a lease token, not a credential.
        release_task_lease(repo, "T-0001", token="not-the-token")  # noqa: S106
    assert task_lock_path(repo, "T-0001").exists()
    release_task_lease(repo, "T-0001", token=lease.token)
    assert not task_lock_path(repo, "T-0001").exists()


# ---------------------------------------------------------------------------
# AC-2: an expired lease is reaped and the task becomes claimable.
# ---------------------------------------------------------------------------


def test_expired_lease_reaped(repo: Path) -> None:
    """Expiry is what stops a crashed run from wedging a task forever."""
    lease = acquire_task_lease(repo, "T-0003", holder="dead-run", ttl_seconds=1)
    assert not lease_is_expired(lease, now=datetime.now(tz=UTC))

    later = datetime.now(tz=UTC) + timedelta(seconds=5)
    assert lease_is_expired(lease, now=later)

    # Still held until something reaps it -- expiry is not self-executing.
    with pytest.raises(RunLockedError):
        acquire_task_lease(repo, "T-0003", holder="new-run")

    reaped = reap_expired_leases(repo, now=later, actor="engine")
    assert reaped == ["T-0003"]
    assert not task_lock_path(repo, "T-0003").exists()

    # And the task is claimable again.
    fresh = acquire_task_lease(repo, "T-0003", holder="new-run")
    assert fresh.holder == "new-run"


def test_reaping_appends_a_lease_expired_event(repo: Path) -> None:
    """Section 3.6: reaping records whether it counted as an attempt. G3 says
    a lease lost to a dead process is not the agent's failure, so it does
    not."""
    lease = acquire_task_lease(repo, "T-0009", holder="dead-run", ttl_seconds=1)
    later = datetime.now(tz=UTC) + timedelta(seconds=5)

    reap_expired_leases(repo, now=later, actor="engine")

    events = replay_events(events_path(repo))
    assert len(events) == 1
    event = events[0]
    assert event.event == "lease_expired"
    assert event.task_id == "T-0009"
    assert event.claim_token == lease.token
    assert event.counted_as_attempt is False


def test_reaping_leaves_live_leases_alone(repo: Path) -> None:
    acquire_task_lease(repo, "T-0001", holder="live", ttl_seconds=900)
    acquire_task_lease(repo, "T-0002", holder="dead", ttl_seconds=1)

    later = datetime.now(tz=UTC) + timedelta(seconds=5)
    reaped = reap_expired_leases(repo, now=later, actor="engine")

    assert reaped == ["T-0002"]
    assert task_lock_path(repo, "T-0001").exists()
    assert not task_lock_path(repo, "T-0002").exists()


# ---------------------------------------------------------------------------
# AC-3: one run per repository.
# ---------------------------------------------------------------------------


def test_single_run_per_repo(repo: Path) -> None:
    """A second loop dispatching into the same working tree would interleave
    edits and make every diff audit fiction."""
    with run_lock(repo, holder="run_A") as first:
        assert run_lock_path(repo).exists()

        with pytest.raises(RunLockedError) as excinfo, run_lock(repo, holder="run_B"):
            pass
        assert excinfo.value.code == "E_RUN_LOCKED"
        # The refusal names the incumbent, so the user knows what to stop.
        assert "run_A" in json.dumps(excinfo.value.detail)
        assert first.holder == "run_A"

    # Released on exit -- the next run starts cleanly.
    assert not run_lock_path(repo).exists()
    with run_lock(repo, holder="run_B") as second:
        assert second.holder == "run_B"


def test_run_lock_refused_across_processes(repo: Path) -> None:
    """Threads share an interpreter; a second `redgear run` does not. The
    exclusion must be an OS-level fact, not a Python-level one."""
    with run_lock(repo, holder="run_A"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "from pathlib import Path\n"
                "from redgear.locks import run_lock\n"
                "from redgear.errors import RunLockedError\n"
                "try:\n"
                "    with run_lock(Path(sys.argv[1]), holder='other'):\n"
                "        sys.stdout.write('ACQUIRED')\n"
                "except RunLockedError:\n"
                "    sys.stdout.write('REFUSED')\n",
                str(repo),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "REFUSED"


# ---------------------------------------------------------------------------
# AC-4: no orphaned locks.
# ---------------------------------------------------------------------------


def test_no_orphan_locks_on_any_exit(repo: Path) -> None:
    """Normal return, raised exception, and nested acquisition all clean up.

    A stale lock means the next run refuses to start and the user has to
    delete a file they do not understand -- the failure mode section 8.3
    calls out by name.
    """
    lock_dir = repo / ".redgear" / "locks"

    # 1. Normal exit.
    with run_lock(repo, holder="run_A"):
        pass
    assert sorted(p.name for p in lock_dir.iterdir()) == []

    # 2. Exception inside the context.
    with pytest.raises(ValueError, match="boom"), run_lock(repo, holder="run_B"):
        raise ValueError("boom")
    assert sorted(p.name for p in lock_dir.iterdir()) == []

    # 3. Task lease, both ways.
    with task_lease(repo, "T-0001", holder="run_C"):
        assert task_lock_path(repo, "T-0001").exists()
    assert sorted(p.name for p in lock_dir.iterdir()) == []

    with pytest.raises(RuntimeError, match="kaboom"), task_lease(repo, "T-0002", holder="run_D"):
        raise RuntimeError("kaboom")
    assert sorted(p.name for p in lock_dir.iterdir()) == []

    # 4. Run lock and task lease together, exception in the inner one.
    with (
        pytest.raises(RuntimeError),
        run_lock(repo, holder="run_E"),
        task_lease(repo, "T-0003", holder="run_E"),
    ):
        raise RuntimeError("inner")
    assert sorted(p.name for p in lock_dir.iterdir()) == []


def test_abrupt_death_leaves_a_reapable_lock(repo: Path) -> None:
    """The honest limit of AC-4.

    A process killed without unwinding cannot clean up -- no lock design
    prevents that. What the design DOES guarantee is that the leftover is
    self-describing and expirable, so the next run diagnoses and reaps it
    instead of the user deleting a mystery file.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from pathlib import Path\n"
            "from redgear.locks import acquire_task_lease\n"
            "acquire_task_lease(Path(sys.argv[1]), 'T-0055', holder='doomed', ttl_seconds=1)\n"
            "os._exit(1)\n",
            str(repo),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    stale = task_lock_path(repo, "T-0055")
    assert stale.exists(), "the abrupt exit should have left the lock behind"

    info = read_lock_file(stale)
    assert info is not None
    assert info.holder == "doomed"

    time.sleep(1.1)
    reaped = reap_expired_leases(repo, now=datetime.now(tz=UTC), actor="engine")
    assert reaped == ["T-0055"]
    assert not stale.exists()


def test_read_lock_file_handles_absent_and_corrupt(repo: Path) -> None:
    """Diagnosis must not itself crash on a damaged lock."""
    assert read_lock_file(task_lock_path(repo, "T-0404")) is None

    corrupt = task_lock_path(repo, "T-0500")
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_lock_file(corrupt) is None
