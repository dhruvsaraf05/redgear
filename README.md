# redgear

**Status: pre-alpha. No working code exists yet.** This repository currently
contains `T-0001` of 41 in the build plan — packaging, tooling, CI, and
secret hygiene scaffolding. There is no orchestrator, no verification
harness, and no `redgear` command that does anything yet.

## What redgear is

redgear is an autonomous orchestrator that plans software projects,
generates task prompts, drives a coding agent CLI (Claude Code is the
reference implementation) in a continuous verified loop, and refuses to
accept unproven work.

It does not write code itself. It decides what should be worked on next,
composes the prompt that says so, dispatches it to an agent CLI, and
independently verifies the result — running the real test suite, the real
linter, and a real diff audit itself, rather than trusting anything the
agent claims. Continuously, without a human relaying messages between
turns.

## The seven guarantees

Every design decision in this project is traceable to one of these:

1. **G1 — Independent verification.** The orchestrator runs the test suite,
   linter, and coverage tooling itself. No field an agent emits ever
   influences a gate verdict.
2. **G2 — Two-phase scope freeze.** Test-authoring and implementation are
   separate task types with mutually exclusive, SHA-256-hash-enforced write
   scopes. No agent grades its own homework.
3. **G3 — Honest exit.** An agent can declare itself blocked or
   under-scoped without penalty. Only silence and false claims cost an
   attempt.
4. **G4 — Event sourcing.** `.redgear/events.jsonl` is the sole
   append-only source of truth; every other state file is a reconstructible
   projection.
5. **G5 — No credentials, no direct inference.** redgear never calls a
   model API, never holds a credential, and never opens a socket. All
   inference is delegated to the agent CLI subprocess.
6. **G6 — Bounded autonomy.** Every run carries hard iteration, wall-clock,
   and failure caps, honors an out-of-band stop signal, and never commits,
   pushes, or rewrites history in the target repository.
7. **G7 — Untrusted-input containment.** Harness output, diffs, and source
   documents are treated as data to diagnose, never as instructions, and
   are explicitly delimited in every prompt.

## The plan

The full build plan — 41 tasks, 49 edges, 22 requirements — lives in
[`.redgear/task_graph.json`](.redgear/task_graph.json) and
[`.redgear/spec/spec.json`](.redgear/spec/spec.json). The architectural
contract for this repository is [`CLAUDE.md`](CLAUDE.md); read it before
touching any code here.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff format --check .
ruff check .
mypy
pytest -q
```
