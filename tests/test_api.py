"""T-0038: failing tests for api/app.py -- the read-only control plane API.

``redgear/api/app.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

FR-12: "A local read-only control plane renders the task graph, dispatched
prompts, diffs, and verification proofs derived solely from the event log."
Five acceptance criteria, five tests, one each:

* **AC-1** (``test_all_responses_log_derived``) is proven by *divergence*,
  not by trusting a response that merely looks right. The fixture corrupts
  ``task_graph.json``'s on-disk mutable fields directly -- bypassing every
  ``state_engine`` write path -- and leaves ``events.jsonl`` untouched. A
  handler that read the file straight would echo the corruption; a handler
  that replays the log the way ``redgear rebuild`` does reports the truth.
  Only the second is FR-12 AC-1.
* **AC-2** (``test_no_state_mutation_endpoints``) is proven by *route
  enumeration* -- walking ``app.routes`` and asserting which HTTP methods
  each path accepts -- rather than by grepping the source for the word
  "mutate". A grep proves nothing about what the framework actually wires up;
  the route table is the framework's own ground truth.
* **AC-3** (``test_prompts_retrievable_verbatim``) asserts byte-identical
  recovery of dispatched prompt text, per §2.3's "prompt.txt" -- one entry per
  *iteration* (a task can dispatch twice on one attempt if the first parse
  failed), not deduplicated to one per attempt.
* **AC-4** (``test_proofs_retrievable_per_attempt``) exercises two attempts
  on one task -- a rejection, then a verified retry -- and checks the raw
  diff and gate verdict are both retrievable, keyed by attempt.

  Proof artifacts (``verdict.json`` / ``diff.patch`` under
  ``.redgear/runs/<run_id>/iterations/<NNNN>/proof/``) are §2.3's documented
  layout, but nothing in ``redgear/`` writes them yet -- ``verifier.py``
  computes a ``Proof`` and hands it back in memory, and nothing persists it.
  That gap is closed as part of T-0039 (state_engine gets ``persist_proof``,
  orchestrator calls it), but T-0038 cannot write to ``redgear/`` at all
  (frozen). So this fixture writes the two files by hand, in the exact shape
  §2.3 already specifies -- the implementation is made to match a documented
  contract, not the other way around.

  One more thing worth flagging here because a passing test would otherwise
  hide it: ``state_engine.mark_verified``'s ``task_verified`` event carries
  ``node.attempts`` from *before* the transition, which is the count of
  *prior rejections*, not the number of the attempt just verified -- so on a
  first-try pass it is 0, not 1, and after one rejection it is 1 where the
  matching ``prompt_dispatched``/``turn_completed`` pair both say 2. No
  existing test pins the numeric value (only event *kinds* are asserted
  elsewhere), so this is a latent, previously-unflagged defect rather than a
  frozen behaviour this suite endorses. The API works around it by deriving
  the verified attempt's number from the count of rejections that preceded
  it rather than trusting the event's own field. See docs/PROGRESS.md.
* **AC-5** (``test_responses_redacted``) follows the T-0032 CLI redaction
  test's shape: a *short* secret plus an assertion that the ``[REDACTED]``
  marker is present, not merely that the raw secret is absent. A absence-only
  check would also pass if redaction never ran and the secret was simply
  never echoed back for unrelated reasons -- proving nothing. Short, because
  the only thing that matters here is byte-for-byte substring matching in a
  JSON body; there is no terminal width to wrap around the way rich's
  ``CliRunner`` output can.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redgear import state_engine
from redgear.api.app import create_app
from redgear.schemas import (
    GateName,
    GateResult,
    GateStatus,
    Proof,
    TurnOutcome,
    TurnResult,
    Verdict,
)

#: For hand-assembled JSON payloads -- graph fixtures need a JSON string.
FIXED_TS = "2026-01-01T00:00:00Z"
#: For constructing `Proof` directly -- the model field is `datetime`.
FIXED_DATETIME = datetime(2026, 1, 1, tzinfo=UTC)

SECRET = "SECRET_1234"  # noqa: S105 -- test fixture value, not a real credential
CREDENTIAL_ENV_VAR = "REDGEAR_TEST_API_TOKEN"


# ---------------------------------------------------------------------------
# Fixture repository: real git, real .redgear/, built with state_engine's own
# write path -- not hand-assembled JSON -- so the sequence is what a real
# claim/dispatch/verify cycle actually produces.
# ---------------------------------------------------------------------------


def _node(task_id: str, *, state: str = "ready") -> dict[str, Any]:
    return {
        "id": task_id,
        "type": "test_authoring",
        "title": f"task {task_id}",
        "state": state,
        "spec_refs": ["FR-12"],
        "spec_hash": "sha256:" + "a" * 64,
        "depends_on": [],
        "scope": {
            "writable_globs": ["tests/**"],
            "creatable_globs": ["tests/**"],
            "frozen_globs": ["src/**"],
        },
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "statement": "The placeholder test exists.",
                "verified_by": {"kind": "test", "selector": "tests/test_pkg.py::test_placeholder"},
            }
        ],
        "inherits_criteria_from": [],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }


def _write_graph(root: Path, nodes: list[dict[str, Any]]) -> None:
    redgear = root / ".redgear"
    (redgear / "spec").mkdir(parents=True, exist_ok=True)
    (redgear / "task_graph.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_hash": "sha256:" + "a" * 64,
                "state": "active",
                "generated_at": FIXED_TS,
                "nodes": nodes,
                "edges": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _proof(attempt: int, verdict: Verdict, *, failed: GateName | None) -> Proof:
    gates = [
        GateResult(
            name=name,
            status=(
                GateStatus.FAILED
                if name == failed
                else (GateStatus.PASSED if verdict is Verdict.PASS else GateStatus.SKIPPED)
            ),
            reasons=["lint_violation: src/pkg/__init__.py:1:1 F401"] if name == failed else [],
        )
        for name in GateName
    ]
    return Proof(
        task_id="T-0001", attempt=attempt, verdict=verdict, gates=gates, computed_at=FIXED_DATETIME
    )


def _write_proof(
    prompt_path: Path, *, attempt: int, verdict: Verdict, failed: GateName | None, diff: str
) -> None:
    """§2.3's documented layout, written by hand -- see the module docstring.

    ``prompt_path`` is what ``persist_prompt`` returned: the proof sits
    alongside it, in a ``proof/`` directory under the same iteration.
    """
    directory = prompt_path.parent / "proof"
    directory.mkdir(parents=True, exist_ok=True)
    proof = _proof(attempt, verdict, failed=failed)
    (directory / "verdict.json").write_text(
        json.dumps(proof.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    (directory / "diff.patch").write_text(diff, encoding="utf-8")


def _turn_result(*, summary: str = "did the work") -> TurnResult:
    return TurnResult(
        outcome=TurnOutcome.COMPLETED,
        summary=summary,
        changed_files=["tests/test_pkg.py"],
        exit_code=0,
        session_id=None,
        num_turns=1,
        duration_ms=10,
        cost_usd_estimate=None,
        raw_stdout_path="agent_stdout.log",
        parse_ok=True,
    )


PROMPT_ONE = "attempt one prompt text: fix the lint violation\n"
PROMPT_TWO_TEMPLATE = "attempt two prompt text, carrying the credential {secret}\n"
DIFF_ONE = "--- a/src/pkg/__init__.py\n+++ b/src/pkg/__init__.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
DIFF_TWO_TEMPLATE = (
    "--- a/src/pkg/__init__.py\n+++ b/src/pkg/__init__.py\n@@ -0,0 +1,1 @@\n+token = {secret!r}\n"
)


@pytest.fixture
def repo(git_repo: Path) -> Path:
    """One task, two real attempts: rejected, then verified.

    Built entirely through ``state_engine``'s own public write functions
    (``claim_task``, ``persist_prompt``, ``record_turn``, ``reject_task``,
    ``mark_verified``) -- the same functions the real orchestrator calls --
    so the event log and the two prompt files are exactly what a real run
    would leave behind. Only the two proof directories are hand-written
    (see the module docstring for why).
    """
    _write_graph(git_repo, [_node("T-0001")])

    graph = state_engine.load_graph(git_repo)

    # Attempt 1: claim, dispatch, reject.
    graph = state_engine.claim_task(git_repo, graph, "T-0001", actor="engine")
    prompt_path_1 = state_engine.persist_prompt(
        git_repo,
        "run_TEST",
        0,
        PROMPT_ONE,
        task_id="T-0001",
        attempt=1,
        allowed_tools=["Read", "Edit"],
    )
    state_engine.record_turn(git_repo, task_id="T-0001", attempt=1, result=_turn_result())
    _write_proof(
        prompt_path_1, attempt=1, verdict=Verdict.FAIL, failed=GateName.LINT, diff=DIFF_ONE
    )
    graph = state_engine.reject_task(
        git_repo,
        graph,
        "T-0001",
        actor="engine",
        proof_id="proof-A",
        failed_gates=["lint"],
        summary="GATE lint FAILED",
    )

    # Attempt 2: reclaim, dispatch (carrying a credential, for AC-5), verify.
    graph = state_engine.claim_task(git_repo, graph, "T-0001", actor="engine")
    prompt_path_2 = state_engine.persist_prompt(
        git_repo,
        "run_TEST",
        1,
        PROMPT_TWO_TEMPLATE.format(secret=SECRET),
        task_id="T-0001",
        attempt=2,
        allowed_tools=["Read", "Edit"],
    )
    state_engine.record_turn(git_repo, task_id="T-0001", attempt=2, result=_turn_result())
    _write_proof(
        prompt_path_2,
        attempt=2,
        verdict=Verdict.PASS,
        failed=None,
        diff=DIFF_TWO_TEMPLATE.format(secret=SECRET),
    )
    state_engine.mark_verified(git_repo, graph, "T-0001", actor="engine", proof_id="proof-B")

    return git_repo


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root))


# ---------------------------------------------------------------------------
# AC-1: every response is derived from the log, not the raw projection file.
# ---------------------------------------------------------------------------


def test_all_responses_log_derived(repo: Path) -> None:
    """Corrupt the on-disk projection's mutable fields directly, leaving the
    event log untouched. A handler reading `task_graph.json` straight would
    echo the corruption; one that replays the log the way `redgear rebuild`
    does reports the truth regardless.
    """
    graph_path = repo / ".redgear" / "task_graph.json"
    on_disk = json.loads(graph_path.read_text(encoding="utf-8"))
    node = on_disk["nodes"][0]
    assert node["id"] == "T-0001"
    # The real, event-derived truth is state=verified, attempts=1. Corrupt
    # both directly, bypassing every state_engine write path.
    node["state"] = "blocked"
    node["attempts"] = 0
    node["claim"] = None
    graph_path.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")

    response = _client(repo).get("/graph")
    assert response.status_code == 200
    body = response.json()

    entries = {entry["task_id"]: entry for entry in body["state"]}
    assert entries["T-0001"]["state"] == "verified", (
        "the response echoed the corrupted on-disk file instead of replaying the log"
    )
    assert entries["T-0001"]["attempts"] == 1

    # The plan half is untouched by the corruption either way -- it comes
    # from the pristine definition, never from the file's mutable fields.
    plan_nodes = {entry["id"]: entry for entry in body["plan"]["nodes"]}
    assert plan_nodes["T-0001"]["type"] == "test_authoring"
    assert "state" not in plan_nodes["T-0001"], (
        "mutable state leaked into the plan half; §1.4's G4 split must be visible in the response"
    )


# ---------------------------------------------------------------------------
# AC-2: no endpoint mutates state other than the human approval action.
# ---------------------------------------------------------------------------


def test_no_state_mutation_endpoints(tmp_path: Path) -> None:
    """Route enumeration, not a source grep (§9: "read-only plus approval
    endpoints"). Walks the actual route table FastAPI built, which is the
    framework's own ground truth about what each path accepts.
    """
    app = create_app(tmp_path)
    mutating = {"POST", "PUT", "PATCH", "DELETE"}

    offenders = {
        route.path: methods & mutating
        for route in app.routes
        if (methods := getattr(route, "methods", None)) and methods & mutating
    }

    assert offenders, "expected at least one route (the approval endpoint) to be mutating"
    assert set(offenders) == {"/plan/approve"}, (
        f"unexpected mutating route(s), only plan approval may mutate state: {offenders}"
    )
    assert offenders["/plan/approve"] == {"POST"}


# ---------------------------------------------------------------------------
# AC-3: dispatched prompts are retrievable verbatim, per iteration.
# ---------------------------------------------------------------------------


def test_prompts_retrievable_verbatim(repo: Path) -> None:
    response = _client(repo).get("/tasks/T-0001/prompts")
    assert response.status_code == 200
    records = response.json()

    assert len(records) == 2, "one prompt per iteration -- two dispatches happened"
    by_attempt = {record["attempt"]: record for record in records}
    assert by_attempt[1]["prompt"] == PROMPT_ONE
    # No credential is configured in this test's environment, so nothing is
    # redacted and attempt 2 comes back verbatim too, secret and all. The
    # AC-5 test below is what checks redaction actually fires when a
    # credential *is* configured.
    assert by_attempt[2]["prompt"] == PROMPT_TWO_TEMPLATE.format(secret=SECRET)
    assert by_attempt[1]["iteration"] == 0
    assert by_attempt[2]["iteration"] == 1


def test_prompts_unknown_task_is_404(repo: Path) -> None:
    response = _client(repo).get("/tasks/T-9999/prompts")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# AC-4: proof artifacts, including the raw diff, are retrievable per attempt.
# ---------------------------------------------------------------------------


def test_proofs_retrievable_per_attempt(repo: Path) -> None:
    response = _client(repo).get("/tasks/T-0001/proofs")
    assert response.status_code == 200
    records = response.json()

    assert len(records) == 2, "one rejection, one verification"
    by_attempt = {record["attempt"]: record for record in records}

    first = by_attempt[1]
    assert first["outcome"] == "rejected"
    assert first["diff"] == DIFF_ONE
    assert first["verdict"]["verdict"] == "fail"
    failed_names = {g["name"] for g in first["verdict"]["gates"] if g["status"] == "failed"}
    assert failed_names == {"lint"}

    second = by_attempt[2]
    assert second["outcome"] == "verified"
    assert second["verdict"]["verdict"] == "pass"
    assert all(g["status"] == "passed" for g in second["verdict"]["gates"])
    # Not asserted verbatim here -- attempt 2's diff carries the same
    # credential as its prompt, and redaction is what AC-5 checks.


def test_verified_attempt_number_is_not_the_raw_event_field(repo: Path) -> None:
    """Regression guard for the ``mark_verified`` off-by-one documented in the
    module docstring: the raw `task_verified` event's `attempt` field is 0
    for this fixture (one prior rejection, so `node.attempts` was 1 -- no,
    the point is it is *not* 2), while the real attempt being reported on is
    the second. The API must not surface the raw field uncorrected.
    """
    response = _client(repo).get("/tasks/T-0001/proofs")
    records = response.json()
    verified = next(r for r in records if r["outcome"] == "verified")
    assert verified["attempt"] == 2


# ---------------------------------------------------------------------------
# AC-5: no response body contains a credential value.
# ---------------------------------------------------------------------------


def test_responses_redacted(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Short secret, `[REDACTED]` marker asserted present -- not merely that
    the raw secret is absent, which would also pass if redaction never ran.
    """
    monkeypatch.setenv(CREDENTIAL_ENV_VAR, SECRET)

    prompts = _client(repo).get("/tasks/T-0001/prompts")
    assert SECRET not in prompts.text
    assert "[REDACTED]" in prompts.text

    proofs = _client(repo).get("/tasks/T-0001/proofs")
    assert SECRET not in proofs.text
    assert "[REDACTED]" in proofs.text


def test_responses_redacted_when_no_secret_is_configured(repo: Path) -> None:
    """The complementary case: nothing credential-shaped in the environment,
    nothing redacted, and the real prompt text comes through untouched --
    proving the marker in the test above is redaction firing, not the
    response body being scrubbed unconditionally.
    """
    response = _client(repo).get("/tasks/T-0001/prompts")
    assert "[REDACTED]" not in response.text


# ---------------------------------------------------------------------------
# The approval endpoint calls planner.approve_plan directly.
# ---------------------------------------------------------------------------


def test_approve_endpoint_approves_a_draft_plan(git_repo: Path) -> None:
    """Identical recording to `redgear approve` is structural, not asserted:
    the endpoint calls `planner.approve_plan`, the same function the CLI
    command calls, so there is exactly one implementation of approval to
    drift from the CLI's."""
    nodes = [_node("T-0001")]
    redgear = git_repo / ".redgear"
    (redgear / "spec").mkdir(parents=True, exist_ok=True)
    (redgear / "task_graph.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_hash": "sha256:" + "a" * 64,
                "state": "draft",
                "generated_at": FIXED_TS,
                "nodes": nodes,
                "edges": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    response = _client(git_repo).post("/plan/approve", json={"approved_by": "a reviewer"})
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is True
    assert body["approved_by"] == "a reviewer"

    graph = state_engine.load_graph(git_repo)
    assert graph.state == "active"


def test_approve_endpoint_refuses_a_second_approval(repo: Path) -> None:
    """`repo` is already `active` -- approving it again must fail cleanly,
    not silently record a second approval of the same thing."""
    response = _client(repo).post("/plan/approve", json={"approved_by": "someone else"})
    assert response.status_code == 400


def test_approve_endpoint_requires_a_named_approver(git_repo: Path) -> None:
    _write_graph(git_repo, [_node("T-0001")])
    on_disk = json.loads((git_repo / ".redgear" / "task_graph.json").read_text(encoding="utf-8"))
    on_disk["state"] = "draft"
    (git_repo / ".redgear" / "task_graph.json").write_text(json.dumps(on_disk), encoding="utf-8")

    response = _client(git_repo).post("/plan/approve", json={"approved_by": ""})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /graph and /events exist and are readable independent of the above.
# ---------------------------------------------------------------------------


def test_graph_reports_plan_and_state_as_separate_keys(repo: Path) -> None:
    body = _client(repo).get("/graph").json()
    assert set(body) == {"plan", "state"}
    assert isinstance(body["plan"]["nodes"], list)
    assert isinstance(body["state"], list)


def test_events_endpoint_lists_the_log(repo: Path) -> None:
    response = _client(repo).get("/events")
    assert response.status_code == 200
    kinds = [event["event"] for event in response.json()]
    assert kinds.count("task_claimed") == 2
    assert kinds.count("task_rejected") == 1
    assert kinds.count("task_verified") == 1
