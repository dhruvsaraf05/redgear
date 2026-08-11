"""T-0002: failing tests for redgear.schemas -- every Pydantic model.

This is a ``test_authoring`` task (CLAUDE.md section 4.5 / section 4.4
invariant 8 does not apply -- this is the two-phase pattern, not scaffold).
``redgear/schemas.py`` does not exist yet, so the single import block below
fails at COLLECTION with ``ModuleNotFoundError``, not at assertion time.
That is the correct red state for this task: schemas.py is a leaf module
(CLAUDE.md section 2.2, and this node's own ``note`` field) with no
try/except, no ``importorskip``, and no mock standing in for it. T-0003
must make this file collect and every assertion below pass by actually
implementing the models, not by working around the import.

Design decisions this file bakes in, because CLAUDE.md pins some models
down exactly (Budget: section 8.1, TurnResult: section 6.4) but is silent
or only indirectly suggestive about others. Each is grounded in a specific
citation; none is an arbitrary guess. Flagged prominently in the T-0002
turn report for confirmation before T-0003 -- these are the load-bearing
ones:

* ``Frozen`` base class -- named verbatim in CLAUDE.md's own pseudocode
  (``class Budget(Frozen):`` section 8.1, ``class TurnResult(Frozen):``
  section 6.4), so it must exist in schemas.py with that exact name.
  ``frozen=True, extra="forbid"``.
* The **event taxonomy** (AC-3) is closed at 14 types per CLAUDE.md
  section 3.6's normative table, which this file now matches field for
  field -- not inferred. Every event carries the three base fields section
  3.6 requires on all of them: ``event`` (discriminator), ``seq``, ``ts``,
  and ``actor`` (an agent id, ``"human"``, or ``"engine"``). Payload
  fields beyond the base come verbatim from the section 3.6 table for each
  of the 14 rows, including the ones this file previously got wrong or
  omitted: ``prompt_dispatched.prompt_sha256`` (not a raw prompt string --
  section 3.6 is explicit that this is what makes G4 verifiable rather
  than asserted), ``run_started.base_commit`` (this file previously
  invented ``repo_path`` instead), ``plan_generated.node_count`` /
  ``edge_count`` / ``source_document`` (this file previously had a single
  invented ``task_count``), and the full ``task_claimed`` /
  ``turn_completed`` / ``task_verified`` / ``task_rejected`` /
  ``task_escalated`` / ``lease_expired`` / ``adr_logged`` payloads, none of
  which this file previously modelled beyond one or two fields. Fields the
  table marks nullable (``run_aborted.task_id``,
  ``spec_updated.old_spec_hash``, ``turn_completed.num_turns`` /
  ``cost_usd_estimate``, ``task_escalated.category``,
  ``adr_logged.supersedes``) are each exercised both populated and null in
  ``_cases()``. There is deliberately no ``scope_change`` event pair; this
  file never assumed one.
* ``Claim`` fields (``base_commit``, ``frozen_hashes``, ``allowed_tools``,
  ``claimed_at``) come directly from the section 4.1 loop pseudocode
  comment ("base_commit + frozen hashes") and from
  ``runner.dispatch(prompt, lease.allowed_tools, ...)`` in the same
  listing.
* ``Verdict`` / ``GateName`` / ``GateStatus`` / ``Proof`` / ``GateResult``:
  gate names and order come verbatim from section 7.1. Status vocabulary
  (passed/failed/skipped) comes from section 7.1's short-circuit
  description. ``Verdict.PASS`` is used directly in the section 4.1
  pseudocode. The enclosing ``Proof`` envelope shape (task_id, attempt,
  verdict, gates, computed_at) is not spelled out field-by-field anywhere
  in CLAUDE.md; the fields chosen here are the minimum needed to satisfy
  what section 4.1 and section 2.3's proof/ directory listing require.
* ``BlockerCategory`` values (``ambiguous_task``, ``missing_dependency``,
  ``environment_broken``, ``contradictory_rules``) are lifted directly
  from section 5.3's outcome contract prose: "ambiguous, a dependency is
  missing, the environment is broken, or two rules contradict."
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from redgear.schemas import (
    AcceptanceCriterion,
    AdrLoggedEvent,
    Budget,
    Claim,
    Edge,
    Escalation,
    Event,
    Frozen,
    GateName,
    GateResult,
    GateStatus,
    LeaseExpiredEvent,
    PlanApprovedEvent,
    PlanGeneratedEvent,
    Project,
    PromptDispatchedEvent,
    Proof,
    Requirement,
    RunAbortedEvent,
    RunEndedEvent,
    RunStartedEvent,
    Scope,
    Spec,
    SpecUpdatedEvent,
    TaskClaimedEvent,
    TaskEscalatedEvent,
    TaskGraph,
    TaskNode,
    TaskRejectedEvent,
    TaskVerifiedEvent,
    TurnCompletedEvent,
    TurnOutcome,
    TurnResult,
    Verdict,
    VerifiedBy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / ".redgear" / "spec" / "spec.json"
GRAPH_PATH = REPO_ROOT / ".redgear" / "task_graph.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def real_spec() -> dict[str, Any]:
    return _load_json(SPEC_PATH)  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def real_graph() -> dict[str, Any]:
    return _load_json(GRAPH_PATH)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Synthetic example payloads for models with no real on-disk example
# (Budget, TurnResult, Proof/GateResult, and every event type).
# ---------------------------------------------------------------------------

BUDGET_EXAMPLE: dict[str, Any] = {
    "max_iterations": 50,
    "max_wall_clock_s": 7200,
    "max_consecutive_failures": 5,
    "max_turns_per_dispatch": 25,
    "per_turn_usd": None,
    "dispatch_timeout_s": 900,
}

TURN_RESULT_EXAMPLE: dict[str, Any] = {
    "outcome": "completed",
    "summary": "Implemented the schemas module per the failing tests.",
    "changed_files": ["redgear/schemas.py"],
    "known_gaps": [],
    "blocker_category": None,
    "blocker_detail": None,
    "exit_code": 0,
    "session_id": "sess-abc123",
    "num_turns": 4,
    "duration_ms": 12345,
    "cost_usd_estimate": 0.42,
    "raw_stdout_path": ".redgear/runs/run_01/iterations/0003/agent_stdout.log",
    "parse_ok": True,
}

TURN_RESULT_BLOCKED_EXAMPLE: dict[str, Any] = {
    "outcome": "blocked",
    "summary": "CLAUDE.md section 3.5 is referenced but does not exist.",
    "changed_files": [],
    "known_gaps": ["canonical hashing algorithm undocumented"],
    "blocker_category": "contradictory_rules",
    "blocker_detail": "Task instructions cite CLAUDE.md section 3.5, which is absent.",
    "exit_code": 0,
    "session_id": "sess-def456",
    "num_turns": 1,
    "duration_ms": 890,
    "cost_usd_estimate": 0.01,
    "raw_stdout_path": ".redgear/runs/run_01/iterations/0002/agent_stdout.log",
    "parse_ok": True,
}

GATE_RESULT_EXAMPLE: dict[str, Any] = {
    "name": "scope_check",
    "status": "passed",
    "reasons": [],
}

GATE_RESULT_FAILED_EXAMPLE: dict[str, Any] = {
    "name": "frozen_hash_check",
    "status": "failed",
    "reasons": ["frozen_file_modified"],
}

PROOF_EXAMPLE: dict[str, Any] = {
    "task_id": "T-0003",
    "attempt": 1,
    "verdict": "pass",
    "gates": [
        {"name": "scope_check", "status": "passed", "reasons": []},
        {"name": "frozen_hash_check", "status": "passed", "reasons": []},
        {"name": "lint", "status": "passed", "reasons": []},
        {"name": "tests_pass", "status": "passed", "reasons": []},
        {"name": "criteria_coverage", "status": "passed", "reasons": []},
        {"name": "coverage_delta", "status": "passed", "reasons": []},
    ],
    "computed_at": "2026-08-11T00:00:00Z",
}

# Every event payload below matches CLAUDE.md section 3.6's table exactly:
# base fields (event, seq, ts, actor) plus that row's "Payload beyond the
# base" column, no more, no less.

RUN_STARTED_EXAMPLE: dict[str, Any] = {
    "event": "run_started",
    "seq": 0,
    "ts": "2026-08-11T00:00:00Z",
    "actor": "engine",
    "run_id": "run_01J8X",
    "budget": BUDGET_EXAMPLE,
    "base_commit": "8ac1471000000000000000000000000000000a",
}

RUN_ENDED_EXAMPLE: dict[str, Any] = {
    "event": "run_ended",
    "seq": 42,
    "ts": "2026-08-11T01:00:00Z",
    "actor": "engine",
    "run_id": "run_01J8X",
    "reason": "complete",
    "iterations": 12,
    "tasks_verified": 5,
    "tasks_escalated": 0,
    "duration_ms": 3_600_000,
}

# task_id is nullable: populated when a task was in flight, null when the
# abort landed between tasks.
RUN_ABORTED_EXAMPLE: dict[str, Any] = {
    "event": "run_aborted",
    "seq": 7,
    "ts": "2026-08-11T00:30:00Z",
    "actor": "engine",
    "run_id": "run_01J8X",
    "signal": "SIGINT",
    "iteration": 3,
    "task_id": "T-0003",
}

RUN_ABORTED_BETWEEN_TASKS_EXAMPLE: dict[str, Any] = {
    **RUN_ABORTED_EXAMPLE,
    "seq": 8,
    "task_id": None,
}

PLAN_GENERATED_EXAMPLE: dict[str, Any] = {
    "event": "plan_generated",
    "seq": 0,
    "ts": "2026-08-11T00:00:00Z",
    "actor": "engine",
    "spec_hash": "sha256:dd2914150ecf303b0e5a584f508c32807f72f75277c488c788eae06c4f31e988",
    "node_count": 41,
    "edge_count": 49,
    "source_document": "docs/PRD.md",
}

PLAN_APPROVED_EXAMPLE: dict[str, Any] = {
    "event": "plan_approved",
    "seq": 1,
    "ts": "2026-08-11T00:00:01Z",
    "actor": "human",
    "spec_hash": "sha256:dd2914150ecf303b0e5a584f508c32807f72f75277c488c788eae06c4f31e988",
    "approved_by": "dhruvsaraf05@gmail.com",
}

# old_spec_hash is nullable: null the first time a spec is ever recorded.
SPEC_UPDATED_EXAMPLE: dict[str, Any] = {
    "event": "spec_updated",
    "seq": 50,
    "ts": "2026-08-12T00:00:00Z",
    "actor": "human",
    "old_spec_hash": "sha256:dd2914150ecf303b0e5a584f508c32807f72f75277c488c788eae06c4f31e988",
    "new_spec_hash": "sha256:" + "b" * 64,
    "added": ["FR-13"],
    "removed": [],
    "modified": ["FR-2"],
    "tasks_marked_drift": ["T-0005"],
}

SPEC_UPDATED_INITIAL_EXAMPLE: dict[str, Any] = {
    **SPEC_UPDATED_EXAMPLE,
    "seq": 0,
    "old_spec_hash": None,
    "added": ["FR-1", "FR-2"],
    "modified": [],
    "tasks_marked_drift": [],
}

TASK_CLAIMED_EXAMPLE: dict[str, Any] = {
    "event": "task_claimed",
    "seq": 2,
    "ts": "2026-08-11T00:01:00Z",
    "actor": "engine",
    "task_id": "T-0003",
    "attempt": 1,
    "claim_token": "claim-T-0003-1-a1b2c3",
    "base_commit": "8ac1471000000000000000000000000000000a",
    "lease_expires": "2026-08-11T00:16:00Z",
    "frozen_file_count": 3,
}

PROMPT_DISPATCHED_EXAMPLE: dict[str, Any] = {
    "event": "prompt_dispatched",
    "seq": 3,
    "ts": "2026-08-11T00:01:05Z",
    "actor": "engine",
    "task_id": "T-0003",
    "attempt": 1,
    "prompt_path": ".redgear/runs/run_01J8X/iterations/0001/prompt.txt",
    "prompt_sha256": "c" * 64,
    "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(git status *)"],
}

# num_turns and cost_usd_estimate are nullable: null when the runner never
# got far enough to produce them (e.g. a malformed structured_output).
TURN_COMPLETED_EXAMPLE: dict[str, Any] = {
    "event": "turn_completed",
    "seq": 4,
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
}

TURN_COMPLETED_UNPARSEABLE_EXAMPLE: dict[str, Any] = {
    **TURN_COMPLETED_EXAMPLE,
    "seq": 40,
    "outcome": "blocked",
    "exit_code": 1,
    "num_turns": None,
    "cost_usd_estimate": None,
    "parse_ok": False,
}

TASK_VERIFIED_EXAMPLE: dict[str, Any] = {
    "event": "task_verified",
    "seq": 5,
    "ts": "2026-08-11T00:05:10Z",
    "actor": "engine",
    "task_id": "T-0003",
    "attempt": 1,
    "proof_id": "proof-0003-1",
    "spec_hash": "sha256:dd2914150ecf303b0e5a584f508c32807f72f75277c488c788eae06c4f31e988",
    "gates_passed": [
        "scope_check",
        "frozen_hash_check",
        "lint",
        "tests_pass",
        "criteria_coverage",
        "coverage_delta",
    ],
    "duration_ms": 890,
}

TASK_REJECTED_EXAMPLE: dict[str, Any] = {
    "event": "task_rejected",
    "seq": 5,
    "ts": "2026-08-11T00:05:10Z",
    "actor": "engine",
    "task_id": "T-0003",
    "attempt": 1,
    "proof_id": "proof-0003-1",
    "failed_gates": ["tests_pass"],
    "attempts_remaining": 2,
    "summary": "GATE tests_pass FAILED (1 failed, 3 passed)",
}

# category is nullable: populated for a `blocker` escalation (reusing
# BlockerCategory), null for `attempts_exhausted` -- there is no blocker
# category when the agent simply ran out of attempts.
TASK_ESCALATED_EXAMPLE: dict[str, Any] = {
    "event": "task_escalated",
    "seq": 6,
    "ts": "2026-08-11T00:05:11Z",
    "actor": "engine",
    "task_id": "T-0003",
    "reason": "blocker",
    "category": "ambiguous_task",
    "detail": "CLAUDE.md section 3.5 is referenced but does not exist.",
    "attempted": 0,
}

TASK_ESCALATED_ATTEMPTS_EXHAUSTED_EXAMPLE: dict[str, Any] = {
    **TASK_ESCALATED_EXAMPLE,
    "seq": 60,
    "reason": "attempts_exhausted",
    "category": None,
    "detail": "3 attempts consumed without a passing verdict.",
    "attempted": 3,
}

LEASE_EXPIRED_EXAMPLE: dict[str, Any] = {
    "event": "lease_expired",
    "seq": 51,
    "ts": "2026-08-12T00:05:00Z",
    "actor": "engine",
    "task_id": "T-0007",
    "attempt": 1,
    "claim_token": "claim-T-0007-1-d4e5f6",
    "counted_as_attempt": False,
}

# supersedes is nullable: populated when this ADR replaces an earlier one
# (ADRs are immutable, section 1.4 -- a change is a new record that
# supersedes the old, never an edit), null for a brand-new ADR.
ADR_LOGGED_EXAMPLE: dict[str, Any] = {
    "event": "adr_logged",
    "seq": 52,
    "ts": "2026-08-12T00:10:00Z",
    "actor": "human",
    "adr_id": "ADR-0008",
    "task_id": "T-0007",
    "title": "hashing.py tasks get a 15-minute lease, not the 5-minute default",
    "rule": "Lease timeout for hashing.py tasks is 15 minutes.",
    "applies_to": ["redgear/hashing.py"],
    "supersedes": "ADR-0003",
}

ADR_LOGGED_NEW_EXAMPLE: dict[str, Any] = {
    **ADR_LOGGED_EXAMPLE,
    "seq": 10,
    "adr_id": "ADR-0007",
    "title": "Integer minor units only for money",
    "rule": "Store money as integer minor units; never a float.",
    "applies_to": ["redgear/ledger/**"],
    "supersedes": None,
}

CLAIM_EXAMPLE: dict[str, Any] = {
    "base_commit": "8ac1471000000000000000000000000000000a",
    "frozen_hashes": {"tests/test_schemas.py": "a" * 64},
    "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(git status *)"],
    "claimed_at": "2026-08-11T00:01:00Z",
}

ESCALATION_EXAMPLE: dict[str, Any] = {
    "reason": "attempts_exhausted",
    "detail": "3 attempts consumed without a passing verdict.",
    "escalated_at": "2026-08-11T00:10:00Z",
}


# ---------------------------------------------------------------------------
# AC-1 + AC-2: round trip and extra-forbid, parametrized across every model.
# ---------------------------------------------------------------------------


def _cases(
    real_spec: dict[str, Any], real_graph: dict[str, Any]
) -> list[tuple[str, type, dict[str, Any]]]:
    requirement = next(r for r in real_spec["requirements"] if r["id"] == "FR-1")
    project = real_spec["project"]

    scaffold_node = next(n for n in real_graph["nodes"] if n["id"] == "T-0001")
    test_authoring_node = next(n for n in real_graph["nodes"] if n["id"] == "T-0002")
    implementation_node = next(n for n in real_graph["nodes"] if n["id"] == "T-0003")
    edge = real_graph["edges"][0]

    scope = test_authoring_node["scope"]
    verified_by = test_authoring_node["acceptance_criteria"][0]["verified_by"]
    acceptance_criterion = test_authoring_node["acceptance_criteria"][0]

    populated_node = dict(implementation_node)
    populated_node["claim"] = CLAIM_EXAMPLE
    populated_node["escalation"] = ESCALATION_EXAMPLE
    populated_node["prior_attempts"] = [
        {
            "attempt_number": 1,
            "outcome": "completed",
            "failed_gate": "tests_pass",
            "failure_excerpt": "GATE tests_pass FAILED (1 failed, 3 passed)",
            "recorded_at": "2026-08-11T00:09:00Z",
        }
    ]

    return [
        ("Project", Project, project),
        ("Requirement", Requirement, requirement),
        ("Spec", Spec, real_spec),
        ("Scope", Scope, scope),
        ("VerifiedBy", VerifiedBy, verified_by),
        ("AcceptanceCriterion", AcceptanceCriterion, acceptance_criterion),
        ("TaskNode-scaffold", TaskNode, scaffold_node),
        ("TaskNode-test_authoring", TaskNode, test_authoring_node),
        ("TaskNode-implementation", TaskNode, implementation_node),
        ("TaskNode-populated", TaskNode, populated_node),
        ("Edge", Edge, edge),
        ("TaskGraph", TaskGraph, real_graph),
        ("Claim", Claim, CLAIM_EXAMPLE),
        ("Escalation", Escalation, ESCALATION_EXAMPLE),
        ("Budget", Budget, BUDGET_EXAMPLE),
        ("TurnResult-completed", TurnResult, TURN_RESULT_EXAMPLE),
        ("TurnResult-blocked", TurnResult, TURN_RESULT_BLOCKED_EXAMPLE),
        ("GateResult-passed", GateResult, GATE_RESULT_EXAMPLE),
        ("GateResult-failed", GateResult, GATE_RESULT_FAILED_EXAMPLE),
        ("Proof", Proof, PROOF_EXAMPLE),
        ("RunStartedEvent", RunStartedEvent, RUN_STARTED_EXAMPLE),
        ("RunEndedEvent", RunEndedEvent, RUN_ENDED_EXAMPLE),
        ("RunAbortedEvent-with-task", RunAbortedEvent, RUN_ABORTED_EXAMPLE),
        ("RunAbortedEvent-no-task", RunAbortedEvent, RUN_ABORTED_BETWEEN_TASKS_EXAMPLE),
        ("PlanGeneratedEvent", PlanGeneratedEvent, PLAN_GENERATED_EXAMPLE),
        ("PlanApprovedEvent", PlanApprovedEvent, PLAN_APPROVED_EXAMPLE),
        ("SpecUpdatedEvent-with-old-hash", SpecUpdatedEvent, SPEC_UPDATED_EXAMPLE),
        ("SpecUpdatedEvent-initial", SpecUpdatedEvent, SPEC_UPDATED_INITIAL_EXAMPLE),
        ("TaskClaimedEvent", TaskClaimedEvent, TASK_CLAIMED_EXAMPLE),
        ("PromptDispatchedEvent", PromptDispatchedEvent, PROMPT_DISPATCHED_EXAMPLE),
        ("TurnCompletedEvent-populated", TurnCompletedEvent, TURN_COMPLETED_EXAMPLE),
        ("TurnCompletedEvent-unparseable", TurnCompletedEvent, TURN_COMPLETED_UNPARSEABLE_EXAMPLE),
        ("TaskVerifiedEvent", TaskVerifiedEvent, TASK_VERIFIED_EXAMPLE),
        ("TaskRejectedEvent", TaskRejectedEvent, TASK_REJECTED_EXAMPLE),
        ("TaskEscalatedEvent-blocker", TaskEscalatedEvent, TASK_ESCALATED_EXAMPLE),
        (
            "TaskEscalatedEvent-attempts-exhausted",
            TaskEscalatedEvent,
            TASK_ESCALATED_ATTEMPTS_EXHAUSTED_EXAMPLE,
        ),
        ("LeaseExpiredEvent", LeaseExpiredEvent, LEASE_EXPIRED_EXAMPLE),
        ("AdrLoggedEvent-supersedes", AdrLoggedEvent, ADR_LOGGED_EXAMPLE),
        ("AdrLoggedEvent-new", AdrLoggedEvent, ADR_LOGGED_NEW_EXAMPLE),
    ]


def test_all_models_round_trip(real_spec: dict[str, Any], real_graph: dict[str, Any]) -> None:
    """Model -> JSON -> model is lossless, with no coercion drift.

    Not "no exception raised": every field of the reloaded instance must
    equal the original, and dumping twice must be idempotent. A model that
    silently coerces int to str, drops a None, or reorders a dict would
    pass a bare "no exception" check but fail this one.
    """
    for label, model_cls, data in _cases(real_spec, real_graph):
        instance = model_cls.model_validate(data)
        dumped = instance.model_dump(mode="json")

        reloaded = model_cls.model_validate(dumped)
        assert reloaded == instance, f"{label}: round trip changed the model"

        redumped = reloaded.model_dump(mode="json")
        assert redumped == dumped, f"{label}: dumping twice is not idempotent"


def test_extra_forbid_and_frozen(real_spec: dict[str, Any], real_graph: dict[str, Any]) -> None:
    """Extra fields are rejected everywhere; declared-frozen models reject mutation."""
    for label, model_cls, data in _cases(real_spec, real_graph):
        with pytest.raises(ValidationError) as excinfo:
            model_cls.model_validate({**data, "__redgear_unknown_field__": "x"})
        errors = excinfo.value.errors()
        assert any(e["type"] == "extra_forbidden" for e in errors), (
            f"{label}: extra field was not rejected as extra_forbidden"
        )

    # Frozen models: CLAUDE.md pseudocodes `class Budget(Frozen)` (section 8.1)
    # and `class TurnResult(Frozen)` (section 6.4) verbatim. Event records are
    # append-only by G4 and are treated as frozen here for the same reason --
    # every one of the 14 event types (section 3.6) is checked, not a sample.
    frozen_cases: list[tuple[str, type, dict[str, Any]]] = [
        ("Budget", Budget, BUDGET_EXAMPLE),
        ("TurnResult", TurnResult, TURN_RESULT_EXAMPLE),
        *(
            (discriminator, EVENT_CLASSES_BY_DISCRIMINATOR[discriminator], payload)
            for discriminator, payload in EVENT_EXAMPLES
        ),
    ]
    assert len(frozen_cases) == 2 + 14
    for label, model_cls, data in frozen_cases:
        instance = model_cls.model_validate(data)
        assert isinstance(instance, Frozen), f"{label}: does not inherit from Frozen"
        first_field = next(iter(model_cls.model_fields))
        with pytest.raises(ValidationError) as excinfo:
            setattr(instance, first_field, getattr(instance, first_field))
        errors = excinfo.value.errors()
        assert any(e["type"] == "frozen_instance" for e in errors), (
            f"{label}: mutating a frozen instance did not raise frozen_instance"
        )


# ---------------------------------------------------------------------------
# AC-3: the event union discriminates on `event` for every type.
# ---------------------------------------------------------------------------


EVENT_EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    ("run_started", RUN_STARTED_EXAMPLE),
    ("run_ended", RUN_ENDED_EXAMPLE),
    ("run_aborted", RUN_ABORTED_EXAMPLE),
    ("plan_approved", PLAN_APPROVED_EXAMPLE),
    ("task_claimed", TASK_CLAIMED_EXAMPLE),
    ("prompt_dispatched", PROMPT_DISPATCHED_EXAMPLE),
    ("turn_completed", TURN_COMPLETED_EXAMPLE),
    ("task_verified", TASK_VERIFIED_EXAMPLE),
    ("task_rejected", TASK_REJECTED_EXAMPLE),
    ("task_escalated", TASK_ESCALATED_EXAMPLE),
    ("plan_generated", PLAN_GENERATED_EXAMPLE),
    ("spec_updated", SPEC_UPDATED_EXAMPLE),
    ("lease_expired", LEASE_EXPIRED_EXAMPLE),
    ("adr_logged", ADR_LOGGED_EXAMPLE),
]

EVENT_CLASSES_BY_DISCRIMINATOR: dict[str, type] = {
    "run_started": RunStartedEvent,
    "run_ended": RunEndedEvent,
    "run_aborted": RunAbortedEvent,
    "plan_approved": PlanApprovedEvent,
    "task_claimed": TaskClaimedEvent,
    "prompt_dispatched": PromptDispatchedEvent,
    "turn_completed": TurnCompletedEvent,
    "task_verified": TaskVerifiedEvent,
    "task_rejected": TaskRejectedEvent,
    "task_escalated": TaskEscalatedEvent,
    "plan_generated": PlanGeneratedEvent,
    "spec_updated": SpecUpdatedEvent,
    "lease_expired": LeaseExpiredEvent,
    "adr_logged": AdrLoggedEvent,
}


def test_event_union_discriminates() -> None:
    """`Event` resolves to the right concrete class for every declared type,
    and rejects a discriminator value that names none of them."""
    from pydantic import TypeAdapter

    adapter: TypeAdapter[Any] = TypeAdapter(Event)

    assert len(EVENT_EXAMPLES) == len(EVENT_CLASSES_BY_DISCRIMINATOR) == 14, (
        "CLAUDE.md section 3.6 closes the event taxonomy at 14 types; "
        "every event type must have exactly one example"
    )

    for discriminator, payload in EVENT_EXAMPLES:
        resolved = adapter.validate_python(payload)
        expected_cls = EVENT_CLASSES_BY_DISCRIMINATOR[discriminator]
        assert type(resolved) is expected_cls, (
            f"event={discriminator!r} resolved to {type(resolved).__name__}, "
            f"expected {expected_cls.__name__}"
        )
        assert resolved.event == discriminator  # type: ignore[union-attr]

    with pytest.raises(ValidationError) as excinfo:
        adapter.validate_python({**RUN_STARTED_EXAMPLE, "event": "not_a_real_event"})
    errors = excinfo.value.errors()
    assert any(e["type"] == "union_tag_invalid" for e in errors), (
        "an unknown discriminator value must be rejected, not silently coerced"
    )


# ---------------------------------------------------------------------------
# AC-4: G2 as a schema rule -- an implementation node may not author its own
# acceptance criteria.
# ---------------------------------------------------------------------------


def test_impl_node_may_not_author_criteria(real_graph: dict[str, Any]) -> None:
    """CLAUDE.md section 4.4 invariant 5 / G2: if a node inherits criteria,
    it must not also carry its own. This must fail at the model layer, not
    only in a later verifier gate -- guessing wrong here is exactly the
    "confidently verified wrong software" section 3.3 warns about."""
    implementation_node = next(n for n in real_graph["nodes"] if n["id"] == "T-0003")
    assert implementation_node["type"] == "implementation"
    assert implementation_node["acceptance_criteria"] == []
    assert implementation_node["inherits_criteria_from"]

    # Sanity: the real node, unmodified, is valid.
    TaskNode.model_validate(implementation_node)

    tampered = dict(implementation_node)
    tampered["acceptance_criteria"] = [
        {
            "id": "AC-1",
            "statement": "Sneaking in a self-authored criterion.",
            "verified_by": {"kind": "test", "selector": "tests/test_x.py::test_y"},
        }
    ]

    with pytest.raises(ValidationError) as excinfo:
        TaskNode.model_validate(tampered)
    message = str(excinfo.value)
    assert "acceptance_criteria" in message or "inherits_criteria_from" in message


def test_scaffold_node_has_no_inherited_criteria(real_graph: dict[str, Any]) -> None:
    """CLAUDE.md section 4.4 invariant 8: a scaffold node is exempt from the
    two-phase inheritance rule entirely -- it authors its own smoke criteria
    directly. T-0001 itself is the real example."""
    scaffold_node = next(n for n in real_graph["nodes"] if n["id"] == "T-0001")
    assert scaffold_node["type"] == "scaffold"
    assert scaffold_node["inherits_criteria_from"] == []
    assert scaffold_node["acceptance_criteria"]

    validated = TaskNode.model_validate(scaffold_node)
    assert validated.type == "scaffold"


def test_test_authoring_node_cannot_inherit(real_graph: dict[str, Any]) -> None:
    """A test_authoring node authors criteria; it must not inherit any --
    the inverse direction of AC-4's rule."""
    test_authoring_node = next(n for n in real_graph["nodes"] if n["id"] == "T-0002")
    assert test_authoring_node["inherits_criteria_from"] == []
    assert test_authoring_node["acceptance_criteria"]

    tampered = dict(test_authoring_node)
    tampered["inherits_criteria_from"] = ["T-0001"]

    with pytest.raises(ValidationError):
        TaskNode.model_validate(tampered)


