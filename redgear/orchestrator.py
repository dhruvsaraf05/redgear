"""THE LOOP. Select, compose, dispatch, verify, decide (section 4.1).

This module owns **control flow and nothing else** (section 2.2). It does not
compose prompt text, does not build argv, does not run gates. It asks
``prompt_engine`` for a prompt, hands it to a ``Runner``, asks ``verifier``
for a proof, and decides what happens next. If a function here is formatting
something that ends up in a prompt, it is in the wrong module.

**The retry path is not a special case.** This is the single most important
structural decision in the file. A failed task returns to the queue and is
re-selected on the next iteration; ``prompt_engine`` emits a corrective prompt
because it can *see* the failure in ``prior_attempts``, not because anything
here told it to. One code path, one prompt builder, and no branch anywhere
below that tests whether this is a second attempt. The loop does not know that
a retry is happening, and it should stay that way -- the moment it knows, the
two paths start to drift and the divergence is invisible until it changes
behaviour nobody can trace.

(``tests/test_orchestrator.py`` greps this file for the shape of such a
branch, which is why the sentence above describes it rather than spelling it.)

**Ordering that carries a guarantee.** Each of these has a failure mode that
no later check can recover from:

* Budget and STOP are checked **before any work begins** (G6). A cap firing
  mid-turn leaves the tree written and the verdict unrecorded.
* The prompt is persisted **before dispatch is attempted** (G4, section 11.1
  rule 6). A run you cannot read back prompt-by-prompt is not auditable.
* ``blocked`` and ``scope_insufficient`` escalate **without running gates and
  without consuming an attempt** (G3). The agent said it could not proceed;
  verifying work it did not do is meaningless, and charging for honesty is
  what makes faking a pass the rational move.
* Verification runs only **after the agent process has exited**. There is no
  interleaving.

**Typed against the protocol** (NFR-8): ``Runner`` is a structural type and
this module imports no concrete runner. ``verify`` is injectable for the same
reason -- the loop's job is deciding what a verdict *means*, and its tests
should not have to spawn a pytest per iteration to find out.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from redgear import gitctx, prompt_engine, state_engine, verifier
from redgear.budget import RunCounters, budget_exhausted, stop_requested
from redgear.errors import DirtyTreeError, PlanUnreviewedError
from redgear.locks import run_lock
from redgear.paths import is_state_path
from redgear.prompt_engine import PromptContext
from redgear.runner import Runner, allowed_tools_for
from redgear.schemas import (
    AcceptanceCriterion,
    AdrRule,
    Budget,
    Claim,
    GateResult,
    GateStatus,
    HarnessConfig,
    Proof,
    TaskGraph,
    TaskNode,
    TurnOutcome,
    TurnResult,
    Verdict,
)

#: Section 4.3, normative. Every run ends on exactly one of these, and the
#: exit code is the CLI's contract with whatever is driving it.
#:
#: Note there is no entry for "nothing was ready". Exhausting the queue is how
#: a run *ends* -- `complete` when everything is verified, `blocked` when
#: something is waiting on a human -- not an error. See the T-0031 note in
#: docs/PROGRESS.md for why `E_NO_READY_TASK` was removed from section 4.7
#: rather than implemented.
TERMINATION_EXIT_CODES: dict[str, int] = {
    "complete": 0,
    "stopped": 0,
    "blocked": 2,
    "budget_exhausted": 3,
    "runner_error": 4,
    "engine_error": 5,
}

#: Section 6.4 rule 4: one retry, then the run ends loudly. Two unparseable
#: results in a row is an integration bug, not a flaky turn.
_MAX_PARSE_ATTEMPTS = 2

#: Outcomes that end the turn honestly (G3). They never reach a gate.
_HONEST_EXITS = frozenset({TurnOutcome.BLOCKED, TurnOutcome.SCOPE_INSUFFICIENT})


@dataclass(frozen=True)
class RunOutcome:
    """What one ``redgear run`` did. Printed as the closing status line
    (section 8.4) and returned to the CLI for its exit code."""

    run_id: str
    reason: str
    exit_code: int
    iterations: int
    tasks_verified: int
    tasks_escalated: int


class Verify(Protocol):
    """``verifier.run_gates``, as a structural type.

    Injectable so the loop's own tests can substitute a stub. Gate mechanics
    have 43 tests of their own; re-running them once per loop iteration would
    spawn a nested pytest for no additional coverage and make the suite slow.
    The default is the real thing, so production wiring is never the fake.
    """

    def __call__(
        self,
        repo_root: Path,
        *,
        task: TaskNode,
        claim: Claim,
        declared: Sequence[str],
        attempt: int,
        harness: HarnessConfig | None = None,
        criteria: Sequence[AcceptanceCriterion] | None = None,
    ) -> Proof: ...


def _node(graph: TaskGraph, task_id: str) -> TaskNode:
    for node in graph.nodes:
        if node.id == task_id:
            return node
    raise KeyError(task_id)


def _resolve_criteria(graph: TaskGraph, task: TaskNode) -> list[AcceptanceCriterion]:
    """The criteria this task is judged against.

    A ``test_authoring`` or ``scaffold`` node carries its own. An
    ``implementation`` node inherits from a verified sibling and, by invariant
    5, necessarily has none of its own -- G2 made mechanical at the schema
    level. Resolving it here rather than inside ``prompt_engine`` keeps that
    module a pure function of what it is handed.
    """
    if task.acceptance_criteria:
        return list(task.acceptance_criteria)

    inherited: list[AcceptanceCriterion] = []
    for source_id in task.inherits_criteria_from:
        try:
            inherited.extend(_node(graph, source_id).acceptance_criteria)
        except KeyError:  # pragma: no cover - validate_graph rejects this first
            continue
    return inherited


def _first_failure(proof: Proof) -> GateResult | None:
    """The first failed gate.

    Section 5.5 rule 1: only the first failed gate appears in a prompt. Later
    gates were skipped, and reporting them would imply information redgear
    does not have.
    """
    for gate in proof.gates:
        if gate.status is GateStatus.FAILED:
            return gate
    return None


def run(
    repo_root: Path,
    *,
    runner: Runner,
    budget: Budget,
    harness: HarnessConfig,
    adr_rules: Sequence[AdrRule] = (),
    verify: Verify | None = None,
    actor: str = "engine",
) -> RunOutcome:
    """Drive tasks to completion until a terminal condition is reached.

    Refuses to start on a ``draft`` plan (section 3.3 -- the plan defines the
    tests, so a wrong plan produces confidently verified wrong software) or on
    a dirty working tree (section 8.4 -- without a clean baseline the diff
    audit is fiction).
    """
    checker: Verify = verify if verify is not None else verifier.run_gates
    started = time.monotonic()

    with run_lock(repo_root, holder=actor):
        graph = state_engine.load_graph(repo_root)

        if graph.state != "active":
            raise PlanUnreviewedError(
                "the task graph has not been approved; `redgear run` refuses a draft plan",
                detail={"state": graph.state},
            )

        # The run lock is already held, and it lives in `.redgear/`. Section
        # 8.4 is about *uncommitted user work*, not about redgear's own
        # bookkeeping -- without this exclusion no run could ever start.
        dirty = [path for path in gitctx.dirty_paths(repo_root) if not is_state_path(path)]
        if dirty:
            paths: list[str] = []
            paths.extend(dirty)
            raise DirtyTreeError(
                "the working tree has uncommitted changes; commit or stash them first",
                detail={"paths": ", ".join(paths)},
            )

        run_id = state_engine.start_run(repo_root, budget, actor=actor)
        counters = RunCounters(
            iterations=0, consecutive_failures=0, started_at=datetime.now(tz=UTC)
        )
        verified = 0
        escalated = 0

        def finish(reason: str) -> RunOutcome:
            state_engine.end_run(
                repo_root,
                run_id,
                reason=reason,
                iterations=counters.iterations,
                tasks_verified=verified,
                tasks_escalated=escalated,
                duration_ms=int((time.monotonic() - started) * 1000),
                actor=actor,
            )
            return RunOutcome(
                run_id=run_id,
                reason=reason,
                exit_code=TERMINATION_EXIT_CODES[reason],
                iterations=counters.iterations,
                tasks_verified=verified,
                tasks_escalated=escalated,
            )

        while True:
            # --- G6 gates, before any work in this iteration ---------------
            if stop_requested(repo_root):
                return finish("stopped")
            if budget_exhausted(budget, counters, datetime.now(tz=UTC)) is not None:
                return finish("budget_exhausted")

            # --- A. SELECT --------------------------------------------------
            graph = state_engine.recompute_readiness(graph)
            selected = state_engine.next_ready_task(graph)
            if selected is None:
                # Nothing claimable. Everything verified is a complete run;
                # anything escalated means a human is holding the queue.
                blocked_by_human = any(node.state == "escalated" for node in graph.nodes)
                return finish("blocked" if blocked_by_human else "complete")

            graph = state_engine.claim_task(repo_root, graph, selected.id, actor=actor)
            task = _node(graph, selected.id)
            claim = task.claim
            if claim is None:  # pragma: no cover - claim_task always sets one
                return finish("engine_error")

            attempt = task.attempts + 1
            criteria = _resolve_criteria(graph, task)
            tools = allowed_tools_for(task)

            # --- B. COMPOSE -------------------------------------------------
            # Note what is NOT here: no branch on whether this is a retry.
            # `prior_attempts` already carries the last failure, so the same
            # call produces a corrective prompt when there is one to correct.
            context = PromptContext(
                repo_root=str(repo_root),
                attempt=attempt,
                max_attempts=task.max_attempts,
                harness=harness,
                criteria=criteria,
                adr_rules=list(adr_rules),
                prior_attempts=list(task.prior_attempts),
            )
            prompt = prompt_engine.build(task, claim, context)

            # --- C. DISPATCH ------------------------------------------------
            turn = _dispatch_with_one_retry(
                repo_root,
                runner=runner,
                run_id=run_id,
                iteration=counters.iterations,
                task=task,
                attempt=attempt,
                prompt=prompt,
                tools=tools,
                budget=budget,
                actor=actor,
            )
            if turn is None:
                # Two unparseable results running. An integration bug, and it
                # must be loud rather than retried forever (section 6.4 r4).
                return finish("runner_error")

            state_engine.record_turn(
                repo_root, task_id=task.id, attempt=attempt, result=turn, actor=actor
            )

            # --- D. VERIFY --------------------------------------------------
            if turn.outcome in _HONEST_EXITS:
                # G3: no gates, no attempt consumed. The run pauses here.
                graph = state_engine.escalate_task(
                    repo_root,
                    graph,
                    task.id,
                    actor=actor,
                    outcome=turn.outcome.value,
                    detail=turn.blocker_detail or turn.summary,
                )
                escalated += 1
                counters = counters.with_success()
                return finish("blocked")

            proof = checker(
                repo_root,
                task=task,
                claim=claim,
                declared=turn.changed_files,
                attempt=attempt,
                harness=harness,
                criteria=criteria,
            )
            proof_id = f"{run_id}-{task.id}-{attempt:02d}"

            # --- E. DECIDE --------------------------------------------------
            if proof.verdict is Verdict.PASS:
                graph = state_engine.mark_verified(
                    repo_root, graph, task.id, actor=actor, proof_id=proof_id
                )
                verified += 1
                counters = counters.with_success()
                continue

            failed = _first_failure(proof)
            graph = state_engine.reject_task(
                repo_root,
                graph,
                task.id,
                actor=actor,
                proof_id=proof_id,
                failed_gates=[failed.name.value] if failed is not None else [],
                # The section 5.5 excerpt, formatted by the prompt engine --
                # this module hands the string over, it does not compose one.
                # It goes in `summary` because that is the field the event log
                # already carries, and G4 needs it replayable: a value held
                # only in the projection would make every `rebuild` diverge.
                summary=prompt_engine.format_failure_excerpt(
                    failed.name if failed is not None else None,
                    "\n".join(failed.reasons) if failed is not None else "",
                    repo_root=str(repo_root),
                ),
            )
            counters = counters.with_failure()

            rejected = _node(graph, task.id)
            if rejected.attempts >= rejected.max_attempts:
                graph = state_engine.escalate_task(
                    repo_root,
                    graph,
                    task.id,
                    actor=actor,
                    outcome="attempts_exhausted",
                    detail=(
                        f"{rejected.attempts} attempts spent; last failed gate: "
                        f"{failed.name.value if failed is not None else 'unknown'}"
                    ),
                )
                escalated += 1
                return finish("blocked")
            # Otherwise fall through. The next iteration re-selects this task
            # and composes a corrective prompt from what was just recorded --
            # no branch, no special case.


def _dispatch_with_one_retry(
    repo_root: Path,
    *,
    runner: Runner,
    run_id: str,
    iteration: int,
    task: TaskNode,
    attempt: int,
    prompt: str,
    tools: Sequence[str],
    budget: Budget,
    actor: str,
) -> TurnResult | None:
    """Dispatch, retrying once on an unparseable result. ``None`` means both
    tries failed to parse.

    A parse failure is a *runner* fault, not a task failure (section 6.4 rule
    4), so it never touches the attempt counter. Each try persists its prompt
    first and separately, because G4 is about what was actually sent -- two
    dispatches are two records even when the bytes are identical.
    """
    for index in range(_MAX_PARSE_ATTEMPTS):
        state_engine.persist_prompt(
            repo_root,
            run_id,
            iteration * _MAX_PARSE_ATTEMPTS + index,
            prompt,
            task_id=task.id,
            attempt=attempt,
            allowed_tools=list(tools),
            actor=actor,
        )
        turn = runner.dispatch(
            prompt,
            list(tools),
            repo_root,
            budget.dispatch_timeout_s,
            budget.max_turns_per_dispatch,
        )
        if turn.parse_ok:
            return turn
    return None
