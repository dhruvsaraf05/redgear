"""T-0014: failing tests for redgear.state_engine -- the write path.

``state_engine`` exists (T-0013) but its write functions do not, so the
import block below fails at COLLECTION with ``ImportError``. That is the
correct red state for this pair.

This is the only module permitted to write ``.redgear/`` (section 11.1 rule
4). Three properties carry the weight:

* **One event per transition.** G4 says every state transition appends
  exactly one line. Two events for one transition double-counts history;
  zero events makes the transition invisible to replay. Counted on both
  sides below, not eyeballed.
* **Atomic persistence.** NFR-7: temp file, fsync, ``os.replace``. A
  truncate-and-write leaves a window where a concurrent reader sees a
  half-written projection, and a crash in that window destroys the audit
  trail the product exists to provide.
* **Honest exit is free.** G3: a ``blocked`` or ``scope_insufficient``
  outcome escalates without consuming an attempt. If honesty costs an
  attempt, a stuck agent is structurally incentivised to fake a pass
  instead -- which is the single failure mode redgear exists to prevent.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from redgear.errors import RedgearError
from redgear.events import replay as replay_events
from redgear.schemas import TaskGraph
from redgear.state_engine import (
    claim_task,
    escalate_task,
    mark_verified,
    persist_graph,
    recompute_readiness,
    reject_task,
    render_graph,
    replay_graph,
)

# --- a small, real repository to write into -------------------------------


def _node(
    node_id: str,
    *,
    node_type: str = "test_authoring",
    state: str = "blocked",
    depends_on: list[str] | None = None,
    writable: list[str] | None = None,
    frozen: list[str] | None = None,
    inherits: list[str] | None = None,
) -> dict[str, Any]:
    own = (
        []
        if node_type == "implementation"
        else [
            {
                "id": "AC-1",
                "statement": "It holds.",
                "verified_by": {"kind": "test", "selector": f"tests/t.py::test_{node_id}"},
            }
        ]
    )
    return {
        "id": node_id,
        "type": node_type,
        "title": f"node {node_id}",
        "state": state,
        "spec_refs": ["FR-1"],
        "spec_hash": "sha256:" + "0" * 64,
        "depends_on": depends_on or [],
        "scope": {
            "writable_globs": writable if writable is not None else ["tests/**"],
            "creatable_globs": writable if writable is not None else ["tests/**"],
            "frozen_globs": frozen if frozen is not None else ["src/**"],
        },
        "acceptance_criteria": own,
        "inherits_criteria_from": inherits or [],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }


def _graph_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "spec_hash": "sha256:" + "0" * 64,
        "state": "active",
        "generated_at": "2026-08-11T00:00:00Z",
        "nodes": [
            _node("T-0001", state="ready"),
            _node("T-0002", node_type="implementation", depends_on=["T-0001"], inherits=["T-0001"]),
        ],
        "edges": [{"from": "T-0001", "to": "T-0002", "kind": "hard"}],
    }


@pytest.fixture
def repo(git_repo: Path) -> Path:
    """A real git repository (conftest's fixture) with a .redgear/ in it.

    Real git, because claiming records a real ``base_commit`` -- section 10.4
    forbids mocking subprocess for anything that is about real repository
    state.
    """
    redgear = git_repo / ".redgear"
    (redgear / "spec").mkdir(parents=True)
    (redgear / "task_graph.json").write_text(
        json.dumps(_graph_payload(), indent=2) + "\n", encoding="utf-8"
    )
    return git_repo


@pytest.fixture
def graph() -> TaskGraph:
    return TaskGraph.model_validate(_graph_payload())


def _events_path(repo: Path) -> Path:
    return repo / ".redgear" / "events.jsonl"


def _event_count(repo: Path) -> int:
    return len(replay_events(_events_path(repo)))


# ---------------------------------------------------------------------------
# AC-1: exactly one event per transition.
# ---------------------------------------------------------------------------


def test_one_event_per_transition(repo: Path, graph: TaskGraph) -> None:
    """Count transitions, count events, assert equal. G4 admits no slack:
    a transition with two events double-counts history, one with none is
    invisible to replay."""
    assert _event_count(repo) == 0

    transitions = 0

    after_claim = claim_task(repo, graph, "T-0001", actor="engine")
    transitions += 1
    assert _event_count(repo) == transitions

    after_reject = reject_task(
        repo,
        after_claim,
        "T-0001",
        actor="engine",
        proof_id="proof-1",
        failed_gates=["tests_pass"],
        summary="GATE tests_pass FAILED",
    )
    transitions += 1
    assert _event_count(repo) == transitions

    after_reclaim = claim_task(repo, after_reject, "T-0001", actor="engine")
    transitions += 1
    assert _event_count(repo) == transitions

    final = mark_verified(repo, after_reclaim, "T-0001", actor="engine", proof_id="proof-2")
    transitions += 1
    assert _event_count(repo) == transitions

    assert transitions == 4
    assert final.nodes[0].state == "verified"

    # Each event is a distinct, correctly typed record in order.
    kinds = [event.event for event in replay_events(_events_path(repo))]
    assert kinds == ["task_claimed", "task_rejected", "task_claimed", "task_verified"]


def test_replay_round_trips_after_every_write(repo: Path, graph: TaskGraph) -> None:
    """After each write the projection on disk must equal replay(plan, log).
    This round trip is what makes G4 true rather than asserted."""
    definition = TaskGraph.model_validate(_graph_payload())
    current = graph

    for step in range(3):
        current = claim_task(repo, current, "T-0001", actor="engine")
        current = reject_task(
            repo,
            current,
            "T-0001",
            actor="engine",
            proof_id=f"proof-{step}",
            failed_gates=["lint"],
            summary="nope",
        )
        on_disk = (repo / ".redgear" / "task_graph.json").read_text(encoding="utf-8")
        rebuilt = replay_graph(definition, replay_events(_events_path(repo)))
        assert render_graph(rebuilt) == on_disk, f"divergence after step {step}"


# ---------------------------------------------------------------------------
# AC-2: claiming records the baseline and a digest per frozen file.
# ---------------------------------------------------------------------------


def test_claim_records_baseline_and_digests(repo: Path, graph: TaskGraph) -> None:
    """base_commit is the real HEAD; every file matching a frozen glob gets a
    SHA-256. That digest map is the mechanical heart of G2 -- without it the
    frozen-hash gate has nothing to compare against."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    claimed = claim_task(repo, graph, "T-0001", actor="engine")
    node = next(n for n in claimed.nodes if n.id == "T-0001")

    assert node.state == "claimed"
    assert node.claim is not None
    assert node.claim.base_commit == head, "base_commit is not the real HEAD"

    # T-0001 freezes src/**; the fixture repo has exactly one file there.
    assert set(node.claim.frozen_hashes) == {"src/pkg/__init__.py"}
    for path, digest in node.claim.frozen_hashes.items():
        assert digest.startswith("sha256:"), f"{path} digest is not algorithm-prefixed"
        assert len(digest) == len("sha256:") + 64

    # The event records the same baseline and the count of frozen files.
    events = replay_events(_events_path(repo))
    claimed_event = events[-1]
    assert claimed_event.event == "task_claimed"
    assert claimed_event.base_commit == head
    assert claimed_event.frozen_file_count == len(node.claim.frozen_hashes)
    assert claimed_event.attempt == node.attempts + 1


def test_claim_digests_change_when_a_frozen_file_changes(repo: Path, graph: TaskGraph) -> None:
    """The digest map must actually track content -- a map that did not move
    when a frozen file moved would make the G2 gate blind."""
    first = claim_task(repo, graph, "T-0001", actor="engine")
    first_node = next(n for n in first.nodes if n.id == "T-0001")
    assert first_node.claim is not None
    before = dict(first_node.claim.frozen_hashes)

    (repo / "src" / "pkg" / "__init__.py").write_bytes(b"# changed\n")

    second = claim_task(repo, graph, "T-0001", actor="engine")
    second_node = next(n for n in second.nodes if n.id == "T-0001")
    assert second_node.claim is not None
    assert second_node.claim.frozen_hashes != before


# ---------------------------------------------------------------------------
# AC-3: writes are atomic.
# ---------------------------------------------------------------------------


def test_writes_are_atomic(repo: Path, graph: TaskGraph, monkeypatch: pytest.MonkeyPatch) -> None:
    """temp -> fsync -> os.replace, never truncate-and-write (NFR-7)."""
    target = repo / ".redgear" / "task_graph.json"
    original = target.read_text(encoding="utf-8")

    replaces: list[tuple[str, str]] = []
    fsyncs: list[int] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def spy_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        replaces.append((str(src), str(dst)))
        real_replace(src, dst, **kwargs)

    def spy_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "fsync", spy_fsync)

    persist_graph(repo, graph)

    assert replaces, "projection was written without os.replace -- not atomic"
    assert fsyncs, "projection was written without fsync -- a crash loses it"
    src, dst = replaces[-1]
    assert src != dst, "replace source and destination are the same path"
    assert Path(dst) == target
    assert render_graph(graph) == target.read_text(encoding="utf-8")

    # No temp file survives a successful write.
    leftovers = sorted(
        p.name for p in target.parent.iterdir() if p.is_file() and p.name != target.name
    )
    assert leftovers == [], f"temp files left behind: {leftovers}"

    # If the replace fails, the previous projection must survive intact --
    # that is the whole point of not truncating the target.
    target.write_text(original, encoding="utf-8")

    def exploding_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError, match="simulated crash"):
        persist_graph(repo, graph)
    assert target.read_text(encoding="utf-8") == original, "failed write corrupted the projection"


