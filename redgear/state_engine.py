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

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from redgear.errors import (
    AlreadyInitializedError,
    GraphCycleError,
    JsonValue,
    ScopeOverlapError,
    TaskStateError,
    UnknownNodeRefError,
)
from redgear.events import append as append_event
from redgear.hashing import digest_map
from redgear.paths import (
    events_path,
    find_scope_overlaps,
    match_glob,
    redgear_dir,
    spec_path,
    task_graph_path,
)
from redgear.schemas import (
    Budget,
    Claim,
    Escalation,
    Event,
    GateName,
    PriorAttempt,
    Spec,
    TaskGraph,
    TaskNode,
    TurnOutcome,
    TurnResult,
)

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
    (``if task is None: ...``). Exhaustion is how a run *ends*, not an error --
    see the T-0031 note in docs/PROGRESS.md and section 4.3's `complete` and
    `blocked` terminations.

    Selects anything **claimable**, which is `ready` *or* `rejected`. A
    rejected task is a task that failed a gate and still has attempts left,
    and `_CLAIMABLE_FROM` already admits it. Leaving it out here would strand
    it forever: `recompute_readiness` deliberately does not touch `rejected`
    (it must not resurrect a task that left the queue), so nothing else would
    ever put it back. Including it is also what keeps the retry path free of a
    special case in the orchestrator -- a retry is just the next selection.
    """
    ready = [node for node in graph.nodes if node.state in _CLAIMABLE_FROM]
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
            # `prior_attempts` is G4-reconstructible state (section 1.4), so it
            # is rebuilt here from exactly what the event carries. Anything the
            # projection held that the log does not would make every `rebuild`
            # report divergence.
            history = [
                *node.prior_attempts,
                PriorAttempt(
                    attempt_number=event.attempt,
                    outcome=TurnOutcome.COMPLETED,
                    failed_gate=GateName(event.failed_gates[0]) if event.failed_gates else None,
                    failure_excerpt=event.summary,
                    recorded_at=event.ts,
                ),
            ]
            mutated[task_id] = node.model_copy(
                update={
                    "state": "rejected",
                    "attempts": event.attempt,
                    "claim": None,
                    "prior_attempts": history,
                }
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


def frozen_digests(repo_root: Path, frozen_globs: Sequence[str]) -> dict[str, str]:
    """The claim-time digest map, exposed read-only.

    ``compose_dry_run`` needs the real frozen-file count for the prompt's
    scope section but must not claim anything, so the computation is shared
    while the persistence is not.
    """
    return _frozen_digest_map(repo_root, frozen_globs)


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
    G3's exemption for an honest exit mean anything.

    The attempt is appended to ``prior_attempts``, and that record is the
    entire mechanism behind the corrective retry: ``prompt_engine`` composes a
    different prompt next time because it can *see* the failure here, not
    because the orchestrator branched on one.

    ``summary`` carries the section 5.5 failure excerpt, formatted by the
    prompt engine and passed in -- this module stores it, it does not compose
    it. It is deliberately the *same* string in the event and in the stored
    ``PriorAttempt``, because G4 requires the projection to be reconstructible
    from the log: a second field held only in the projection could not be
    replayed, and `rebuild` would report divergence forever.
    """
    node = _require(graph, task_id)
    _require_state(node, _REJECTABLE_FROM, "reject")

    attempts = node.attempts + 1
    now = _utc_now()
    gates: list[JsonValue] = list(failed_gates)
    history = [
        *node.prior_attempts,
        PriorAttempt(
            attempt_number=attempts,
            outcome=TurnOutcome.COMPLETED,
            failed_gate=GateName(failed_gates[0]) if failed_gates else None,
            failure_excerpt=summary,
            recorded_at=now,
        ),
    ]
    return _commit_transition(
        repo_root,
        graph,
        node,
        {
            "state": "rejected",
            "attempts": attempts,
            "claim": None,
            "prior_attempts": history,
        },
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


# ---------------------------------------------------------------------------
# Run lifecycle and prompt persistence (T-0031)
#
# The orchestrator owns control flow, not writes. Everything below exists so
# the loop can record a run without opening `.redgear/` itself (section 11.1
# rule 4).
# ---------------------------------------------------------------------------


def _run_dir(repo_root: Path, run_id: str) -> Path:
    return redgear_dir(repo_root) / "runs" / run_id


def start_run(repo_root: Path, budget: Budget, *, actor: str = "engine") -> str:
    """Open a run: append `run_started` and return the run id.

    The id is derived from the wall clock rather than randomly, so a run
    directory sorts chronologically and a human reading `.redgear/runs/` can
    tell which is which without opening anything.
    """
    now = _utc_now()
    run_id = "run_" + now.strftime("%Y%m%dT%H%M%S%f")
    _run_dir(repo_root, run_id).mkdir(parents=True, exist_ok=True)

    append_event(
        events_path(repo_root),
        {
            "event": "run_started",
            "ts": _stamp(now),
            "actor": actor,
            "run_id": run_id,
            "budget": budget.model_dump(mode="json"),
            "base_commit": _git(repo_root, "rev-parse", "HEAD"),
        },
    )
    return run_id


def end_run(
    repo_root: Path,
    run_id: str,
    *,
    reason: str,
    iterations: int,
    tasks_verified: int,
    tasks_escalated: int,
    duration_ms: int,
    actor: str = "engine",
) -> None:
    """Close a run: exactly one `run_ended`, whatever the termination.

    ``reason`` is one of section 4.3's six. The event schema enumerates
    exactly those, so an invalid reason fails validation here rather than
    silently producing an unreadable log.
    """
    append_event(
        events_path(repo_root),
        {
            "event": "run_ended",
            "ts": _stamp(_utc_now()),
            "actor": actor,
            "run_id": run_id,
            "reason": reason,
            "iterations": iterations,
            "tasks_verified": tasks_verified,
            "tasks_escalated": tasks_escalated,
            "duration_ms": duration_ms,
        },
    )


def persist_prompt(
    repo_root: Path,
    run_id: str,
    iteration: int,
    prompt: str,
    *,
    task_id: str,
    attempt: int,
    allowed_tools: Sequence[str],
    actor: str = "engine",
) -> Path:
    """Write the prompt to disk, then record that it was dispatched.

    G4 and section 11.1 rule 6: never dispatch a prompt before persisting it.
    "Every prompt redgear sends is persisted verbatim before dispatch. A run
    you cannot read back prompt-by-prompt is not auditable, and auditability
    is the product."

    The `prompt_sha256` on the event is what makes that verifiable rather than
    asserted -- the file can be re-hashed and matched against the log.
    """
    directory = _run_dir(repo_root, run_id) / "iterations" / f"{iteration:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "prompt.txt"

    payload = prompt.encode("utf-8")
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)  # noqa: PTH105
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    tools: list[JsonValue] = []
    tools.extend(allowed_tools)
    append_event(
        events_path(repo_root),
        {
            "event": "prompt_dispatched",
            "ts": _stamp(_utc_now()),
            "actor": actor,
            "task_id": task_id,
            "attempt": attempt,
            "prompt_path": str(path.relative_to(repo_root)).replace("\\", "/"),
            "prompt_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "allowed_tools": tools,
        },
    )
    return path


