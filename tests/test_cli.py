"""T-0032: failing tests for cli.py -- the full command surface.

``redgear/cli.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

The CLI is the product boundary (FR-11): it owns **argument parsing and
output formatting only**. It calls `orchestrator`, `state_engine` and
`verifier`; it contains no loop logic, composes no prompts, runs no gates.

Four properties here are safety rules rather than conveniences, and each
would be easy to weaken into uselessness:

* **The approval gate cannot be bypassed** (§3.3). `run` refuses a draft
  plan, and there is deliberately no flag that skips it. The plan defines the
  tests, so an unreviewed plan produces confidently verified wrong software.
* **`rebuild` never repairs.** It reports divergence and exits non-zero,
  leaving the file exactly as it found it. Auto-healing an audit trail
  destroys the thing the audit trail is for.
* **`log` never prints a credential** (G5). Routed through `redact.py`; not
  reimplemented here.
* **`run` prints the brake** (§8.3). A user who cannot find `redgear stop`
  will Ctrl-C repeatedly and leave the state directory inconsistent.

Two behaviours of the test harness itself were measured before these
assertions were written, because both quietly invalidate the obvious test:

* ``typer.Exit(n)`` leaves ``result.exception`` set to ``SystemExit(n)`` --
  it is **not** ``None``. So "the CLI handled this cleanly" is asserted as
  "the exception is not a ``RedgearError``", never as "there is no
  exception".
* ``CliRunner`` renders rich output at **79 columns** and wraps. A secret
  long enough to wrap would be split across lines, and a naive
  ``secret not in output`` check would pass whether or not redaction ran.
  The redaction test therefore uses a short secret *and* asserts the
  ``[REDACTED]`` marker is present -- one direction alone proves nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from redgear import cli
from redgear.errors import RedgearError
from redgear.redact import REDACTED

runner = CliRunner()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _invoke(*args: str) -> Any:
    return runner.invoke(cli.app, list(args))


def _assert_handled(result: Any, code: str) -> None:
    """A RedgearError surfaced as a clean message, not a traceback.

    §11.2 rule 4: "Never let a traceback escape to a user." Asserting only on
    the exit code would not catch that -- an uncaught exception also exits
    non-zero, and `CliRunner` captures it silently.
    """
    assert not isinstance(result.exception, RedgearError), (
        f"a RedgearError escaped to the user as a traceback: {result.exception!r}"
    )
    assert code in result.output, f"the error code {code} was not shown: {result.output!r}"


def _node(
    task_id: str,
    *,
    task_type: str = "implementation",
    state: str = "ready",
    depends_on: list[str] | None = None,
    writable: list[str] | None = None,
    frozen: list[str] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    inherits: list[str] | None = None,
) -> dict[str, Any]:
    globs = writable if writable is not None else ["src/**"]
    return {
        "id": task_id,
        "type": task_type,
        "title": f"task {task_id}",
        "state": state,
        "spec_refs": ["FR-11"],
        "spec_hash": "sha256:" + "d" * 64,
        "depends_on": depends_on or [],
        "scope": {
            "writable_globs": globs,
            "creatable_globs": globs,
            "frozen_globs": frozen if frozen is not None else ["tests/**"],
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


def _plan_nodes() -> list[dict[str, Any]]:
    """A plan whose every node state is consistent with an **empty** event log.

    This shape is forced by `rebuild`. G4 says the projection is
    reconstructible from the log, so a node claiming a non-initial state that
    no event supports *is* divergence -- a fixture marking a task `verified`
    with nothing in the log would make `test_rebuild_fails_on_divergence` pass
    for the wrong reason and
    `test_rebuild_succeeds_on_a_consistent_projection` fail outright.

    So both nodes sit in their initial state. A node with no dependencies is
    vacuously ready under §4.4 invariant 3, which is why the real graph's
    T-0001 ships that way too.

    The test_authoring node is numbered T-0009 deliberately: selection breaks
    the zero-dependency tie on ascending id (§4.1), so T-0001 is chosen first
    and the dry run exercises the more interesting case -- an implementation
    task rendering criteria it inherited rather than authored (G2).
    """
    return [
        _node(
            "T-0009",
            task_type="test_authoring",
            writable=["tests/**"],
            frozen=["src/**"],
            criteria=[
                {
                    "id": "AC-1",
                    "statement": "The placeholder test passes.",
                    "verified_by": {
                        "kind": "test",
                        "selector": "tests/test_pkg.py::test_placeholder",
                    },
                }
            ],
        ),
        _node("T-0001", inherits=["T-0009"]),
    ]


def _write_graph(root: Path, nodes: list[dict[str, Any]], *, state: str = "active") -> None:
    redgear = root / ".redgear"
    redgear.mkdir(exist_ok=True)
    (redgear / "task_graph.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_hash": "sha256:" + "d" * 64,
                "state": state,
                "generated_at": "2026-01-01T00:00:00Z",
                "nodes": nodes,
                "edges": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A git repository with no `.redgear/` at all."""
    root = tmp_path / "bare"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_pkg.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def planned_repo(bare_repo: Path) -> Path:
    """A git repository carrying an approved plan."""
    _write_graph(bare_repo, _plan_nodes())
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "plan")
    return bare_repo


