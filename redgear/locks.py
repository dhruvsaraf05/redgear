"""Run lock and task leases.

Two locks with different lifetimes:

* **The run lock** is held for one whole ``redgear run``. A second run in the
  same repository is refused with ``E_RUN_LOCKED`` -- two loops dispatching
  into one working tree would interleave edits and make every diff audit
  fiction (G1).
* **A task lease** is held for one task and carries an expiry. Expiry is what
  stops a crashed run from wedging a task forever: the next run reaps the
  stale lease and the task becomes claimable again.

Every lock file is **self-describing** -- holder, acquisition time, expiry,
token -- because the failure users actually hit is a stale lock they must
diagnose. Section 8.3: "a stale lock means the next run refuses to start and
the user has to delete a file they do not understand."

Acquisition is ``O_CREAT | O_EXCL``, which is atomic at the OS level and so
excludes other processes, not merely other threads.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from redgear.errors import RunLockedError
from redgear.events import append as append_event
from redgear.paths import events_path, run_lock_path, task_lock_path
from redgear.schemas import LockRecord

#: Default lease lifetimes. A task lease matches the dispatch timeout
#: (section 8.1's ``dispatch_timeout_s``); a run lock matches the wall-clock
#: cap, so an abandoned run frees itself no later than its own budget would.
DEFAULT_LEASE_SECONDS = 900
DEFAULT_RUN_LOCK_SECONDS = 7200

#: Marks the run lock in a LockRecord, which is otherwise keyed by task id.
_RUN_LOCK_ID = "__run__"


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def read_lock_file(path: Path) -> LockRecord | None:
    """Parse a lock file, or None if absent or unreadable.

    Diagnosis must never itself crash: a corrupt lock is exactly the
    situation a human is trying to understand, and raising here would hide
    it behind a traceback.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return LockRecord.model_validate(raw)
    except ValueError:
        return None


def _acquire(path: Path, record: LockRecord, *, what: str) -> LockRecord:
    """Create a lock file exclusively, or refuse.

    ``O_CREAT | O_EXCL`` is the whole mechanism. On Windows a file pending
    deletion reports ``PermissionError`` rather than ``FileExistsError`` --
    the same race already found in events.py -- so both mean "held".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    try:
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, PermissionError):
        incumbent = read_lock_file(path)
        raise RunLockedError(
            f"{what} is already held"
            + (f" by {incumbent.holder}" if incumbent is not None else ""),
            detail={
                "path": str(path),
                "holder": incumbent.holder if incumbent is not None else None,
                "expires_at": _stamp(incumbent.expires_at) if incumbent is not None else None,
            },
        ) from None
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return record


def _new_record(lock_id: str, holder: str, ttl_seconds: int, now: datetime) -> LockRecord:
    return LockRecord(
        task_id=lock_id,
        holder=holder,
        token=secrets.token_hex(8),
        acquired_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )


# ---------------------------------------------------------------------------
# Task leases
# ---------------------------------------------------------------------------


def acquire_task_lease(
    repo_root: Path,
    task_id: str,
    *,
    holder: str,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LockRecord:
    """Take the lease for one task, or raise ``RunLockedError``."""
    now = datetime.now(tz=UTC)
    record = _new_record(task_id, holder, ttl_seconds, now)
    return _acquire(task_lock_path(repo_root, task_id), record, what=f"lease for {task_id}")


def release_task_lease(repo_root: Path, task_id: str, *, token: str) -> None:
    """Release a lease, but only if the caller still holds it.

    The token check stops a stale holder -- one whose lease was already
    reaped and re-granted -- from deleting the *new* holder's lock and
    handing the task to two runners at once.
    """
    path = task_lock_path(repo_root, task_id)
    record = read_lock_file(path)
    if record is None:
        return
    if record.token != token:
        raise RunLockedError(
            f"lease for {task_id} is held by another token; refusing to release it",
            detail={"task_id": task_id, "holder": record.holder},
        )
    path.unlink(missing_ok=True)


def lease_is_expired(record: LockRecord, *, now: datetime) -> bool:
    return now >= record.expires_at


@contextmanager
def task_lease(
    repo_root: Path,
    task_id: str,
    *,
    holder: str,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Iterator[LockRecord]:
    """Hold a task lease for the duration of the block.

    Released on normal exit and on exception alike -- the ``finally`` is the
    whole point (AC-4).
    """
    record = acquire_task_lease(repo_root, task_id, holder=holder, ttl_seconds=ttl_seconds)
    try:
        yield record
    finally:
        release_task_lease(repo_root, task_id, token=record.token)


def reap_expired_leases(repo_root: Path, *, now: datetime, actor: str = "engine") -> list[str]:
    """Remove every expired task lease, recording each in the log.

    Expiry is not self-executing: a lease stays held until something reaps
    it, so that a live-but-slow holder is never silently displaced. Reaping
    appends ``lease_expired`` with ``counted_as_attempt: false`` -- G3, a
    lease lost to a dead process is not the agent's failure.

    Returns the task ids reaped, sorted, so a caller can report them.
    """
    lock_dir = run_lock_path(repo_root).parent
    if not lock_dir.is_dir():
        return []

    reaped: list[str] = []
    for path in sorted(lock_dir.glob("*.lock")):
        if path.name == run_lock_path(repo_root).name:
            continue
        record = read_lock_file(path)
        if record is None or not lease_is_expired(record, now=now):
            continue
        append_event(
            events_path(repo_root),
            {
                "event": "lease_expired",
                "ts": _stamp(now),
                "actor": actor,
                "task_id": record.task_id,
                "attempt": record.attempt,
                "claim_token": record.token,
                "counted_as_attempt": False,
            },
        )
        path.unlink(missing_ok=True)
        reaped.append(record.task_id)
    return reaped


# ---------------------------------------------------------------------------
# Run lock
# ---------------------------------------------------------------------------


@contextmanager
def run_lock(
    repo_root: Path,
    *,
    holder: str,
    ttl_seconds: int = DEFAULT_RUN_LOCK_SECONDS,
) -> Iterator[LockRecord]:
    """Hold the single-run lock for the duration of the block.

    Released on every exit path that unwinds the stack. A process killed
    without unwinding leaves the file behind -- unavoidable for any lock
    design -- which is why the record carries an expiry and a holder, so the
    next run can diagnose and reap it rather than the user deleting a file
    they do not understand.
    """
    now = datetime.now(tz=UTC)
    record = _new_record(_RUN_LOCK_ID, holder, ttl_seconds, now)
    path = run_lock_path(repo_root)
    _acquire(path, record, what="the run lock")
    try:
        yield record
    finally:
        current = read_lock_file(path)
        if current is not None and current.token == record.token:
            path.unlink(missing_ok=True)
