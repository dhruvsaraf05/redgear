"""State engine -- read path: load, validate, project.

The only module permitted to write ``.redgear/`` (section 11.1 rule 4). This
half does not write; the write path arrives with T-0014/T-0015.

``task_graph.json`` is a **projection** of ``events.jsonl`` (G4). The
projection is rendered byte-identically, because `redgear rebuild` compares a
replay against the file on disk and reports divergence -- a serialiser that
reordered a key or dropped a null would make every rebuild look divergent and
the check worthless.

**Scope of replay (a real limitation, not an oversight).** Section 3.6's
`plan_generated` carries `spec_hash`, `node_count`, `edge_count` and
`source_document` -- not the node and edge definitions, and no other event in
the closed taxonomy carries them either. The log therefore cannot rebuild
graph *structure*; it rebuilds the *mutable* state laid over a plan. So
``replay_graph`` takes the plan as its base. Closing that gap needs a new or
widened event, which is an ADR-worthy change to section 3.6.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from redgear.errors import (
    GraphCycleError,
    JsonValue,
    ScopeOverlapError,
    TaskStateError,
    UnknownNodeRefError,
)
from redgear.events import append as append_event
from redgear.hashing import digest_map
from redgear.paths import events_path, find_scope_overlaps, match_glob, task_graph_path
from redgear.schemas import Claim, Escalation, Event, GateName, TaskGraph, TaskNode

#: States recomputation may move between. Everything else has already left the
#: queue -- recomputing a `verified` node back to `ready` would re-run proven
#: work, and reviving an `escalated` node would bypass the human it is waiting
#: on.
_RECOMPUTABLE = frozenset({"blocked", "ready"})

#: Applied to a node when its dependencies are all verified.
_READY = "ready"
_BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Load and render
# ---------------------------------------------------------------------------


def load_graph(repo_root: Path, *, tracked_files: Sequence[str] | None = None) -> TaskGraph:
    """Read and validate ``.redgear/task_graph.json``.

    Validation runs on every load (section 4.4), so no caller can forget it
    and operate on a graph that breaks an invariant.
    """
    path = task_graph_path(repo_root)
    graph = TaskGraph.model_validate(json.loads(path.read_text(encoding="utf-8")))
    validate_graph(graph, tracked_files=tracked_files)
    return graph


def render_graph(graph: TaskGraph) -> str:
    """Serialise the projection exactly as it is stored on disk.

    ``exclude_unset`` is what makes this byte-identical: fields the plan never
    set (``note`` on most nodes) stay absent, while fields explicitly recorded
    as null (``claim``, ``verified_at``, ``proof_id``, ``escalation``) are
    written as null. Dumping everything would add ``"note": null`` to 30-odd
    nodes and diverge from the file.
    """
    payload = graph.model_dump(mode="json", exclude_unset=True)
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Invariants -- section 4.4
# ---------------------------------------------------------------------------


def find_cycle(graph: TaskGraph) -> list[str] | None:
    """Return the ids left unsorted by Kahn's algorithm, or None if acyclic.

    Whatever cannot be topologically ordered is exactly the set involved in
    (or downstream of) a cycle, which is what a human needs named.
    """
    ids = {node.id for node in graph.nodes}
    remaining: dict[str, int] = {}
    successors: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for node in graph.nodes:
        deps = [dep for dep in node.depends_on if dep in ids]
        remaining[node.id] = len(deps)
        for dep in deps:
            successors[dep].append(node.id)

    queue = sorted(node_id for node_id, count in remaining.items() if count == 0)
    ordered = 0
    while queue:
        current = queue.pop(0)
        ordered += 1
        for successor in successors[current]:
            remaining[successor] -= 1
            if remaining[successor] == 0:
                queue.append(successor)
        queue.sort()

    if ordered == len(ids):
        return None
    return sorted(node_id for node_id, count in remaining.items() if count > 0)


def validate_graph(graph: TaskGraph, *, tracked_files: Sequence[str] | None = None) -> None:
    """Enforce every section 4.4 invariant. A violation is a hard error.

    Scaffold nodes are exempt from invariants 4-6 (invariant 8 / section 4.5):
    they author their own smoke criteria and have no two-phase partner.
    """
    ids = {node.id for node in graph.nodes}
    by_id = {node.id: node for node in graph.nodes}

    # Invariant 2 -- references resolve. Checked before acyclicity so a
    # dangling id is reported as such rather than as a phantom cycle.
    for node in graph.nodes:
        for dep in node.depends_on:
            if dep not in ids:
                raise UnknownNodeRefError(
                    f"task {node.id} depends on unknown node {dep}",
                    detail={"task_id": node.id, "unknown_ref": dep},
                )
    for edge in graph.edges:
        for endpoint in (edge.from_, edge.to):
            if endpoint not in ids:
                raise UnknownNodeRefError(
                    f"edge endpoint {endpoint} names no existing node",
                    detail={"edge_from": edge.from_, "edge_to": edge.to, "unknown_ref": endpoint},
                )

    # Invariant 1 -- acyclic.
    cycle = find_cycle(graph)
    if cycle is not None:
        raise GraphCycleError(
            "task graph edge set is not acyclic",
            detail={"cycle": list(cycle)},
        )

    for node in graph.nodes:
        # Invariant 8: scaffold is exempt from 4-6.
        if node.type != "scaffold":
            # Invariant 6 -- an implementation node inherits its criteria.
            if node.type == "implementation" and not node.inherits_criteria_from:
                raise UnknownNodeRefError(
                    f"implementation task {node.id} inherits no acceptance criteria; "
                    "nothing could verify it",
                    detail={"task_id": node.id, "invariant": "4.4-6"},
                )
            # Invariant 4 -- inherit only from test_authoring nodes.
            for source_id in node.inherits_criteria_from:
                source = by_id.get(source_id)
                if source is None:
                    raise UnknownNodeRefError(
                        f"task {node.id} inherits criteria from unknown node {source_id}",
                        detail={"task_id": node.id, "unknown_ref": source_id},
                    )
                if source.type != "test_authoring":
                    raise UnknownNodeRefError(
                        f"task {node.id} inherits criteria from {source_id}, which is a "
                        f"{source.type} node, not test_authoring",
                        detail={
                            "task_id": node.id,
                            "source_id": source_id,
                            "source_type": source.type,
                            "invariant": "4.4-4",
                        },
                    )
            # Invariant 5 -- inheriting means authoring nothing of its own.
            if node.inherits_criteria_from and node.acceptance_criteria:
                raise UnknownNodeRefError(
                    f"task {node.id} both inherits and authors acceptance criteria",
                    detail={"task_id": node.id, "invariant": "4.4-5"},
                )

        # Invariant 7 -- writable and frozen globs disjoint over tracked files.
        if tracked_files:
            overlaps = find_scope_overlaps(
                node.scope.writable_globs, node.scope.frozen_globs, list(tracked_files)
            )
            if overlaps:
                raise ScopeOverlapError(
                    f"task {node.id} has writable and frozen globs that overlap",
                    detail={"task_id": node.id, "overlaps": list(overlaps)},
                )


# ---------------------------------------------------------------------------
# Readiness and selection
# ---------------------------------------------------------------------------


def recompute_readiness(graph: TaskGraph) -> TaskGraph:
    """Recompute blocked/ready from dependency state (invariant 3).

    Never cached and never trusted from the file: a stale `ready` would make
    an unready task selectable, which is how work gets built on an unproven
    foundation.
    """
    by_id = {node.id: node for node in graph.nodes}
    updated: list[TaskNode] = []
    for node in graph.nodes:
        if node.state not in _RECOMPUTABLE:
            updated.append(node)
            continue
        deps_verified = all(
            by_id[dep].state == "verified" for dep in node.depends_on if dep in by_id
        )
        target = _READY if deps_verified else _BLOCKED
        updated.append(node if node.state == target else node.model_copy(update={"state": target}))
    return graph.model_copy(update={"nodes": updated})


def next_ready_task(graph: TaskGraph) -> TaskNode | None:
    """The next task to work on, or None when nothing is claimable.

    Ordered by descending hard-dependency count, then ascending id: draining
    the most-depended-upon work first shortens the critical path, and the id
    tiebreak keeps selection deterministic so a run is reproducible from its
    log.

    Returns None rather than raising, matching section 4.1's loop skeleton
    (``if task is None: ... "complete_or_blocked"``).
    """
    ready = [node for node in graph.nodes if node.state == _READY]
    if not ready:
        return None
    ready.sort(key=lambda node: (-len(node.depends_on), node.id))
    return ready[0]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_graph(definition: TaskGraph, events: Sequence[Event]) -> TaskGraph:
    """Fold the event log onto the plan to produce the current projection.

    The plan supplies immutable node definitions; the log supplies mutable
    state. See the module docstring for why the log alone is not sufficient.
    """
    by_id = {node.id: node for node in definition.nodes}
    mutated: dict[str, TaskNode] = {}

    def current(task_id: str) -> TaskNode | None:
        if task_id in mutated:
            return mutated[task_id]
        return by_id.get(task_id)

    for event in events:
        task_id = getattr(event, "task_id", None)
        if not isinstance(task_id, str):
            continue
        node = current(task_id)
        if node is None:
            continue

        if event.event == "task_claimed":
            mutated[task_id] = node.model_copy(update={"state": "claimed"})
        elif event.event == "task_verified":
            mutated[task_id] = node.model_copy(
                update={
                    "state": "verified",
                    "verified_at": event.ts,
                    "proof_id": event.proof_id,
                    "claim": None,
                }
            )
        elif event.event == "task_rejected":
            mutated[task_id] = node.model_copy(
                update={"state": "rejected", "attempts": event.attempt, "claim": None}
            )
        elif event.event == "task_escalated":
            mutated[task_id] = node.model_copy(update={"state": "escalated", "claim": None})

    if not mutated:
        return definition
    nodes = [mutated.get(node.id, node) for node in definition.nodes]
    return definition.model_copy(update={"nodes": nodes})


# ---------------------------------------------------------------------------
# Write path -- transitions and atomic persistence (T-0014/T-0015)
#
# This is the only code permitted to write `.redgear/` (section 11.1 rule 4).
# ---------------------------------------------------------------------------


#: Section 4.2's state machine, as the states each transition may start from.
#: `rejected` is claimable because section 4.1 returns a failed task to the
#: queue for a corrective attempt; that re-entry is one transition and
#: therefore one event, not two.
_CLAIMABLE_FROM = frozenset({"ready", "rejected"})
_VERIFIABLE_FROM = frozenset({"claimed", "dispatched", "verifying"})
_REJECTABLE_FROM = frozenset({"claimed", "dispatched", "verifying"})
_ESCALATABLE_FROM = frozenset({"claimed", "dispatched", "verifying", "rejected"})

#: Outcomes that are honest exits rather than failures (G3). They escalate the
#: task and must never consume an attempt -- if honesty costs an attempt, a
#: stuck agent is structurally better off faking a pass.
_FREE_OUTCOMES = frozenset({"blocked", "scope_insufficient"})

#: How long a claim is good for. locks.py (T-0016/T-0017) owns lease policy;
#: this is the placeholder it will absorb.
_LEASE_SECONDS = 900


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _stamp(moment: datetime) -> str:
    """RFC 3339 UTC with a Z suffix, as section 3.6 requires of every `ts`."""
    return moment.isoformat().replace("+00:00", "Z")


def _git(repo_root: Path, *args: str) -> str:
    """Read-only git interrogation.

    A deliberately minimal stand-in: gitctx.py (T-0021) owns this and will
    absorb it. `shell=False` with a fixed argv (section 11.1 rule 1), and
    nothing here mutates the repository.
    """
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607 - git resolved via PATH, fixed argv
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo_files(repo_root: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored paths.

    Both halves matter for G2: a newly *added* file under a frozen glob is a
    violation the gate must catch, and it is untracked by definition.
    """
    listing = _git(repo_root, "ls-files", "--cached", "--others", "--exclude-standard")
    return [line.strip() for line in listing.splitlines() if line.strip()]