# ---------------------------------------------------------------------------
# AC-4: illegal transitions are rejected.
# ---------------------------------------------------------------------------


def test_illegal_transition_rejected(repo: Path, graph: TaskGraph) -> None:
    """Section 4.2 is the authority. A blocked task cannot be claimed, and a
    verified task cannot be re-verified."""
    # T-0002 is blocked (its dependency is not verified) -- not claimable.
    with pytest.raises(RedgearError) as excinfo:
        claim_task(repo, recompute_readiness(graph), "T-0002", actor="engine")
    assert excinfo.value.code == "E_TASK_STATE"
    assert "T-0002" in json.dumps(excinfo.value.detail)

    # An illegal transition must not have appended an event.
    assert _event_count(repo) == 0

    verified = mark_verified(
        repo,
        claim_task(repo, graph, "T-0001", actor="engine"),
        "T-0001",
        actor="engine",
        proof_id="proof-1",
    )
    before = _event_count(repo)
    with pytest.raises(RedgearError) as excinfo:
        mark_verified(repo, verified, "T-0001", actor="engine", proof_id="proof-2")
    assert excinfo.value.code == "E_TASK_STATE"
    assert _event_count(repo) == before, "a rejected transition still wrote an event"


def test_unknown_task_id_rejected(repo: Path, graph: TaskGraph) -> None:
    with pytest.raises(RedgearError):
        claim_task(repo, graph, "T-9999", actor="engine")
    assert _event_count(repo) == 0


