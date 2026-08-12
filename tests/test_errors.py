"""Tests for redgear.errors -- the RedgearError hierarchy and its registry.

Originally T-0004's failing tests, written before ``redgear/errors.py``
existed. The module now exists (T-0005) and these run against it.

## History worth keeping

When this file was written, CLAUDE.md named only three error codes literally
-- ``E_PLAN_UNREVIEWED`` (§3.3), ``E_SPEC_DRIFT`` (§3.5) and ``E_DIRTY_TREE``
(§8.4) -- and the rest had to be constructed from refuse/reject/fail-loudly
conditions scattered through the contract. That gap was reported and closed:
**§4.7 is now the normative, closed table of twenty codes**, and
``_section_4_7_codes`` below parses it directly so the registry cannot drift
from it.

## What the registry assertion means

``ERROR_CODES`` is a **subset** of §4.7, not an exact match. §4.7 is the
closed design; the registry is what is implemented so far, and it grows as
each raising module lands. Registering a code whose raiser does not exist
would let ``deserialize_error`` mint an exception nothing can produce.

This assertion previously demanded exact equality at twelve entries, which
capped the registry permanently and blocked ``E_TASK_STATE`` (needed by
T-0015) from ever being registered. Corrected under explicit authorization;
see ``docs/PROGRESS.md``.
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


def _section_4_7_codes() -> frozenset[str]:
    """The codes CLAUDE.md section 4.7 declares, read from the contract.

    Parsed rather than duplicated so the registry cannot drift from the
    normative table. The length is asserted at the call site so a change to
    the table's formatting fails loudly instead of silently yielding an
    empty set that every subset check would then pass.
    """
    import re
    from pathlib import Path as _Path

    text = (_Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    block = text[text.index("### 4.7 Error codes") : text.index("## 5. The prompt engine")]
    return frozenset(re.findall(r"^\| `(E_[A-Z_]+)` \|", block, re.M))


def test_all_codes_have_classes() -> None:
    """AC-1: every registered code maps to exactly one exception subclass,
    every subclass carries its code as a class attribute, codes are unique,
    and all inherit from a single RedgearError base.

    The registry is a **subset** of section 4.7, not an exact match. Section
    4.7 is the closed design; ``ERROR_CODES`` is what is implemented so far,
    and it grows as each raising module lands. An exact-equality assertion
    here would either force every code into existence before its module
    does, or permanently cap the registry -- and the cap is what previously
    blocked E_TASK_STATE and E_NO_READY_TASK from being registered at all.
    """
    documented = _section_4_7_codes()
    assert len(documented) == 20, f"section 4.7 table did not parse as expected: {documented}"

    codes = [cls.code for cls in ALL_ERROR_CLASSES]
    assert len(codes) == len(set(codes)), f"duplicate codes: {codes}"

    for cls in ALL_ERROR_CLASSES:
        assert issubclass(cls, RedgearError)
        assert cls is not RedgearError
        assert isinstance(cls.code, str)
        assert cls.code.startswith("E_")

    # Every originally-asserted class is still registered.
    assert set(codes) <= set(ERROR_CODES), (
        f"classes missing from ERROR_CODES: {sorted(set(codes) - set(ERROR_CODES))}"
    )
    # Nothing is registered that section 4.7 does not declare -- section 4.7
    # is closed, so an unlisted code means someone invented one.
    assert set(ERROR_CODES) <= documented, (
        f"registered codes absent from section 4.7: {sorted(set(ERROR_CODES) - documented)}"
    )
    # Exactly one class per code, both directions.
    assert len(set(ERROR_CODES.values())) == len(ERROR_CODES), "two codes share one class"
    for code, cls in ERROR_CODES.items():
        assert cls.code == code

    instance = PlanUnreviewedError("draft graph", detail={"graph_state": "draft"})
    assert instance.code == "E_PLAN_UNREVIEWED"
    assert PlanUnreviewedError.code == "E_PLAN_UNREVIEWED"


def test_task_state_error_is_registered() -> None:
    """E_TASK_STATE is raised by state_engine's write path, so it must
    resolve through the registry like any other -- this is the case the old
    exact-count assertion made impossible."""
    from redgear.errors import TaskStateError

    assert ERROR_CODES["E_TASK_STATE"] is TaskStateError

    rebuilt = deserialize_error(
        {"code": "E_TASK_STATE", "message": "illegal transition", "detail": {"task_id": "T-0002"}}
    )
    assert type(rebuilt) is TaskStateError
    assert rebuilt.code == "E_TASK_STATE"
    assert rebuilt.detail == {"task_id": "T-0002"}


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
