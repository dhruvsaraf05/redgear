"""The agent-CLI seam.

``runner.py`` is the only module permitted to spawn an agent CLI (section 2.2).
This file defines the *shape* of that capability; the Claude Code adapter
arrives at T-0034 and the deterministic fake lives in ``tests/fake_runner/``.

**Why a protocol rather than a base class.** NFR-8 requires that agent-CLI
specifics stay confined to a single adapter behind a stable interface, and
section 2.4 forbids branching on agent identity anywhere outside this module.
A ``Protocol`` gets that without inheritance: an adapter conforms by having the
right methods, so it never has to import redgear to satisfy the seam, and
``orchestrator.py`` can be typed against ``Runner`` while importing no concrete
implementation at all.

This is also the one place section 11.3's ban on speculative abstraction is
deliberately not applied, and the justification is specific: the second
implementation already exists. The fake runner ships on day one and is the
primary test harness for every milestone after this one, so the seam is being
drawn between two real callers rather than in anticipation of one.

**What an adapter must provide** (section 2.4). A CLI that cannot do all four
is not supportable, and the right response is to say so rather than to
half-support it:

1. send a single prompt non-interactively,
2. constrain tool permissions,
3. cap the number of turns,
4. return a machine-readable result.

Those map onto ``dispatch``'s parameters one for one. Note what is absent:
there is no way to ask a runner what it did, no callback, and no streaming
handle. A turn is one prompt in, one result out, process exits (section 1.2),
and the agent CLI is a stateless worker holding no memory between turns.

**The result is a claim, not a verdict** (G1). ``TurnResult.changed_files`` and
``outcome`` are what the agent *says*. Every one of them is cross-checked
against real git state by ``verifier.py`` after the process has exited. No
field an adapter populates may influence a gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import Field, ValidationError
from redgear.budget import terminate_process_tree
from redgear.errors import JsonValue, RunnerError
from redgear.schemas import (
    AgentTurnReport,
    BlockerCategory,
    Strict,
    TaskNode,
    TurnOutcome,
    TurnResult,
    agent_report_schema,
)


@runtime_checkable
class Runner(Protocol):
    """One agent CLI, reduced to what the orchestrator needs of it.

    ``@runtime_checkable`` so a test can assert conformance with
    ``isinstance``. Be aware of what that buys: it checks method *names* only,
    never signatures. Signature conformance is a static property, enforced by
    ``mypy --strict`` at every call site and asserted explicitly in
    ``tests/test_fake_runner.py``.
    """

    def dispatch(
        self,
        prompt: str,
        allowed_tools: list[str],
        cwd: Path,
        timeout_s: int,
        max_turns: int,
    ) -> TurnResult:
        """Run one turn: one prompt in, one result out, process exits.

        :param prompt: The complete composed prompt. Passed as a single argv
            element, never interpolated into a shell string (section 6.5) --
            a prompt containing backticks must not be able to execute
            anything.
        :param allowed_tools: The permission allowlist derived from the task's
            scope. Never a bare ``Bash`` and never
            ``--dangerously-skip-permissions`` (section 8.2).
        :param cwd: The target repository root, resolved and asserted to be
            inside the repository.
        :param timeout_s: Wall-clock ceiling. On expiry the adapter terminates
            the process tree -- not just the direct child -- and the turn
            counts as a failed attempt (section 6.5).
        :param max_turns: Ceiling on agentic turns within this one dispatch.

        Implementations must not raise on a *task* failure: a turn that went
        badly is still a ``TurnResult``, because the orchestrator decides what
        a failure means and needs the record either way. Raising is reserved
        for the adapter itself being broken -- the CLI missing, or output that
        could not be parsed twice running (section 6.4 rule 4).
        """
        ...

    def version(self) -> str:
        """The installed agent CLI's version string.

        Surfaced by ``redgear doctor``. Adapter flags change between releases
        of a third-party CLI, so being able to see the installed version is
        what makes drift diagnosable rather than mysterious (section 2.4).
        """
        ...


@runtime_checkable
class PlanRunner(Protocol):
    """A dispatch that returns a **document** rather than an outcome contract.

    Section 3.2 has the planner go "through ``runner.py``" like the loop does,
    and G5 means planning is no exception -- redgear never calls a model. But
    it cannot reuse :meth:`Runner.dispatch`: that returns a ``TurnResult``,
    whose only free-text field is a 1500-character ``summary``. A 41-node plan
    does not fit, and widening ``TurnResult`` to carry one would pollute the
    loop's type for the planner's benefit.

    So the two dispatches are separate shapes because they genuinely differ: a
    task turn reports an outcome and edits files, a planning turn returns a
    document and edits nothing. The second implementation (the test fake)
    exists on day one, which is the same justification section 6.1 gives for
    ``Runner`` itself.
    """

    def dispatch_json(
        self,
        prompt: str,
        allowed_tools: list[str],
        cwd: Path,
        timeout_s: int,
        max_turns: int,
        schema: Mapping[str, object],
    ) -> JsonValue:
        """Run one turn and return its structured output, parsed.

        ``schema`` is passed to the CLI so the reply is schema-conforming by
        construction rather than by hope. Raises :class:`RunnerError` when
        there is no parseable document -- the planner's retry loop is about
        *invalid plans*, not about a broken integration.
        """
        ...


#: Always granted. Reading the repository cannot violate a scope guarantee,
#: and an agent that cannot read cannot work.
_READ_ONLY_TOOLS = ("Read", "Glob", "Grep")

#: Granted to task types that produce files.
_WRITE_TOOLS = ("Edit", "Write")


def allowed_tools_for(task: TaskNode) -> list[str]:
    """The permission allowlist for one task, derived from its scope.

    Section 8.2 puts this helper here rather than in the orchestrator, because
    it is about what an agent CLI is permitted to do -- the same reason argv
    construction lives in this module.

    Two rules, both absolute:

    * **Never a bare ``Bash``.** An unrestricted shell defeats every scope
      guarantee at once, because the agent can write any file with it and the
      allowlist stops meaning anything. Where a task genuinely needs a shell,
      it gets specific prefixes such as ``Bash(git status *)`` -- and the
      space before ``*`` is significant (section 6.2).
    * **Never ``--dangerously-skip-permissions``.** There is no configuration
      that enables it (section 11.1 rule 3).

    The write tools are granted on task type rather than on glob contents: a
    task with no writable globs has nothing to write, and one with any has to
    be able to write it. The *enforcement* of which paths is the verifier's
    job (G2) -- this is the "instruct, then constrain, then verify" middle
    step, not the guarantee itself.
    """
    tools = list(_READ_ONLY_TOOLS)
    if task.scope.writable_globs or task.scope.creatable_globs:
        tools.extend(_WRITE_TOOLS)
    return tools


# ---------------------------------------------------------------------------
# The Claude Code adapter (T-0035)
#
# Verified against: claude 2.1.229 on 2026-08-19 (Windows, MSIX/Claude-Desktop
#   install, OAuth via the desktop app, bare=false).
#
#   docs/agents/claude-code.md's manual procedure was run against a real,
#   authenticated CLI: three dispatches, real --output-format json stdout
#   captured verbatim under tests/fixtures/claude_payloads/ (see that
#   directory's README for the full account). This verifies the three shapes
#   actually exercised -- an unauthenticated failure, a plain success with no
#   --json-schema, and a success with --json-schema proving structured_output
#   is a real, separate object from result -- not the CLI's full flag surface.
#   --bare, --mcp-config and --max-spend-usd were not exercised this session.
#
#   Nothing in build_argv needed to change: every flag already matched what
#   the real CLI accepted. What changed was parsing-adjacent understanding,
#   not the argv shape -- see the fixtures README and CLAUDE.md section 6.2's
#   observed-field list.
# ---------------------------------------------------------------------------


#: Flags that must never appear in an argv this module builds, for any input
#: or configuration (section 11.1 rule 3). Named rather than merely omitted so
#: a test can assert unreachability, and so a reader meets the prohibition here
#: rather than inferring it from an absence.
FORBIDDEN_FLAGS: tuple[str, ...] = (
    "--dangerously-skip-permissions",
    "--dangerously-skip-permission",
    "--no-permissions",
)

#: Appended to the prompt on the single retry after an unparseable result
#: (section 6.4 rule 4). Trusted, generated text -- never anything the agent or
#: the harness produced, which section 5.4 rule 2 keeps out of argv entirely.
_PARSE_REMINDER = """