# ---------------------------------------------------------------------------
# AC-5: honest exit is free (G3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["blocked", "scope_insufficient"])
def test_blocked_does_not_consume_attempt(repo: Path, graph: TaskGraph, outcome: str) -> None:
    """G3, and it is not negotiable. If honesty costs an attempt, a stuck
    agent is better off faking a pass -- which is the exact failure mode this
    project exists to catch."""
    claimed = claim_task(repo, graph, "T-0001", actor="engine")
    node_before = next(n for n in claimed.nodes if n.id == "T-0001")
    attempts_before = node_before.attempts

    escalated = escalate_task(
        repo,
        claimed,
        "T-0001",
        actor="claude-code",
        outcome=outcome,
        detail="cannot proceed honestly",
    )
    node_after = next(n for n in escalated.nodes if n.id == "T-0001")

    assert node_after.state == "escalated"
    assert node_after.attempts == attempts_before, (
        f"{outcome} consumed an attempt -- G3 requires honest exit to be free"
    )

    events = replay_events(_events_path(repo))
    assert events[-1].event == "task_escalated"
    assert events[-1].attempted == attempts_before


def test_rejection_does_consume_an_attempt(repo: Path, graph: TaskGraph) -> None:
    """The counterpart: a real failed verification DOES cost an attempt.
    Without this, G3's exemption would be meaningless."""
    claimed = claim_task(repo, graph, "T-0001", actor="engine")
    before = next(n for n in claimed.nodes if n.id == "T-0001").attempts

    rejected = reject_task(
        repo,
        claimed,
        "T-0001",
        actor="engine",
        proof_id="proof-1",
        failed_gates=["tests_pass"],
        summary="failed",
    )
    after = next(n for n in rejected.nodes if n.id == "T-0001").attempts
    assert after == before + 1


def test_attempts_exhausted_escalates_and_counts(repo: Path, graph: TaskGraph) -> None:
    """Exhaustion is the other escalation route, and it reports the attempts
    actually spent."""
    current = graph
    for index in range(3):
        current = claim_task(repo, current, "T-0001", actor="engine")
        current = reject_task(
            repo,
            current,
            "T-0001",
            actor="engine",
            proof_id=f"proof-{index}",
            failed_gates=["tests_pass"],
            summary="failed",
        )
    node = next(n for n in current.nodes if n.id == "T-0001")
    assert node.attempts == 3

    escalated = escalate_task(
        repo,
        current,
        "T-0001",
        actor="engine",
        outcome="attempts_exhausted",
        detail="3 attempts consumed without a passing verdict",
    )
    node = next(n for n in escalated.nodes if n.id == "T-0001")
    assert node.state == "escalated"
    assert node.attempts == 3
    assert replay_events(_events_path(repo))[-1].attempted == 3