def test_gate_and_proof_fields_are_real_enums() -> None:
    """CLAUDE.md section 4.1 uses `Verdict.PASS` as an enum member access,
    and section 6.4 defines TurnOutcome as `class TurnOutcome(str, Enum)`
    rather than a bare Literal. GateName/GateStatus/Verdict follow the same
    convention: a wrong implementation using plain str/Literal fields would
    still round-trip correctly but would fail this identity check."""
    gate = GateResult.model_validate(GATE_RESULT_EXAMPLE)
    assert isinstance(gate.name, GateName)
    assert gate.name == GateName.SCOPE_CHECK
    assert isinstance(gate.status, GateStatus)
    assert gate.status == GateStatus.PASSED

    proof = Proof.model_validate(PROOF_EXAMPLE)
    assert isinstance(proof.verdict, Verdict)
    assert proof.verdict == Verdict.PASS
    assert [g.name for g in proof.gates] == [
        GateName.SCOPE_CHECK,
        GateName.FROZEN_HASH_CHECK,
        GateName.LINT,
        GateName.TESTS_PASS,
        GateName.CRITERIA_COVERAGE,
        GateName.COVERAGE_DELTA,
    ]


def test_turn_outcome_enum_matches_contract() -> None:
    """CLAUDE.md section 5.3 / section 6.4: exactly these three outcomes."""
    assert {member.value for member in TurnOutcome} == {
        "completed",
        "blocked",
        "scope_insufficient",
    }


def test_datetime_fields_preserve_utc_z_suffix(real_spec: dict[str, Any]) -> None:
    """spec.json's created_at is `...Z`; a model that silently normalises
    away the Z or drops timezone info has changed the recorded value."""
    spec = Spec.model_validate(real_spec)
    assert isinstance(spec.created_at, datetime)
    assert spec.created_at.tzinfo is not None
    dumped = spec.model_dump(mode="json")
    assert dumped["created_at"] == real_spec["created_at"]
