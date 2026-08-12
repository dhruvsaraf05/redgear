"""T-0010: failing tests for redgear.events -- the append-only log.

``redgear/events.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

This is G4. ``.redgear/events.jsonl`` is the single source of truth for the
whole system and every other state file is a projection of it, so the
properties asserted here are not hygiene -- they are the guarantee:

* **Gapless, strictly monotonic ``seq``.** A gap means an event was lost; a
  repeat means two writers raced. Either way the log no longer describes what
  happened, and every projection built from it is fiction.
* **Corruption is reported, never repaired.** Silently renumbering a corrupt
  audit trail produces a log that looks clean and is a lie. A crash naming the
  problem is strictly better.
* **Durable before the call returns.** A buffered write that a crash loses
  gives an orchestrator that believes it dispatched a prompt it cannot prove
  it sent.
* **Concurrency-safe.** Exercised with real threads below, not a mock -- a
  lock that only works because nothing contended it is not a lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from redgear.events import append, last_seq, replay
from redgear.errors import EventLogCorruptError

REPO_ROOT = Path(__file__).resolve().parents[1]

SPEC_HASH = "sha256:dd2914150ecf303b0e5a584f508c32807f72f75277c488c788eae06c4f31e988"
BASE_COMMIT = "8ac1471000000000000000000000000000000a"

BUDGET: dict[str, Any] = {
    "max_iterations": 50,
    "max_wall_clock_s": 7200,
    "max_consecutive_failures": 5,
    "max_turns_per_dispatch": 25,
    "per_turn_usd": None,
    "dispatch_timeout_s": 900,
}

# One payload per CLAUDE.md section 3.6 event type, WITHOUT `seq` -- append()
# owns sequence assignment and a caller must never supply it. Each carries the
# other two base fields (`ts`, `actor`) plus that row's payload column.
EVENT_PAYLOADS: list[dict[str, Any]] = [
    {
        "event": "run_started",
        "ts": "2026-08-11T00:00:00Z",
        "actor": "engine",
        "run_id": "run_01J8X",
        "budget": BUDGET,
        "base_commit": BASE_COMMIT,
    },
    {
        "event": "run_ended",
        "ts": "2026-08-11T01:00:00Z",
        "actor": "engine",
        "run_id": "run_01J8X",
        "reason": "complete",
        "iterations": 12,
        "tasks_verified": 5,
        "tasks_escalated": 0,
        "duration_ms": 3_600_000,
    },
    {
        "event": "run_aborted",
        "ts": "2026-08-11T00:30:00Z",
        "actor": "engine",
        "run_id": "run_01J8X",
        "signal": "SIGINT",
        "iteration": 3,
        "task_id": None,
    },
    {
        "event": "plan_generated",
        "ts": "2026-08-11T00:00:00Z",
        "actor": "engine",
        "spec_hash": SPEC_HASH,
        "node_count": 41,
        "edge_count": 49,
        "source_document": "docs/PRD.md",
    },
    {
        "event": "plan_approved",
        "ts": "2026-08-11T00:00:01Z",
        "actor": "human",
        "spec_hash": SPEC_HASH,
        "approved_by": "dhruvsaraf05@gmail.com",
    },
    {
        "event": "spec_updated",
        "ts": "2026-08-12T00:00:00Z",
        "actor": "human",
        "old_spec_hash": None,
        "new_spec_hash": SPEC_HASH,
        "added": ["FR-1"],
        "removed": [],
        "modified": [],
        "tasks_marked_drift": [],
    },
    {
        "event": "task_claimed",
        "ts": "2026-08-11T00:01:00Z",
        "actor": "engine",
        "task_id": "T-0003",
        "attempt": 1,
        "claim_token": "claim-T-0003-1-a1b2c3",
        "base_commit": BASE_COMMIT,
        "lease_expires": "2026-08-11T00:16:00Z",
        "frozen_file_count": 3,
    },
    {
        "event": "prompt_dispatched",
        "ts": "2026-08-11T00:01:05Z",
        "actor": "engine",
        "task_id": "T-0003",
        "attempt": 1,
        "prompt_path": ".redgear/runs/run_01J8X/iterations/0001/prompt.txt",
        "prompt_sha256": "c" * 64,
        "allowed_tools": ["Read", "Glob", "Grep"],
    },
    {
        "event": "turn_completed",
        "ts": "2026-08-11T00:05:00Z",
        "actor": "claude-code",
        "task_id": "T-0003",
        "attempt": 1,
        "outcome": "completed",
        "exit_code": 0,
        "num_turns": 4,
        "duration_ms": 12345,
        "cost_usd_estimate": 0.42,
        "parse_ok": True,
    },
    {
        "event": "task_verified",
        "ts": "2026-08-11T00:05:10Z",
        "actor": "engine",
        "task_id": "T-0003",
        "attempt": 1,
        "proof_id": "proof-0003-1",
        "spec_hash": SPEC_HASH,
        "gates_passed": ["scope_check", "frozen_hash_check"],
        "duration_ms": 890,
    },
    {
        "event": "task_rejected",
        "ts": "2026-08-11T00:05:10Z",
        "actor": "engine",
        "task_id": "T-0003",
        "attempt": 1,
        "proof_id": "proof-0003-1",
        "failed_gates": ["tests_pass"],
        "attempts_remaining": 2,
        "summary": "GATE tests_pass FAILED",
    },
    {
        "event": "task_escalated",
        "ts": "2026-08-11T00:05:11Z",
        "actor": "engine",
        "task_id": "T-0003",
        "reason": "attempts_exhausted",
        "category": None,
        "detail": "3 attempts consumed without a passing verdict.",
        "attempted": 3,
    },
    {
        "event": "lease_expired",
        "ts": "2026-08-12T00:05:00Z",
        "actor": "engine",
        "task_id": "T-0007",
        "attempt": 1,
        "claim_token": "claim-T-0007-1-d4e5f6",
        "counted_as_attempt": False,
    },
    {
        "event": "adr_logged",
        "ts": "2026-08-12T00:10:00Z",
        "actor": "human",
        "adr_id": "ADR-0007",
        "task_id": "T-0007",
        "title": "Integer minor units only for money",
        "rule": "Store money as integer minor units; never a float.",
        "applies_to": ["redgear/ledger/**"],
        "supersedes": None,
    },
]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    """An event log under tmp_path. Never the repository's real one."""
    return tmp_path / "events.jsonl"


