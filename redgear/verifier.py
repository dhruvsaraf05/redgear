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

**Gates 3-6 -- lint, tests_pass, criteria_coverage, coverage_delta.** These
execute real tools, which brings section 7.3 into play: ``shell=False``, an
argv from configuration only, a scrubbed environment, and a timeout that is a
recorded gate failure rather than an escaping exception.

They require inputs the verifier cannot invent -- a ``HarnessConfig`` saying
which commands to run, and the resolved inherited criteria. Called without
them they are reported ``skipped`` with a reason, never stubbed to pass. A
gate that always passes is worse than an absent one, because the proof would
show a green it never earned.

**The nested-runner problem.** Gate 4 runs pytest inside a target repository
while redgear's own suite is running under pytest. Left alone the child
process inherits far more than it looks like it should, and every item below
was *observed* rather than anticipated:

* An ancestor ``pyproject.toml`` is discovered by walking up out of the
  target repository, and its ``addopts`` silently rewrite the child's argv.
* ``--rootdir`` does **not** prevent that. It pins the reported rootdir while
  ``configfile`` still resolves to the ancestor. Only ``-c`` pins the config.
* An ancestor ``conftest.py`` is still imported even with ``-c`` and
  ``--rootdir``; that needs ``--confcutdir``.
* ``PYTEST_ADDOPTS`` from the outer session is inherited and applied.
* The cache provider writes ``.pytest_cache`` into the user's tree.

So the isolation is five things at once, and dropping any one of them
reintroduces a failure whose symptom -- "tests pass alone, fail in the
suite" -- points nowhere near its cause.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from redgear import gitctx
from redgear.budget import terminate_process_tree
from redgear.errors import JsonValue, UnsafeHarnessCommandError
from redgear.hashing import digest_map
from redgear.paths import is_state_path, match_glob, matches_any
from redgear.schemas import (
    AcceptanceCriterion,
    Claim,
    GateName,
    GateResult,
    GateStatus,
    HarnessConfig,
    Proof,
    TaskNode,
    Verdict,
)

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

#: Gates that shell out and therefore need configured commands to run at all.
_NEEDS_HARNESS = frozenset(GATE_ORDER[2:])

_SKIPPED_REASON = "skipped: an earlier gate failed and the pipeline short-circuits"
_NO_HARNESS_REASON = (
    "no_harness_config: gates 3-6 execute configured commands and were not "
    "given a HarnessConfig, so they did not run"
)

#: Section 7.2 caps the mapped lint diagnostics; section 5.5 caps the reported
#: locations per gate at 3. Both exist so a corrective prompt stays inside the
#: section 5.6 token budget while still stating the true totals.
_MAX_LINT_VIOLATIONS = 20
_MAX_TEST_FAILURES = 3
_MAX_UNCOVERED_LINES = 10

#: Where the harness keeps the pytest report, the coverage data file and the
#: coverage JSON. Inside the target repository so a nested run can never
#: clobber the outer session's own report, and removed again before
#: ``run_harness`` returns so the working tree is left as it was found.
_SCRATCH_DIR = ".redgear_harness"

#: A collection error is not one thing. A missing implementation is the
#: *expected* red state for a test_authoring task (PROGRESS.md section 6);
#: a test file that does not parse is the cheapest possible fake red. The
#: report structure is byte-for-byte identical in both cases -- only the
#: exception type inside ``longrepr`` tells them apart.
_SYNTAX_MARKERS = ("SyntaxError", "IndentationError", "TabError")
_IMPORT_MARKERS = ("ModuleNotFoundError", "ImportError")


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
# JSON narrowing
#
# Section 11.2 rule 1 forbids `Any` outside `hashing.canonical_json`, and
# every one of these reports is untrusted external input (G7) that may be
# truncated, empty, or a different version's shape. These narrow a JsonValue
# to something usable and return a harmless empty value rather than raising,
# so a malformed report degrades into a reported gate failure instead of a
# traceback that kills the run.
# ---------------------------------------------------------------------------