def _frozen_digest_map(repo_root: Path, frozen_globs: Sequence[str]) -> dict[str, str]:
    """SHA-256 every file matching a frozen glob. The mechanical heart of G2."""
    matched = [
        path
        for path in _repo_files(repo_root)
        if any(match_glob(path, pattern) for pattern in frozen_globs)
    ]
    return digest_map(repo_root, sorted(matched))


def persist_graph(repo_root: Path, graph: TaskGraph) -> None:
    """Write the projection atomically: temp, fsync, ``os.replace``.

    Never truncate-and-write (NFR-7). A concurrent reader sees either the
    whole previous projection or the whole new one, and a crash mid-write
    leaves the previous file untouched rather than a half-written audit trail.
    """
    target = task_graph_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = render_graph(graph).encode("utf-8")

    temp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # os.replace, not Path.replace: this is the atomic-rename syscall and
        # naming it directly is the point of the guarantee (NFR-7).
        os.replace(temp_path, target)  # noqa: PTH105
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _require(graph: TaskGraph, task_id: str) -> TaskNode:
    for node in graph.nodes:
        if node.id == task_id:
            return node
    raise UnknownNodeRefError(f"no task {task_id} in the graph", detail={"task_id": task_id})


def _require_state(node: TaskNode, allowed: frozenset[str], action: str) -> None:
    if node.state not in allowed:
        # Built by extend rather than list(): list is invariant, so
        # list[str] will not satisfy list[JsonValue] directly.
        allowed_from: list[JsonValue] = []
        allowed_from.extend(sorted(allowed))
        raise TaskStateError(
            f"cannot {action} task {node.id} from state {node.state!r}",
            detail={
                "task_id": node.id,
                "current_state": node.state,
                "action": action,
                "allowed_from": allowed_from,
            },
        )


