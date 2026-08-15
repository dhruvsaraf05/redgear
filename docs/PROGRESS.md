# PROGRESS

**Live handoff document.** Read this after `CLAUDE.md` when starting a session
with no prior context. It records where the build is, decisions that are not
derivable from the contract, traps already hit, and open questions.

Keep it **current**, not cumulative. Update entries in place; delete what stops
being true. This is not a changelog — git history is the changelog.

*Last updated: after T-0033 (cli). The command surface is complete. **The
self-hosting crossover is NOT achieved — see §5.***

---

## 1. Where we are

**`T-0001` through `T-0033` are done.** The §9 command surface is complete
for the eight commands in scope, and `redgear run --dry-run` works against the
real repository today. Next up is **`T-0034`/`T-0035` — the Claude Code
adapter**, which is the first pair needing an agent CLI.

⚠️ **§4.6 says T-0033 is the point where redgear can drive its own tasks. That
is not true yet**, for three concrete reasons rather than one. See §5 — the
most important is that the manual phase never wrote events, so redgear
currently believes none of the 33 finished tasks were done.

| | |
| --- | --- |
| Main is | **green** |
| Test suite | **331 passed, 1 skipped** (was 311) |
| Suite runtime | **Unreliable on this machine — measured 83 s to 240 s for the same tree.** Trust CI, not a Windows laptop. See §5. |
| The skip | `test_gitleaks_clean` — the `gitleaks` binary is not on PATH locally. Its pre-commit config is still asserted. CI runs the real scan. |
| Modules built | `schemas`, `errors`, `paths`, `hashing`, `redact`, `events`, `state_engine`, `locks`, `budget`, `gitctx`, `verifier` (**all six gates**), `runner` (protocol), `prompt_engine`, `orchestrator`, `cli` |
| Test rig | `tests/fake_runner/` — 12 declarative scenarios, no subprocess |
| Golden prompts | `tests/snapshots/` — 5 files. A prompt change is now a reviewable diff. |
| Not yet built | `runner` adapter (T-0034), `planner` + `plan`/`approve` commands (T-0036), `api/`, `ui/` |
| Crossover | **Not reached.** §4.6 places it at T-0033; in practice it needs T-0034 (adapter) and an approved plan. §5. |

**Gates 3–6 need inputs the verifier cannot invent** — a `HarnessConfig` (§7.3:
commands come from configuration only) and the resolved inherited criteria.
`run_gates` takes both as *optional* arguments; without them those gates are
recorded `skipped` with reason `no_harness_config`, never stubbed to pass.
That default is a footgun and is written up in §2.

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
| T-0016/17 | test/impl | done | `locks.py` — task leases, single-run lock |
| T-0018/19 | test/impl | done | `budget.py` — autonomy caps, STOP sentinel, signals |
| T-0020/21 | test/impl | done | `gitctx.py` — read-only git interrogation, diff parsing |
| T-0022/23 | test/impl | done | `verifier.py` gates 1–2 — scope check, frozen hash |
| T-0024/25 | test/impl | done | `verifier.py` gates 3–6 — lint, tests, criteria, coverage |
| T-0026/27 | test/impl | done | runner protocol + deterministic fake runner |
| T-0028/29 | test/impl | done | `prompt_engine.py` — 5 golden snapshots committed |
| T-0030/31 | test/impl | done | `orchestrator.py` — the continuous loop |
| T-0032/33 | test/impl | done | `cli.py` — init, run, status, stop, verify, rebuild, log, doctor |
| **T-0034/35** | test/impl | **next** | `runner.py` — Claude Code headless adapter (needs an agent CLI) |
| T-0036/37 | test/impl | todo | `planner.py` — plan generation + approval gate (needs an agent CLI) |
| T-0038/39 | test/impl | todo | `api/app.py` — read-only control plane |
| T-0040 | scaf | todo | control plane UI |
| T-0041 | scaf | todo | packaging and release |

---

## 2. Decisions taken (with reasoning)

These are not derivable from `CLAUDE.md` alone. Each records *why*, because the
conclusion without the reasoning invites someone to "fix" it back.

### §4.7 declares 19 codes; `ERROR_CODES` holds fewer

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

### `E_NO_READY_TASK` — RESOLVED at T-0031: removed from §4.7

Deferred since T-0013, decided now. **Exhausting the queue is a normal
termination, not an error**, and the code has been struck from §4.7 (nineteen
codes remain).

The decisive evidence is that §4.7's own table argued against itself: the
"correct response" column read *"Run ends `complete_or_blocked`"*. **A code
whose documented handling is "terminate normally" is not an error** — it is a
control-flow signal wearing an error's clothes, and raising it would mean every
successful run ends by catching an exception.

Three other parts of the contract already agreed, so §4.7 was the lone dissenter:

