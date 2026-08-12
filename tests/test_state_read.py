"""T-0012: failing tests for redgear.state_engine -- the read path.

``redgear/state_engine.py`` does not exist yet, so the import block below
fails at COLLECTION with ``ModuleNotFoundError``. That is the correct red
state.

Load, validate, project. Nothing here writes; the write path is T-0014.

The load-bearing property is AC-1: ``replay`` reproduces the projection
**byte-identically**, asserted against the real 41-node
``.redgear/task_graph.json`` rather than a fixture. "Equivalent" is not
enough -- G4 says the projection is reconstructible from the log, and a
reconstruction that differs by a key order or a dropped null is a
reconstruction you cannot diff, which makes `redgear rebuild` useless as a
divergence detector.

NOTE ON SCOPE OF REPLAY (contract gap, reported in this task's turn):
section 3.6's `plan_generated` carries only `spec_hash`, `node_count`,
`edge_count` and `source_document` -- it does **not** carry the node and edge
definitions. No event in the closed 14-type taxonomy does. So the log alone
cannot reconstruct graph *structure*; it can only reconstruct the *mutable*
state laid over a plan. ``replay_graph`` therefore takes the plan definition
as its base and folds events onto it, which is the strongest form of G4
actually available under section 3.6.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from redgear.errors import GraphCycleError, ScopeOverlapError, UnknownNodeRefError
from redgear.schemas import TaskGraph
from redgear.state_engine import (
    find_cycle,
    load_graph,
    next_ready_task,
    recompute_readiness,
    render_graph,
    replay_graph,
    validate_graph,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = REPO_ROOT / ".redgear" / "task_graph.json"
RAW_GRAPH_TEXT = GRAPH_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def real_graph() -> TaskGraph:
    return load_graph(REPO_ROOT)


def _synthetic(nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> dict[str, Any]:
    """A minimal well-formed graph payload, for invariant tests that need a
    shape the real graph deliberately does not have."""
    return {
        "schema_version": 1,
        "spec_hash": "sha256:" + "0" * 64,
        "state": "draft",
        "generated_at": "2026-08-11T00:00:00Z",
        "nodes": nodes,
        "edges": edges,
    }


def _node(
    node_id: str,
    *,
    node_type: str = "test_authoring",
    state: str = "blocked",
    depends_on: list[str] | None = None,
    writable: list[str] | None = None,
    frozen: list[str] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    inherits: list[str] | None = None,
) -> dict[str, Any]:
    default_criteria = [
        {
            "id": "AC-1",
            "statement": "It holds.",
            "verified_by": {"kind": "test", "selector": f"tests/test_{node_id}.py::test_x"},
        }
    ]
    if node_type == "implementation":
        own_criteria: list[dict[str, Any]] = []
    else:
        own_criteria = default_criteria if criteria is None else criteria
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
            "frozen_globs": frozen if frozen is not None else ["redgear/**"],
        },
        "acceptance_criteria": own_criteria,
        "inherits_criteria_from": inherits or [],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }


# ---------------------------------------------------------------------------
# AC-1: replay reproduces the projection byte-identically.
# ---------------------------------------------------------------------------


def test_replay_reproduces_projection(real_graph: TaskGraph) -> None:
    """Against the real 41-node graph, not a fixture.

    With an empty log the projection is the plan itself, so this pins the
    serialiser exactly: key order, 2-space indent, which nulls are written
    and which optional fields are omitted, and the trailing newline.
    """
    projected = replay_graph(real_graph, [])
    rendered = render_graph(projected)

    assert rendered == RAW_GRAPH_TEXT, "projection is not byte-identical to task_graph.json"
    assert len(real_graph.nodes) == 41
    assert len(real_graph.edges) == 49

    # Idempotent: projecting a projection changes nothing.
    assert render_graph(replay_graph(projected, [])) == RAW_GRAPH_TEXT

    # And it really is a round trip through the model, not a file passthrough.
    assert json.loads(rendered) == json.loads(RAW_GRAPH_TEXT)


def test_render_is_stable_across_calls(real_graph: TaskGraph) -> None:
    assert render_graph(real_graph) == render_graph(real_graph)
    assert render_graph(real_graph).endswith("\n")


# ---------------------------------------------------------------------------
# AC-2: a cycle is rejected and named.
# ---------------------------------------------------------------------------


def test_cycle_detected_and_named() -> None:
    """Section 4.4 invariant 1. "Report the cycle" is the point -- a bare
    "graph is cyclic" leaves a human diffing 41 nodes by hand."""
    payload = _synthetic(
        nodes=[
            _node("T-0001", depends_on=["T-0003"]),
            _node("T-0002", depends_on=["T-0001"]),
            _node("T-0003", depends_on=["T-0002"]),
        ],
        edges=[
            {"from": "T-0003", "to": "T-0001", "kind": "hard"},
            {"from": "T-0001", "to": "T-0002", "kind": "hard"},
            {"from": "T-0002", "to": "T-0003", "kind": "hard"},
        ],
    )
    graph = TaskGraph.model_validate(payload)

    with pytest.raises(GraphCycleError) as excinfo:
        validate_graph(graph)

    assert excinfo.value.code == "E_GRAPH_CYCLE"
    named = json.dumps(excinfo.value.detail)
    for node_id in ("T-0001", "T-0002", "T-0003"):
        assert node_id in named, f"{node_id} is in the cycle but was not named"

    cycle = find_cycle(graph)
    assert cycle is not None
    assert set(cycle) == {"T-0001", "T-0002", "T-0003"}

    # The real graph is acyclic, so find_cycle returns None there.
    assert find_cycle(load_graph(REPO_ROOT)) is None


def test_unknown_dependency_reference_rejected() -> None:
    """Section 4.4 invariant 2: every depends_on and edge endpoint must
    name an existing node."""
    payload = _synthetic(
        nodes=[_node("T-0001", depends_on=["T-9999"])],
        edges=[],
    )
    graph = TaskGraph.model_validate(payload)
    with pytest.raises(UnknownNodeRefError) as excinfo:
        validate_graph(graph)
    assert excinfo.value.code == "E_GRAPH_INVALID"
    assert "T-9999" in json.dumps(excinfo.value.detail)

    payload = _synthetic(
        nodes=[_node("T-0001")],
        edges=[{"from": "T-0001", "to": "T-8888", "kind": "hard"}],
    )
    with pytest.raises(UnknownNodeRefError):
        validate_graph(TaskGraph.model_validate(payload))


def test_overlapping_scope_globs_rejected() -> None:
    """Section 4.4 invariant 7, surfaced as E_SCOPE_CONTRADICTION."""
    payload = _synthetic(
        nodes=[_node("T-0001", writable=["redgear/**"], frozen=["redgear/**"])],
        edges=[],
    )
    graph = TaskGraph.model_validate(payload)
    with pytest.raises(ScopeOverlapError) as excinfo:
        validate_graph(graph, tracked_files=["redgear/schemas.py"])
    assert excinfo.value.code == "E_SCOPE_CONTRADICTION"
    assert "redgear/schemas.py" in json.dumps(excinfo.value.detail)


def test_real_graph_passes_every_invariant(real_graph: TaskGraph) -> None:
    """The shipped plan must satisfy the rules it is validated against."""
    validate_graph(real_graph)


# ---------------------------------------------------------------------------
# AC-3: readiness is all-hard-deps-verified, recomputed per load.
# ---------------------------------------------------------------------------


def test_readiness_requires_verified_deps() -> None:
    """Section 4.4 invariant 3, and it is recomputed rather than trusted --
    a stale `state` in the file must not make an unready task selectable."""
    payload = _synthetic(
        nodes=[
            _node("T-0001", state="verified"),
            _node("T-0002", depends_on=["T-0001"], state="blocked"),
            _node("T-0003", depends_on=["T-0002"], state="ready"),
        ],
        edges=[
            {"from": "T-0001", "to": "T-0002", "kind": "hard"},
            {"from": "T-0002", "to": "T-0003", "kind": "hard"},
        ],
    )
    graph = recompute_readiness(TaskGraph.model_validate(payload))
    by_id = {n.id: n for n in graph.nodes}

    # T-0002's only dependency is verified -> ready, despite the file saying blocked.
    assert by_id["T-0002"].state == "ready"
    # T-0003 depends on an unverified node -> blocked, despite the file saying ready.
    assert by_id["T-0003"].state == "blocked"
    # A verified node is never demoted by recomputation.
    assert by_id["T-0001"].state == "verified"


def test_recompute_preserves_terminal_and_in_flight_states() -> None:
    """Only blocked<->ready is recomputed. A claimed, escalated or verified
    node keeps its state -- readiness is about eligibility to start, and
    recomputation must not resurrect a task that already left the queue."""
    payload = _synthetic(
        nodes=[
            _node("T-0001", state="verified"),
            _node("T-0002", depends_on=["T-0001"], state="claimed"),
            _node("T-0003", depends_on=["T-0001"], state="escalated"),
            _node("T-0004", depends_on=["T-0001"], state="rejected"),
        ],
        edges=[],
    )
    graph = recompute_readiness(TaskGraph.model_validate(payload))
    by_id = {n.id: n for n in graph.nodes}
    assert by_id["T-0002"].state == "claimed"
    assert by_id["T-0003"].state == "escalated"
    assert by_id["T-0004"].state == "rejected"


def test_recompute_on_real_graph_is_a_fixed_point(real_graph: TaskGraph) -> None:
    """The shipped graph's states already satisfy the readiness rule, so
    recomputation is a no-op -- which is why replay stays byte-identical."""
    assert render_graph(recompute_readiness(real_graph)) == RAW_GRAPH_TEXT


# ---------------------------------------------------------------------------
# AC-4: an implementation node must inherit its criteria.
# ---------------------------------------------------------------------------


def test_impl_requires_inherited_criteria() -> None:
    """Section 4.4 invariant 6 / G2. An implementation node with an empty
    inherits_criteria_from has no acceptance criteria at all, so nothing
    could ever verify it -- it would pass by having nothing to check."""
    payload = _synthetic(
        nodes=[_node("T-0002", node_type="implementation", inherits=[])],
        edges=[],
    )
    graph = TaskGraph.model_validate(payload)
    with pytest.raises(UnknownNodeRefError) as excinfo:
        validate_graph(graph)
    assert excinfo.value.code == "E_GRAPH_INVALID"
    assert "T-0002" in json.dumps(excinfo.value.detail)


def test_inherits_must_point_at_test_authoring() -> None:
    """Section 4.4 invariant 4: criteria may only be inherited from a
    test_authoring node. Inheriting from an implementation node would mean
    inheriting an empty criteria list."""
    payload = _synthetic(
        nodes=[
            _node("T-0001", node_type="scaffold"),
            _node("T-0002", node_type="implementation", inherits=["T-0001"]),
        ],
        edges=[],
    )
    with pytest.raises(UnknownNodeRefError):
        validate_graph(TaskGraph.model_validate(payload))


def test_scaffold_exempt_from_invariants_four_to_six() -> None:
    """Section 4.4 invariant 8 / section 4.5: a scaffold node authors its own
    smoke criteria and has no two-phase partner. T-0001 in the real graph is
    exactly this shape, so the exemption is not hypothetical."""
    payload = _synthetic(
        nodes=[_node("T-0001", node_type="scaffold", inherits=[])],
        edges=[],
    )
    validate_graph(TaskGraph.model_validate(payload))


# ---------------------------------------------------------------------------
# AC-5: selection drains the critical path.
# ---------------------------------------------------------------------------


def test_selection_drains_critical_path() -> None:
    """Ordered by descending hard-dependency count, then ascending id.

    Draining the most-depended-upon work first shortens the critical path;
    the id tiebreak makes selection deterministic, which matters because a
    run must be reproducible from its log.
    """
    payload = _synthetic(
        nodes=[
            _node("T-0001", state="verified"),
            _node("T-0002", state="verified"),
            _node("T-0003", state="verified"),
            # Three ready candidates with differing dependency counts.
            _node("T-0010", depends_on=["T-0001"], state="ready"),
            _node("T-0011", depends_on=["T-0001", "T-0002", "T-0003"], state="ready"),
            _node("T-0012", depends_on=["T-0001", "T-0002"], state="ready"),
        ],
        edges=[],
    )
    graph = recompute_readiness(TaskGraph.model_validate(payload))

    chosen = next_ready_task(graph)
    assert chosen is not None
    assert chosen.id == "T-0011", "did not pick the highest dependency count"

    # Tiebreak on id, ascending.
    tie_payload = _synthetic(
        nodes=[
            _node("T-0001", state="verified"),
            _node("T-0009", depends_on=["T-0001"], state="ready"),
            _node("T-0005", depends_on=["T-0001"], state="ready"),
            _node("T-0007", depends_on=["T-0001"], state="ready"),
        ],
        edges=[],
    )
    tie_graph = recompute_readiness(TaskGraph.model_validate(tie_payload))
    tie_choice = next_ready_task(tie_graph)
    assert tie_choice is not None
    assert tie_choice.id == "T-0005", "tiebreak is not ascending id"


def test_selection_returns_none_when_nothing_ready() -> None:
    """Section 4.1's loop skeleton reads `if task is None:` and ends the run
    `complete_or_blocked`, so exhaustion is a return value, not an exception.
    (§4.7 lists E_NO_READY_TASK; see this task's turn report -- the normative
    loop in §4.1 expects None here.)"""
    payload = _synthetic(nodes=[_node("T-0001", state="verified")], edges=[])
    assert next_ready_task(TaskGraph.model_validate(payload)) is None

    # Every node must have an unverified dependency to actually be blocked.
    # A node with depends_on=[] is vacuously ready under section 4.4
    # invariant 3 -- which is exactly why the real graph's T-0001 ships as
    # `ready` -- so the root here is escalated rather than dependency-free.
    blocked = _synthetic(
        nodes=[
            _node("T-0000", state="escalated"),
            _node("T-0001", depends_on=["T-0000"], state="blocked"),
            _node("T-0002", depends_on=["T-0001"], state="blocked"),
        ],
        edges=[],
    )
    assert next_ready_task(recompute_readiness(TaskGraph.model_validate(blocked))) is None


def test_selection_on_real_graph_picks_the_root(real_graph: TaskGraph) -> None:
    """Nothing is verified yet, so T-0001 -- the only node with no
    dependencies -- is the only ready task."""
    chosen = next_ready_task(recompute_readiness(real_graph))
    assert chosen is not None
    assert chosen.id == "T-0001"
    assert chosen.type == "scaffold"


def test_load_graph_validates(real_graph: TaskGraph) -> None:
    """load_graph validates on every load (section 4.4: "validate on every
    load and every write"), so a caller cannot forget to."""
    assert isinstance(real_graph, TaskGraph)
    assert real_graph.state == "draft"
    assert {n.id for n in real_graph.nodes} >= {"T-0001", "T-0041"}
