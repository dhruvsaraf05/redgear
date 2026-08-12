# PROGRESS

**Live handoff document.** Read this after `CLAUDE.md` when starting a session
with no prior context. It records where the build is, decisions that are not
derivable from the contract, traps already hit, and open questions.

Keep it **current**, not cumulative. Update entries in place; delete what stops
being true. This is not a changelog — git history is the changelog.

*Last updated: after T-0015 (state_engine write path).*

---

## 1. Where we are

**Milestone 2 complete.** `T-0001` through `T-0015` are done. Next up is
**`T-0016`/`T-0017` — `locks.py`** (exclusive task leases and the single-run
lock).

| | |
| --- | --- |
| Main is | **green** |
| Test suite | **124 passed, 1 skipped** |
| The skip | `test_gitleaks_clean` — the `gitleaks` binary is not on PATH locally. Its pre-commit config is still asserted. CI runs the real scan. |
| Modules built | `schemas`, `errors`, `paths`, `hashing`, `redact`, `events`, `state_engine` |
| Not yet built | `locks`, `budget`, `gitctx`, `verifier`, `runner`, `prompt_engine`, `orchestrator`, `cli`, `planner`, `api/`, `ui/` |
| Crossover | `T-0033` (`cli.py`). Until then every task is driven manually by a human running Claude Code. |

### Task status

`scaf` = scaffold, `test` = test_authoring, `impl` = implementation.

| Task | Type | Status | Subject |
| --- | --- | --- | --- |
| T-0001 | scaf | done | repo bootstrap: packaging, tooling, CI, secret hygiene |
| T-0002/03 | test/impl | done | `schemas.py` — every Pydantic model |
| T-0004/05 | test/impl | done | `errors.py` + `paths.py` — error codes, glob resolution |
| T-0006/07 | test/impl | done | `hashing.py` — canonical JSON, content addressing |
| T-0008/09 | test/impl | done | `redact.py` — credential redaction |
| T-0010/11 | test/impl | done | `events.py` — append-only log, gapless sequencing |
| T-0012/13 | test/impl | done | `state_engine.py` read path — load, validate, project |
| T-0014/15 | test/impl | done | `state_engine.py` write path — transitions, atomic persistence |
| **T-0016/17** | test/impl | **next** | `locks.py` — task leases, single-run lock |
| T-0018/19 | test/impl | todo | `budget.py` — autonomy caps, STOP sentinel, signals |
| T-0020/21 | test/impl | todo | `gitctx.py` — read-only git interrogation, diff parsing |
| T-0022/23 | test/impl | todo | `verifier.py` gates 1–2 — scope, frozen hash |
| T-0024/25 | test/impl | todo | `verifier.py` gates 3–6 — lint, tests, criteria, coverage |
| T-0026/27 | test/impl | todo | runner protocol + deterministic fake runner |
| T-0028/29 | test/impl | todo | `prompt_engine.py` — **highest risk**, snapshots mandatory |
| T-0030/31 | test/impl | todo | `orchestrator.py` — the continuous loop |
| T-0032/33 | test/impl | todo | `cli.py` — full command surface (**self-hosting crossover**) |
| T-0034/35 | test/impl | todo | `runner.py` — Claude Code headless adapter (needs an agent CLI) |
| T-0036/37 | test/impl | todo | `planner.py` — plan generation + approval gate (needs an agent CLI) |
| T-0038/39 | test/impl | todo | `api/app.py` — read-only control plane |
| T-0040 | scaf | todo | control plane UI |
| T-0041 | scaf | todo | packaging and release |

---

## 2. Decisions taken (with reasoning)

These are not derivable from `CLAUDE.md` alone. Each records *why*, because the
conclusion without the reasoning invites someone to "fix" it back.

### §4.7 declares 20 codes; `ERROR_CODES` holds fewer

§4.7 is the **closed design**. `ERROR_CODES` is the **implemented subset**, and
it grows as each raising module lands.