def record_turn(
    repo_root: Path,
    *,
    task_id: str,
    attempt: int,
    result: TurnResult,
    duration_ms: int = 0,
    actor: str = "engine",
) -> None:
    """Append `turn_completed` once the agent process has exited."""
    append_event(
        events_path(repo_root),
        {
            "event": "turn_completed",
            "ts": _stamp(_utc_now()),
            "actor": actor,
            "task_id": task_id,
            "attempt": attempt,
            "outcome": result.outcome.value,
            "exit_code": result.exit_code,
            "num_turns": result.num_turns,
            "duration_ms": duration_ms,
            "cost_usd_estimate": result.cost_usd_estimate,
            "parse_ok": result.parse_ok,
        },
    )


# ---------------------------------------------------------------------------
# Scaffolding and rebuild support (T-0033)
# ---------------------------------------------------------------------------


def scaffold(repo_root: Path) -> Path:
    """Create an empty ``.redgear/`` (section 2.3). Used by ``redgear init``.

    Refuses over existing state rather than merging into it: re-initialising
    a live directory would overwrite the event log, which is the one artifact
    redgear exists to protect.

    No ``.gitignore`` entry is written. The whole directory is committed to
    the target repo on purpose -- it *is* the audit trail (section 2.3).
    """
    root = redgear_dir(repo_root)
    if root.exists():
        raise AlreadyInitializedError(
            f"{root} already exists; refusing to overwrite existing state",
            detail={"path": str(root)},
        )
    for child in ("spec", "spec/history", "adrs", "runs", "locks"):
        (root / child).mkdir(parents=True, exist_ok=True)
    events_path(repo_root).touch()
    return root


