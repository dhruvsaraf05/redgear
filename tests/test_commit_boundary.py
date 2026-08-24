"""The regression this whole change exists for: two real dependent tasks.

The first live ``redgear run`` against an actual Claude Code CLI could not get
past a two-task plan. ``T-0001`` (test_authoring) wrote ``tests/test_calc.py``
and verified cleanly. ``T-0002`` (implementation) then failed ``scope_check``
on all three attempts over that same file -- a file it never touched. Nothing
had committed it: redgear never committed (G6 as originally worded), and in an
unattended run there is no human to do it either. So ``T-0002``'s freshly
computed ``base_commit`` was still the *pre-T-0001* commit, and its predecessor's
legitimate, already-verified output showed up in its own diff as an
out-of-scope write. Permanently, whatever its agent did.

**Nothing in the suite caught it**, and the reason is worth keeping: every
existing multi-task test either pre-verifies the ``test_authoring`` node
without ever dispatching it, or gives the two tasks non-overlapping paths that
are never frozen for each other. Neither shape can reproduce the bug. This
file exists to hold the shape that can -- two genuine dispatches in sequence,
where the second task's frozen glob covers what the first one actually wrote.

Kept out of ``test_orchestrator.py`` deliberately: that module is frozen, and
this is a new property rather than a correction to an existing one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from fake_runner import FakeRunner
from fake_runner.scenarios import FileEdit, Scenario
from redgear.orchestrator import run
from redgear.schemas import Budget, HarnessConfig
from redgear.state_engine import load_graph

CRITERION_SELECTOR = "tests/test_calc.py::test_add"

#: T-0001 writes the failing test. Frozen: `src/**`.
AUTHORS_TEST = Scenario(
    name="authors_the_failing_test",
    doc="A correct test_authoring turn: a new file under tests/**, red for the right reason.",
    edits=(
        FileEdit(
            "tests/test_calc.py",
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        ),
    ),
    summary="Added the failing test for add().",
)

#: T-0002 implements it. Frozen: `tests/**` -- which is exactly where T-0001's
#: output lives, so a stale baseline shows up here as an out-of-scope write.
IMPLEMENTS = Scenario(
    name="implements_add",
    doc="A correct implementation turn: only src/**, inheriting T-0001's criteria.",
    edits=(FileEdit("calc/__init__.py", "def add(a, b):\n    return a + b\n"),),
    summary="Implemented add().",
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def _node(
    task_id: str,
    *,
    task_type: str,
    writable: list[str],
    frozen: list[str],
    depends_on: list[str] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    inherits: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "type": task_type,
        "title": f"{task_type} {task_id}",
        "state": "ready" if not depends_on else "blocked",
        "spec_refs": ["FR-7"],
        "spec_hash": "sha256:" + "d" * 64,
        "depends_on": depends_on or [],
        "scope": {
            "writable_globs": writable,
            "creatable_globs": writable,
            "frozen_globs": frozen,
        },
        "acceptance_criteria": criteria or [],
        "inherits_criteria_from": inherits or [],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }


def _two_task_repo(tmp_path: Path) -> Path:
    """A real repo whose two tasks genuinely depend on each other.

    ``calc/`` ships stubbed so the test_authoring phase is red for the right
    reason (a wrong answer, not a missing module) and the implementation
    phase has something to correct.
    """
    root = tmp_path / "target"
    (root / "calc").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "calc" / "__init__.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (root / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent))\n",
        encoding="utf-8",
    )

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "commit.gpgsign", "false")

    nodes = [
        _node(
            "T-0001",
            task_type="test_authoring",
            writable=["tests/**"],
            frozen=["calc/**"],
            criteria=[
                {
                    "id": "AC-1",
                    "statement": "add() returns the sum.",
                    "verified_by": {"kind": "test", "selector": CRITERION_SELECTOR},
                }
            ],
        ),
        _node(
            "T-0002",
            task_type="implementation",
            writable=["calc/**"],
            frozen=["tests/**"],
            depends_on=["T-0001"],
            inherits=["T-0001"],
        ),
    ]
    redgear = root / ".redgear"
    redgear.mkdir()
    (redgear / ".gitignore").write_text("locks/\nSTOP\n", encoding="utf-8")
    (redgear / "task_graph.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_hash": "sha256:" + "d" * 64,
                "state": "active",
                "generated_at": "2026-01-01T00:00:00Z",
                "nodes": nodes,
                "edges": [{"from": "T-0001", "to": "T-0002", "kind": "hard"}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline: stubbed calc.add")
    return root


def _harness() -> HarnessConfig:
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=["calc"],
        coverage_floor=0.0,
        timeout_s=180,
    )


def test_two_dependent_tasks_complete_in_one_unattended_run(tmp_path: Path) -> None:
    """The bug, reproduced end to end and shown fixed.

    Runs the REAL six-gate pipeline -- no injected verifier -- because the
    failure was in what ``scope_check`` diffs against, and a stub cannot
    exhibit it. Two genuine dispatches, in sequence, with the second task's
    frozen glob covering exactly what the first one wrote.
    """
    repo = _two_task_repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD").strip()

    outcome = run(
        repo,
        runner=FakeRunner(AUTHORS_TEST, IMPLEMENTS),
        budget=Budget(max_iterations=6),
        harness=_harness(),
    )

    assert outcome.reason == "complete", (
        f"a two-task plan did not complete unattended: {outcome}. This is the "
        f"exact failure the first real run hit."
    )
    assert outcome.tasks_verified == 2
    assert outcome.tasks_escalated == 0

    nodes = {node.id: node for node in load_graph(repo).nodes}
    assert nodes["T-0001"].state == "verified"
    assert nodes["T-0002"].state == "verified"
    assert nodes["T-0002"].attempts == 0, (
        "T-0002 burned an attempt; it was blamed for its predecessor's output"
    )

    # HEAD moved twice, which is the mechanism: the second task's baseline is
    # its predecessor's commit, not the pre-run one.
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert head != baseline
    subjects = _git(repo, "log", "--format=%s", f"{baseline}..HEAD").split("\n")
    assert [line for line in subjects if line.strip()] == [
        "T-0002: implementation T-0002",
        "T-0001: test_authoring T-0001",
    ]


def test_each_verified_task_is_its_own_commit_and_its_work_is_undoable(tmp_path: Path) -> None:
    """G6 as amended: one commit per verified task, and undoing one task's
    work does not disturb another's.

    **A plain ``git revert`` of a task commit conflicts, and that is correct
    rather than a defect.** Each commit carries the appended event log, and
    every later commit appends to the same file -- so reverting one would
    have to *delete* log lines that later entries were written on top of.
    Section 11.1 rule 5 forbids exactly that: lines are appended, never
    edited, reordered or deleted. The conflict is the audit trail refusing to
    lose history, so the right way to undo a task is to restore its work
    paths and leave the log to record that the task was verified and later
    undone.
    """
    repo = _two_task_repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD").strip()

    run(
        repo,
        runner=FakeRunner(AUTHORS_TEST, IMPLEMENTS),
        budget=Budget(max_iterations=6),
        harness=_harness(),
    )

    shas = [
        line.strip()
        for line in _git(repo, "rev-list", f"{baseline}..HEAD").splitlines()
        if line.strip()
    ]
    assert len(shas) == 2, f"expected exactly one commit per verified task, got {shas}"

    # Each commit is one task's work and only that task's work, so restoring
    # T-0002's writable scope from its parent undoes exactly T-0002.
    _git(repo, "checkout", f"{shas[0]}^", "--", "calc/")
    assert (repo / "calc" / "__init__.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return 0\n"
    )
    assert (repo / "tests" / "test_calc.py").exists(), "undoing T-0002 disturbed T-0001's work"

    # And the log still records both tasks. Undoing work is not unwriting
    # history: the record that T-0002 was verified remains true, because it
    # was.
    log = (repo / ".redgear" / "events.jsonl").read_text(encoding="utf-8")
    assert log.count('"task_verified"') == 2
    assert log.count('"task_committed"') == 2


def test_the_commit_carries_the_proof_that_justifies_it(tmp_path: Path) -> None:
    """A commit containing the work but not the evidence for it is the
    split-brain this project exists to prevent."""
    repo = _two_task_repo(tmp_path)

    run(
        repo,
        runner=FakeRunner(AUTHORS_TEST, IMPLEMENTS),
        budget=Budget(max_iterations=6),
        harness=_harness(),
    )

    tracked = _git(repo, "ls-files").splitlines()
    assert ".redgear/events.jsonl" in tracked
    assert any(path.endswith("proof/verdict.json") for path in tracked), (
        "no proof artifact was committed alongside the work it justifies"
    )
    assert any(path.endswith("prompt.txt") for path in tracked)

    # The two transient control files are never committed.
    assert not any(path.startswith(".redgear/locks/") for path in tracked)
    assert ".redgear/STOP" not in tracked

    # And the commit message points at a proof directory that actually exists.
    body = _git(repo, "log", "-1", "--format=%B")
    proof_line = next(line for line in body.splitlines() if line.startswith("proof: "))
    assert (repo / proof_line.removeprefix("proof: ").strip()).is_dir()