Registering a code whose raising module does not exist yet would let
`deserialize_error` mint an exception nothing in the tree can produce — a
failure mode that looks like working code. So a code joins the registry when
its module does.

`tests/test_errors.py` enforces this as a **subset** relationship: every
registered code must appear in §4.7 (parsed from `CLAUDE.md`, so it cannot
drift), every code maps to exactly one class, codes are unique. It originally
asserted exact equality at twelve, which capped the registry permanently and
blocked `E_TASK_STATE` — that was corrected (see §4).

### `replay()` folds events onto the plan; it does not reconstruct structure

**No event carries node or edge definitions.** `plan_generated` records the
plan's hash and shape (`spec_hash`, `node_count`, `edge_count`,
`source_document`), not its contents.

This is deliberate, and §3.6 now says so: duplicating the graph into the log
would create two sources of truth for structure and guarantee they drift. The
plan is a separate content-addressed artifact; the log records what *happened
to* it.

So the signature is `replay_graph(definition, events)`. G4 in §1.4 was amended
to match: the plan is the input, mutable task state (`state`, `attempts`,
`claim`, `prior_attempts`, `verified_at`, `proof_id`, `escalation`) is what
replay reconstructs. `rebuild` compares both — structure divergence means the
plan was edited out of band, state divergence means an engine bug.

### `E_NO_READY_TASK` is deferred, not implemented