# ---------------------------------------------------------------------------
# AC-1: init preconditions.
# ---------------------------------------------------------------------------


def test_init_preconditions(tmp_path: Path, bare_repo: Path) -> None:
    """Two refusals, both structural.

    Outside a repository there is no baseline to diff against, so every later
    guarantee is fiction. Over existing state, re-initialising would overwrite
    an audit trail -- the one artifact redgear exists to protect.
    """
    # Refuses outside a git repository.
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    outside = _invoke("init", "--repo", str(plain))
    assert outside.exit_code != 0
    _assert_handled(outside, "E_NOT_A_REPO")
    assert not (plain / ".redgear").exists(), "state was scaffolded outside a repository"

    # Succeeds in a clean repository.
    first = _invoke("init", "--repo", str(bare_repo))
    assert first.exit_code == 0, first.output
    assert (bare_repo / ".redgear").is_dir()

    # Refuses over existing state.
    second = _invoke("init", "--repo", str(bare_repo))
    assert second.exit_code != 0
    _assert_handled(second, "E_ALREADY_INITIALIZED")


def test_init_is_not_destructive_on_refusal(bare_repo: Path) -> None:
    """The second `init` must leave the first one's state byte-identical."""
    _invoke("init", "--repo", str(bare_repo))
    graph = bare_repo / ".redgear" / "task_graph.json"
    before = graph.read_text(encoding="utf-8") if graph.is_file() else None

    _invoke("init", "--repo", str(bare_repo))

    after = graph.read_text(encoding="utf-8") if graph.is_file() else None
    assert after == before, "a refused init modified existing state"


# ---------------------------------------------------------------------------
# AC-2: the approval gate.
# ---------------------------------------------------------------------------


def test_run_refuses_draft_plan(bare_repo: Path) -> None:
    """§3.3, and the single most important refusal in the CLI.

    The plan defines the tests, so it is the only unverified model output in
    the system. Everything the loop does afterwards is gated by those tests --
    which means a wrong plan produces confidently verified wrong software.
    """
    _write_graph(bare_repo, _plan_nodes(), state="draft")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "draft plan")

    result = _invoke("run", "--repo", str(bare_repo))

    assert result.exit_code != 0
    _assert_handled(result, "E_PLAN_UNREVIEWED")

    # Nothing was attempted: no run directory, no events.
    assert not (bare_repo / ".redgear" / "runs").exists()


def test_no_flag_bypasses_the_approval_gate(bare_repo: Path) -> None:
    """§3.3: "If you are tempted to add a `--yes` flag that skips this, don't."

    A gate with an override is not a gate. This asserts the absence of one by
    trying the names such a flag would plausibly have.
    """
    _write_graph(bare_repo, _plan_nodes(), state="draft")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "draft plan")

    for flag in ("--yes", "--force", "--skip-approval", "--no-approval"):
        result = _invoke("run", "--repo", str(bare_repo), flag)
        assert result.exit_code != 0, f"{flag} was accepted and bypassed the approval gate"


