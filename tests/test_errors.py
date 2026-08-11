"""T-0004: failing tests for redgear.errors -- the RedgearError hierarchy.

``redgear/errors.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state
(CLAUDE.md section 10.4 / this task's own instructions): no try/except, no
``importorskip``, no mock standing in for the module.

## The error-code set is not exhaustively documented anywhere -- flagged

CLAUDE.md names exactly three codes literally, as ``E_``-prefixed tokens in
running prose:

* ``E_PLAN_UNREVIEWED`` -- section 3.3: "`redgear run` refuses to start on a
  `draft` graph with `E_PLAN_UNREVIEWED`."
* ``E_SPEC_DRIFT`` -- section 3.5: "Refuse to serve drifted tasks; return
  `E_SPEC_DRIFT` naming them."
* ``E_DIRTY_TREE`` -- section 8.4: "Refuse to start on a dirty tree
  (`E_DIRTY_TREE`), listing the offending paths."

Everything else in the set below is *constructed* by this file from clearly
stated refuse/reject/fail-loudly conditions elsewhere in the contract, not
from a literal ``E_`` token in the text. Each constructed code is cited at
its class definition site in ``redgear/errors.py`` once T-0005 writes it, and
listed here so the provenance is auditable:

* ``E_NOT_A_GIT_REPO`` -- section 8.4 ("Refuse to start outside a git
  repository") + section 9's `redgear init` row ("...or the tree is not a
  git repo").
* ``E_ALREADY_INITIALIZED`` -- section 9's `redgear init` row ("Refuses if
  it exists...").
* ``E_GRAPH_CYCLE`` -- section 4.4 invariant 1 / FR-2 acceptance ("A cycle
  in the edge set is rejected with the offending cycle named").
* ``E_UNKNOWN_NODE_REF`` -- section 4.4 invariant 2 / FR-2 acceptance
  ("Every depends_on entry and edge endpoint refers to an existing node").
* ``E_SCOPE_OVERLAP`` -- section 4.4 invariant 7, and this task's own AC-3
  (``tests/test_paths.py::test_scope_overlap_detected``) -- the most
  directly load-bearing of the constructed set, since paths.py raises it.
* ``E_EVENT_LOG_CORRUPT`` -- FR-1 acceptance ("A gap or repeat in sequence
  numbers is reported as corruption and never auto-repaired").
* ``E_PROJECTION_MISMATCH`` -- section 9's `redgear rebuild` row ("fail
  loudly on mismatch") + section 4.3's `engine_error` termination reason.
* ``E_RUN_LOCKED`` -- T-0016 AC-3 ("A second run in the same repository is
  refused while a run lock is held").
* ``E_UNSAFE_HARNESS_COMMAND`` -- section 7.3 ("Reject any configured `cmd`
  containing `..`").

**This needs to become a normative table like section 3.6, the same class of
gap as the pre-section-3.6 event taxonomy and the pre-section-3.5 hash
algorithm** -- flagged in this task's turn report for confirmation before
later modules (locks.py, state_engine.py, verifier.py, cli.py) each need a
subset of this set to exist with these exact names.
"""

from __future__ import annotations

import pytest
from redgear.errors import (
    ERROR_CODES,
    AlreadyInitializedError,
    DirtyTreeError,
    EventLogCorruptError,
    GraphCycleError,
    NotAGitRepoError,
    PlanUnreviewedError,
    ProjectionMismatchError,
    RedgearError,
    RunLockedError,
    ScopeOverlapError,
    SpecDriftError,
    UnknownNodeRefError,
    UnsafeHarnessCommandError,
    deserialize_error,
)

ALL_ERROR_CLASSES: list[type[RedgearError]] = [
    PlanUnreviewedError,
    SpecDriftError,
    DirtyTreeError,
    NotAGitRepoError,
    AlreadyInitializedError,
    GraphCycleError,
    UnknownNodeRefError,
    ScopeOverlapError,
    EventLogCorruptError,
    ProjectionMismatchError,
    RunLockedError,
    UnsafeHarnessCommandError,
]

# Self-checking rather than a magic number: fails loudly if a class is
# added to redgear.errors without also being added to ALL_ERROR_CLASSES
# above (or vice versa), instead of silently under-covering the set.
EXPECTED_CODE_COUNT = 12


def test_all_codes_have_classes() -> None:
    """AC-1: every documented code maps to exactly one exception subclass,
    every subclass carries its code as a class attribute, codes are
    unique, and all inherit from a single RedgearError base."""
    assert len(ALL_ERROR_CLASSES) == EXPECTED_CODE_COUNT

    codes = [cls.code for cls in ALL_ERROR_CLASSES]
    assert len(codes) == len(set(codes)), f"duplicate codes: {codes}"

    for cls in ALL_ERROR_CLASSES:
        assert issubclass(cls, RedgearError)
        assert cls is not RedgearError
        assert isinstance(cls.code, str)
        assert cls.code.startswith("E_")

    assert set(ERROR_CODES.keys()) == set(codes), (
        "the ERROR_CODES registry must map every code to its class, and contain nothing else"
    )
    for code, cls in ERROR_CODES.items():
        assert cls.code == code

    instance = PlanUnreviewedError("draft graph", detail={"graph_state": "draft"})
    assert instance.code == "E_PLAN_UNREVIEWED"
    assert PlanUnreviewedError.code == "E_PLAN_UNREVIEWED"


