"""The harness comes from `config.json`, and its coverage source is checked.

Section 7.3 has always said harness commands come from configuration only.
Nothing in the CLI read any: ``_default_harness`` was applied unconditionally
with ``coverage_source=["src"]`` hardcoded, so a target repository could not
configure its own commands at all, and any project not laid out as ``src/``
silently produced no coverage data.

**That is not a cosmetic gap.** It was found by pre-flighting a real run: the
gate reports ``harness_error: no coverage data was produced by the harness``,
which fails every implementation task for a reason that has nothing to do with
the agent's work -- after paying for the three dispatches that got there. The
whole-run test at the bottom of this file is the case that would have cost
that money.

``test_commit_boundary`` is imported by bare name rather than as
``tests.test_commit_boundary``: ``tests/`` has no ``__init__.py``, so pytest
puts the directory itself on ``sys.path`` -- the same reason ``fake_runner``
resolves as a top-level package here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fake_runner import FakeRunner
from test_commit_boundary import AUTHORS_TEST, IMPLEMENTS, _git, _two_task_repo
from redgear.cli import _guess_coverage_source, _load_harness, _starter_config
from redgear.errors import UnsafeHarnessCommandError
from redgear.orchestrator import run
from redgear.schemas import Budget
from redgear.state_engine import load_graph, write_default_config


def _write_config(root: Path, payload: dict[str, object]) -> None:
    (root / ".redgear").mkdir(exist_ok=True)
    (root / ".redgear" / "config.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# The loader reads configuration -- the thing section 7.3 always specified.
# ---------------------------------------------------------------------------


def test_commands_come_from_config_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "mypkg").mkdir(parents=True)
    (repo / "mypkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    _write_config(
        repo,
        {
            "commands": {
                "lint_cmd": ["my-linter", "--json"],
                "test_cmd": ["my-runner"],
                "coverage_cmd": ["my-coverage"],
                "coverage_source": ["mypkg"],
            },
            "gates": {"coverage_floor": 0.5, "timeout_s": 42},
        },
    )

    harness = _load_harness(repo)

    assert harness.lint_cmd == ["my-linter", "--json"]
    assert harness.test_cmd == ["my-runner"]
    assert harness.coverage_cmd == ["my-coverage"]
    assert harness.coverage_source == ["mypkg"]
    assert harness.coverage_floor == 0.5
    assert harness.timeout_s == 42


def test_absent_config_falls_back_to_the_default(tmp_path: Path) -> None:
    """A repo laid out as `src/` still works with no configuration at all --
    the fallback is not removed, only validated."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)

    harness = _load_harness(repo)

    assert harness.coverage_source == ["src"]
    assert harness.test_cmd[0] == sys.executable