def _load_json(text: str) -> JsonValue:
    parsed: JsonValue = json.loads(text)
    return parsed


def _as_map(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_seq(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _as_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _as_count(value: JsonValue) -> int:
    # `bool` is an `int` subclass; a boolean here would mean the shape changed.
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_lines(value: JsonValue) -> set[int]:
    return {n for n in _as_seq(value) if isinstance(n, int) and not isinstance(n, bool)}


# ---------------------------------------------------------------------------
# Subprocess execution -- section 7.3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """One harness command's outcome. A timeout is data here, not an
    exception: section 7.3 requires it to become a gate failure, and an
    escaping ``TimeoutExpired`` would abort the run and lose the proof."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


def harness_env() -> dict[str, str]:
    """The scrubbed environment for harness commands (section 7.3).

    Deliberately *not* the parent environment. A test in a target repository
    is arbitrary code executing on the user's machine, and G5 means it must
    not be able to read the user's credentials out of ``os.environ``. This
    differs from the agent subprocess (section 6), which needs the full
    environment in order to authenticate at all.

    It also shuts out the outer pytest session. ``PYTEST_ADDOPTS`` is the one
    that bites: inherited, it silently rewrites the child's argv, and a
    deselect-everything value turns a real suite into "0 collected" with no
    error anywhere.

    **Deviation from section 7.3, deliberate and load-bearing.** The allowlist
    as written there cannot start a Python interpreter on Windows: without
    ``SYSTEMROOT`` the runtime fails to initialise the platform networking
    layer and pytest dies with an INTERNALERROR before collecting anything.
    A scrubbed environment that cannot launch the harness is not a safety
    measure, so the platform's minimum is added back. None of these carry
    credentials.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI": "1",
        "NO_COLOR": "1",
    }
    if sys.platform == "win32":
        for name in ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
    return env


def run_command(cmd: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandResult:
    """Run one harness command. ``shell=False``, fixed argv, scrubbed env.

    ``Popen`` rather than ``subprocess.run`` because the timeout has to take
    the whole process tree with it (NFR-4). ``run``'s own timeout kills the
    direct child only, which leaves a pytest that spawned workers still
    holding the repository.
    """
    argv = list(cmd)
    # Section 7.3: reject any configured command containing `..`. A harness
    # command is operator-supplied, but it is still the one place a path could
    # be walked out of the repository, and the check costs nothing.
    for part in argv:
        if ".." in part:
            raise UnsafeHarnessCommandError(
                "harness command contains a parent-directory reference",
                detail={"argv": list(argv), "offending": part},
            )

    started = time.monotonic()
    timed_out = False
    process = subprocess.Popen(  # noqa: S603 - fixed argv from config, shell=False
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=harness_env(),
        # MANDATORY on POSIX, and not an optimisation. `terminate_process_tree`
        # kills a process *group*; without a new session the child inherits
        # redgear's own group, and killing it on timeout signals the whole
        # group -- including the process that started the run. In CI that
        # meant a timeout test killed pytest itself.
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        # The tree is dead; this drains whatever it already wrote. Bounded so
        # an unkillable grandchild holding the pipe cannot hang the run.
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            stdout, stderr = "", ""

    return CommandResult(
        argv=argv,
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _repo_relative(repo_root: Path, filename: str) -> str | None:
    """An absolute tool-reported path as a repo-relative POSIX one.

    ruff reports absolute native paths. Left alone they leak the user's home
    directory into a prompt and waste tokens (section 5.4 rule 3), and they
    never match a scope glob. ``None`` means the path is outside the
    repository entirely, which is not this task's business.
    """
    try:
        return Path(filename).resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


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
    # redgear's own state directory is not agent work. The loop writes the
    # event log, the projection and the persisted prompt *between* the claim
    # and this audit, so every one of those would otherwise surface as an
    # `out_of_scope_write` and fail every task ever run. Excluded here rather
    # than in `gitctx`, which is a general-purpose reader with no business
    # knowing redgear's own layout.
    actual = {entry.path: entry for entry in changed if not is_state_path(entry.path)}
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
# Gate 3 -- lint
# ---------------------------------------------------------------------------


def lint_check(repo_root: Path, *, task: TaskNode, harness: HarnessConfig) -> GateResult:
    """Are there lint violations in the paths this task was allowed to write?

    Scoped deliberately. Section 7.2: "a pre-existing violation elsewhere is
    not this agent's failure." Failing on one would be unfixable by
    construction -- the file is outside the write scope, so the agent cannot
    correct it, and every retry would burn an attempt on someone else's debt.

    Out-of-scope violations are still *counted* in a note. The run is
    proceeding past real lint debt, and a proof that never mentions it reads
    as a clean repository.
    """
    result = run_command(harness.lint_cmd, cwd=repo_root, timeout_s=harness.timeout_s)

    if result.timed_out:
        return GateResult(
            name=GateName.LINT,
            status=GateStatus.FAILED,
            reasons=[_reason("timeout", f"lint exceeded {harness.timeout_s}s")],
        )

    # ruff exits 1 when it finds violations and still writes JSON to stdout,
    # so the exit code alone says nothing. An unparseable stdout is a broken
    # harness, not a failing task.
    try:
        diagnostics = _as_seq(_load_json(result.stdout)) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return GateResult(
            name=GateName.LINT,
            status=GateStatus.FAILED,
            reasons=[
                _reason(
                    "harness_error",
                    f"lint output was not JSON (exit {result.exit_code}): "
                    f"{result.stderr.strip()[:200] or result.stdout.strip()[:200]}",
                )
            ],
        )

    in_scope_globs = list(task.scope.writable_globs) + list(task.scope.creatable_globs)
    in_scope: list[str] = []
    out_of_scope = 0

    for entry in diagnostics:
        diagnostic = _as_map(entry)
        relative = _repo_relative(repo_root, _as_text(diagnostic.get("filename")))
        if relative is None:
            continue
        if not matches_any(relative, in_scope_globs):
            out_of_scope += 1
            continue
        location = _as_map(diagnostic.get("location"))
        in_scope.append(
            f"{relative}:{_as_count(location.get('row'))}:{_as_count(location.get('column'))} "
            f"{_as_text(diagnostic.get('code'))} {_as_text(diagnostic.get('message'))}".strip()
        )

    reasons = [_reason("lint_violation", item) for item in in_scope[:_MAX_LINT_VIOLATIONS]]
    if len(in_scope) > _MAX_LINT_VIOLATIONS:
        hidden = len(in_scope) - _MAX_LINT_VIOLATIONS
        reasons.append(_reason("truncated", f"{hidden} more violation(s) of this kind not shown"))
    if out_of_scope:
        reasons.append(
            _reason(
                "ignored_out_of_scope",
                f"{out_of_scope} violation(s) outside this task's scope were not counted",
            )
        )

    return GateResult(
        name=GateName.LINT,
        status=GateStatus.FAILED if in_scope else GateStatus.PASSED,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# The nested harness run -- shared by gates 4, 5 and 6
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessRun:
    """One nested pytest invocation, optionally under coverage.

    Gates 4, 5 and 6 all read this. Running the suite once and sharing the
    result is not merely an optimisation: three separate runs could disagree
    with each other, and a proof assembled from three different executions of
    the same suite is not evidence about any one of them.
    """

    argv: list[str]
    exit_code: int
    timed_out: bool
    report: dict[str, JsonValue] | None = None
    coverage: dict[str, JsonValue] | None = None
    stdout: str = ""
    stderr: str = ""
    collectors_failed: list[str] = field(default_factory=list)


def _pytest_config_path(repo_root: Path, scratch: Path) -> Path:
    """The config file the nested pytest is pinned to.

    A repository's own configuration is legitimate and honoured. What must not
    happen is *discovery* -- with no config of its own, pytest walks up out of
    the repository and adopts whatever it finds above, which on a developer's
    machine is frequently another project's ``pyproject.toml``.

    ``--rootdir`` does not prevent this. It was measured: rootdir gets pinned
    while ``configfile`` still resolves to the ancestor, and the ancestor's
    ``addopts`` still apply. Only ``-c`` stops it, so there is always a config
    to point at -- the repository's, or an inert one written for the purpose.
    """
    for name in ("pytest.ini", "tox.ini", "setup.cfg"):
        candidate = repo_root / name
        if candidate.is_file():
            return candidate

    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            if "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8"):
                return pyproject
        except OSError:  # pragma: no cover - unreadable file, fall through
            pass

    inert = scratch / "pytest.ini"
    inert.write_text("[pytest]\n", encoding="utf-8")
    return inert


def run_harness(
    repo_root: Path,
    *,
    harness: HarnessConfig,
    with_coverage: bool = False,
) -> HarnessRun:
    """Run the target repository's test suite, isolated from this session.

    Every flag below closes an inheritance that was observed happening, not
    one that was guessed at. See the module docstring for the measurements.

    The scratch directory lives inside the target repository so the nested
    report can never overwrite the outer session's own, and is removed again
    before returning so the working tree is left exactly as it was found --
    otherwise the next ``git add -A -N`` inside ``gitctx`` would stage it.
    """
    scratch = repo_root / _SCRATCH_DIR
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    report_path = scratch / "pytest-report.json"
    data_path = scratch / "coverage.data"
    coverage_json = scratch / "coverage.json"
    config_path = _pytest_config_path(repo_root, scratch)

    pytest_flags = [
        # Do not write `.pytest_cache` into the user's tree.
        "-p",
        "no:cacheprovider",
        # Coverage is driven by `coverage run` directly, so pytest-cov is dead
        # weight in the child -- and if the outer session is itself running
        # under `--cov`, leaving it enabled invites the two to contend for the
        # same data file.
        "-p",
        "no:cov",
        # Pin the config: the only thing that stops upward ini discovery.
        "-c",
        str(config_path),
        # Pin the rootdir so node ids are repo-relative and stable.
        "--rootdir",
        str(repo_root),
        # Stop `conftest.py` collection at the repository boundary; an
        # ancestor conftest is loaded even with `-c` and `--rootdir`.
        "--confcutdir",
        str(repo_root),
        "--json-report",
        f"--json-report-file={report_path}",
        "-q",
    ]

    if with_coverage:
        argv = [
            *harness.coverage_cmd,
            "run",
            f"--data-file={data_path}",
            f"--source={','.join(harness.coverage_source)}",
            "-m",
            "pytest",
            *pytest_flags,
        ]
    else:
        argv = [*harness.test_cmd, *pytest_flags]

    try:
        command = run_command(argv, cwd=repo_root, timeout_s=harness.timeout_s)

        report: dict[str, JsonValue] | None = None
        if report_path.is_file():
            try:
                report = _as_map(_load_json(report_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                report = None

        coverage: dict[str, JsonValue] | None = None
        if with_coverage and not command.timed_out and data_path.exists():
            export = run_command(
                [
                    *harness.coverage_cmd,
                    "json",
                    f"--data-file={data_path}",
                    "-o",
                    str(coverage_json),
                    "-q",
                ],
                cwd=repo_root,
                timeout_s=harness.timeout_s,
            )
            if coverage_json.is_file() and not export.timed_out:
                try:
                    coverage = _as_map(_load_json(coverage_json.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    coverage = None

        failed_collectors: list[str] = []
        if report is not None:
            for entry in _as_seq(report.get("collectors")):
                collector = _as_map(entry)
                if _as_text(collector.get("outcome")) == "failed":
                    failed_collectors.append(
                        f"{_as_text(collector.get('nodeid')) or '<root>'} "
                        f"{_as_text(collector.get('longrepr'))}"
                    )

        return HarnessRun(
            argv=argv,
            exit_code=command.exit_code,
            timed_out=command.timed_out,
            report=report,
            coverage=coverage,
            stdout=command.stdout,
            stderr=command.stderr,
            collectors_failed=failed_collectors,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def classify_collection_error(longrepr: str) -> str:
    """Why did collection fail? ``import_error``, ``syntax_error``, ``other``.

    This distinction is the whole of the test_authoring polarity decision. A
    ``ModuleNotFoundError`` is the red state the two-phase protocol *asks*
    for -- the tests are written, the implementation is not. A
    ``SyntaxError`` is a test file that does not parse, which would let an
    agent satisfy a test_authoring task by writing garbage.

    Syntax is checked first: a file that fails to compile never reaches its
    imports, so an ``ImportError`` marker in the same text would be
    misleading.
    """
    if any(marker in longrepr for marker in _SYNTAX_MARKERS):
        return "syntax_error"
    if any(marker in longrepr for marker in _IMPORT_MARKERS):
        return "import_error"
    return "other"


# ---------------------------------------------------------------------------
# Gate 4 -- tests_pass
# ---------------------------------------------------------------------------


def _test_outcomes(run: HarnessRun) -> dict[str, str]:
    """Node id -> outcome, read from the JSON report.

    From the report on disk, never scraped from stdout (G1). Node ids are
    POSIX-normalised so a selector authored on Windows still resolves.
    """
    outcomes: dict[str, str] = {}
    if run.report is None:
        return outcomes
    for entry in _as_seq(run.report.get("tests")):
        test = _as_map(entry)
        node_id = _as_text(test.get("nodeid")).replace("\\", "/")
        if node_id:
            outcomes[node_id] = _as_text(test.get("outcome"))
    return outcomes


def _failure_reasons(run: HarnessRun) -> list[str]:
    """The failing tests, capped per section 5.5 with the true total kept."""
    failures = [
        node for node, outcome in sorted(_test_outcomes(run).items()) if outcome == "failed"
    ]
    reasons = [_reason("test_failed", node) for node in failures[:_MAX_TEST_FAILURES]]
    if len(failures) > _MAX_TEST_FAILURES:
        hidden = len(failures) - _MAX_TEST_FAILURES
        reasons.append(_reason("truncated", f"{hidden} more failing test(s) not shown"))
    return reasons


def tests_pass_check(task: TaskNode, run: HarnessRun) -> GateResult:
    """Did the suite do what this task type requires of it?

    **Polarity inverts for ``test_authoring``** (section 7.2): the gate passes
    only when the target tests exist, collected, and *failed*. Tests that
    already pass are a tautology -- assertions that were true before the agent
    started prove nothing about an implementation that does not exist yet.

    Two report shapes are easy to get wrong, and both were measured:

    * When collection fails, ``summary`` carries **no ``error`` key at all**.
      Section 7.2's "fail on ``failed > 0`` or ``error > 0``" read literally
      against that report is ``0 > 0 or 0 > 0`` -- false -- so a suite that
      never ran would sail through as a pass.
    * A collection error and an assertion failure are reported identically
      apart from the exception type. They demand opposite responses from the
      agent, so they get distinct reasons.
    """
    if task.type == "scaffold":
        # Section 4.5: a scaffold task is verified by smoke checks. There is
        # no meaningful failing test for packaging metadata.
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.SKIPPED,
            reasons=[_reason("not_applicable", "scaffold tasks are verified by smoke checks")],
        )

    if run.timed_out:
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.FAILED,
            reasons=[_reason("timeout", "the test suite exceeded its configured timeout")],
        )

    if run.report is None:
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.FAILED,
            reasons=[
                _reason(
                    "harness_error",
                    f"no parseable JSON report was produced (exit {run.exit_code}): "
                    f"{run.stderr.strip()[:200]}",
                )
            ],
        )

    summary = _as_map(run.report.get("summary"))
    failed = _as_count(summary.get("failed")) + _as_count(summary.get("error"))
    collected = _as_count(summary.get("collected"))

    if task.type == "test_authoring":
        return _test_authoring_verdict(run, failed=failed, collected=collected)

    if run.collectors_failed:
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.FAILED,
            reasons=[
                _reason("collection_error", detail.strip()[:300])
                for detail in run.collectors_failed[:_MAX_TEST_FAILURES]
            ],
        )

    if failed:
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.FAILED,
            reasons=_failure_reasons(run),
        )

    return GateResult(name=GateName.TESTS_PASS, status=GateStatus.PASSED)


# `tests_pass_check` matches pytest's default `python_functions = test*` glob,
# so any test module that imports it has it collected as a test -- which then
# errors with "fixture 'task' not found". The name is not negotiable (it
# mirrors `GateName.TESTS_PASS` alongside `scope_check`, `lint_check` and the
# rest), and the test modules that import it are frozen under G2, so the opt
# out has to live here. `setattr` rather than direct assignment because mypy
# strict rejects attributes it cannot see on a `Callable`, and NFR-5 forbids
# suppressions.
setattr(tests_pass_check, "__test__", False)  # noqa: B010


def _test_authoring_verdict(run: HarnessRun, *, failed: int, collected: int) -> GateResult:
    """The inverted polarity, kept separate so the normal path stays legible."""
    if run.collectors_failed:
        kinds = {classify_collection_error(detail) for detail in run.collectors_failed}
        if kinds == {"import_error"}:
            # The expected bootstrap red: the tests are written and the module
            # they import has not been built yet.
            return GateResult(
                name=GateName.TESTS_PASS,
                status=GateStatus.PASSED,
                reasons=[
                    _reason("red_via_import_error", "the target implementation does not exist yet")
                ],
            )
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.FAILED,
            reasons=[
                _reason(
                    "invalid_red", f"[{classify_collection_error(detail)}] {detail.strip()[:260]}"
                )
                for detail in run.collectors_failed[:_MAX_TEST_FAILURES]
            ],
        )

    if collected == 0:
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.FAILED,
            reasons=[
                _reason(
                    "tests_not_red",
                    "no tests were collected; the target tests must exist and fail",
                )
            ],
        )

    if failed:
        return GateResult(
            name=GateName.TESTS_PASS,
            status=GateStatus.PASSED,
            reasons=[_reason("red_via_failure", f"{failed} of {collected} target test(s) fail")],
        )

    return GateResult(
        name=GateName.TESTS_PASS,
        status=GateStatus.FAILED,
        reasons=[
            _reason(
                "tests_not_red",
                f"all {collected} collected test(s) already pass; a test_authoring task "
                "must leave its target tests failing",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Gate 5 -- criteria_coverage
# ---------------------------------------------------------------------------


def criteria_coverage_check(
    task: TaskNode,
    criteria: Sequence[AcceptanceCriterion],
    run: HarnessRun,
) -> GateResult:
    """Did the specific tests the plan named actually run, and pass?

    This is the gate that catches "implemented and fully tested" when the
    cited test never ran. Gate 4 asks whether the suite passed -- a suite of
    one trivial test passes. This asks whether *these* node ids appeared in
    the report, which an agent cannot satisfy by working on something else.

    The two reasons are kept apart because they demand opposite work.
    ``evidence_not_found`` means write the test; ``evidence_did_not_pass``
    means fix the code. Telling an agent a test is missing when it is merely
    red sends it to write a second copy of a test that already exists.

    For a ``test_authoring`` task only *existence* is required. Its tests are
    supposed to be red -- gate 4 has already established that -- so demanding
    they pass here would contradict gate 4 outright and make the pair
    unsatisfiable.
    """
    if task.type == "scaffold":
        return GateResult(
            name=GateName.CRITERIA_COVERAGE,
            status=GateStatus.SKIPPED,
            reasons=[_reason("not_applicable", "scaffold tasks are verified by smoke checks")],
        )

    if run.report is None:
        return GateResult(
            name=GateName.CRITERIA_COVERAGE,
            status=GateStatus.FAILED,
            reasons=[_reason("harness_error", "no parseable JSON report to resolve criteria in")],
        )

    if task.type == "test_authoring" and run.collectors_failed:
        # The legitimate bootstrap red leaves nothing collected, so there are
        # no node ids to resolve against. Gate 4 has already confirmed the red
        # is the expected one; failing every criterion here would contradict it.
        return GateResult(
            name=GateName.CRITERIA_COVERAGE,
            status=GateStatus.SKIPPED,
            reasons=[
                _reason(
                    "not_applicable",
                    "collection did not complete, so no node ids exist to resolve; "
                    "gate 4 established the red state",
                )
            ],
        )

    outcomes = _test_outcomes(run)
    reasons: list[str] = []

    for criterion in criteria:
        selector = criterion.verified_by.selector.replace("\\", "/")
        if selector not in outcomes:
            reasons.append(_reason("evidence_not_found", f"{criterion.id} cites {selector}"))
        elif task.type != "test_authoring" and outcomes[selector] != "passed":
            reasons.append(
                _reason(
                    "evidence_did_not_pass",
                    f"{criterion.id} cites {selector} (outcome: {outcomes[selector]})",
                )
            )

    return GateResult(
        name=GateName.CRITERIA_COVERAGE,
        status=GateStatus.FAILED if reasons else GateStatus.PASSED,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Gate 6 -- coverage_delta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageDelta:
    """Changed-line coverage. ``considered`` is the denominator, and it is
    reported alongside the ratio because 1/1 and 400/400 are both 1.0 and mean
    very different things."""

    ratio: float
    covered: int
    considered: int
    uncovered: dict[str, list[int]]


def changed_line_ratio(
    repo_root: Path,
    base_commit: str,
    coverage: Mapping[str, JsonValue],
) -> CoverageDelta:
    """Coverage over **the lines this task changed** (section 7.5).

    Never the repository. A global threshold punishes an agent for debt that
    predates it -- unfixable inside its scope -- and is trivially gamed by
    touching one line in a well-covered module.

    The denominator is ``changed & (executed | missing)`` per file. Lines
    coverage.py does not classify -- blank, comment, excluded -- drop out,
    because counting them would penalise an agent for adding a docstring.
    Files with no coverage data at all drop out too: a changed test file is
    not uncovered production code, and scoring it as such would fail every
    test_authoring task in the project.

    coverage.py keys its JSON by **native** separators, so on Windows the
    entries read ``src\\pkg\\__init__.py`` while git emits forward slashes.
    Without normalisation the two sets never intersect, every denominator is
    empty, and the gate silently passes everything.
    """
    changed = gitctx.changed_lines_from_patch(gitctx.diff_patch(repo_root, base_commit))

    covered_total = 0
    considered_total = 0
    uncovered: dict[str, list[int]] = {}

    for raw_path, raw_data in _as_map(coverage.get("files")).items():
        path = raw_path.replace("\\", "/")
        changed_here = changed.get(path)
        if not changed_here:
            continue

        data = _as_map(raw_data)
        executed = _as_lines(data.get("executed_lines"))
        missing = _as_lines(data.get("missing_lines"))

        considered = changed_here & (executed | missing)
        if not considered:
            continue

        covered_total += len(considered & executed)
        considered_total += len(considered)
        still_missing = sorted(considered - executed)
        if still_missing:
            uncovered[path] = still_missing

    # Section 7.5: "Ratio is 1.0 when the denominator is empty." A change with
    # nothing measurable in it is not a coverage regression, and dividing by
    # zero would crash the gate on a comment-only edit.
    ratio = 1.0 if considered_total == 0 else covered_total / considered_total
    return CoverageDelta(
        ratio=ratio,
        covered=covered_total,
        considered=considered_total,
        uncovered=uncovered,
    )


def coverage_delta_check(
    repo_root: Path,
    *,
    task: TaskNode,
    claim: Claim,
    harness: HarnessConfig,
    run: HarnessRun,
) -> GateResult:
    """Is changed-line coverage at or above the configured floor?"""
    if task.type in ("scaffold", "test_authoring"):
        # Section 4.5 exempts scaffold. A test_authoring task writes tests
        # rather than covered code, and its suite is red by design -- coverage
        # measured from that run says nothing about an implementation that has
        # not been written.
        return GateResult(
            name=GateName.COVERAGE_DELTA,
            status=GateStatus.SKIPPED,
            reasons=[
                _reason(
                    "not_applicable",
                    f"{task.type} tasks are not measured for changed-line coverage",
                )
            ],
        )

    if run.coverage is None:
        return GateResult(
            name=GateName.COVERAGE_DELTA,
            status=GateStatus.FAILED,
            reasons=[_reason("harness_error", "no coverage data was produced by the harness")],
        )

    delta = changed_line_ratio(repo_root, claim.base_commit, run.coverage)
    if delta.ratio >= harness.coverage_floor:
        return GateResult(name=GateName.COVERAGE_DELTA, status=GateStatus.PASSED)

    reasons = [
        _reason(
            "coverage_below_floor",
            f"{delta.ratio:.3f} < {harness.coverage_floor:.3f} "
            f"({delta.covered}/{delta.considered} changed lines covered)",
        )
    ]
    # Naming the lines is the difference between an actionable failure and a
    # number the agent cannot act on (section 5.5).
    shown = 0
    for path in sorted(delta.uncovered):
        for line in delta.uncovered[path]:
            if shown >= _MAX_UNCOVERED_LINES:
                break
            reasons.append(_reason("uncovered_line", f"{path}:{line}"))
            shown += 1

    return GateResult(name=GateName.COVERAGE_DELTA, status=GateStatus.FAILED, reasons=reasons)


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
    harness: HarnessConfig | None = None,
    criteria: Sequence[AcceptanceCriterion] | None = None,
) -> Proof:
    """Run the gate pipeline in order, short-circuiting on first failure.

    Gates after a failure are recorded ``skipped`` **and never omitted**
    (section 7.1): the proof has to show the full contract including what was
    not reached, or an omitted gate is indistinguishable from a gate that does
    not exist.

    ``harness`` and ``criteria`` are the inputs gates 3-6 cannot invent.
    Section 7.3 forbids the verifier from making up commands -- they come from
    configuration only -- and the inherited criteria are resolved from the
    graph by the caller, which the verifier does not read. Without them those
    gates are recorded ``skipped`` with a reason saying so.

    **That default is a footgun worth naming.** A caller who forgets the
    harness gets a FAIL with four skipped gates rather than an error. It fails
    safe -- a proof can never claim a green it did not earn -- but the
    orchestrator must always pass a harness, and nothing here forces it to.

    The verdict is PASS only when every gate in the contracted list actually
    passed. A skipped gate is not a passed gate.
    """
    results: list[GateResult] = []
    short_circuited = False
    # The suite runs at most once and gates 4-6 share the result. Three
    # separate runs could disagree, and a proof assembled from three different
    # executions is not evidence about any one of them.
    harness_run: HarnessRun | None = None

    for name in GATE_ORDER:
        if short_circuited:
            results.append(
                GateResult(name=name, status=GateStatus.SKIPPED, reasons=[_SKIPPED_REASON])
            )
            continue

        if name in _NEEDS_HARNESS and harness is None:
            results.append(
                GateResult(name=name, status=GateStatus.SKIPPED, reasons=[_NO_HARNESS_REASON])
            )
            continue

        if name is GateName.SCOPE_CHECK:
            result = scope_check(repo_root, task=task, claim=claim, declared=declared)
        elif name is GateName.FROZEN_HASH_CHECK:
            result = frozen_hash_check(repo_root, task=task, claim=claim)
        else:
            assert harness is not None  # noqa: S101 - guarded by _NEEDS_HARNESS above
            if name is GateName.LINT:
                result = lint_check(repo_root, task=task, harness=harness)
            else:
                if harness_run is None:
                    harness_run = run_harness(
                        repo_root,
                        harness=harness,
                        with_coverage=task.type == "implementation",
                    )
                if name is GateName.TESTS_PASS:
                    result = tests_pass_check(task, harness_run)
                elif name is GateName.CRITERIA_COVERAGE:
                    result = criteria_coverage_check(task, criteria or [], harness_run)
                else:
                    result = coverage_delta_check(
                        repo_root, task=task, claim=claim, harness=harness, run=harness_run
                    )

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
