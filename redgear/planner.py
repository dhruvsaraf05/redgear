"""Phase 1 — plan generation and the mandatory approval gate.

**This module produces the only unverified model output in the system.**
Everything the loop does afterwards is gated by tests, but the plan *defines*
those tests — so a wrong plan produces confidently verified wrong software,
and no amount of gate rigour downstream will catch it. That is why §3.3 puts a
human between `plan` and `run`, and why nothing here can cross that line.

Three properties carry the weight:

* **The dispatch is read-only by construction** (§3.2). ``PLANNER_TOOLS`` is a
  module constant, not a parameter, and it is ``Read, Glob, Grep``. A planner
  that can edit files is an unsupervised agent with no verification gate —
  exactly what redgear exists to prevent.
* **The generated plan is `draft`, and redgear forces it.** The agent's own
  claim about state is discarded. An agent able to declare its plan approved
  would have removed the only human gate in the system.
* **Validation runs before the plan is written.** A rejected plan leaves
  nothing on disk, so there is no half-plan for someone to approve by mistake.

G5 holds here as everywhere: redgear never calls a model. Planning goes
through the same ``runner.py`` the loop uses, over the ``PlanRunner`` seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from redgear import prompt_engine, state_engine
from redgear.errors import JsonValue, PlanInvalidError, RedgearError
from redgear.hashing import compute_spec_hash, spec_id_from_hash
from redgear.paths import spec_path, task_graph_path
from redgear.runner import PlanRunner
from redgear.schemas import Spec, TaskGraph

#: §3.2, and not negotiable: "`--allowedTools "Read,Glob,Grep"` and nothing
#: more." A module constant rather than a parameter, because a caller-supplied
#: list would make the read-only property a default instead of a guarantee.
PLANNER_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

#: §3.4's retry cap. Bounded on purpose: retrying gives a planner that made a
#: structural mistake a chance to fix it, and *bounded* retrying means one that
#: cannot produce a valid plan surfaces to a human rather than looping on the
#: user's money.
DEFAULT_MAX_ATTEMPTS = 3

#: A writable glob this broad is §3.4 rule 3's named example of a planning
#: failure. Anything matching means the task was not scoped to what it touches.
_TOO_WIDE = frozenset({"**", "*", "src/**", "tests/**", "./**", "/**"})

#: Mutable run state, stripped from whatever the agent returned. These belong
#: to the event log, and a plan arriving with `attempts: 2` would make replay
#: diverge from the moment it was written.
_PRISTINE: dict[str, JsonValue] = {
    "state": "blocked",
    "attempts": 0,
    "claim": None,
    "prior_attempts": [],
    "verified_at": None,
    "proof_id": None,
    "escalation": None,
}


@dataclass(frozen=True)
class PlanResult:
    """A generated, validated, unapproved plan."""

    spec: Spec
    graph: TaskGraph
    attempts: int


def _as_map(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_seq(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def normalise_plan(payload: JsonValue) -> tuple[Spec, TaskGraph]:
    """Turn an agent's plan document into validated models.

    Everything redgear owns is computed here rather than taken: the spec hash
    (§3.5's content addressing), the spec id, the graph's `draft` state, and
    every node's mutable run state. The agent supplies the *plan*; it does not
    get to describe the plan's status.

    Raises ``PlanInvalidError`` when the document is not a plan at all — a
    truncated object, prose, the wrong shape.
    """
    document = _as_map(payload)
    if not document:
        raise PlanInvalidError(
            "the planner returned something that is not a plan document",
            detail={"received": str(type(payload).__name__)},
        )

    requirements = _as_seq(document.get("requirements"))
    nodes = _as_seq(document.get("nodes"))
    if not requirements or not nodes:
        raise PlanInvalidError(
            "the plan has no requirements or no tasks",
            detail={"requirements": len(requirements), "nodes": len(nodes)},
        )

    # §3.5: the hash is computed over requirements and out_of_scope only, and
    # never read from the document. A spec whose hash the agent chose would be
    # content-addressed in name only.
    spec_body: dict[str, JsonValue] = {
        "requirements": requirements,
        "out_of_scope": _as_seq(document.get("out_of_scope")),
    }
    spec_hash = compute_spec_hash(cast("dict[str, object]", spec_body))

    project = _as_map(document.get("project")) or {
        "name": "unnamed",
        "root_globs": ["src/**", "tests/**"],
    }
    spec_payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "spec_id": spec_id_from_hash(spec_hash),
        "hash": spec_hash,
        "created_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "supersedes": None,
        "project": project,
        "requirements": requirements,
        "out_of_scope": _as_seq(document.get("out_of_scope")),
    }

    normalised_nodes: list[JsonValue] = []
    for entry in nodes:
        node = dict(_as_map(entry))
        node.update(_PRISTINE)
        node["spec_hash"] = spec_hash
        node.setdefault("max_attempts", 3)
        normalised_nodes.append(node)

    graph_payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "spec_hash": spec_hash,
        # §3.3: forced, never taken from the document.
        "state": "draft",
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "nodes": normalised_nodes,
        "edges": _as_seq(document.get("edges")),
    }

    try:
        spec = Spec.model_validate(spec_payload)
        graph = TaskGraph.model_validate(graph_payload)
    except Exception as exc:
        raise PlanInvalidError(
            "the plan did not validate against the schema",
            detail={"reason": str(exc)[:600]},
        ) from exc

    return spec, state_engine.recompute_readiness(graph)


def check_plan_quality(spec: Spec, graph: TaskGraph) -> list[str]:
    """§3.4's plan-quality rules, as a list of things to fix.

    Returns problems rather than raising, because they are fed straight back
    to the planner as a corrective prompt — so each one has to name the task
    and say what is wrong with it. "The plan was invalid" produces an identical
    plan, for the same reason §5.5 caps and formats gate output.

    These are *quality* rules. Structural invariants (§4.4) are checked
    separately by ``state_engine.validate_graph``, which raises.
    """
    problems: list[str] = []
    by_id = {node.id: node for node in graph.nodes}

    # Rule 1 -- every requirement is testable.
    for requirement in spec.requirements:
        if not requirement.acceptance:
            problems.append(
                f"requirement {requirement.id} has no acceptance criteria; "
                "every requirement needs at least one testable assertion"
            )

    # Rule 4 -- out_of_scope is populated.
    if not spec.out_of_scope:
        problems.append(
            "out_of_scope is empty; it is the field that stops an agent from "
            "building things nobody asked for"
        )

    for node in graph.nodes:
        # Rule 2 -- no orphan implementation tasks.
        if node.type == "implementation":
            if not node.inherits_criteria_from:
                problems.append(
                    f"task {node.id} is an implementation task with no "
                    "inherits_criteria_from; every implementation task must inherit "
                    "its criteria from a test_authoring task (no orphans)"
                )
            for source_id in node.inherits_criteria_from:
                source = by_id.get(source_id)
                if source is None:
                    problems.append(f"task {node.id} inherits from unknown task {source_id}")
                elif source.type != "test_authoring":
                    problems.append(
                        f"task {node.id} inherits criteria from {source_id}, which is a "
                        f"{source.type} task, not test_authoring"
                    )

        # Rule 1 again, at the task level -- a criterion needs a test to check it.
        for criterion in node.acceptance_criteria:
            if not criterion.verified_by.selector.strip():
                problems.append(f"task {node.id} criterion {criterion.id} names no test selector")

        # Rule 3 -- scope as narrow as the task allows.
        for glob in list(node.scope.writable_globs) + list(node.scope.creatable_globs):
            if glob.strip() in _TOO_WIDE:
                problems.append(
                    f"task {node.id} is writable across {glob!r}, which is too broad; "
                    "scope it to the directory or module the task actually touches"
                )

        if node.type != "scaffold" and not node.scope.frozen_globs:
            problems.append(
                f"task {node.id} freezes nothing; a two-phase task must freeze the "
                "half it is not writing (G2)"
            )

    return problems


def generate_plan(
    repo_root: Path,
    *,
    runner: PlanRunner,
    source_document: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout_s: int = 900,
    max_turns: int = 25,
    actor: str = "engine",
) -> PlanResult:
    """Generate a plan, retrying on validation failure, and write it as `draft`.

    Note what this function cannot do: there is no ``approve`` parameter, no
    ``yes``, no ``force``. §11.1 rule 15 — plan and run are never fused, and a
    parameter is what a future caller would reach for.

    Nothing is written until a plan passes every check. A run that surfaces to
    the human leaves no half-plan behind for someone to approve by accident.
    """
    problems: list[str] = []
    last_error: RedgearError | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = prompt_engine.build_planning_prompt(source_document, problems=problems)
        payload = runner.dispatch_json(
            prompt,
            list(PLANNER_TOOLS),
            repo_root,
            timeout_s,
            max_turns,
            _plan_schema(),
        )

        try:
            spec, graph = normalise_plan(payload)
            # Structural invariants first (§4.4): they raise, and a graph that
            # breaks one cannot meaningfully be assessed for quality.
            state_engine.validate_graph(graph)
            problems = check_plan_quality(spec, graph)
        except RedgearError as error:
            last_error = error
            problems = [error.message]
            continue

        if not problems:
            state_engine.persist_plan(
                repo_root,
                spec,
                graph,
                source_document=_source_label(source_document),
                actor=actor,
            )
            return PlanResult(spec=spec, graph=graph, attempts=attempt)

    detail: dict[str, JsonValue] = {
        "attempts": max_attempts,
        "problems": "; ".join(problems) or (last_error.message if last_error else "unknown"),
    }
    raise PlanInvalidError(
        f"the planner could not produce a valid plan in {max_attempts} attempt(s); "
        "a human needs to look at the source document",
        detail=detail,
    )


def _source_label(source_document: str) -> str:
    """A short, stable identifier for the document a plan came from.

    The whole document is not recorded: §3.6 keeps the log a record of what
    happened rather than a copy of its inputs, and a PRD is untrusted text.
    """
    first = source_document.strip().splitlines()[0] if source_document.strip() else "(empty)"
    return first[:120]


def _plan_schema() -> dict[str, object]:
    """The shape the planner is asked to return.

    Deliberately hand-written rather than generated from ``TaskGraph``: the
    agent supplies plan *definitions* only, and a schema derived from the full
    model would invite it to fill in `attempts`, `claim` and `state` — the
    mutable fields ``normalise_plan`` then throws away.
    """
    return {
        "type": "object",
        "required": ["project", "requirements", "out_of_scope", "nodes", "edges"],
        "properties": {
            "project": {"type": "object"},
            "requirements": {"type": "array"},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
            "nodes": {"type": "array"},
            "edges": {"type": "array"},
        },
    }


# ---------------------------------------------------------------------------
# The approval gate -- section 3.3
# ---------------------------------------------------------------------------


def approve_plan(repo_root: Path, *, approved_by: str) -> TaskGraph:
    """Move a plan from `draft` to `active`, recording who approved what.

    §3.3: "The approval records the `spec_hash`. Editing the spec afterwards
    invalidates approval and requires re-approval." Recording *which* spec
    version was approved is the entire mechanism — without it a human could
    approve one plan and the loop could execute a different one.
    """
    if not approved_by.strip():
        raise PlanInvalidError(
            "approval requires a named approver; naming who approved it is the "
            "whole content of the record",
            detail={"approved_by": approved_by},
        )

    graph = state_engine.load_graph(repo_root)
    if graph.state == "active":
        raise PlanInvalidError(
            "this plan is already approved; a second approval would make the log "
            "claim two approvals of one thing",
            detail={"state": graph.state},
        )

    return state_engine.approve_plan(repo_root, graph, approved_by=approved_by.strip())


def approval_is_valid(repo_root: Path) -> bool:
    """Is the plan approved, and approved against the spec that is on disk now?

    Two questions, and both have to be yes:

    * the graph is `active` — a `draft` plan was never approved at all;
    * the spec's current content hashes to what the graph was pinned to.

    The second is §3.5's content addressing doing its job. A requirement edited
    after approval changes the hash, and the approval stops applying — which is
    what stops a human approving one thing and the loop running another.

    A missing spec file means there is nothing to drift *from*, so only the
    first question applies.
    """
    graph_file = task_graph_path(repo_root)
    if not graph_file.is_file():
        return False

    graph = TaskGraph.model_validate_json(graph_file.read_text(encoding="utf-8"))
    if graph.state != "active":
        return False

    spec_file = spec_path(repo_root)
    if not spec_file.is_file():
        return True

    import json

    stored = json.loads(spec_file.read_text(encoding="utf-8"))
    return bool(compute_spec_hash(stored) == graph.spec_hash)


def drifted_requirements(repo_root: Path) -> Sequence[str]:
    """Which requirement ids differ from what the graph was planned against.

    Used to explain a refusal rather than merely make one (§3.5 step 4:
    "return `E_SPEC_DRIFT` naming them").
    """
    spec_file = spec_path(repo_root)
    graph_file = task_graph_path(repo_root)
    if not spec_file.is_file() or not graph_file.is_file():
        return []

    import json

    stored = json.loads(spec_file.read_text(encoding="utf-8"))
    graph = TaskGraph.model_validate_json(graph_file.read_text(encoding="utf-8"))
    if compute_spec_hash(stored) == graph.spec_hash:
        return []

    return [str(requirement.get("id", "?")) for requirement in stored.get("requirements", [])]
