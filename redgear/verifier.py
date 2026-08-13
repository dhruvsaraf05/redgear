"""The verification harness -- gates 1 and 2.

This is G1 and G2 made mechanical. Every other agent wrapper *asks* an agent
to stay in scope and *believes* what it reports; this module recomputes both
from real git state and real file contents, after the agent process has
exited, and believes nothing it can check itself.

Pure computation over the repository: it returns a ``Proof`` and persists
nothing. Writing the proof is ``state_engine``'s job (section 11.1 rule 4).

**Gate 1 -- scope_check.** The changed set comes from ``gitctx``, computed
independently. The agent's ``changed_files`` is a *claim*, cross-checked
against it, never the source of truth (G1).

**Gate 2 -- frozen_hash_check.** The mechanical heart of G2 -- the reason an
implementation agent cannot weaken a failing assertion to make the suite
green. Frozen globs are re-expanded against tracked *and untracked* files,
because a brand-new file inside a frozen glob is absent from the recorded
digest map and would otherwise walk straight through.

Gates 3-6 (lint, tests_pass, criteria_coverage, coverage_delta) arrive at
T-0025. Until then they are reported ``skipped`` with a reason -- never
stubbed to pass. A gate that always passes is worse than an absent one,
because the proof would show a green it never earned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from redgear import gitctx
from redgear.hashing import digest_map
from redgear.paths import match_glob, matches_any
from redgear.schemas import Claim, GateName, GateResult, GateStatus, Proof, TaskNode, Verdict

#: Section 7.1, normative order. Cheap structural checks run before expensive
#: ones, and the frozen-hash audit completes before anything executes code
#: from the working tree.
GATE_ORDER: list[GateName] = [
    GateName.SCOPE_CHECK,
    GateName.FROZEN_HASH_CHECK,
    GateName.LINT,
    GateName.TESTS_PASS,
    GateName.CRITERIA_COVERAGE,
    GateName.COVERAGE_DELTA,
]

#: Gates whose implementation lands at T-0025. Named explicitly so the proof
#: can say *why* they did not run rather than leaving a blank.
_NOT_YET_IMPLEMENTED = frozenset(GATE_ORDER[2:])

_PENDING_REASON = "not_implemented: arrives at T-0025 (verifier gates 3-6)"
_SKIPPED_REASON = "skipped: an earlier gate failed and the pipeline short-circuits"


def _posix(path: str) -> str:
    """Normalise a caller-supplied path for comparison with git output.

    Git always emits forward slashes. An agent running on Windows may declare
    ``src\\pkg\\x.py``; without this every such declaration would read as a
    phantom change and every real edit as undeclared.
    """
    return path.replace("\\", "/")


def _reason(kind: str, path: str) -> str:
    """Reasons are ``"<kind>: <path>"``.

    Flat strings because they are interpolated into a prompt (section 5.5),
    where a nested structure would cost tokens and read worse than a list a
    human or an agent can scan.
    """
    return f"{kind}: {path}"


# ---------------------------------------------------------------------------
# Gate 1 -- scope_check
# ---------------------------------------------------------------------------


def scope_check(
    repo_root: Path,
    *,
    task: TaskNode,
    claim: Claim,
    declared: Sequence[str],
) -> GateResult:
    """Did the diff stay inside the granted scope, and does the agent know
    what it changed?

    Three independent failures, all reported together so a corrective prompt
    carries the whole picture rather than one item per attempt:

    * ``out_of_scope_write`` -- a changed path matches no writable or
      creatable glob. New files must match ``creatable_globs`` specifically:
      a task permitted to edit existing files is not thereby permitted to
      invent new ones beside them.
    * ``undeclared_change`` -- redgear saw a change the agent did not report.
    * ``phantom_change`` -- the agent reported a file it never touched.

    The last two can fire while every path is technically in scope. That is
    the point: an agent that has lost track of its own edits cannot be
    trusted about anything else in the same submission.
    """
    changed = gitctx.changed_files(repo_root, claim.base_commit)
    actual = {entry.path: entry for entry in changed}
    declared_paths = {_posix(path) for path in declared}

    reasons: list[str] = []

    for path in sorted(actual):
        entry = actual[path]
        # An addition must satisfy creatable_globs specifically; anything
        # else need only be writable.
        allowed = (
            matches_any(path, task.scope.creatable_globs)
            if entry.status == "A"
            else matches_any(path, task.scope.writable_globs)
        )
        if not allowed:
            reasons.append(_reason("out_of_scope_write", path))

    for path in sorted(set(actual) - declared_paths):
        reasons.append(_reason("undeclared_change", path))

    for path in sorted(declared_paths - set(actual)):
        reasons.append(_reason("phantom_change", path))

    return GateResult(
        name=GateName.SCOPE_CHECK,
        status=GateStatus.FAILED if reasons else GateStatus.PASSED,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Gate 2 -- frozen_hash_check
# ---------------------------------------------------------------------------


def _current_frozen_digests(repo_root: Path, frozen_globs: Sequence[str]) -> dict[str, str]:
    """Re-expand the frozen globs and hash whatever matches *now*.

    Deliberately re-expanded rather than re-hashing the recorded paths.
    A new file inside a frozen glob is untracked and absent from the recorded
    map, so re-hashing only what was recorded would find every entry
    unchanged and pass -- while an agent-authored test sits in the frozen
    directory (section 7.2).

    Uses the same primitives as ``state_engine.claim_task``: ``git ls-files``
    over tracked and untracked files, then binary chunked digests. Claim-time
    and verify-time digests are therefore computed identically, so a
    difference means the content really differs.
    """
    if not frozen_globs:
        return {}
    candidates = [
        path
        for path in gitctx.tracked_and_untracked(repo_root)
        if any(match_glob(path, pattern) for pattern in frozen_globs)
        # `git ls-files --cached` still lists a tracked file after it has been
        # deleted from the working tree. Hashing it would raise; excluding it
        # is what makes it fall out of the current map and be reported as
        # `frozen_file_deleted` -- which is the violation, not an error.
        and (repo_root / path).is_file()
    ]
    return digest_map(repo_root, sorted(candidates))


def frozen_hash_check(
    repo_root: Path,
    *,
    task: TaskNode,
    claim: Claim,
    recorded: Mapping[str, str] | None = None,
) -> GateResult:
    """Is every frozen file byte-identical to what was recorded at claim time?

    Three kinds, distinct because the correct agent response differs for
    each: restore the content, restore the file, or remove the one it should
    never have created.

    Digests are binary and chunked (``hashing.file_digest``). Text mode would
    make a CRLF and an LF copy hash identically, so a frozen test's line
    endings could be rewritten unnoticed -- and a digest taken on Windows
    would disagree with the same file on Linux, failing this gate for every
    Linux user for no reason at all.

    Every violation is reported, sorted, so the report is complete and
    stable across runs (the prompt engine snapshots these).
    """
    baseline = dict(claim.frozen_hashes if recorded is None else recorded)
    current = _current_frozen_digests(repo_root, task.scope.frozen_globs)

    reasons: list[str] = []

    for path in sorted(set(baseline) | set(current)):
        was = baseline.get(path)
        now = current.get(path)
        if was is not None and now is None:
            reasons.append(_reason("frozen_file_deleted", path))
        elif was is None and now is not None:
            reasons.append(_reason("frozen_file_added", path))
        elif was != now:
            reasons.append(_reason("frozen_file_modified", path))

    return GateResult(
        name=GateName.FROZEN_HASH_CHECK,
        status=GateStatus.FAILED if reasons else GateStatus.PASSED,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def run_gates(
    repo_root: Path,
    *,
    task: TaskNode,
    claim: Claim,
    declared: Sequence[str],
    attempt: int,
) -> Proof:
    """Run the gate pipeline in order, short-circuiting on first failure.

    Gates after a failure are recorded ``skipped`` **and never omitted**
    (section 7.1): the proof has to show the full contract including what was
    not reached, or an omitted gate is indistinguishable from a gate that
    does not exist.

    The verdict is PASS only when every gate in the contracted list actually
    passed. While gates 3-6 are unimplemented that can never happen, which is
    correct -- a proof must not claim a green it did not earn.
    """
    implemented = {
        GateName.SCOPE_CHECK: lambda: scope_check(
            repo_root, task=task, claim=claim, declared=declared
        ),
        GateName.FROZEN_HASH_CHECK: lambda: frozen_hash_check(repo_root, task=task, claim=claim),
    }

    results: list[GateResult] = []
    short_circuited = False

    for name in GATE_ORDER:
        if short_circuited:
            results.append(
                GateResult(name=name, status=GateStatus.SKIPPED, reasons=[_SKIPPED_REASON])
            )
            continue

        runner = implemented.get(name)
        if runner is None:
            # Not yet built. Reported as skipped with a reason -- never as a
            # pass, which would put an unearned green in the proof.
            results.append(
                GateResult(name=name, status=GateStatus.SKIPPED, reasons=[_PENDING_REASON])
            )
            continue

        result = runner()
        results.append(result)
        if result.status is GateStatus.FAILED:
            short_circuited = True

    passed_everything = all(gate.status is GateStatus.PASSED for gate in results)
    return Proof(
        task_id=task.id,
        attempt=attempt,
        verdict=Verdict.PASS if passed_everything else Verdict.FAIL,
        gates=results,
        computed_at=datetime.now(tz=UTC),
    )
