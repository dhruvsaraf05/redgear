"""The RedgearError hierarchy and its code registry.

Leaf module (CLAUDE.md section 2.2): imports nothing else from ``redgear``.

Every code here appears in CLAUDE.md section 4.7's normative table. Section
4.7 is closed: a new code is an ADR-worthy change, because this module is the
single import point every other module resolves failures through.

Section 4.7 declares the full taxonomy, including codes whose raising module
does not exist yet. ``ERROR_CODES`` registers the *implemented* subset -- a
code joins it when the module that raises it lands. Registering a code with
no raiser would let ``deserialize_error`` mint an exception nothing can
produce, which is worse than a KeyError naming the gap.

Section 11.2 rule 4: structured errors carrying a code and a ``detail``
mapping, never a bare message string, and never a traceback escaping to a
user.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

#: JSON-shaped detail values. Spelled out recursively rather than as ``Any``,
#: which section 11.2 rule 1 permits only in ``hashing.canonical_json``.
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


class RedgearError(Exception):
    """Base of every error the engine surfaces.

    A caller can always ``except RedgearError`` to catch the whole family.
    Subclasses set ``code``; this base deliberately does not appear in
    ``ERROR_CODES`` and is never raised directly.
    """

    #: Overridden by every concrete subclass. Section 4.7 owns the vocabulary.
    code: ClassVar[str] = "E_UNSPECIFIED"

    def __init__(self, message: str, detail: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__(message)
        self.message = message
        # Copied, never aliased: two errors constructed from the same literal
        # must not share one mapping, and a caller mutating what it passed in
        # must not reach inside an already-raised error.
        self.detail: dict[str, JsonValue] = dict(detail) if detail is not None else {}

    def to_dict(self) -> dict[str, JsonValue]:
        """A JSON-shaped payload: no exception objects, no tracebacks.

        This is what an event record, a log line, or a CLI surface carries.
        ``deserialize_error`` is its inverse.
        """
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


# --- Plan and spec ---------------------------------------------------------


class PlanUnreviewedError(RedgearError):
    """`run` invoked on a `draft` graph (section 3.3)."""

    code: ClassVar[str] = "E_PLAN_UNREVIEWED"


class SpecDriftError(RedgearError):
    """A task's stored `spec_hash` differs from current (section 3.5)."""

    code: ClassVar[str] = "E_SPEC_DRIFT"


class PlanInvalidError(RedgearError):
    """A plan produced a task the engine cannot execute as written.

    Section 4.7 files this under "a generated plan fails a section 4.4
    invariant". It also covers the section 5.6 case: a task whose prompt
    exceeds the character cap is a task too large to brief, and the contract
    is explicit that this "is a signal the task is too large. Surface it as a
    planning problem, do not silently truncate the scope section" -- a prompt
    quietly shortened to fit would drop the very frozen paths whose absence
    causes the scope violation.
    """

    code: ClassVar[str] = "E_PLAN_INVALID"


# --- Graph and state -------------------------------------------------------


class GraphCycleError(RedgearError):
    """The edge set is not acyclic (section 4.4 invariant 1)."""

    code: ClassVar[str] = "E_GRAPH_CYCLE"


class UnknownNodeRefError(RedgearError):
    """A `depends_on` entry or edge endpoint names no existing node.

    Section 4.4 invariant 2, which section 4.7 files under the broader
    `E_GRAPH_INVALID` -- "a section 4.4 invariant other than acyclicity
    fails".
    """

    code: ClassVar[str] = "E_GRAPH_INVALID"


class ScopeOverlapError(RedgearError):
    """Writable and frozen globs overlap (section 4.4 invariant 7)."""

    code: ClassVar[str] = "E_SCOPE_CONTRADICTION"


class TaskStateError(RedgearError):
    """An illegal transition for the task's current state (section 4.2)."""

    code: ClassVar[str] = "E_TASK_STATE"


# --- Locking and concurrency ----------------------------------------------