def _commit_transition(
    repo_root: Path,
    graph: TaskGraph,
    node: TaskNode,
    updates: Mapping[str, object],
    event: Mapping[str, JsonValue],
) -> TaskGraph:
    """Apply one transition: exactly one event, then persist the projection.

    The event is appended before the projection is written, so a crash
    between the two leaves the log *ahead* of the projection -- a state
    `rebuild` can reconcile. The reverse order would leave a state change
    with no record, which it cannot.
    """
    updated = node.model_copy(update=dict(updates))
    nodes = [updated if existing.id == node.id else existing for existing in graph.nodes]
    new_graph = graph.model_copy(update={"nodes": nodes})

    append_event(events_path(repo_root), event)
    persist_graph(repo_root, new_graph)
    return new_graph


def claim_task(repo_root: Path, graph: TaskGraph, task_id: str, *, actor: str) -> TaskGraph:
    """Claim a ready task, recording the baseline and freezing the frozen set."""
    node = _require(graph, task_id)
    _require_state(node, _CLAIMABLE_FROM, "claim")

    now = _utc_now()
    attempt = node.attempts + 1
    base_commit = _git(repo_root, "rev-parse", "HEAD")
    frozen_hashes = _frozen_digest_map(repo_root, node.scope.frozen_globs)
    claim = Claim(
        base_commit=base_commit,
        frozen_hashes=frozen_hashes,
        allowed_tools=["Read", "Glob", "Grep"],
        claimed_at=now,
    )
    expires = _stamp(now + timedelta(seconds=_LEASE_SECONDS))

    return _commit_transition(
        repo_root,
        graph,
        node,
        {"state": "claimed", "claim": claim},
        {
            "event": "task_claimed",
            "ts": _stamp(now),
            "actor": actor,
            "task_id": task_id,
            "attempt": attempt,
            "claim_token": f"claim-{task_id}-{attempt}-{base_commit[:8]}",
            "base_commit": base_commit,
            "lease_expires": expires,
            "frozen_file_count": len(frozen_hashes),
        },
    )