# ---------------------------------------------------------------------------
# AC-1: gapless, strictly monotonic seq.
# ---------------------------------------------------------------------------


def test_seq_monotonic_gapless(log_path: Path) -> None:
    """append() owns seq assignment: 0, 1, 2, ... with no gaps or repeats."""
    assert last_seq(log_path) == -1, "an absent log reports -1, not 0"

    written = []
    for index in range(25):
        payload = dict(EVENT_PAYLOADS[index % len(EVENT_PAYLOADS)])
        event = append(log_path, payload)
        written.append(event.seq)
        assert last_seq(log_path) == index

    assert written == list(range(25))

    replayed = replay(log_path)
    assert [event.seq for event in replayed] == list(range(25))

    # A caller must not be able to dictate seq -- that is how a duplicate gets
    # in. Supplying one is rejected rather than silently honoured.
    with pytest.raises((ValueError, TypeError)):
        append(log_path, {**EVENT_PAYLOADS[0], "seq": 999})


def test_seq_survives_reopen(log_path: Path) -> None:
    """Sequence continues across separate append sessions -- state lives in
    the file, never in process memory."""
    append(log_path, EVENT_PAYLOADS[0])
    append(log_path, EVENT_PAYLOADS[1])

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,json;from pathlib import Path;"
            "from redgear.events import append,last_seq;"
            "p=Path(sys.argv[1]);"
            "append(p, json.loads(sys.argv[2]));"
            "sys.stdout.write(str(last_seq(p)))",
            str(log_path),
            json.dumps(EVENT_PAYLOADS[2]),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "2"
    assert [event.seq for event in replay(log_path)] == [0, 1, 2]


def test_concurrent_appends_never_duplicate_seq(log_path: Path) -> None:
    """Real threads, real contention. A lock that is never contended is not
    evidence of anything."""
    thread_count = 8
    per_thread = 12
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            for _ in range(per_thread):
                append(log_path, EVENT_PAYLOADS[worker_id % len(EVENT_PAYLOADS)])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"appender raised: {errors[0]!r}"

    seqs = [event.seq for event in replay(log_path)]
    expected = thread_count * per_thread
    assert len(seqs) == expected, f"expected {expected} lines, got {len(seqs)}"
    assert sorted(seqs) == list(range(expected)), "duplicate or missing seq under contention"


# ---------------------------------------------------------------------------
# AC-2: every event type round-trips.
# ---------------------------------------------------------------------------


def test_all_event_types_round_trip(log_path: Path) -> None:
    """All 14 section 3.6 types survive write-then-replay without loss."""
    assert len(EVENT_PAYLOADS) == 14, "section 3.6 closes the taxonomy at 14 types"
    assert len({p["event"] for p in EVENT_PAYLOADS}) == 14, "one payload per type"

    for payload in EVENT_PAYLOADS:
        append(log_path, payload)

    replayed = replay(log_path)
    assert len(replayed) == 14

    for index, (payload, event) in enumerate(zip(EVENT_PAYLOADS, replayed, strict=True)):
        assert event.event == payload["event"]
        assert event.seq == index
        assert event.actor == payload["actor"]
        # Every non-base field the payload declared survives with its value.
        dumped = event.model_dump(mode="json")
        for key, value in payload.items():
            assert dumped[key] == value, f"{payload['event']}.{key} changed in round trip"