---
NOTE FROM THE ORCHESTRATOR: your previous reply could not be parsed. Return a
single JSON object matching the provided schema, with the keys `outcome`,
`summary`, `changed_files` and `known_gaps`. `outcome` must be exactly one of
"completed", "blocked", or "scope_insufficient". Do not wrap it in prose."""

#: Section 6.5: cap captured stdout/stderr per turn and note truncation inline.
_MAX_CAPTURE_BYTES = 8 * 1024 * 1024
_TRUNCATION_NOTE = "\n[... truncated by redgear at 8 MiB]"

#: Section 6.4 rule 4: one retry, then stop. Two consecutive parse failures is
#: an integration bug and must be loud rather than silently retried forever.
_MAX_PARSE_ATTEMPTS = 2


class ClaudeCodeConfig(Strict):
    """Adapter configuration. Deliberately small.

    **There is no field carrying arbitrary argv**, and that absence is a
    safety property rather than an oversight: an ``extra_args`` hatch is
    exactly how ``--dangerously-skip-permissions`` would come back. With
    ``extra="forbid"`` inherited from ``Strict``, an attempt to smuggle one in
    through configuration fails validation instead of silently working.
    """

    executable: str = "claude"

    #: Section 6.3. **Off by default, and the reason is a credential one.** In
    #: bare mode Claude Code never reads OAuth credentials or the system
    #: keychain and requires an API key in the environment instead. Defaulting
    #: to it would push users toward putting a raw key in their shell for a
    #: tool that otherwise needs no credential at all -- trading away the
    #: cleanest property redgear has (G5).
    #:
    #: The tradeoff when you do opt in: bare mode skips auto-discovery of
    #: hooks, skills, plugins, MCP servers and the target repo's own
    #: ``CLAUDE.md``. That last one usually *helps* -- with bare off, the
    #: target repo's instructions and redgear's prompt are both in context and
    #: can contradict each other.
    bare: bool = False

    #: Command prefixes the agent may run, rendered through ``bash_rule``.
    #: Never a bare ``Bash`` (section 8.2).
    bash_prefixes: list[str] = Field(default_factory=list)

    #: Section 6.6's optional read-only sidecar. A token optimisation, not a
    #: correctness mechanism -- the loop does not require MCP.
    mcp_config: str | None = None

    #: Passed through where the CLI supports a per-run spend cap (section 8.1).
    per_turn_usd: float | None = Field(default=None, ge=0)


def bash_rule(command_prefix: str) -> str:
    """Render one Bash permission rule with the significant space.

    Section 6.2: ``Bash(git diff *)`` -- **the space before the star is
    significant**; without it the pattern also matches ``git diff-index``.

    That is a real privilege escalation and not a formatting nit: an agent
    granted ``git diff`` would silently also be granted every command sharing
    that prefix. Building these by hand is how the space goes missing, which
    is why section 6.2 says to use a tested helper.
    """
    prefix = command_prefix.strip()
    if prefix.endswith("*"):
        prefix = prefix[:-1].strip()
    return f"Bash({prefix} *)"


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_CAPTURE_BYTES:
        return text
    return encoded[:_MAX_CAPTURE_BYTES].decode("utf-8", errors="replace") + _TRUNCATION_NOTE


def _load_json(text: str) -> JsonValue:
    parsed: JsonValue = json.loads(text)
    return parsed


def _as_map(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _as_int_or_none(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float_or_none(value: JsonValue) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None


class ClaudeCodeRunner:
    """Drive Claude Code headlessly. The only module that spawns an agent CLI.

    One dispatch is one ``claude -p`` invocation: one prompt in, one result
    out, process exits (section 1.2). The CLI is a stateless worker -- it holds
    no memory between turns, which is what stops a long run accumulating a
    poisoned context.

    **The environment is propagated in full, and this is the one place in
    redgear where a credential legitimately crosses a boundary.** The child
    cannot authenticate without it. G5's rule is that redgear never *reads*
    one: ``os.environ`` is handed over as a whole, no auth key is inspected,
    logged, stored or branched on, and the persisted ``argv.json`` records
    environment **key names only**.

    Note this is the opposite of the harness in section 7.3, which gets a
    scrubbed allowlist precisely so a malicious test in a target repository
    cannot read the user's credentials. Two subprocess paths, two rules, two
    different threats.
    """

    def __init__(self, *, config: ClaudeCodeConfig, artifacts_root: Path) -> None:
        self.config = config
        self.artifacts_root = artifacts_root
        self._turn = 0

    # -- argv ---------------------------------------------------------------

    def tools_for(self, task: TaskNode) -> list[str]:
        """The permission allowlist for one task: scope plus configured Bash
        prefixes, each rendered through :func:`bash_rule`."""
        tools = allowed_tools_for(task)
        tools.extend(bash_rule(prefix) for prefix in self.config.bash_prefixes)
        return tools

    def build_argv(
        self,
        *,
        prompt: str,
        allowed_tools: Sequence[str],
        max_turns: int,
        schema: Mapping[str, object] | None = None,
    ) -> list[str]:
        """Section 6.2's invocation, as a list.

        Every element is discrete. The prompt is one element and is never
        concatenated into another, so a prompt containing backticks, ``$()`` or
        ``;`` is inert -- combined with ``shell=False`` at the spawn site there
        is nothing left to interpret those characters.
        """
        for tool in allowed_tools:
            if tool in FORBIDDEN_FLAGS or tool.startswith("--"):
                raise RunnerError(
                    "a permission entry looks like a command-line flag; refusing to build argv",
                    detail={"offending": tool},
                )
            if tool == "Bash":
                raise RunnerError(
                    "a bare Bash permission was requested; section 8.2 requires prefixes",
                    detail={"offending": tool},
                )

        argv = [
            self.config.executable,
            # Non-interactive: one prompt in, result to stdout, process exits.
            "-p",
            prompt,
            "--output-format",
            "json",
            # This is what makes the section 5.3 outcome contract mechanical
            # rather than a hope that the agent formats correctly.
            "--json-schema",
            json.dumps(schema if schema is not None else agent_report_schema(), sort_keys=True),
            "--allowedTools",
            ",".join(allowed_tools),
            "--max-turns",
            str(max_turns),
        ]
        if self.config.bare:
            argv.append("--bare")
        if self.config.mcp_config is not None:
            argv.extend(["--mcp-config", self.config.mcp_config])
        if self.config.per_turn_usd is not None:
            argv.extend(["--max-spend-usd", f"{self.config.per_turn_usd:.4f}"])

        # Belt and braces: whatever configuration said, the result cannot carry
        # a forbidden flag (section 11.1 rule 3).
        for flag in FORBIDDEN_FLAGS:
            if flag in argv:  # pragma: no cover - unreachable by construction
                raise RunnerError(
                    "refusing to dispatch with a permission-skipping flag",
                    detail={"flag": flag},
                )
        return argv

    # -- dispatch -----------------------------------------------------------

    def dispatch(
        self,
        prompt: str,
        allowed_tools: list[str],
        cwd: Path,
        timeout_s: int,
        max_turns: int,
    ) -> TurnResult:
        """Run one turn, retrying once if the result cannot be parsed.

        Raises :class:`RunnerError` only when there is no usable result at all
        -- two consecutive unparseable replies. A turn that merely went badly
        still returns a ``TurnResult``, because the orchestrator decides what a
        failure means and needs the record either way.
        """
        last_detail: dict[str, JsonValue] = {}

        for attempt in range(_MAX_PARSE_ATTEMPTS):
            sent = prompt if attempt == 0 else prompt + _PARSE_REMINDER
            argv = self.build_argv(prompt=sent, allowed_tools=allowed_tools, max_turns=max_turns)
            directory = self._turn_dir()
            self._persist_argv(directory, argv)

            outcome = self._spawn(argv, cwd=cwd, timeout_s=timeout_s, directory=directory)
            if outcome is None:
                # Timed out. The tree is dead and the turn is recorded; there
                # is nothing to parse and nothing to gain from a second try
                # against the same wall clock.
                return self._timeout_result(directory, timeout_s)

            stdout, stderr, exit_code = outcome
            result = self._parse(stdout, exit_code, directory)
            if result is not None:
                return result

            last_detail = {
                "exit_code": exit_code,
                "attempt": attempt + 1,
                "stdout_head": stdout.strip()[:400],
                "stderr_head": stderr.strip()[:200],
                "raw_stdout": str(directory / "agent_stdout.log"),
            }

        raise RunnerError(
            "the agent CLI returned an unparseable result twice; this is an integration bug",
            detail=last_detail,
        )

    def dispatch_json(
        self,
        prompt: str,
        allowed_tools: list[str],
        cwd: Path,
        timeout_s: int,
        max_turns: int,
        schema: Mapping[str, object],
    ) -> JsonValue:
        """One dispatch that returns a document (the :class:`PlanRunner` seam).

        Shares the spawn, the artifact persistence and the environment policy
        with :meth:`dispatch`; differs only in what it asks for and what it
        gives back. There is no retry here -- the planner runs its own bounded
        retry loop over *invalid plans*, and a second layer underneath it
        would multiply the attempts without saying so.
        """
        argv = self.build_argv(
            prompt=prompt, allowed_tools=allowed_tools, max_turns=max_turns, schema=schema
        )
        directory = self._turn_dir()
        self._persist_argv(directory, argv)

        outcome = self._spawn(argv, cwd=cwd, timeout_s=timeout_s, directory=directory)
        if outcome is None:
            raise RunnerError(
                f"the planning dispatch exceeded its {timeout_s}s wall clock",
                detail={"raw_stdout": str(directory / "agent_stdout.log")},
            )

        stdout, stderr, exit_code = outcome
        try:
            payload = _as_map(_load_json(stdout))
        except json.JSONDecodeError:
            payload = {}

        # Same rule as a task turn: the exit code is recorded, never consulted.
        # A run that failed internally says so in the payload (6.4 rules 1-2).
        if not payload or payload.get("is_error") is True:
            raise RunnerError(
                "the planning dispatch returned no usable document",
                detail={
                    "exit_code": exit_code,
                    "stdout_head": stdout.strip()[:400],
                    "stderr_head": stderr.strip()[:200],
                    "raw_stdout": str(directory / "agent_stdout.log"),
                },
            )

        structured = payload.get("structured_output")
        if structured is None:
            raise RunnerError(
                "the planning dispatch returned no structured output",
                detail={"raw_stdout": str(directory / "agent_stdout.log")},
            )
        return structured

    def version(self) -> str:
        """The installed CLI's version, or a readable note if it is absent.

        Never raises: ``redgear doctor`` calls this to *report* the
        environment, and a diagnostic that crashes on the very problem it
        exists to diagnose is worse than useless.
        """
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
                [self.config.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"unavailable ({type(exc).__name__})"
        return (completed.stdout or completed.stderr).strip() or "unknown"

    # -- internals ----------------------------------------------------------

    def _turn_dir(self) -> Path:
        directory = self.artifacts_root / f"turn-{self._turn:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        self._turn += 1
        return directory

    def _persist_argv(self, directory: Path, argv: Sequence[str]) -> None:
        """Section 2.3: argv.json holds the exact argv, env **keys only**.

        The allowlist is recorded because a run where you cannot see what the
        agent was permitted to do is not auditable (section 8.2). Environment
        names are recorded and values are not read at all -- not redacted after
        the fact, simply never looked at (G5).
        """
        # Built by extend rather than a literal: `list` is invariant, so
        # `list[str]` does not satisfy `list[JsonValue]` directly.
        argv_record: list[JsonValue] = []
        argv_record.extend(argv)
        env_names: list[JsonValue] = []
        env_names.extend(sorted(os.environ))

        record: dict[str, JsonValue] = {
            "argv": argv_record,
            "env_keys": env_names,
            "note": "environment values are never read or recorded (G5)",
        }
        (directory / "argv.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _spawn(
        self, argv: Sequence[str], *, cwd: Path, timeout_s: int, directory: Path
    ) -> tuple[str, str, int] | None:
        """Run the CLI. ``None`` means it timed out and the tree was killed.

        Raw streams are written **before** this returns, so the bytes survive
        whatever the parser later makes of them (section 6.4 rule 6).
        """
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False
                list(argv),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                # G5: the whole environment, handed over without inspection.
                # The child cannot authenticate without it, and redgear never
                # looks inside. This is NOT the scrubbed harness env of 7.3.
                env=dict(os.environ),
                # MANDATORY on POSIX. `terminate_process_tree` kills a process
                # *group*, so the agent must be in its own -- otherwise a
                # timeout signals redgear's group too and takes the run down
                # with the turn it was trying to abort.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RunnerError(
                f"the agent CLI {self.config.executable!r} is not installed or not on PATH",
                detail={"executable": self.config.executable},
            ) from exc

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Section 6.5: the whole tree, not just the direct child. A pytest
            # the agent started would otherwise keep running against the repo.
            terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                stdout, stderr = "", ""

        stdout, stderr = _truncate(stdout or ""), _truncate(stderr or "")
        # Persisted before any parsing is attempted, on every path.
        (directory / "agent_stdout.log").write_text(stdout, encoding="utf-8")
        # Section 6.4 rule 3: stderr is a progress log. Archived, never grepped
        # for a decision.
        (directory / "agent_stderr.log").write_text(stderr, encoding="utf-8")

        if timed_out:
            return None
        return stdout, stderr, process.returncode if process.returncode is not None else -1

    def _timeout_result(self, directory: Path, timeout_s: int) -> TurnResult:
        """Section 6.5: a timeout is a recorded outcome, never an escaping
        exception. One slow turn must not abort the run and lose the proof."""
        return TurnResult(
            outcome=TurnOutcome.COMPLETED,
            summary=f"the agent CLI exceeded its {timeout_s}s wall clock (runner_timeout)",
            changed_files=[],
            known_gaps=[],
            blocker_category=BlockerCategory.ENVIRONMENT_BROKEN,
            blocker_detail=f"runner_timeout after {timeout_s}s; the process tree was terminated",
            exit_code=-1,
            session_id=None,
            num_turns=None,
            duration_ms=timeout_s * 1000,
            cost_usd_estimate=None,
            raw_stdout_path=str(directory / "agent_stdout.log"),
            parse_ok=False,
        )

    def _parse(self, stdout: str, exit_code: int, directory: Path) -> TurnResult | None:
        """Turn one payload into a ``TurnResult``, or ``None`` if unparseable.

        **The exit code is recorded, never consulted** (section 6.4 rules 1 and
        2). A failure inside the run arrives as the result on stdout with exit
        0, and there is no published table giving meaning to any specific
        non-zero code -- so the payload is the only thing that decides.
        """
        try:
            parsed: JsonValue = json.loads(stdout)
        except json.JSONDecodeError:
            return None

        payload = _as_map(parsed)
        if not payload:
            return None

        # A run that failed internally reports it here, whatever the exit code.
        if payload.get("is_error") is True:
            return None

        report = _as_map(payload.get("structured_output"))
        if not report:
            return None

        try:
            agent = AgentTurnReport.model_validate(report)
        except ValidationError:
            return None

        return TurnResult(
            outcome=agent.outcome,
            summary=agent.summary,
            # A CLAIM (G1), recorded verbatim and cross-checked by the scope
            # gate against the real diff. Never used to decide what to verify.
            changed_files=list(agent.changed_files),
            known_gaps=list(agent.known_gaps),
            blocker_category=agent.blocker_category,
            blocker_detail=agent.blocker_detail,
            exit_code=exit_code,
            session_id=_as_text(payload.get("session_id")) or None,
            num_turns=_as_int_or_none(payload.get("num_turns")),
            duration_ms=_as_int_or_none(payload.get("duration_ms")),
            # Section 6.2: client-side estimates. Used for budget signalling,
            # never presented as authoritative billing.
            cost_usd_estimate=_as_float_or_none(payload.get("total_cost_usd")),
            raw_stdout_path=str(directory / "agent_stdout.log"),
            parse_ok=True,
        )