class RunLockedError(RedgearError):
    """A second run starts while the run lock is held."""

    code: ClassVar[str] = "E_RUN_LOCKED"


# --- Repository and environment -------------------------------------------


class DirtyTreeError(RedgearError):
    """Uncommitted changes at claim time (section 8.4)."""

    code: ClassVar[str] = "E_DIRTY_TREE"


class NotAGitRepoError(RedgearError):
    """The target directory is not a git repository (section 8.4)."""

    code: ClassVar[str] = "E_NOT_A_REPO"


class AlreadyInitializedError(RedgearError):
    """`redgear init` run over an existing `.redgear/` (section 9)."""

    code: ClassVar[str] = "E_ALREADY_INITIALIZED"


class RunnerError(RedgearError):
    """The agent CLI integration is broken (section 6.4 rule 4).

    Reserved for the *adapter* failing, never for a task failing. A turn that
    went badly is still a ``TurnResult`` -- the orchestrator decides what a
    failure means and needs the record either way. This is raised when there
    is no usable result at all: the CLI could not launch, or its output could
    not be parsed twice running.

    That distinction is load-bearing. Charging a task's attempt budget for the
    integration's problem is how a perfectly good agent gets escalated.
    """

    code: ClassVar[str] = "E_RUNNER_ERROR"


class RunnerTimeoutError(RedgearError):
    """A dispatch exceeded its wall clock (section 6.5)."""

    code: ClassVar[str] = "E_RUNNER_TIMEOUT"


class UnsafeHarnessCommandError(RedgearError):
    """A configured harness command is rejected or fails to launch.

    Section 7.3 rejects any `cmd` containing `..`; section 4.7 files both
    that rejection and a launch failure under `E_HARNESS_ERROR`.
    """

    code: ClassVar[str] = "E_HARNESS_ERROR"


# --- Audit integrity -------------------------------------------------------


class EventLogCorruptError(RedgearError):
    """A gap or repeat in event `seq` (FR-1). Never auto-repaired."""

    code: ClassVar[str] = "E_LOG_CORRUPT"


class ProjectionMismatchError(RedgearError):
    """Rebuild differs from the on-disk projection (G4). Surfaced loudly."""

    code: ClassVar[str] = "E_PROJECTION_DIVERGED"


#: Code -> class, for resolving a serialized error back into an exception.
#: A plain inspectable dict, not a function: every later module (locks.py,
#: state_engine.py, verifier.py, cli.py) reads it directly.
ERROR_CODES: dict[str, type[RedgearError]] = {
    cls.code: cls
    for cls in (
        PlanUnreviewedError,
        SpecDriftError,
        PlanInvalidError,
        GraphCycleError,
        UnknownNodeRefError,
        ScopeOverlapError,
        TaskStateError,
        RunLockedError,
        DirtyTreeError,
        NotAGitRepoError,
        AlreadyInitializedError,
        RunnerError,
        RunnerTimeoutError,
        UnsafeHarnessCommandError,
        EventLogCorruptError,
        ProjectionMismatchError,
    )
}


def deserialize_error(payload: Mapping[str, JsonValue]) -> RedgearError:
    """Rebuild an error from ``to_dict`` output.

    An unregistered code raises ``KeyError`` rather than degrading to a
    generic ``RedgearError``: silently widening the type would hide that the
    registry and the serialized data have drifted apart, which is exactly the
    kind of divergence G4 requires be surfaced rather than smoothed over.
    """
    code = payload["code"]
    if not isinstance(code, str):
        raise ValueError(f"error payload has a non-string code: {code!r}")
    cls = ERROR_CODES[code]

    message = payload.get("message", "")
    if not isinstance(message, str):
        raise ValueError(f"error payload has a non-string message: {message!r}")

    detail = payload.get("detail", {})
    if not isinstance(detail, dict):
        raise ValueError(f"error payload has a non-mapping detail: {detail!r}")

    return cls(message, detail=detail)