def test_log_line_format(log_path: Path) -> None:
    """One JSON object per line, UTF-8, newline-terminated, no trailing
    whitespace. A log a human cannot `tail` is a worse audit trail."""
    for payload in EVENT_PAYLOADS[:5]:
        append(log_path, payload)

    raw = log_path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw, "CRLF would make the log platform-dependent"

    text = raw.decode("utf-8")
    lines = text.split("\n")[:-1]
    assert len(lines) == 5
    for line in lines:
        assert line == line.rstrip(), "trailing whitespace on a log line"
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        assert "seq" in parsed and "ts" in parsed and "actor" in parsed and "event" in parsed


# ---------------------------------------------------------------------------
# AC-3: corruption reported, never repaired.
# ---------------------------------------------------------------------------


def _write_raw(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def test_corruption_reported_not_repaired(log_path: Path) -> None:
    """A gap or a repeat raises E_LOG_CORRUPT, and the file is left exactly
    as it was found. Silent renumbering would produce a log that looks clean
    and is a lie."""
    base = dict(EVENT_PAYLOADS[0])

    # --- a gap: 0, 1, 3 ---
    _write_raw(log_path, [{**base, "seq": s} for s in (0, 1, 3)])
    before = log_path.read_bytes()
    with pytest.raises(EventLogCorruptError) as excinfo:
        replay(log_path)
    assert excinfo.value.code == "E_LOG_CORRUPT"
    assert log_path.read_bytes() == before, "replay modified a corrupt log -- never repair"
    # The report names where it broke, or the human cannot act on it.
    assert "3" in json.dumps(excinfo.value.detail) or "2" in json.dumps(excinfo.value.detail)

    # --- a repeat: 0, 1, 1 ---
    _write_raw(log_path, [{**base, "seq": s} for s in (0, 1, 1)])
    before = log_path.read_bytes()
    with pytest.raises(EventLogCorruptError):
        replay(log_path)
    assert log_path.read_bytes() == before

    # --- out of order: 0, 2, 1 ---
    _write_raw(log_path, [{**base, "seq": s} for s in (0, 2, 1)])
    with pytest.raises(EventLogCorruptError):
        replay(log_path)

    # --- not starting at 0 ---
    _write_raw(log_path, [{**base, "seq": s} for s in (1, 2)])
    with pytest.raises(EventLogCorruptError):
        replay(log_path)


def test_unparseable_line_is_corruption(log_path: Path) -> None:
    """A truncated or malformed line is corruption too, not a line to skip."""
    append(log_path, EVENT_PAYLOADS[0])
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"event": "run_started", "seq": 1, trunc\n')

    before = log_path.read_bytes()
    with pytest.raises(EventLogCorruptError):
        replay(log_path)
    assert log_path.read_bytes() == before


def test_unknown_event_type_is_corruption(log_path: Path) -> None:
    """Section 3.6 is closed. A line naming a type outside it means the log
    was written by something that does not share this contract."""
    _write_raw(log_path, [{**EVENT_PAYLOADS[0], "seq": 0, "event": "not_a_real_event"}])
    with pytest.raises(EventLogCorruptError):
        replay(log_path)


def test_replay_of_absent_or_empty_log_is_empty(log_path: Path) -> None:
    """No log yet is not corruption -- it is a run that has not started."""
    assert replay(log_path) == []
    log_path.write_bytes(b"")
    assert replay(log_path) == []


# ---------------------------------------------------------------------------
# AC-4: durability.
# ---------------------------------------------------------------------------


def test_append_is_durable(log_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The data is on disk before append() returns.

    Two independent checks, because either alone is weak: a separate OS
    process can read the record back (so it left this process's buffers), and
    ``os.fsync`` was actually invoked (so it was pushed past the OS cache
    rather than left for the writeback thread to lose on power failure).
    """
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)

    append(log_path, EVENT_PAYLOADS[0])
    assert fsync_calls, "append() returned without fsync -- a crash would lose the record"

    # A separate process sees it, so it is genuinely written, not buffered.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from pathlib import Path\n"
            "sys.stdout.write(Path(sys.argv[1]).read_text('utf-8'))",
            str(log_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout.strip())["seq"] == 0


def test_append_leaves_no_lock_behind(log_path: Path) -> None:
    """A crash-free append must not strand a lock file: the next run would
    refuse to start and the user would have to delete a file they do not
    understand (section 8.3's reasoning, applied to the log)."""
    for payload in EVENT_PAYLOADS[:3]:
        append(log_path, payload)

    leftovers = [p.name for p in log_path.parent.iterdir() if p.name != log_path.name]
    assert leftovers == [], f"append left files behind: {leftovers}"