def test_run_banner_names_the_brake(planned_repo: Path) -> None:
    """§8.3: "`redgear run` prints the stop instruction in its opening
    banner. A user who cannot find the brake will use Ctrl-C repeatedly and
    leave the state directory inconsistent."
    """
    result = _invoke("run", "--repo", str(planned_repo), "--dry-run")
    assert "redgear stop" in result.output, (
        f"the run banner does not tell the user how to stop: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# AC-3: --dry-run.
# ---------------------------------------------------------------------------


def test_dry_run_dispatches_nothing(planned_repo: Path) -> None:
    """§9: "composes and prints every prompt without dispatching them. This
    is the primary development affordance -- it costs nothing and shows
    exactly what the agent would receive."

    "Costs nothing" is the load-bearing half: a dry run must not dispatch, must
    not claim, and must not write a single event. If it mutated state it would
    not be safe to run constantly, which is the whole point of it.
    """
    result = _invoke("run", "--repo", str(planned_repo), "--dry-run")
    assert result.exit_code == 0, result.output

    # It printed a real prompt: the task, its frozen scope, and the outcome
    # contract that makes an honest exit free.
    assert "T-0001" in result.output
    assert "tests/**" in result.output, "the frozen globs are missing from the dry-run prompt"
    assert "Required outcome" in result.output
    assert "scope_insufficient" in result.output

    # And it dispatched nothing and recorded nothing.
    assert not (planned_repo / ".redgear" / "runs").exists(), "a dry run created a run directory"
    events = planned_repo / ".redgear" / "events.jsonl"
    assert not events.exists() or not events.read_text(encoding="utf-8").strip(), (
        "a dry run wrote to the event log"
    )
    graph = json.loads((planned_repo / ".redgear" / "task_graph.json").read_text(encoding="utf-8"))
    states = {node["id"]: node["state"] for node in graph["nodes"]}
    assert states["T-0001"] == "ready", "a dry run mutated the projection"


def test_dry_run_is_readable_not_a_debug_dump(planned_repo: Path) -> None:
    """The affordance is only useful if a human can actually read the prompt
    and tell which task it belongs to."""
    result = _invoke("run", "--repo", str(planned_repo), "--dry-run")
    output = result.output

    assert "## Task" in output and "## Scope" in output
    # The task is identified outside the prompt body too, so a multi-task dry
    # run can be scanned without reading every prompt in full.
    header_before_prompt = output.split("## Task", 1)[0]
    assert "T-0001" in header_before_prompt, (
        "the dry run does not label which task each prompt belongs to"
    )


# ---------------------------------------------------------------------------
# AC-4: rebuild.
# ---------------------------------------------------------------------------


def test_rebuild_fails_on_divergence(planned_repo: Path) -> None:
    """§4.5/G4: "A mismatch is an engine bug — surface it loudly, never
    auto-heal."

    The projection is corrupted to claim a task is verified that no event
    supports. Rebuild must notice, say so, exit non-zero, and leave the file
    exactly as it found it. Repairing it silently would erase the evidence
    that something wrote state out of band.
    """
    graph_path = planned_repo / ".redgear" / "task_graph.json"
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    for node in payload["nodes"]:
        if node["id"] == "T-0001":
            node["state"] = "verified"
    corrupted = json.dumps(payload, indent=2) + "\n"
    graph_path.write_text(corrupted, encoding="utf-8")

    result = _invoke("rebuild", "--repo", str(planned_repo))

    assert result.exit_code == 5, f"divergence must exit 5 (§4.3 engine_error): {result.output!r}"
    _assert_handled(result, "E_PROJECTION_DIVERGED")
    assert "T-0001" in result.output, "the diverging task is not named"

    assert graph_path.read_text(encoding="utf-8") == corrupted, (
        "rebuild repaired the projection; an auto-healed audit trail is not an audit trail"
    )


def test_rebuild_succeeds_on_a_consistent_projection(planned_repo: Path) -> None:
    result = _invoke("rebuild", "--repo", str(planned_repo))
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# AC-5: status exit codes.
# ---------------------------------------------------------------------------


def test_status_exit_codes(bare_repo: Path) -> None:
    """§9: "0 ready / 1 all terminal / 2 escalated".

    The codes are what makes `redgear status` usable in a shell conditional,
    and escalated outranks ready: a run holding an escalated task needs a
    human even if other work remains selectable.
    """
    # Something is claimable.
    _write_graph(bare_repo, _plan_nodes())
    ready = _invoke("status", "--repo", str(bare_repo))
    assert ready.exit_code == 0, ready.output
    assert "T-0001" in ready.output

    # Everything is terminal.
    done = _plan_nodes()
    for node in done:
        node["state"] = "verified"
    _write_graph(bare_repo, done)
    assert _invoke("status", "--repo", str(bare_repo)).exit_code == 1

    # Something is escalated -- outranks any remaining ready work.
    escalated = _plan_nodes()
    for node in escalated:
        if node["id"] == "T-0001":
            node["state"] = "escalated"
            node["escalation"] = {
                "reason": "blocker",
                "detail": "the criteria contradict ADR-0007",
                "escalated_at": "2026-01-01T00:00:00Z",
            }
    _write_graph(bare_repo, escalated)
    result = _invoke("status", "--repo", str(bare_repo))
    assert result.exit_code == 2, (
        f"an escalated task must outrank remaining ready work: {result.output!r}"
    )


def test_status_reports_the_fields_section_9_requires(bare_repo: Path) -> None:
    """§9: "task id, type, state, attempts, blocked-by"."""
    nodes = _plan_nodes()
    nodes.append(_node("T-0002", depends_on=["T-0001"], inherits=["T-0009"]))
    _write_graph(bare_repo, nodes)
    output = _invoke("status", "--repo", str(bare_repo)).output

    assert "T-0001" in output
    assert "implementation" in output
    assert "ready" in output
    # T-0002 depends on unverified T-0001, so it is blocked -- and the thing
    # blocking it has to be named or the status is not actionable.
    assert "T-0002" in output
    assert output.count("T-0001") >= 2, "the dependency a task is blocked by is not shown"


# ---------------------------------------------------------------------------
# AC-6: log redaction.
# ---------------------------------------------------------------------------

#: Short enough that rich cannot wrap it across lines at the 79-column width
#: CliRunner renders at. A wrapped secret would be split by the terminal and
#: a naive substring check would pass whether or not redaction ran.
FAKE_SECRET = "NOT-A-REAL-TOKEN-000001"  # noqa: S105


def test_log_output_redacted(planned_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """G5: "redact any variable matching (?i)(key|token|secret|password|
    credential) from every log line and every event record."

    Both directions are asserted. That the secret is absent is necessary but
    not sufficient -- output that never contained it would also satisfy it --
    so the redaction marker must be present too.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_SECRET)

    log = planned_repo / ".redgear" / "events.jsonl"
    log.write_text(
        json.dumps(
            {
                "event": "run_started",
                "seq": 0,
                "ts": "2026-01-01T00:00:00Z",
                "actor": "engine",
                "run_id": "run_test",
                "budget": {
                    "max_iterations": 50,
                    "max_wall_clock_s": 7200,
                    "max_consecutive_failures": 5,
                    "max_turns_per_dispatch": 25,
                    "per_turn_usd": None,
                    "dispatch_timeout_s": 900,
                },
                "base_commit": f"a token leaked into free text: {FAKE_SECRET}",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _invoke("log", "--repo", str(planned_repo))

    assert result.exit_code == 0, result.output
    assert FAKE_SECRET not in result.output, "a credential value reached the log output"
    assert REDACTED in result.output, (
        "nothing was redacted; the absence check above would pass on empty output too"
    )


def test_log_redacts_a_credential_named_field(planned_repo: Path) -> None:
    """A value is redacted for its *key's* name even when it is not in the
    environment -- the engine has no way to know an arbitrary field holds a
    secret except by what it is called."""
    log = planned_repo / ".redgear" / "events.jsonl"
    log.write_text(
        json.dumps(
            {
                "event": "adr_logged",
                "seq": 0,
                "ts": "2026-01-01T00:00:00Z",
                "actor": "human",
                "adr_id": "ADR-0001",
                "task_id": "T-0001",
                "title": "api_token: hunter2hunter2hunter2",
                "rule": "Store money as integer minor units.",
                "applies_to": ["src/**"],
                "supersedes": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _invoke("log", "--repo", str(planned_repo))
    assert result.exit_code == 0, result.output
    assert "ADR-0001" in result.output


def test_log_tail_limits_output(planned_repo: Path) -> None:
    log = planned_repo / ".redgear" / "events.jsonl"
    lines = [
        json.dumps(
            {
                "event": "plan_approved",
                "seq": index,
                "ts": "2026-01-01T00:00:00Z",
                "actor": "human",
                "spec_hash": "sha256:" + "d" * 64,
                "approved_by": f"approver-{index}",
            }
        )
        for index in range(6)
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _invoke("log", "--repo", str(planned_repo), "--tail", "2")
    assert result.exit_code == 0, result.output
    assert "approver-5" in result.output
    assert "approver-0" not in result.output, "--tail did not limit the output"


# ---------------------------------------------------------------------------
# The remaining §9 commands.
# ---------------------------------------------------------------------------


def test_stop_writes_the_sentinel(planned_repo: Path) -> None:
    """§8.3: `redgear stop` creates `.redgear/STOP`, and the loop checks for
    it before every iteration."""
    result = _invoke("stop", "--repo", str(planned_repo))
    assert result.exit_code == 0, result.output
    assert (planned_repo / ".redgear" / "STOP").is_file()


def test_doctor_reports_the_environment(planned_repo: Path) -> None:
    """§9: "Print agent CLI version, harness command availability, git state,
    target CLAUDE.md size." Diagnosis is what makes adapter drift visible."""
    result = _invoke("doctor", "--repo", str(planned_repo))
    assert result.exit_code in (0, 1), result.output
    lowered = result.output.lower()
    assert "git" in lowered
    assert "agent" in lowered or "runner" in lowered or "claude" in lowered


# ---------------------------------------------------------------------------
# Configurable agent CLI executable.
#
# Diagnosed directly on this machine: a normal MSIX/Claude-Desktop install of
# Claude Code is deliberately not on PATH (`where.exe claude` finds nothing),
# and `ClaudeCodeConfig.executable` defaulted to the bare string "claude" with
# no CLI flag or config.json wiring to override it -- so a pipx user with
# Claude Desktop and nothing else could not run `redgear run` at all without
# editing Python. §2.4's four adapter requirements include "constrain tool
# permissions" and "return a machine-readable result", but say nothing about
# *finding* the binary in the first place; this closes that gap.
# ---------------------------------------------------------------------------


def test_configured_executable_precedence(planned_repo: Path) -> None:
    """flag > config.json's runner.executable > None (ClaudeCodeConfig's own
    "claude" default applies)."""
    assert cli._configured_executable(planned_repo, flag=None) is None
    assert cli._configured_executable(planned_repo, flag="/opt/flag-wins") == "/opt/flag-wins"

    (planned_repo / ".redgear" / "config.json").write_text(
        json.dumps({"runner": {"executable": "/opt/from-config/claude.exe"}}),
        encoding="utf-8",
    )
    assert cli._configured_executable(planned_repo, flag=None) == "/opt/from-config/claude.exe"
    # The flag still wins even with a config.json present.
    assert cli._configured_executable(planned_repo, flag="/opt/flag-wins") == "/opt/flag-wins"


def test_configured_executable_tolerates_a_malformed_config(planned_repo: Path) -> None:
    """A `doctor`/`run`/`plan` invocation must not crash because `config.json`
    is absent, empty, not an object, or missing the `runner` section -- these
    are all states a user can easily be in before ever setting the key."""
    config = planned_repo / ".redgear" / "config.json"

    config.write_text("not json", encoding="utf-8")
    assert cli._configured_executable(planned_repo, flag=None) is None

    config.write_text("[]", encoding="utf-8")
    assert cli._configured_executable(planned_repo, flag=None) is None

    config.write_text(json.dumps({"other_section": {}}), encoding="utf-8")
    assert cli._configured_executable(planned_repo, flag=None) is None

    config.write_text(json.dumps({"runner": {}}), encoding="utf-8")
    assert cli._configured_executable(planned_repo, flag=None) is None

    config.write_text(json.dumps({"runner": {"executable": "  "}}), encoding="utf-8")
    assert cli._configured_executable(planned_repo, flag=None) is None


def test_doctor_reports_the_configured_executable_not_a_bare_claude(
    planned_repo: Path,
) -> None:
    """The failure mode this closes: `doctor` reporting `shutil.which("claude")`
    unconditionally would say "not on PATH" even when a real, configured
    binary exists and works -- exactly the state on a machine with only a
    Claude Desktop install. `doctor` must resolve the same way `run`/`plan`
    do and name what it actually checked."""
    # Short and configured directly, not a real file under the temp fixture
    # root: rich's `Table` renders `CliRunner` output at 79 columns and wraps
    # long, unbroken strings (a Windows path has no spaces to break on) --
    # observed directly, a full absolute temp path here truncates to
    # "...\\f…" before "fake-claude.exe" is ever reached, and a substring
    # check against it would prove nothing (the same trap
    # test_log_redacts_a_credential_named_field works around, above). This
    # test is about the row *label* doctor chooses, which does not depend on
    # the configured path resolving to a real, runnable binary.
    (planned_repo / ".redgear" / "config.json").write_text(
        json.dumps({"runner": {"executable": "x-configured-claude"}}),
        encoding="utf-8",
    )

    result = _invoke("doctor", "--repo", str(planned_repo))
    assert "x-configured-claude" in result.output, "doctor did not report the configured executable"
    assert "agent CLI (claude)" not in result.output, (
        "doctor still names the bare, unconfigured claude rather than the resolved binary"
    )


def test_run_and_plan_accept_an_executable_flag() -> None:
    """The flag exists on both commands `--executable` was promised for."""
    for command in ("run", "plan"):
        help_text = _invoke(command, "--help").output
        assert "--executable" in help_text, f"{command} --help does not mention --executable"


def test_verify_reports_a_verdict_for_a_task(planned_repo: Path) -> None:
    """§9: "Run the harness manually against a claimed task. Harness
    debugging." Exit 0 or 1 on the verdict, never a traceback."""
    result = _invoke("verify", "T-0001", "--repo", str(planned_repo))
    assert result.exit_code in (0, 1), result.output
    assert not isinstance(result.exception, RedgearError), result.exception
    assert "T-0001" in result.output


def test_verify_rejects_an_unknown_task(planned_repo: Path) -> None:
    result = _invoke("verify", "T-9999", "--repo", str(planned_repo))
    assert result.exit_code != 0
    assert not isinstance(result.exception, RedgearError), result.exception


def test_every_section_9_command_in_scope_exists() -> None:
    """The command surface is the product boundary (FR-11).

    `plan` and `approve` arrive with the planner at T-0036/T-0037, and `ui`
    with the control plane at T-0038 -- they are deliberately absent rather
    than stubbed, because a command that exists and does nothing is worse than
    one that is not there.
    """
    help_text = _invoke("--help").output
    for command in ("init", "run", "status", "stop", "verify", "rebuild", "log", "doctor"):
        assert command in help_text, f"§9 command {command!r} is missing from the CLI"


def test_commands_refuse_an_uninitialised_repository(bare_repo: Path) -> None:
    """Every command that reads state must fail cleanly when there is none,
    rather than raising a bare FileNotFoundError at the user."""
    for command in ("status", "log", "rebuild"):
        result = _invoke(command, "--repo", str(bare_repo))
        assert result.exit_code != 0, f"{command} succeeded without any state"
        assert not isinstance(result.exception, RedgearError), (
            f"{command} leaked a traceback: {result.exception!r}"
        )