- §4.3 gives exhaustion two real terminations — `complete` (exit 0) when every
  task is verified, `blocked` (exit 2) when one is escalated waiting on a human.
- `RunEndedEvent`'s schema (frozen since T-0003) enumerates exactly those six
  reasons. There is no representable event for "no ready task", so an error
  path could not even be recorded. Note this also kills the literal string
  `complete_or_blocked` from §4.1's pseudocode — it was shorthand for "pick one
  of the two", and §4.1 has been amended to say so.
- `state_engine.next_ready_task` has returned `None` since T-0013.

The orchestrator implements exactly that: `None` from selection means end the
run, `complete` unless some node is `escalated`.

### `next_ready_task` selects `rejected` as well as `ready`

Forced by a frozen test, and the constraint is worth knowing before touching
readiness again. `test_state_read.py` pins `recompute_readiness` to **leave a
`rejected` node alone** ("recomputation must not resurrect a task that already
left the queue"), so nothing moves `rejected` back to `ready`.

But `_CLAIMABLE_FROM` has always been `{"ready", "rejected"}`. Selecting only
`ready` therefore stranded every failed task forever: the loop would skip it and
report `complete` with the work unfinished.

So selection now returns anything **claimable**. That is also what keeps the
retry free of a special case — a retry is simply the next selection, which is
the whole of AC-3.

### Python is 3.12; the spec still says 3.11 (**unresolved** — see §5)

`pyproject.toml`, the ruff target, mypy, the CI matrix and `CLAUDE.md` §1.1 all
say **3.12**. Local development runs **3.14**.

The 3.11 floor was dropped because nothing ever ran against it — a floor no
build exercises is a promise nothing checks.

`.redgear/spec/spec.json` `NFR-10` still says 3.11 and is **not** edited: it is
content-addressed, and changing it moves `sha256:dd2914…` and marks all 41
nodes `spec_drift` (§3.5).

### Gate 2 is defence in depth, not a second chance at gate 1

For a validly-scoped task, **any touch of a frozen path fails gate 1 first**
and gate 2 is recorded skipped. §4.4 invariant 7 guarantees frozen and
writable globs are disjoint, and gate 1 checks a *modification* against
`writable_globs` — so a frozen modification is always also an
`out_of_scope_write`.

That does not make gate 2 redundant. It catches what gate 1's glob logic
cannot see:

- a **newly created** file inside a frozen glob (untracked, absent from the
  recorded digest map, so re-hashing only the recorded paths would pass it);
- a **deleted** frozen file — the crudest way to make a failing suite green;
- any content difference the diff does not reflect.

Gate 2 rarely fires alone, and that is correct behaviour rather than a sign
it is doing nothing. Now recorded in **CLAUDE.md §7.2** as well, since it is
a property of the design and not just an observation about the tests.

### Branches were abandoned; work commits directly on main

Branch-per-pair was tried and abandoned after it produced more failure than it
prevented:

- A PR from a branch carrying only the `test_authoring` half triggers CI on a
  deliberately-red tree. The rule was already "open the PR only once the pair is
  green", so the branch bought nothing during the pair and only added a merge.
- A GitHub rebase-and-merge rewrote the SHAs of three commits, producing a
  conflict against local `main` where **every remote commit was a byte-identical
  copy of a local one**. Pure churn, zero content.

Work happens on `main`: both phases of a pair in one session, commit once when
green. The red state lives only in the working tree, never in a commit.
**§4.6.1 has been rewritten to describe this** — it no longer prescribes
branches.

### `gitctx` requires the repository *root*, not merely a directory inside one

`git rev-parse HEAD` succeeds in any directory *inside* a repository — git
walks upward to find one. On this machine it succeeds in the system temp
directory, because some ancestor is a repo.

Left alone, `redgear` pointed at a stray directory would silently diff, hash
and report against an unrelated enclosing repository. So `gitctx._git`
verifies `rev-parse --show-toplevel` equals the requested root and raises
`E_NOT_A_REPO` otherwise. §8.4's "refuse to start outside a git repository" is
only a safety property if "outside" means "not this repository's root".

The check is cached per resolved path, so it costs one extra git call per
repository per process.

### Gates 3–6 take their inputs as optional arguments — and that is a footgun

§7.3 forbids the verifier from inventing harness commands ("Harness commands
come from `config.json` only"), and `criteria_coverage` needs inherited
criteria resolved from the graph, which the verifier does not read. So both
arrive as arguments.

They are **optional**, defaulting to "skip with reason `no_harness_config`".
Two things forced that: three frozen tests from T-0022 call `run_gates` with
the old five-argument signature, so a required parameter would be a `TypeError`
in already-verified tests.

It fails safe — a skipped gate is not a passed gate, so the verdict is `FAIL`
and a proof can never claim a green it did not earn. But a caller that forgets
the harness gets a confusing four-skip proof rather than a loud error. **The
orchestrator (T-0030) must always pass a `HarnessConfig`, and nothing in the
type system forces it to.** Worth revisiting there.

### A collection error is not one thing: `import_error` is valid red, `syntax_error` is not

For a `test_authoring` task, gate 4's polarity inverts and the red state during
the manual bootstrap phase is a *collection* error, not an assertion failure —
the tests import a module that does not exist yet.

The report structure is **byte-for-byte identical** whether collection failed
because the implementation is missing or because the test file does not parse:
`summary` is `{total: 0, collected: 0}`, `exitcode` is 2, `tests` is empty, and
one `collectors` entry has `outcome: "failed"`. The only difference is the
exception type inside `longrepr`.

Decision, implemented in `verifier.classify_collection_error`:

- **`ModuleNotFoundError` / `ImportError` → valid red.** This is precisely the
  state §6's two-phase protocol tells the agent to leave behind.
- **`SyntaxError` / `IndentationError` / `TabError` → `invalid_red`.** Accepting
  it would let an agent satisfy a `test_authoring` task by writing a file that
  does not parse — the cheapest possible fake red.

Syntax is checked first: a file that fails to compile never reaches its
imports, so an import marker in the same text would be misleading.

### `criteria_coverage` checks existence only for `test_authoring`

A `test_authoring` task's cited tests are *supposed* to be red — gate 4 has
already established that. Requiring them to pass in gate 5 would contradict
gate 4 outright and make the pair unsatisfiable.

So for `test_authoring` the gate resolves the selector but does not require a
`passed` outcome. "I wrote that test" is still the claim worth verifying. When
the red is a collection error there are no node ids at all, so the gate is
skipped with a reason rather than failing every criterion.

`coverage_delta` is skipped for both `scaffold` (§4.5) and `test_authoring` —
the latter writes tests, not covered code, and its suite is red by design.

### Fake-runner scenarios are data, not functions — a deliberate §2.2 deviation

§2.2 sketches `scenarios.py` as "one function per agent behaviour". It is
written as **one frozen dataclass per behaviour** in a registry instead.

§10.5 needs ~23 behaviours and T-0030 needs all of them at once. As functions
they would be 23 near-identical bodies differing in a path string and an enum,
and every new question the orchestrator wants to ask — *what did it declare?
did it lie?* — would mean editing all 23. As data a behaviour is a row, and the
one place that interprets it (`apply_scenario`) is ~20 lines.

The lie surface is explicit fields: `omits_from_declaration` produces
`undeclared_change`, `declares_extra` produces `phantom_change`. Encoding them
as data means the fake **cannot accidentally tell the truth** — the declaration
and the patch are computed from the same record.

12 of §10.5's scenarios are expressible this way and all 12 exist. The
remainder (`retry_then_succeed`, `exhausts_attempts`, `stop_mid_run`,
`budget_exhausted`, `consecutive_failures`, `dispatch_timeout`) are *loop*
behaviours, not turn behaviours: they are a **sequence** of scenarios. That is
why `FakeRunner` takes varargs and the last entry repeats — fail-then-pass and
same-failure-three-times both fall out of it without T-0030 needing to know how
the fake is wired.

`FakeRunner.calls` records every dispatch. Without it, T-0030 has nowhere to
assert that prompt 2 carries the failure excerpt, or that `allowed_tools` never
contains a bare `Bash`.

### `Runner` is `@runtime_checkable`, and that buys less than it looks like

`isinstance` against a runtime-checkable Protocol checks **method names only**,
never signatures. A fake whose `dispatch` took different parameters passes
`isinstance` and then fails the moment the orchestrator calls it by keyword.
So `tests/test_fake_runner.py` compares `inspect.signature` explicitly as well.

Signature conformance is otherwise a static property, enforced by
`mypy --strict` at the call sites — which is why the orchestrator being *typed*
against `Runner` (NFR-8) is the real guarantee, not the isinstance check.

### §5.2's "omitted only on attempt 1" vs. "never omit a section"

§5.2 says three things that cannot all be literally true at once: never omit a
section; empty sections render as an explicit `none`; and PRIOR ATTEMPTS is
"omitted only on attempt 1".

**Reading adopted:** the *heading* is always present; the untrusted *block* is
what is absent on attempt 1, where the section body reads
`none -- this is attempt 1 of 3.`

That is the only reading under which all three statements hold, and AC-2 —
"empty sections render explicitly rather than vanishing" — is the testable
criterion, so it wins over the parenthetical. It is also the safer default: an
absent heading is ambiguous, because the agent cannot distinguish "there were
no prior attempts" from "redgear failed to tell me about them".

### Criteria come from the context, never from the task node

`build()` renders `context.criteria` and never reads `task.acceptance_criteria`.
The caller resolves them — a `test_authoring` or `scaffold` node carries its
own, an `implementation` node inherits from a verified sibling (G2, and §4.4
invariant 5 makes its own list *necessarily* empty).

One source of truth. Reading both and preferring whichever is non-empty would
work until the day they disagree, and then the prompt would silently show the
wrong criteria — exactly the silent-failure class this module is dangerous for.
A Phase-1 fixture got this wrong (§3) and the golden file caught it.

### Glob applicability is compared pattern-to-pattern, not by expansion

FR-9 needs "rules whose globs intersect a task writable scope". The engine has
no filesystem, so it cannot expand either side — and expanding would make
prompt text depend on which files happen to exist, destroying snapshot
stability.

`_globs_overlap` compares literal prefixes before the first wildcard: two
patterns can overlap only if one prefix is a prefix of the other. `src/**` and
`src/ledger/**` overlap; `ui/**` and `src/ledger/**` do not.

Deliberately generous. A false positive shows the agent one rule it did not
strictly need; a false negative **hides a rule it was required to follow**.

### The gate-set mapping is duplicated from `verifier.py` on purpose

§11.2 rule 7 names this exact import as a boundary violation ("If you are about
to import `verifier` into `prompt_engine`, stop"). So `_GATES_BY_TASK_TYPE` is
restated in `prompt_engine` and pinned by tests rather than shared.

Four lines of duplication is the cheaper of the two costs, but it *is* a drift
risk: if `verifier` ever changes which gates a task type runs, this must change
too. The prompt tells the agent which gates apply, so a divergence would brief
the agent to satisfy a check that will not run, or to ignore one that will.

### `.redgear/` must be excluded from both working-tree audits

redgear's own state directory is committed to the target repo on purpose — it
*is* the audit trail — so every write redgear makes during a run is a real git
change. That breaks two checks at once, and neither failure is obvious from
reading the contract:

- **The scope gate.** The event log, the projection and the persisted prompt
  all land between the claim and the verification, so `scope_check` reported
  each as an `out_of_scope_write`. Every task would have failed.
- **The §8.4 dirty-tree refusal.** The run lock is taken *before* the check and
  lives in `.redgear/locks/`, so a run refused to start on dirt it had just
  created itself. This is what the first orchestrator test run actually hit —
  24 failures, all `DirtyTreeError`.

`paths.is_state_path` is the single home for the rule, used by both. Deliberately
not in `gitctx`, which is a general-purpose reader that should not know
redgear's layout. **§7.2 and §8.4 are both silent on this** and arguably should
not be.

### The verifier is injectable into the loop

`run(..., verify=...)` defaults to `verifier.run_gates`. The seam exists for the
same reason the `Runner` protocol does — the second implementation ships on day
one — and it is what keeps the loop's 27 tests at ~19 s instead of spawning a
nested pytest per iteration.

The loop's job is deciding what a verdict *means*; gate mechanics have 43 tests
of their own. One test (`test_real_gates_end_to_end`) runs the real pipeline so
a mis-wired call signature cannot hide behind a stub — without it, every other
test in the file would pass against a broken call.

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

### `git rev-parse` succeeds outside the repo you meant

**Symptom:** a test asserting "this plain directory is not a repository"
fails, because git found one several levels up.

**Cause:** git resolves upward through parent directories. The system temp
directory can sit inside an unrelated repository, so *every* git command
quietly targets that one.

**Fix:** compare `rev-parse --show-toplevel` against the requested root
(`gitctx._git` does this). Never assume a fresh `tmp_path` directory is
outside version control.

### Signal handling differs on Windows; the process-tree kill is platform-split

`os.killpg` and process groups do not exist usefully on Windows, so
`budget.terminate_process_tree` shells out to `taskkill /F /T /PID` there and
uses `killpg` on POSIX. Both are best-effort: a grandchild that has already
reparented is unreachable by either.

**`SIGTERM` is not delivered on Windows the way it is on POSIX** — Python
maps it, but `os.kill` with it terminates immediately rather than running a
handler. The tree-kill path is therefore what is actually tested here; the
POSIX `killpg` branch is exercised only in CI. Treat that branch as
**unverified locally**.

### `git ls-files --cached` lists deleted files; hashing them raises

**Symptom:** `frozen_hash_check` crashed with `FileNotFoundError` on exactly
the case it exists to report — a deleted frozen file.

**Cause:** `git ls-files --cached --others` includes tracked files that have
been deleted from the working tree. Feeding that straight into `digest_map`
tries to hash a path that is not there.

**Fix:** filter the expansion to `(repo_root / path).is_file()`. The deleted
path then falls out of the current map and is correctly reported as
`frozen_file_deleted` rather than raising. See `verifier._current_frozen_digests`.

### Phase 1 must run `ruff format`, not only `ruff check`

**Symptom:** `ruff format --check .` fails in Phase 2 on a test file that is
frozen by then and cannot be corrected.

**Cause:** `ruff check` and `ruff format` are separate tools. Linting a new
test file clean in Phase 1 says nothing about its formatting.

**Fix:** in Phase 1, run `ruff format tests/<new file>` *and*
`ruff check . --no-cache` before declaring the phase done.

### The nested pytest inherits far more than it looks like it should

**This is the trap T-0024's AC-8 was written for, and it is worse than the
warning suggested.** Gate 4 runs pytest inside a target repository while
redgear's own suite is under pytest. Every item below was **measured** on this
machine, not reasoned about.

| What | Result |
| --- | --- |
| Ancestor `pyproject.toml` | **Hijacks the child.** It walks up out of the target repo, adopts the ancestor as rootdir *and* configfile, and applies its `addopts`. A `-k` there turned a real suite into `1 deselected / 0 selected`. |
| `--rootdir=<repo>` | **Does not fix it.** rootdir gets pinned while `configfile` still resolves to `..\pyproject.toml`, and the deselect still applies. This is the single most misleading part. |
| `-c <configfile>` | **This** is what stops ini discovery. |
| Ancestor `conftest.py` | Still imported even with `-c` *and* `--rootdir`. Needs **`--confcutdir=<repo>`**. |
| `PYTEST_ADDOPTS` | Inherited and applied — exit 5, nothing collected. A scrubbed env fixes it. |
| `.pytest_cache` | Written into the user's tree. **`-p no:cacheprovider`** prevents it. |

The working recipe is all five at once (`verifier.run_harness`); dropping any
one reintroduces a failure whose symptom — "passes alone, fails in the suite" —
points nowhere near its cause.

Two things that turned out **not** to be problems: rootdir is computed
correctly when `cwd` is the repo and no ancestor config exists, and
**coverage.py does not walk up** for its config (`config_files_attempted`
showed cwd only), so the outer `[tool.coverage.run] source = ["redgear"]` never
leaked.

### CLAUDE.md §7.3's environment allowlist cannot start Python on Windows

**Symptom:** the harness dies with `INTERNALERROR`, exit 3, before collecting
anything: `OSError: [WinError 10106] The requested service provider could not
be loaded or initialized`.

**Cause:** §7.3's normative env dict is `PATH`, `HOME`, `LANG`,
`PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE`, `CI`, `NO_COLOR`. Without
`SYSTEMROOT`, Python cannot initialise the Windows networking layer at
interpreter startup.

**Fix:** `verifier.harness_env` adds `SYSTEMROOT`, `SYSTEMDRIVE`, `COMSPEC`,
`PATHEXT`, `TEMP`, `TMP` on `win32` only. None carry credentials, so G5 is
intact. **§7.3 as written is wrong for Windows** — a scrubbed environment that
cannot launch the harness is not a safety measure.

### `coverage --source` takes packages and directories, never a file path

**Symptom:** `No data was collected. (no-data-collected)` and `No data to
report`, with a suite that demonstrably ran and passed.

**Cause:** `--source=pkg.py` is silently useless. coverage.py wants a package
name or a directory.

**Fix:** the `python_repo` fixture puts the module at `src/pkg/__init__.py` and
passes `--source=src`. Caught in Phase 1 *before* the tests froze — had it been
caught in Phase 2 the fixture would have been unfixable without an authorized
frozen edit.

Keeping the tests outside the measured tree is also what lets a changed test
file legitimately drop out of the coverage-delta denominator instead of scoring
as uncovered.

### `ruff` is not on PATH in this venv; only `python -m ruff` works

Harness command defaults must be full argv vectors built from `sys.executable`.
A `["ruff", "check", ...]` default looks right and fails on a clean checkout.
This is why `HarnessConfig` has no defaults for the three command vectors.

### pytest collects any imported name matching `test*`

**Symptom:** `ERROR at setup of tests_pass_check` — `fixture 'task' not found`,
pointing at a line in `redgear/verifier.py`.

**Cause:** `verifier.tests_pass_check` matches pytest's default
`python_functions = test*`. Any test module that *imports* it has it collected
as a test. The name mirrors `GateName.TESTS_PASS` alongside `scope_check` and
`lint_check`, and the importing test modules are frozen under G2, so neither
end could be renamed.

**Fix:** `setattr(tests_pass_check, "__test__", False)` in `verifier.py`.
`setattr` rather than direct assignment because mypy strict rejects an
attribute it cannot see on a `Callable`, and NFR-5 forbids suppressions.

### coverage JSON and ruff JSON both use native path separators

coverage keys `files` by `src\pkg\__init__.py` on Windows; ruff reports
`filename` as an **absolute** native path. Git always emits POSIX-relative
paths. Without normalisation the changed set and the coverage data never
intersect — every denominator is empty and `coverage_delta` silently passes
everything, which is indistinguishable from a working gate until you look.

### The fake runner applies patches **cumulatively** across a sequence

**Symptom:** a two-scenario retry sequence where attempt 1 fails on scope and
attempt 2 is a clean patch — and attempt 2 fails too, on a file attempt 1 wrote.

**Cause:** `FakeRunner` writes into the same working tree on every dispatch and
nothing rolls back between turns. The diff is taken against the claim's
`base_commit`, so by attempt 2 it contains the *union* of both patches. A real
agent would clean up after itself; the fake has no such instinct.

The bite is specific: it only shows when attempt 2 touches a **different** path
from attempt 1. If both write the same file, attempt 2 overwrites it and the
union is just that file.

**Workaround, used by the T-0030 sequences:** have every scenario in a sequence
write the *same* path, varying only the content — `WRITES_DIRTY` then
`WRITES_CLEAN` both write `src/pkg/feature.py`. Where a sequence genuinely needs
attempt 2 to touch a different file, either declare the earlier file too or
add a `FileEdit(path, None)` deletion to clean it up.

Do not "fix" this by resetting the tree between dispatches. Cumulative writes
are what a real agent does, and a fake that silently reverted them would hide
the class of bug where an agent leaves debris behind.

### `issubclass` against a runtime-checkable Protocol is *structural*, and returns True

**Symptom:** a test asserting "the fake does not inherit from the protocol"
written as `assert not issubclass(FakeRunner, Runner)` fails — `issubclass`
returns `True`.

**Cause:** for a method-only `@runtime_checkable` Protocol, `issubclass`
performs the same structural check `isinstance` does. It says nothing about
inheritance.

**Fix:** ask the MRO instead — `assert Runner not in FakeRunner.__mro__`. Both
assertions together are the informative pair: structurally yes, nominally no.

Caught by re-reading Phase 1 before the file froze. Had it survived, the
correct fix would have required an authorized frozen edit.

### A missing trailing comma turns a one-element tuple into a bare value

`edits=(FileEdit("tests/test_pkg.py", "..."))` is a `FileEdit`, not a
`tuple[FileEdit, ...]`. Nothing complains — the dataclass is not validated, and
the scenario would have iterated the *fields* of the object at runtime. Frozen
dataclasses buy immutability, not the input validation Pydantic gives at a
boundary. Worth a second look at every single-element tuple literal in a
scenario table.

### `tests/fake_runner/` uses relative imports on purpose

`tests/` has no `__init__.py`, so `tests.fake_runner` is not importable —
pytest puts `tests/` on `sys.path` and the package resolves as `fake_runner`.
Inside the package, `from .scenarios import ...` sidesteps the question
entirely and is stable under isort: a relative import is always `local-folder`,
so it cannot suffer the Phase-1/Phase-2 classification flip described above.
`no-lines-before` means it sits directly under the `redgear` import with no
blank line.

### Hand-written golden files are the point, and they are unforgiving

Snapshots live under `tests/**`, which is frozen in Phase 2 — so they **cannot
be generated from the implementation**. They must be hand-authored in Phase 1
and the implementation made to match byte-for-byte.

That is uncomfortable and it is correct: it forces the prompt format to be a
*specification* written before the code, rather than a transcript of whatever
the code happened to emit. A generated snapshot proves only that the function
is deterministic.

Two things made it tractable:

- Write the first golden by hand, then **derive the others by scripted
  substitution** on anchored strings (`assert old in text` before replacing).
  Retyping five near-identical 3 KB files by hand is where the typos live.
- Keep the format mechanically regular — fixed heading shapes, `- ` bullets,
  one blank line between blocks.

**Verify the snapshot test is live before trusting a green.** Corrupt one
golden deliberately and confirm the test fails; a snapshot test that silently
passes because the file is missing or the comparison is inverted is worse than
none. Done for this pair: removing one frozen glob from a golden failed the
test as expected.

### A forged markdown heading is the second injection vector

Escaping the fence markers (§5.4 rule 1) is the documented attack. The one
next to it: untrusted content containing `## Required outcome` at line start
would appear to *end* the quoted block and resume trusted prompt space, without
ever touching a marker.

`sanitise_untrusted` neuters any line-leading `#` run to `[##] `. Note that
prefixing the line with a space does **not** work — `" ## Required outcome"`
still contains `"## Required outcome"` as a substring, so a `count(...) == 1`
assertion would still see two. The heading text has to actually be broken.

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
| `tests/test_errors.py` | Module docstring rewritten (docstring only, no assertions) | It still opened *"`redgear/errors.py` does not exist yet"* and described deriving codes from prose, which §4.7 had since replaced. Misleading to any new reader. |
| `tests/test_gates_frozen.py` | `test_frozen_gate_runs_even_when_scope_passes` rewritten and renamed to `test_modifying_a_frozen_file_fails_gate_1_and_skips_gate_2` | It asserted gate 1 would *pass* while a frozen file was modified. Impossible under a valid scope (§4.4 inv. 7 + §7.2), so the test could never pass against a correct implementation. Rewritten to assert the real behaviour rather than deleted, so the design is documented where a reader will meet it. |
| `tests/test_gates_scope.py` | `ruff format` (formatting only) | Phase 1 ran `ruff check` but not `ruff format`, so `ruff format --check .` failed on a file frozen by Phase 2. Test count verified unchanged at 16, all still passing. |
| `tests/test_gates_scope.py` | `test_gates_three_to_six_are_not_stubbed` **docstring only**, no assertions touched | Its text still read *"Gates 3-6 arrive at T-0025. Until then…"*, which became false when they landed. The assertions were always correct and are unchanged — what the test guards is now stated accurately: `run_gates` without a `HarnessConfig` skips gates 3-6 with reason `no_harness_config` rather than passing them. Authorized in-session at T-0026. |

**Authorized addition** (not a defect correction):
`tests/test_gates_frozen.py::test_deleted_frozen_file_is_reported_not_crashed`
was added at the human's request as a named regression guard for the
deleted-frozen-file crash described in §3. The fix alone was judged
insufficient — the bug broke gate 2 on the single most likely way an agent
fakes a green suite, so it warranted a test that names it.

| `tests/test_errors.py` | `len(documented) == 20` → `== 19` | Direct consequence of an explicitly authorized §4.7 amendment at T-0031 (striking `E_NO_READY_TASK`). The assertion mirrors §4.7's row count as a parse sanity check; leaving it at 20 would have made the authorized amendment impossible to land. Same class of correction as the earlier exact-count fix in this file. |
| `tests/test_orchestrator.py` | `test_event_sequence_is_gapless_and_monotonic`: `range(1, len(seqs) + 1)` → `range(len(seqs))` | The assertion required event `seq` to start at **1**. Sequences are **0-based**: `events.last_seq` returns `-1` for an empty log so the first append is `last_seq() + 1 == 0`, and `tests/test_events.py` — verified at T-0011 — pins it in three places (`list(range(25))`, `[0, 1, 2]`, `last_seq() == -1`). The new assertion contradicted a verified sibling, so it could never pass against a correct implementation. The FR-1 property it means to check (gapless, monotonic) was always satisfied; only the start index was wrong. |

**These two are different in kind, and the distinction is the whole point of
the escape hatch.** The `test_errors.py` line synchronises a
documentation-mirror count with a contract change that was explicitly
requested — a *consequent* edit. The `test_orchestrator.py` line corrects an
assertion that contradicted an already-verified test in another module — a
*defect* edit. Neither is "the test was inconvenient, so it changed": in both
cases the assertion was demonstrably wrong against something independently
fixed, which is the bar §6 sets before a frozen file may be touched at all.

**T-0028/T-0029 required no frozen-file edits.** One Phase-1 defect was caught
before the freeze: the `scaffold` snapshot fixture passed its acceptance
criteria to the task node but not to the `PromptContext`, so the golden file
and the fixture disagreed about which criteria would render. Fixed in Phase 1;
had it survived, the golden could not have been satisfied without an authorized
edit.

`pyproject.toml` and `.github/**` are frozen to task work but were edited twice
under explicit authorization: to pin exact dev-tool versions and narrow the G5
greps, and to add the two isort settings above.

**T-0024/T-0025 required no frozen-file edits.** The two defects that would
have forced one — `coverage --source` rejecting a file path, and the
`tests_pass_check` collection collision — were both caught before the tests
froze, or fixed entirely inside `redgear/`. The §6 discipline of re-reading
each new test file before ending Phase 1 is what caught the first.

---

## 5. Open questions — need a human decision

### ⚠️ The self-hosting crossover is not reached at T-0033, and the reason is a missed instruction

§4.6 says "From `T-0033` onward redgear can drive its own remaining tasks."
**It cannot.** Three things block it, verified rather than assumed:

**1. There is no event log.** `.redgear/` contains only `spec/` and
`task_graph.json`. Every node in that projection still sits in its *initial*
state — `T-0001` is `ready`, everything else `blocked`. So redgear pointed at
its own repository today would select **T-0001 and try to bootstrap a
repository that has been bootstrapped for 33 tasks**.

This is the one that matters, and §4.6 warned about it in advance:

> "Do not treat the manual phase as throwaway. Every task before T-0033 still
> writes real events to `.redgear/events.jsonl` and still produces real
> proofs. When the loop takes over it must find a coherent state directory,
> not a fresh one."

That instruction was not followed. All progress is recorded in this file
instead. The consequence is now concrete rather than theoretical, and there is
no cheap fix: back-filling 33 tasks' worth of `task_claimed` /
`prompt_dispatched` / `turn_completed` / `task_verified` events would be
*fabricating an audit trail*, which is precisely the thing the audit trail
exists to make impossible. **This needs a human decision**, and the options are
all unattractive:

- **Fabricate the history.** Cheap, and it destroys the meaning of the log.
- **Seed a `plan_approved` event and mark T-0001…T-0033 verified by hand**, in
  one honest "imported from manual phase" record. Still a fiction, but a single
  clearly-labelled one rather than 300 forged events.
- **Start the log from here** and accept that the projection disagrees with
  reality for the already-done tasks — meaning `rebuild` reports divergence
  forever, or those nodes must be trimmed from the graph.
- **Let redgear re-execute from T-0001.** Honest and absurd.

**2. There is no concrete `Runner`.** `redgear/runner.py` exports the protocol
and `allowed_tools_for`, nothing else — verified by inspecting its exports. The
Claude Code adapter is T-0034. `redgear run` therefore refuses with a clear
message pointing at `--dry-run`, rather than pretending to dispatch.

**3. The plan is `draft`, and nothing in scope can approve it.** `redgear run`
correctly refuses with `E_PLAN_UNREVIEWED` (§3.3). The `approve` command
belongs to T-0036/T-0037 with the planner, so today the only way to move the
graph to `active` is hand-editing the file — which skips the `plan_approved`
event that records *who* approved *which* `spec_hash`. That is a real gap in
the audit trail, not a formality.

**What IS true at T-0033:** the full command surface exists and works,
`redgear run --dry-run` composes real prompts against the real graph today,
and every refusal on the path to a real run fires correctly and in the right
order. The engine is complete; what is missing is an agent to drive and a
history to stand on.

### Suite runtime, and why the number here keeps moving

**Whole-suite timings on this machine are not trustworthy.** The same tree has
measured 83 s, 93 s, 127 s, 166 s and 240 s across this project's sessions,
varying with whatever else was running. Earlier entries in this file quoted
whichever number was measured last and read as though the suite had regressed
or improved; it had not.

Only the standalone, back-to-back figures are worth anything:

| File | Standalone |
| --- | --- |
| `test_cli.py` (20 tests) | **27 s** |
| the four `test_gates_*.py` files (43 tests) | ~90 s |
| everything else | small |

The CLI cost is twenty `git init` fixture builds at ~0.85 s each on Windows,
plus one test that runs the real six-gate pipeline
(`test_verify_reports_a_verdict_for_a_task`, ~4 s) because that is literally
what `redgear verify` does.

`doctor`'s tool probe was changed from spawning `--version` to asking
`importlib.util.find_spec`, which removed three subprocess launches and is
also more accurate — launching `--version` conflates "module missing" with
"module present but its version flag exits non-zero".

The dominant remaining cost is fixture `git init`, and those fixtures are
frozen. Same recommendation as before: **take the CI number as authoritative**
rather than a Windows laptop's, and if it breaches there, the lever is a
session-scoped repository fixture — which needs authorized edits to frozen
files and should not be smuggled into an unrelated pair.

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

### `state_engine.py` still carries a private git helper

T-0015 added `_git`, `_repo_files` and `_frozen_digest_map` to
`state_engine.py` as a stand-in, because `gitctx` did not exist yet. **It does
now (T-0021)**, and those three can be replaced by `gitctx.head_commit` and
`gitctx.tracked_and_untracked`.

Not done this turn, deliberately: it is a refactor of a module whose tests are
frozen, so it deserves to be a considered step rather than a side effect. The
duplication is small and harmless until then, but it means two code paths ask
git the same questions — and only one of them has the repository-root guard
described above.

### `E_INVALID_CLAIM` and `E_LEASE_EXPIRED` are declared but unimplemented

Both are in §4.7. `locks.py` raises `RunLockedError` for a wrong-token release
(where `E_INVALID_CLAIM` arguably belongs) and treats lease expiry as an
*event*, not an error, so neither class exists yet. Consistent with the
"registry grows as raisers land" rule, but worth a look when the orchestrator
wires claims together at T-0030.

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

Before ending Phase 1, leave the new test files **lint-clean and
format-clean**, because they cannot be touched afterwards:

```bash
ruff format tests/<new file>       # separate tool from `ruff check`
ruff check . --no-cache
```

Also re-read each new test for vestigial lines and for assertions that
contradict a sibling test. Five Phase-1 defects have now had to be fixed under
authorization (§4); every one was cheap to catch here and expensive to catch
later.

Where a new test needs a real repository to run tools against, add a **fixture
to `tests/conftest.py`** rather than an importable helper module. Fixtures are
injected by name, so there is no import statement for isort to classify — and
therefore no way to hit the Phase-1/Phase-2 classification flip above. Keep
`conftest.py` free of `redgear` imports: a symbol that does not exist yet would
fail collection for the **whole suite**, hiding the real red instead of showing
it.

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
