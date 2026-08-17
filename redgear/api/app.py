"""The read-only control plane API (§9, FR-12).

FR-12: "A local read-only control plane renders the task graph, dispatched
prompts, diffs, and verification proofs derived solely from the event log."
This module is that surface. It calls `state_engine`, `planner`, and
`gitctx`/`events` indirectly through them; it contains no loop logic, no
prompt composition, and runs no gates -- the same one-concern-per-module rule
`orchestrator.py` and `cli.py` both follow (§2.2, §11.2 rule 7).

Every handler here is read-only with exactly one exception: approving a
draft plan (§3.3's human gate). That endpoint calls `planner.approve_plan`
directly rather than re-implementing approval -- the same function
`redgear approve` calls -- so the API and the CLI cannot drift into two
different ideas of what "approved" means. `test_no_state_mutation_endpoints`
in `tests/test_api.py` proves the read-only claim by enumerating
`app.routes`, not by trusting this docstring.

**Two artifacts this module reads did not, until T-0039, exist on disk.**
`verifier.run_gates` has always computed a `Proof` and handed it back in
memory; nothing persisted it. §2.3 documents a `proof/verdict.json` and
`proof/diff.patch` under every iteration directory, and FR-12 AC-4 needs them
retrievable *after the fact* -- the working tree is uncommitted and keeps
changing as a run continues (redgear never commits, G6), so a diff computed
now cannot be recomputed later from git alone. `state_engine.persist_proof`
and the two lines in `orchestrator.run` that call it close that gap; this
module is what makes the artifact worth having.

**One field this module deliberately does not trust.** `state_engine.
mark_verified` writes its `task_verified` event's `attempt` from
`node.attempts` *before* the transition -- the count of prior rejections,
not the number of the attempt just verified. On a first-try pass that is 0;
after one rejection it is 1, one behind `prompt_dispatched`/`turn_completed`
for the same turn. See `docs/PROGRESS.md` for the full account. Nothing here
fixes `state_engine.py` -- that is a different module's bug, out of this
task's scope, and no frozen test currently pins the wrong value. This module
works around it: a verified attempt's number is derived from how many
rejections preceded it, which is what every other event agrees on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from redgear import planner, state_engine
from redgear.errors import RedgearError
from redgear.events import replay as replay_events
from redgear.paths import events_path, task_graph_path
from redgear.redact import JsonValue, collect_secrets, redact_value
from redgear.schemas import (
    PromptDispatchedEvent,
    Strict,
    TaskGraph,
    TaskNode,
    TaskRejectedEvent,
    TaskVerifiedEvent,
)

#: §1.4 G4's split, restated as two field sets: the immutable plan definition
#: a node carries, and the mutable run state layered over it by the log. The
#: response mirrors this exactly (`test_all_responses_log_derived`,
#: `test_graph_reports_plan_and_state_as_separate_keys`).
_PLAN_FIELDS = frozenset(
    {
        "id",
        "type",
        "title",
        "spec_refs",
        "spec_hash",
        "depends_on",
        "scope",
        "acceptance_criteria",
        "inherits_criteria_from",
        "max_attempts",
        "note",
    }
)
_STATE_FIELDS = frozenset(
    {"id", "state", "attempts", "claim", "prior_attempts", "verified_at", "proof_id", "escalation"}
)


class ApprovalRequest(Strict):
    approved_by: str


def create_app(repo_root: Path) -> FastAPI:
    """Build the control plane app for one target repository.

    A factory rather than a module-level `app`, because the repository a
    `redgear ui` invocation serves is a runtime argument (`--repo`), not a
    constant -- and because the test suite needs a fresh app per fixture
    repo without any process-global state leaking between tests.
    """
    app = FastAPI(
        title="redgear control plane",
        description="Read-only over .redgear/events.jsonl. See CLAUDE.md §9, FR-12.",
    )
    app.state.repo_root = repo_root

    # -----------------------------------------------------------------
    # Helpers closed over `repo_root` -- see the module docstring for what
    # each artifact this reads is and where it comes from.
    # -----------------------------------------------------------------

    def _secrets() -> frozenset[str]:
        # G5 is not violated by reading os.environ here: the values are
        # collected only to be removed from output, never inspected,
        # branched on, or exposed -- the same use `cli.py`'s `log` command
        # already makes of `redact.collect_secrets`.
        return collect_secrets(dict(os.environ))

    def _redact(value: JsonValue) -> JsonValue:
        return redact_value(value, _secrets())

    def _require_state() -> None:
        if not task_graph_path(repo_root).is_file():
            raise HTTPException(status_code=404, detail=f"no redgear state in {repo_root}")

    def _require_task(graph: TaskGraph, task_id: str) -> None:
        if not any(node.id == task_id for node in graph.nodes):
            raise HTTPException(status_code=404, detail=f"no task {task_id}")

    def _plan_node(node: TaskNode) -> dict[str, JsonValue]:
        return node.model_dump(mode="json", include=set(_PLAN_FIELDS))

    def _state_entry(node: TaskNode) -> dict[str, JsonValue]:
        payload = node.model_dump(mode="json", include=set(_STATE_FIELDS))
        payload["task_id"] = payload.pop("id")
        return payload

    def _prompt_dispatched_events(task_id: str) -> list[PromptDispatchedEvent]:
        events = replay_events(events_path(repo_root))
        return [
            event
            for event in events
            if isinstance(event, PromptDispatchedEvent) and event.task_id == task_id
        ]

    def _verdict_events(task_id: str) -> list[TaskVerifiedEvent | TaskRejectedEvent]:
        events = replay_events(events_path(repo_root))
        return [
            event
            for event in events
            if isinstance(event, TaskVerifiedEvent | TaskRejectedEvent) and event.task_id == task_id
        ]

    def _prompt_records(task_id: str) -> list[JsonValue]:
        records: list[JsonValue] = []
        for event in _prompt_dispatched_events(task_id):
            payload = event.model_dump(mode="json")
            prompt_file = repo_root / Path(event.prompt_path)
            payload["prompt"] = (
                prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else None
            )
            payload["iteration"] = _iteration_of(event.prompt_path)
            records.append(payload)
        return records

    def _proof_records(task_id: str) -> list[JsonValue]:
        prompts = _prompt_dispatched_events(task_id)
        records: list[JsonValue] = []
        rejections_seen = 0

        for verdict_event in _verdict_events(task_id):
            # The dispatch that produced this verdict is the most recent
            # `prompt_dispatched` for this task strictly before it -- see the
            # module docstring on why `task_verified.attempt` is not used
            # for this lookup either.
            preceding = [p for p in prompts if p.seq < verdict_event.seq]
            dispatch = max(preceding, key=lambda p: p.seq) if preceding else None

            is_verified = isinstance(verdict_event, TaskVerifiedEvent)
            if is_verified:
                attempt_number = rejections_seen + 1
            else:
                attempt_number = verdict_event.attempt
                rejections_seen += 1

            record: dict[str, JsonValue] = {
                "task_id": task_id,
                "attempt": attempt_number,
                "outcome": "verified" if is_verified else "rejected",
                "proof_id": verdict_event.proof_id,
                "computed_at": verdict_event.model_dump(mode="json")["ts"],
            }
            if dispatch is not None:
                proof_dir = (repo_root / Path(dispatch.prompt_path)).parent / "proof"
                verdict_file = proof_dir / "verdict.json"
                diff_file = proof_dir / "diff.patch"
                if verdict_file.is_file():
                    record["verdict"] = json.loads(verdict_file.read_text(encoding="utf-8"))
                if diff_file.is_file():
                    record["diff"] = diff_file.read_text(encoding="utf-8")
            records.append(record)

        return records

    # -----------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, JsonValue]:
        return {"status": "ok"}

    @app.get("/graph")
    def get_graph() -> JsonValue:
        """The plan definition and the replayed mutable state, as separate
        keys (§1.4 G4). The plan half comes from the pristine definition;
        the state half comes from replaying `events.jsonl` onto it -- the
        same mechanism `redgear rebuild` uses, so this is FR-12 AC-1 by
        construction rather than by a handler that happens to read the
        right file.
        """
        _require_state()
        on_disk = state_engine.load_graph(repo_root)
        definition = state_engine.plan_definition(on_disk)
        events = replay_events(events_path(repo_root))
        replayed = state_engine.replay_graph(definition, events)

        payload: dict[str, JsonValue] = {
            "plan": {
                "spec_hash": definition.spec_hash,
                "graph_state": definition.state,
                "generated_at": definition.generated_at.isoformat(),
                "nodes": [_plan_node(node) for node in definition.nodes],
                "edges": [edge.model_dump(mode="json", by_alias=True) for edge in definition.edges],
            },
            "state": [_state_entry(node) for node in replayed.nodes],
        }
        return _redact(payload)

    @app.get("/events")
    def get_events() -> JsonValue:
        _require_state()
        events = replay_events(events_path(repo_root))
        payload: list[JsonValue] = [event.model_dump(mode="json") for event in events]
        return _redact(payload)

    @app.get("/tasks/{task_id}/prompts")
    def get_prompts(task_id: str) -> JsonValue:
        """Dispatched prompts, verbatim, one entry per iteration (FR-12
        AC-3) -- a task dispatches twice on one attempt when the first try's
        result failed to parse (§6.4 rule 4), and both are real records."""
        _require_state()
        graph = state_engine.load_graph(repo_root)
        _require_task(graph, task_id)
        return _redact(_prompt_records(task_id))

    @app.get("/tasks/{task_id}/proofs")
    def get_proofs(task_id: str) -> JsonValue:
        """Verification proofs, including the raw diff, per attempt (FR-12
        AC-4)."""
        _require_state()
        graph = state_engine.load_graph(repo_root)
        _require_task(graph, task_id)
        return _redact(_proof_records(task_id))

    @app.post("/plan/approve")
    def approve(payload: ApprovalRequest) -> JsonValue:
        """§3.3's human gate. Calls `planner.approve_plan` directly -- the
        same function `redgear approve` calls -- so there is exactly one
        implementation of "what does approval record" for the CLI and the
        API to (by construction, not by convention) agree on.
        """
        _require_state()
        try:
            graph = planner.approve_plan(repo_root, approved_by=payload.approved_by)
        except RedgearError as error:
            raise HTTPException(status_code=400, detail=error.to_dict()) from error

        return _redact(
            {
                "approved": True,
                "approved_by": payload.approved_by,
                "spec_hash": graph.spec_hash,
                "node_count": len(graph.nodes),
            }
        )

    return app


def _iteration_of(prompt_path: str) -> int | None:
    """The iteration number embedded in a persisted prompt's directory name.

    ``.redgear/runs/<run_id>/iterations/<NNNN>/prompt.txt`` -- §2.3's layout,
    parsed rather than duplicated as a second source of the same number.
    """
    try:
        return int(Path(prompt_path).parent.name)
    except ValueError:
        return None