def test_all_error_classes_inherit_redgear_error() -> None:
    """A caller can catch broadly: `except RedgearError` must work for
    every one of them."""
    for cls in ALL_ERROR_CLASSES:
        with pytest.raises(RedgearError):
            raise cls("boom", detail={})


def test_error_carries_structured_detail() -> None:
    """section 11.2: structured errors, not just a message string --
    detail is a real mapping the caller can serialise, not something
    baked only into the human-readable text."""
    err = ScopeOverlapError(
        "writable and frozen globs overlap",
        detail={"overlaps": ["redgear/__init__.py", "redgear/py.typed"]},
    )
    assert isinstance(err.detail, dict)
    assert err.detail["overlaps"] == ["redgear/__init__.py", "redgear/py.typed"]
    assert str(err) == "writable and frozen globs overlap"


def test_detail_defaults_to_empty_mapping() -> None:
    """An error raised without detail must not require the caller to
    guess -- it gets a real (empty) mapping, never None."""
    err = NotAGitRepoError("not a git repository")
    assert err.detail == {}
    assert isinstance(err.detail, dict)


@pytest.mark.parametrize("cls", ALL_ERROR_CLASSES)
def test_round_trip_preserves_code_and_detail(cls: type[RedgearError]) -> None:
    """Round-tripping an error to a serialisable form and back preserves
    code and detail -- required so redact.py / event records / a CLI
    surface can carry an error without losing what it means (section 9's
    `redgear log`, section 11.2's "never let a traceback escape")."""
    original = cls("something went wrong", detail={"path": "a/b/c", "n": 3})

    payload = original.to_dict()
    assert payload["code"] == cls.code
    assert payload["message"] == "something went wrong"
    assert payload["detail"] == {"path": "a/b/c", "n": 3}

    # The payload must itself be JSON-shaped -- no exception objects,
    # no non-primitive values smuggled through.
    import json

    reserialized = json.loads(json.dumps(payload))
    assert reserialized == payload

    rebuilt = deserialize_error(payload)
    assert type(rebuilt) is cls
    assert rebuilt.code == original.code
    assert rebuilt.detail == original.detail
    assert str(rebuilt) == str(original)


def test_deserialize_unknown_code_raises() -> None:
    """A payload citing a code with no registered class must not silently
    become a generic RedgearError -- that would hide the fact that the
    registry and the serialized data have drifted apart."""
    with pytest.raises((KeyError, ValueError)):
        deserialize_error({"code": "E_NOT_A_REAL_CODE", "message": "?", "detail": {}})


def test_error_codes_registry_type() -> None:
    """ERROR_CODES is the mechanism every future module (locks.py,
    state_engine.py, verifier.py, cli.py) uses to resolve a code back to
    a class -- it must be a plain, inspectable mapping, not a function."""
    assert isinstance(ERROR_CODES, dict)
    for code, cls in ERROR_CODES.items():
        assert isinstance(code, str)
        assert isinstance(cls, type)
        assert issubclass(cls, RedgearError)


def test_detail_is_not_shared_mutable_default() -> None:
    """Two independently constructed errors must not share one detail dict
    -- a classic mutable-default bug that would let one caller's detail
    leak into another's."""
    first = RunLockedError("locked", detail={"holder": "run_01"})
    second = RunLockedError("locked", detail={"holder": "run_02"})
    first.detail["mutated"] = True
    assert "mutated" not in second.detail


def test_specific_codes_match_literal_claude_md_tokens() -> None:
    """The three codes CLAUDE.md names verbatim (sections 3.3, 3.5, 8.4)
    must use exactly that spelling -- these three are not this file's
    invention, unlike the other nine in the set."""
    assert PlanUnreviewedError.code == "E_PLAN_UNREVIEWED"
    assert SpecDriftError.code == "E_SPEC_DRIFT"
    assert DirtyTreeError.code == "E_DIRTY_TREE"


def test_message_is_not_swallowed_by_str() -> None:
    """Exception.__str__ must return the human-readable message, not a
    repr of the detail mapping -- section 11.2: never leak a traceback,
    but do not lose the message either."""
    err = UnsafeHarnessCommandError(
        "harness command contains a path-escape sequence",
        detail={"cmd": ["pytest", "../../etc/passwd"]},
    )
    assert str(err) == "harness command contains a path-escape sequence"
    assert "cmd" not in str(err)
