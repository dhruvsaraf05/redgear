"""The append-only event log -- G4's single source of truth.

``.redgear/events.jsonl`` is the only authoritative record of what happened.
``task_graph.json`` and every other state file are projections of it, so the
properties this module enforces are the guarantee itself:

* ``seq`` is assigned here and only here, under an exclusive lock: read the
  last value, increment, write, fsync, release. A caller may never supply one.
* A gap or a repeat found on replay is ``E_LOG_CORRUPT``. It is reported and
  never repaired -- silently renumbering an audit trail yields a log that
  looks clean and is a lie, which is strictly worse than a crash that names
  the problem.
* An append is durable before the call returns: ``O_APPEND``, then flush, then
  ``os.fsync``. A buffered write a crash can lose would let the orchestrator
  believe it dispatched a prompt it cannot prove it sent.

One JSON object per line, UTF-8, newline-terminated, keys sorted by the same
canonical encoder used for hashing, so a line is stable and greppable.

Section 11.1 rule 5: lines are appended, never edited, reordered, or deleted.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from redgear.errors import EventLogCorruptError, JsonValue
from redgear.hashing import canonical_json
from redgear.schemas import Event

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

#: How long to wait for the append lock before giving up. Generous: contention
#: is measured in milliseconds, so reaching this means a writer died holding
#: the lock rather than that the system is merely busy.
_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.001


@contextmanager
def _append_lock(log_path: Path) -> Iterator[None]:
    """Exclusive lock around read-last-seq / write / fsync.

    ``O_CREAT | O_EXCL`` is atomic at the OS level, so this excludes other
    threads and other processes with the same mechanism -- an in-process
    ``threading.Lock`` would let two ``redgear`` processes assign the same
    ``seq``, which is exactly the corruption replay would later refuse.

    The lock file is always removed, so a clean append never strands one for
    the next run to trip over.
    """
    lock_path = log_path.with_name(log_path.name + ".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    handle: int | None = None
    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            # Windows reports ERROR_ACCESS_DENIED (PermissionError), not
            # EEXIST, when the lock file exists but is pending deletion -- the
            # exact window another appender is in as it releases. Treating that
            # as anything but "retry" makes concurrent appends fail on Windows
            # while passing on Linux.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not acquire the event-log lock within {_LOCK_TIMEOUT_S}s: "
                    f"{lock_path} exists. If no other redgear process is running, a "
                    f"previous writer died holding it and the file must be removed."
                ) from None
            time.sleep(_LOCK_POLL_S)
    try:
        yield
    finally:
        os.close(handle)
        lock_path.unlink(missing_ok=True)


def _iter_raw_lines(log_path: Path) -> list[str]:
    """Every non-empty line, or [] when the log does not exist yet.

    An absent log is not corruption -- it is a run that has not started.
    """
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line.strip()]


def last_seq(log_path: Path) -> int:
    """The highest sequence number in the log, or -1 when it is empty.

    -1 rather than 0 so the first append is unambiguously ``last_seq() + 1``
    without a special case for the empty log.
    """
    lines = _iter_raw_lines(log_path)
    if not lines:
        return -1
    try:
        record = json.loads(lines[-1])
        value = record["seq"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EventLogCorruptError(
            "the final line of the event log is not a readable event record",
            detail={"path": str(log_path), "line_number": len(lines)},
        ) from exc
    if not isinstance(value, int) or isinstance(value, bool):
        raise EventLogCorruptError(
            "the final event record carries a non-integer seq",
            detail={"path": str(log_path), "seq": str(value)},
        )
    return value


def append(log_path: Path, payload: Mapping[str, JsonValue]) -> Event:
    """Append one event, assigning its ``seq``. Durable when this returns.

    ``payload`` carries ``event``, ``ts``, ``actor`` and that type's fields --
    but never ``seq``. Sequence assignment belongs to the log, and a caller
    able to choose one is a caller able to create a duplicate.
    """
    if "seq" in payload:
        raise ValueError("seq is assigned by the event log and must not be supplied by the caller")

    log_path.parent.mkdir(parents=True, exist_ok=True)

    with _append_lock(log_path):
        event = _EVENT_ADAPTER.validate_python({**payload, "seq": last_seq(log_path) + 1})
        line = canonical_json(event.model_dump(mode="json")) + b"\n"
        # "ab" is O_APPEND: the write lands at end-of-file atomically even if
        # another writer extended it between our seq read and this call.
        with log_path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    return event


def replay(log_path: Path) -> list[Event]:
    """Read the whole log, validating it as we go.

    Raises ``EventLogCorruptError`` on an unparseable line, an unknown event
    type (section 3.6 is closed), or any ``seq`` that is not exactly its
    zero-based position. The file is never written to by this function -- not
    even to fix what it finds.
    """
    events: list[Event] = []
    for index, line in enumerate(_iter_raw_lines(log_path)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventLogCorruptError(
                "event log contains an unparseable line",
                detail={"path": str(log_path), "line_number": index + 1, "reason": str(exc)},
            ) from exc

        try:
            event = _EVENT_ADAPTER.validate_python(record)
        except ValidationError as exc:
            raise EventLogCorruptError(
                "event log contains a record that is not a known event type",
                detail={
                    "path": str(log_path),
                    "line_number": index + 1,
                    "event": str(record.get("event")) if isinstance(record, dict) else None,
                },
            ) from exc

        if event.seq != index:
            raise EventLogCorruptError(
                "event log sequence is not gapless and strictly monotonic",
                detail={
                    "path": str(log_path),
                    "line_number": index + 1,
                    "expected_seq": index,
                    "actual_seq": event.seq,
                },
            )
        events.append(event)

    return events
