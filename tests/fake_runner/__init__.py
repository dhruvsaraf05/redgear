"""A deterministic stand-in for an agent CLI (CLAUDE.md section 10.5).

This is the primary test harness for everything downstream of T-0026. It
implements the ``Runner`` protocol, never spawns a process, and never touches
a network. It applies a canned patch to the working tree and returns a canned
``TurnResult``.

That is what makes the orchestrator's whole test suite deterministic, free and
fast: §10.4 forbids a test from calling a real agent CLI or a model, because a
suite that needs a credential stops working the moment someone else clones the
repository, and one that calls a model is not reproducible.
"""

from __future__ import annotations

from .runner import DispatchCall, FakeRunner
from .scenarios import ALL_SCENARIOS, SCENARIOS, FileEdit, Scenario

__all__ = [
    "ALL_SCENARIOS",
    "SCENARIOS",
    "DispatchCall",
    "FakeRunner",
    "FileEdit",
    "Scenario",
]