@pytest.mark.parametrize(
    "content",
    ["not json at all", "[]", '{"commands": "not an object"}', '{"commands": {"lint_cmd": 7}}'],
)
def test_a_malformed_config_never_crashes(tmp_path: Path, content: str) -> None:
    """A user setting this up for the first time passes through every broken
    state, and a traceback teaches them nothing (§11.2 rule 4)."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".redgear").mkdir()
    (repo / ".redgear" / "config.json").write_text(content, encoding="utf-8")

    harness = _load_harness(repo)
    assert harness.coverage_source == ["src"]


# ---------------------------------------------------------------------------
# The validation: a source that does not exist is loud, never silent.
# ---------------------------------------------------------------------------


def test_a_missing_coverage_source_fails_loudly(tmp_path: Path) -> None:
    """The actual defect. Before this, `coverage_source=["src"]` against a
    repo with no `src/` collected nothing and the gate failed every
    implementation task with `harness_error`, three paid dispatches deep."""
    repo = tmp_path / "repo"
    (repo / "calc").mkdir(parents=True)

    with pytest.raises(UnsafeHarnessCommandError) as excinfo:
        _load_harness(repo)

    assert excinfo.value.code == "E_HARNESS_ERROR"
    assert "src" in str(excinfo.value.detail["missing"])
    assert "coverage_source" in str(excinfo.value.detail["fix"])


def test_a_configured_but_missing_source_also_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "calc").mkdir(parents=True)
    _write_config(repo, {"commands": {"coverage_source": ["nope"]}})

    with pytest.raises(UnsafeHarnessCommandError) as excinfo:
        _load_harness(repo)
    assert "nope" in str(excinfo.value.detail["missing"])


# ---------------------------------------------------------------------------
# `redgear init` seeds a real config, so nobody meets the error above.
# ---------------------------------------------------------------------------


def test_init_config_names_a_real_coverage_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "calc").mkdir(parents=True)
    (repo / "calc" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".redgear").mkdir()

    write_default_config(repo, _starter_config(repo))
    harness = _load_harness(repo)

    assert harness.coverage_source == ["calc"], "init did not seed a usable coverage source"


def test_guessing_prefers_convention_then_a_single_package(tmp_path: Path) -> None:
    conventional = tmp_path / "a"
    (conventional / "src").mkdir(parents=True)
    (conventional / "other").mkdir()
    (conventional / "other" / "m.py").write_text("", encoding="utf-8")
    assert _guess_coverage_source(conventional) == "src"

    single = tmp_path / "b"
    (single / "calc").mkdir(parents=True)
    (single / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (single / "tests").mkdir()
    (single / "tests" / "test_x.py").write_text("", encoding="utf-8")
    assert _guess_coverage_source(single) == "calc"


def test_ambiguity_guesses_nothing(tmp_path: Path) -> None:
    """A wrong guess written into config is worse than an empty field the
    user is told to fill, because the wrong one runs."""
    repo = tmp_path / "repo"
    for name in ("alpha", "beta"):
        (repo / name).mkdir(parents=True)
        (repo / name / "__init__.py").write_text("", encoding="utf-8")

    assert _guess_coverage_source(repo) is None
    assert _starter_config(repo)["commands"]["coverage_source"] == []  # type: ignore[index]


def test_write_default_config_never_clobbers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".redgear").mkdir(parents=True)
    (repo / ".redgear" / "config.json").write_text('{"mine": true}', encoding="utf-8")

    write_default_config(repo, {"runner": {"executable": "claude"}})

    assert json.loads((repo / ".redgear" / "config.json").read_text(encoding="utf-8")) == {
        "mine": True
    }


# ---------------------------------------------------------------------------
# The case that would have cost three paid dispatches.
# ---------------------------------------------------------------------------


def test_a_non_src_repo_completes_a_full_two_task_run(tmp_path: Path) -> None:
    """A project laid out as `calc/` rather than `src/` runs to completion,
    with the harness taken from its own `config.json`.

    This is the pre-flight case: with the old hardcoded `coverage_source`,
    T-0002 failed `coverage_delta` with `harness_error` on all three attempts
    and escalated. Nothing about the agent's work was wrong.
    """
    repo = _two_task_repo(tmp_path)
    _write_config(
        repo,
        {
            "commands": {
                "lint_cmd": [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--output-format=json",
                    "--no-cache",
                    ".",
                ],
                "test_cmd": [sys.executable, "-m", "pytest"],
                "coverage_cmd": [sys.executable, "-m", "coverage"],
                "coverage_source": ["calc"],
            },
            "gates": {"coverage_floor": 0.0, "timeout_s": 180},
        },
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "config"], cwd=repo, check=True, capture_output=True)

    outcome = run(
        repo,
        runner=FakeRunner(AUTHORS_TEST, IMPLEMENTS),
        budget=Budget(max_iterations=6),
        harness=_load_harness(repo),
    )

    assert outcome.reason == "complete", (
        f"a non-src project could not complete: {outcome}. With the hardcoded "
        f"coverage_source this escalated on coverage_delta/harness_error."
    )
    assert outcome.tasks_verified == 2
    assert outcome.tasks_escalated == 0

    nodes = {node.id: node for node in load_graph(repo).nodes}
    assert nodes["T-0002"].state == "verified"
    assert nodes["T-0002"].attempts == 0, "T-0002 burned an attempt on a harness misconfiguration"

    # And coverage_delta actually ran rather than being skipped or erroring.
    body = _git(repo, "log", "-1", "--format=%B")
    gates_line = next(line for line in body.splitlines() if line.startswith("gates: "))
    assert "coverage_delta" in gates_line