def mark_verified(
    repo_root: Path, graph: TaskGraph, task_id: str, *, actor: str, proof_id: str
) -> TaskGraph:
    """Every gate passed: release the claim and record the proof."""
    node = _require(graph, task_id)
    _require_state(node, _VERIFIABLE_FROM, "verify")

    now = _utc_now()
    gates_passed: list[JsonValue] = [gate.value for gate in GateName]
    return _commit_transition(
        repo_root,
        graph,
        node,
        {"state": "verified", "verified_at": now, "proof_id": proof_id, "claim": None},
        {
            "event": "task_verified",
            "ts": _stamp(now),
            "actor": actor,
            "task_id": task_id,
            "attempt": node.attempts,
            "proof_id": proof_id,
            "spec_hash": node.spec_hash,
            "gates_passed": gates_passed,
            "duration_ms": 0,
        },
    )


def reject_task(
    repo_root: Path,
    graph: TaskGraph,
    task_id: str,
    *,
    actor: str,
    proof_id: str,
    failed_gates: Sequence[str],
    summary: str,
) -> TaskGraph:
    """A gate failed. This DOES consume an attempt -- which is what makes
    G3's exemption for an honest exit mean anything."""
    node = _require(graph, task_id)
    _require_state(node, _REJECTABLE_FROM, "reject")

    attempts = node.attempts + 1
    now = _utc_now()
    gates: list[JsonValue] = list(failed_gates)
    return _commit_transition(
        repo_root,
        graph,
        node,
        {"state": "rejected", "attempts": attempts, "claim": None},
        {
            "event": "task_rejected",
            "ts": _stamp(now),
            "actor": actor,
            "task_id": task_id,
            "attempt": attempts,
            "proof_id": proof_id,
            "failed_gates": gates,
            "attempts_remaining": max(node.max_attempts - attempts, 0),
            "summary": summary,
        },
    )


def escalate_task(
    repo_root: Path,
    graph: TaskGraph,
    task_id: str,
    *,
    actor: str,
    outcome: str,
    detail: str,
) -> TaskGraph:
    """Pause the task for a human.

    G3: a ``blocked`` or ``scope_insufficient`` outcome leaves ``attempts``
    untouched. Honesty has to be free, or a stuck agent is better off faking
    a pass -- the exact failure mode redgear exists to prevent.
    """
    node = _require(graph, task_id)
    _require_state(node, _ESCALATABLE_FROM, "escalate")

    now = _utc_now()
    honest_exit = outcome in _FREE_OUTCOMES
    reason: Literal["blocker", "attempts_exhausted"] = (
        "blocker" if honest_exit else "attempts_exhausted"
    )

    return _commit_transition(
        repo_root,
        graph,
        node,
        {
            "state": "escalated",
            "claim": None,
            "escalation": Escalation(reason=reason, detail=detail, escalated_at=now),
        },
        {
            "event": "task_escalated",
            "ts": _stamp(now),
            "actor": actor,
            "task_id": task_id,
            "reason": reason,
            "category": "ambiguous_task" if honest_exit else None,
            "detail": detail,
            # Attempts actually spent -- unchanged by an honest exit (G3).
            "attempted": node.attempts,
        },
    )