def plan_definition(graph: TaskGraph) -> TaskGraph:
    """Strip every mutable field back to its pristine value.

    The inverse of what the log records. G4 splits a graph into an immutable
    *plan* (ids, types, scopes, criteria, edges) and *mutable state* (`state`,
    `attempts`, `claim`, `prior_attempts`, `verified_at`, `proof_id`,
    `escalation`) -- and only the second half is reconstructible from events.
    So `rebuild` resets the second half and replays the log onto it.

    That is what makes divergence detectable at all: a node claiming a
    non-initial state that no event supports is exactly the corruption the
    check exists to find. States are recomputed rather than blanked, because
    `ready` versus `blocked` is a function of the dependency graph, not of
    anything that happened.
    """
    pristine = [
        node.model_copy(
            update={
                "state": _BLOCKED,
                "attempts": 0,
                "claim": None,
                "prior_attempts": [],
                "verified_at": None,
                "proof_id": None,
                "escalation": None,
            }
        )
        for node in graph.nodes
    ]
    return recompute_readiness(graph.model_copy(update={"nodes": pristine}))


def persist_plan(
    repo_root: Path,
    spec: Spec,
    graph: TaskGraph,
    *,
    source_document: str,
    actor: str = "engine",
) -> None:
    """Write a generated plan and record that it was generated (T-0037).

    Spec first, then the graph, then the event. The order matters on a crash:
    a spec with no graph is an obviously incomplete plan, whereas a graph
    pointing at a spec hash nothing on disk produces is a plan that looks
    complete and is not.
    """
    target = spec_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(spec.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    persist_graph(repo_root, graph)

    append_event(
        events_path(repo_root),
        {
            "event": "plan_generated",
            "ts": _stamp(_utc_now()),
            "actor": actor,
            "spec_hash": spec.hash,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            # The plan's shape and hash, never its contents (section 3.6):
            # duplicating the graph into the log would create two sources of
            # truth for structure and guarantee they drift.
            "source_document": source_document,
        },
    )


def approve_plan(repo_root: Path, graph: TaskGraph, *, approved_by: str) -> TaskGraph:
    """Move the graph draft -> active and append `plan_approved`.

    The event records the `spec_hash` because that is what makes approval
    *specific* (section 3.3). An approval that did not say which spec version
    it covered would let a human approve one plan and the loop execute
    another.
    """
    now = _utc_now()
    approved = graph.model_copy(update={"state": "active"})

    append_event(
        events_path(repo_root),
        {
            "event": "plan_approved",
            "ts": _stamp(now),
            "actor": approved_by,
            "spec_hash": graph.spec_hash,
            "approved_by": approved_by,
        },
    )
    persist_graph(repo_root, approved)
    return approved