§4.1's normative loop reads `task = state.next_ready_task(repo)` / `if task is
None:` and ends the run `complete_or_blocked`. That treats exhaustion as a
**normal terminal condition**. §4.7 lists `E_NO_READY_TASK`, which treats it as
an **error**. They disagree.

`next_ready_task` currently returns `None`, matching §4.1. Resolving this needs
a decision about which reading is right, and that belongs with the orchestrator
at **T-0030** — not with the state engine, which has no opinion about how a run
ends. Do not implement it earlier just because the code exists in §4.7.

### Python is 3.12; the spec still says 3.11 (**unresolved** — see §5)

`pyproject.toml`, the ruff target, mypy, the CI matrix and `CLAUDE.md` §1.1 all
say **3.12**. Local development runs **3.14**.

The 3.11 floor was dropped because nothing ever ran against it — a floor no
build exercises is a promise nothing checks.

`.redgear/spec/spec.json` `NFR-10` still says 3.11 and is **not** edited: it is
content-addressed, and changing it moves `sha256:dd2914…` and marks all 41
nodes `spec_drift` (§3.5).

### Branches were abandoned; work commits directly on main

§4.6.1 prescribes one branch per task pair. That was tried and abandoned after
it produced more failure than it prevented:

- A PR from a branch carrying only the `test_authoring` half triggers CI on a
  deliberately-red tree. §4.6.1 says open the PR only once the pair is green,
  which means the branch buys nothing during the pair and only adds a merge.
- A GitHub rebase-and-merge rewrote the SHAs of three commits, producing a
  conflict against local `main` where **every remote commit was a byte-identical
  copy of a local one**. Pure churn, zero content.

Work now happens on `main`, one commit per completed (green) pair.
**§4.6.1 is stale on this point and has not been rewritten.**

### Claude Code does not commit

The human commits, every time. This is G6 (`redgear` never commits in the target
repo) applied to the humans-driving-Claude-Code phase. Claude Code leaves work
uncommitted and reports what changed plus suggested commit messages.

---

## 3. Traps already hit

Each cost real debugging time. Symptom first, so it is recognisable.

### Windows `O_CREAT|O_EXCL` raises `PermissionError`, not `FileExistsError`

**Symptom:** concurrent `events.append()` fails on Windows with
`PermissionError(13)` while passing on Linux.

**Cause:** when a lock file exists but is *pending deletion* — exactly the
window a releasing writer sits in — Windows returns `ERROR_ACCESS_DENIED`, not
`EEXIST`.

**Fix:** the acquire loop catches `(FileExistsError, PermissionError)` and
retries. See `events._append_lock`.

### ruff's warm cache masks I001 failures

**Symptom:** `ruff check .` passes locally, fails in CI, on files nobody
touched.

**Cause:** `.ruff_cache` holds stale per-file results. A fresh CI checkout has
no cache and re-evaluates everything. This once hid an import-order violation in
**five** test files, including one that had been "green" on main for several
commits.

**Fix:** always run `ruff check . --no-cache`. Treat a cached pass as unproven.

### isort classification flips between Phase 1 and Phase 2

**Symptom:** `ruff check --fix` in Phase 1 produces an import order that becomes
an I001 error in Phase 2 — on a file that is frozen by then and cannot be
corrected.

**Cause:** ruff infers first-party from the filesystem. In Phase 1 the module
under test does not exist, so `from redgear.x import ...` looks **third-party**;
in Phase 2 it exists and becomes **first-party**. The two phases therefore
disagree about correct ordering, and the workflow guarantees both happen.

**Fix:** `known-first-party = ["redgear"]` in `[tool.ruff.lint.isort]` pins
classification regardless of what exists on disk. Also present:
`no-lines-before = ["first-party", "local-folder"]`, so `import pytest` may sit
directly above `from redgear.x import ...`.

**Still do this:** in Phase 1, write `redgear.*` imports in final alphabetical
order and do not rely on `--fix` to arrange them.

### `ruff format .` repo-wide rewrites frozen test files

**Symptom:** `git diff --stat -- tests/` is non-empty after an implementation
phase, failing the scope check even though no test was edited deliberately.

**Fix:** scope it — **`ruff format redgear/`**. Never repo-wide during an
implementation phase. If a test file legitimately needs formatting, format that
one file explicitly and only under authorization.

### Local Python is 3.14; CI runs 3.12

Behaviour can differ. Anything version-sensitive — new syntax, stdlib
signatures, deprecation warnings — must be checked against 3.12, not assumed
from a local pass. Pinning exact dev-tool versions in `pyproject.toml` closed the
*tooling* half of this (CI was resolving newer ruff/mypy than local), but not the
interpreter half.

### `list` is invariant; `list[str]` does not satisfy `list[JsonValue]`

**Symptom:** mypy rejects `detail={"k": sorted(x)}`; rewriting as a
comprehension then trips ruff `C416` (unnecessary comprehension). A genuine
catch-22.

**Fix:** build it explicitly —
`v: list[JsonValue] = []` then `v.extend(...)`. Satisfies both.

---

## 4. Frozen-file edits made under human authorization

`tests/**` is frozen to Claude Code during an implementation phase (G2). The
**only** legitimate way to edit one is an explicit human instruction in-session
(§0.1 precedence). This is the audit trail for that escape hatch. Every entry
names the defect that justified it.

| File | Edit | Defect that justified it |
| --- | --- | --- |
| `tests/test_state_read.py` | Rooted the "nothing ready" fixture at an `escalated` node | The test built a graph whose root had `depends_on=[]`, then asserted nothing was ready. A node with zero dependencies is **vacuously ready** under §4.4 invariant 3 — the real graph's `T-0001` ships exactly that way. The test contradicted its own sibling `test_recompute_on_real_graph_is_a_fixed_point`. |
| `tests/test_events.py` | Reordered the import block (`errors` before `events`) | Written under the pre-`known-first-party` classification, so the order was invalid once `redgear.events` existed. Pinning classification could not retroactively repair an order already baked in. |
| `tests/test_state_write.py` | Deleted one dead line, `monkeypatch.setattr(target, "name", ...)` | `Path.name` is a read-only property, so the call could never succeed; `raising=False` suppresses only *missing* attributes. The line was vestigial — the next line does the actual restore. Verified by mutation test that the remaining assertions still catch a truncate-and-write implementation. |
| `tests/test_errors.py` | Exact-count registry assertion → subset relationship; registered `TaskStateError` | The assertion capped `ERROR_CODES` at twelve forever, which blocked `E_TASK_STATE` (needed by T-0015) and would have blocked every future error type. It was simply the wrong assertion — the registry is meant to grow. |

`pyproject.toml` and `.github/**` are frozen to task work but were edited twice
under explicit authorization: to pin exact dev-tool versions and narrow the G5
greps, and to add the two isort settings above.

---

## 5. Open questions — need a human decision

### `spec.json` says Python 3.11; everything else says 3.12

`NFR-10` states *"The tool targets Python 3.11 and above"* with acceptance
criterion *"The package declares a minimum Python version of 3.11."*
`pyproject.toml` declares `>=3.12`. **The spec and the build contradict each
other right now.**

- **Amend the spec.** Correct, and expensive: the hash moves off
  `sha256:dd2914…`, every task's stored `spec_hash` mismatches, and §3.5 requires
  every non-`verified` task move to `spec_drift` and be re-approved.
- **Revert to 3.11.** Cheap, but restores a floor nothing tests, and CI would
  have to actually run 3.11 for the claim to mean anything.
- **Leave it.** Cheapest today; the contradiction stays live and someone will
  hit it during release (T-0041), when packaging metadata gets scrutinised.

No option is free. This wants a deliberate call, not a default.

### `E_NO_READY_TASK`: error or normal terminal condition?

§4.1 says `None`; §4.7 says error code. See §2. Deferred to **T-0030**.

### §4.6.1 is stale

It prescribes branch-per-pair; the project commits on main. Either rewrite
§4.6.1 or document the deviation there. Right now the contract says one thing
and the project does another, which is exactly the drift §0.1 exists to prevent.

### `tests/test_errors.py`'s module docstring is stale

It still opens *"`redgear/errors.py` does not exist yet"* and describes the
pre-§4.7 state where codes were being derived from prose. Harmless to the tests,
misleading to a reader. Not corrected because the authorization for that file
covered the registry assertion only.

---

## 6. Working protocol

### Two-phase TDD

Every module arrives as a **pair**. Never write implementation and tests in one
step.

**Phase 1 — `test_authoring`.** Writable `tests/**`; frozen `redgear/**`,
`pyproject.toml`, `.github/**`.
Write tests to the graph node's exact selectors. **Do not create the module
under test.** Run `pytest -q --continue-on-collection-errors` and confirm the
RED is `ModuleNotFoundError` (or `ImportError` for a new symbol in an existing
module) — not a typo.

**Phase 2 — `implementation`.** Writable `redgear/**`; frozen `tests/**`,
`pyproject.toml`, `.github/**`.
The test file is now **frozen — not one character**. If a test is genuinely
wrong, that is `blocked`: name it, explain, stop. Do not edit it to pass.
Implement until green, then prove scope was respected:

```bash
git diff --stat -- tests/ pyproject.toml .github/
```

That must be **empty**. If it is not, the pair failed regardless of test
results.

### Verification (all four, every time)

```bash
ruff format --check . && ruff check . --no-cache && mypy && pytest -q
```

### Outcome contract

End every task with exactly one of **`completed`** / **`blocked`** /
**`scope_insufficient`**.

`blocked` is free and is the correct answer when the contract is ambiguous, a
test is wrong, or a needed error code does not exist. Guessing is worse.
Editing a frozen file to make a test pass is the worst available outcome — worse
than not finishing.

### Claude Code does not commit

No `git commit`, no `git add`, no staging. Leave work uncommitted, report
`git status --porcelain`, what is new versus modified, and suggested commit
messages. The human commits.

### Flag contract gaps

Several sections of `CLAUDE.md` were written after the code that needed them and
were wrong or absent on first contact — §3.5, §3.6, §4.7, and G4's structure
claim all originated as gaps found mid-task. When the contract is silent,
self-contradictory, or contradicted by a frozen test: **report it**, do not
paper over it.
