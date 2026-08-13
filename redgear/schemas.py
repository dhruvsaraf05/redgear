"""Every Pydantic model in redgear.

Leaf module (CLAUDE.md section 2.2): imports nothing else from ``redgear``.
The contract for every model here is ``tests/test_schemas.py`` (T-0002),
cross-referenced against CLAUDE.md section 3.1 (str-valued Enum subclasses,
RFC 3339 UTC timestamps, algorithm-prefixed hashes, ``extra="forbid"`` with
``frozen`` where the model is immutable), section 3.5 (spec/hash shapes),
section 3.6 (the closed 14-event taxonomy), section 4.2/4.4 (task states and
graph structure), section 6.4 (TurnResult/TurnOutcome), section 7.1/7.2
(gate names, order, verdicts), and section 8.1 (Budget).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class Strict(BaseModel):
    """extra="forbid" everywhere; the mutable-by-copy default."""

    model_config = ConfigDict(extra="forbid")


class Frozen(BaseModel):
    """extra="forbid" and immutable. CLAUDE.md section 8.1 / section 6.4
    pseudocode both name this class verbatim (``class Budget(Frozen):``,
    ``class TurnResult(Frozen):``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TurnOutcome(StrEnum):
    """CLAUDE.md section 5.3 / section 6.4: exactly these three."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    SCOPE_INSUFFICIENT = "scope_insufficient"


class BlockerCategory(StrEnum):
    """CLAUDE.md section 5.3: "ambiguous, a dependency is missing, the
    environment is broken, or two rules contradict"."""

    AMBIGUOUS_TASK = "ambiguous_task"
    MISSING_DEPENDENCY = "missing_dependency"
    ENVIRONMENT_BROKEN = "environment_broken"
    CONTRADICTORY_RULES = "contradictory_rules"


class GateName(StrEnum):
    """CLAUDE.md section 7.1: the six gates, in this order."""

    SCOPE_CHECK = "scope_check"
    FROZEN_HASH_CHECK = "frozen_hash_check"
    LINT = "lint"
    TESTS_PASS = "tests_pass"  # noqa: S105 -- gate name, not a credential
    CRITERIA_COVERAGE = "criteria_coverage"
    COVERAGE_DELTA = "coverage_delta"


class GateStatus(StrEnum):
    """CLAUDE.md section 7.1: short-circuits on first failure; later gates
    are recorded skipped, never omitted."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Verdict(StrEnum):
    """CLAUDE.md section 4.1 pseudocode uses `Verdict.PASS` directly."""

    PASS = "pass"  # noqa: S105 -- gate verdict, not a credential
    FAIL = "fail"


# ---------------------------------------------------------------------------
# Budget -- CLAUDE.md section 8.1
# ---------------------------------------------------------------------------


class Budget(Frozen):
    max_iterations: int = Field(default=50, ge=1, le=1000)
    max_wall_clock_s: int = Field(default=7200, ge=60)
    max_consecutive_failures: int = Field(default=5, ge=1)
    max_turns_per_dispatch: int = Field(default=25, ge=1)
    per_turn_usd: float | None = Field(default=None, ge=0)
    dispatch_timeout_s: int = Field(default=900, ge=30)


# ---------------------------------------------------------------------------
# TurnResult -- CLAUDE.md section 6.4
# ---------------------------------------------------------------------------


class TurnResult(Frozen):
    outcome: TurnOutcome
    summary: str = Field(max_length=1500)
    changed_files: list[str]
    known_gaps: list[str] = Field(default_factory=list)
    blocker_category: BlockerCategory | None = None
    blocker_detail: str | None = Field(default=None, max_length=4000)
    # runner-populated, never agent-supplied:
    exit_code: int
    session_id: str | None
    num_turns: int | None
    duration_ms: int | None
    cost_usd_estimate: float | None
    raw_stdout_path: str
    parse_ok: bool


# ---------------------------------------------------------------------------
# Spec -- CLAUDE.md section 3.5
# ---------------------------------------------------------------------------


class Requirement(Strict):
    id: str
    kind: Literal["functional", "non_functional"]
    statement: str
    rationale: str
    acceptance: list[str]
    priority: Literal["must", "should", "could"]


class Project(Strict):
    name: str
    root_globs: list[str]


class Spec(Strict):
    schema_version: int
    spec_id: str
    hash: str
    created_at: datetime
    supersedes: str | None
    project: Project
    requirements: list[Requirement]
    out_of_scope: list[str]


# ---------------------------------------------------------------------------
# Task graph -- CLAUDE.md section 4.2 / section 4.4
# ---------------------------------------------------------------------------


class VerifiedBy(Strict):
    kind: Literal["test"]
    selector: str


class AcceptanceCriterion(Strict):
    id: str
    statement: str
    verified_by: VerifiedBy


class Scope(Strict):
    writable_globs: list[str]
    creatable_globs: list[str]
    frozen_globs: list[str]


class Claim(Strict):
    """CLAUDE.md section 4.1 loop pseudocode: "base_commit + frozen hashes"
    and ``runner.dispatch(prompt, lease.allowed_tools, ...)``."""

    base_commit: str
    frozen_hashes: dict[str, str]
    allowed_tools: list[str]
    claimed_at: datetime


class Escalation(Strict):
    reason: Literal["blocker", "attempts_exhausted"]
    detail: str
    escalated_at: datetime


class LockRecord(Strict):
    """The contents of a lock file (`locks.py`).

    Self-describing on purpose: the failure users actually hit is a stale
    lock, and section 8.3 is explicit that they must not be left deleting a
    file they do not understand. Holder and expiry make it diagnosable by
    reading it.
    """

    task_id: str
    holder: str
    token: str
    acquired_at: datetime
    expires_at: datetime
    attempt: int = 1


class PriorAttempt(Strict):
    attempt_number: int
    outcome: TurnOutcome
    failed_gate: GateName | None
    failure_excerpt: str | None
    recorded_at: datetime


class TaskNode(Strict):
    id: str
    type: Literal["scaffold", "test_authoring", "implementation"]
    title: str
    state: Literal[
        "blocked",
        "ready",
        "claimed",
        "dispatched",
        "verifying",
        "verified",
        "rejected",
        "escalated",
    ]
    spec_refs: list[str]
    spec_hash: str
    depends_on: list[str]
    scope: Scope
    acceptance_criteria: list[AcceptanceCriterion]
    inherits_criteria_from: list[str]
    attempts: int
    max_attempts: int
    claim: Claim | None
    prior_attempts: list[PriorAttempt]
    verified_at: datetime | None
    proof_id: str | None
    escalation: Escalation | None
    note: str | None = None

    @model_validator(mode="after")
    def _enforce_two_phase_scope(self) -> Self:
        """G2 as a schema rule (CLAUDE.md section 4.4 invariant 5): an
        implementation node never authors its own acceptance_criteria, and
        a test_authoring node never inherits. Scaffold nodes (invariant 8)
        are exempt from both."""
        if self.type == "implementation" and self.acceptance_criteria:
            raise ValueError(
                "an implementation node may not author acceptance_criteria; "
                "it must inherit them from a verified test_authoring node (G2)"
            )
        if self.type == "test_authoring" and self.inherits_criteria_from:
            raise ValueError(
                "a test_authoring node authors its own acceptance_criteria "
                "and must not set inherits_criteria_from"
            )
        return self


class Edge(Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    from_: str = Field(alias="from")
    to: str
    kind: Literal["hard"]


class TaskGraph(Strict):
    schema_version: int
    spec_hash: str
    state: Literal["draft", "active"]
    generated_at: datetime
    nodes: list[TaskNode]
    edges: list[Edge]


# ---------------------------------------------------------------------------
# Verification proof -- CLAUDE.md section 7.1 / section 7.2
# ---------------------------------------------------------------------------


class HarnessConfig(Strict):
    """The commands the verification harness is allowed to run.

    CLAUDE.md section 7.3 is unambiguous: "No agent-supplied value reaches
    ``cmd``. Harness commands come from ``config.json`` only." So the argv
    vectors are *data*, supplied by the operator, and the verifier appends
    only its own fixed isolation flags -- never anything derived from the
    agent's output, a test name, or a file it wrote.

    The commands are full argument vectors rather than tool names because a
    bare name is not resolvable in general: in this project's own virtualenv
    ``ruff`` is not on PATH at all and is reachable only as
    ``python -m ruff``. A default of ``["ruff", ...]`` would look correct and
    fail on a clean checkout.
    """

    lint_cmd: list[str]
    test_cmd: list[str]
    coverage_cmd: list[str]
    #: Passed to ``coverage run --source``. Packages and directories only --
    #: coverage.py silently collects nothing from a bare file path.
    coverage_source: list[str]
    coverage_floor: float = Field(default=0.85, ge=0.0, le=1.0)
    timeout_s: int = Field(default=900, ge=1)


class GateResult(Strict):
    name: GateName
    status: GateStatus
    reasons: list[str] = Field(default_factory=list)


class Proof(Strict):
    task_id: str
    attempt: int
    verdict: Verdict
    gates: list[GateResult]
    computed_at: datetime


# ---------------------------------------------------------------------------
# Events -- CLAUDE.md section 3.6, closed at 14 types
# ---------------------------------------------------------------------------


class EventBase(Frozen):
    """Every event carries these three (section 3.6) beyond its own
    ``event`` discriminator, which each subclass declares itself."""

    seq: int
    ts: datetime
    actor: str


class RunStartedEvent(EventBase):
    event: Literal["run_started"]
    run_id: str
    budget: Budget
    base_commit: str


class RunEndedEvent(EventBase):
    event: Literal["run_ended"]
    run_id: str
    reason: Literal[
        "complete", "stopped", "blocked", "budget_exhausted", "runner_error", "engine_error"
    ]
    iterations: int
    tasks_verified: int
    tasks_escalated: int
    duration_ms: int


class RunAbortedEvent(EventBase):
    event: Literal["run_aborted"]
    run_id: str
    signal: Literal["SIGINT", "SIGTERM"]
    iteration: int
    task_id: str | None


class PlanGeneratedEvent(EventBase):
    event: Literal["plan_generated"]
    spec_hash: str
    node_count: int
    edge_count: int
    source_document: str


class PlanApprovedEvent(EventBase):
    event: Literal["plan_approved"]
    spec_hash: str
    approved_by: str


class SpecUpdatedEvent(EventBase):
    event: Literal["spec_updated"]
    old_spec_hash: str | None
    new_spec_hash: str
    added: list[str]
    removed: list[str]
    modified: list[str]
    tasks_marked_drift: list[str]


class TaskClaimedEvent(EventBase):
    event: Literal["task_claimed"]
    task_id: str
    attempt: int
    claim_token: str
    base_commit: str
    lease_expires: datetime
    frozen_file_count: int


class PromptDispatchedEvent(EventBase):
    event: Literal["prompt_dispatched"]
    task_id: str
    attempt: int
    prompt_path: str
    prompt_sha256: str
    allowed_tools: list[str]


class TurnCompletedEvent(EventBase):
    event: Literal["turn_completed"]
    task_id: str
    attempt: int
    outcome: TurnOutcome
    exit_code: int
    num_turns: int | None
    duration_ms: int
    cost_usd_estimate: float | None
    parse_ok: bool


class TaskVerifiedEvent(EventBase):
    event: Literal["task_verified"]
    task_id: str
    attempt: int
    proof_id: str
    spec_hash: str
    gates_passed: list[GateName]
    duration_ms: int


class TaskRejectedEvent(EventBase):
    event: Literal["task_rejected"]
    task_id: str
    attempt: int
    proof_id: str
    failed_gates: list[GateName]
    attempts_remaining: int
    summary: str


class TaskEscalatedEvent(EventBase):
    event: Literal["task_escalated"]
    task_id: str
    reason: Literal["blocker", "attempts_exhausted"]
    category: BlockerCategory | None
    detail: str
    attempted: int


class LeaseExpiredEvent(EventBase):
    event: Literal["lease_expired"]
    task_id: str
    attempt: int
    claim_token: str
    counted_as_attempt: bool


class AdrLoggedEvent(EventBase):
    event: Literal["adr_logged"]
    adr_id: str
    task_id: str
    title: str
    rule: str
    applies_to: list[str]
    supersedes: str | None


type Event = Annotated[
    RunStartedEvent
    | RunEndedEvent
    | RunAbortedEvent
    | PlanGeneratedEvent
    | PlanApprovedEvent
    | SpecUpdatedEvent
    | TaskClaimedEvent
    | PromptDispatchedEvent
    | TurnCompletedEvent
    | TaskVerifiedEvent
    | TaskRejectedEvent
    | TaskEscalatedEvent
    | LeaseExpiredEvent
    | AdrLoggedEvent,
    Field(discriminator="event"),
]
