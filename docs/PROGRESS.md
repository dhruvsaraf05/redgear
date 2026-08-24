# PROGRESS

**Live handoff document.** Read this after `CLAUDE.md` when starting a session
with no prior context. It records where the build is, decisions that are not
derivable from the contract, traps already hit, and open questions.

Keep it **current**, not cumulative. Update entries in place; delete what stops
being true. This is not a changelog — git history is the changelog.

*Last updated: after resolving the commit-boundary gap — the structural
defect the first real live run found and could not get past. **G6 was
amended: redgear now commits verified work to the local repository.** New
module `redgear/vcs.py` (the only git mutator), two new event types, a
twentieth error code, and a per-claim clean-tree assertion. Before that: the
first real `redgear run` against an actual Claude Code CLI, which found and
fixed a `verifier.py` aggregate-verdict bug and found this one; a cosmetic
README pass; and T-0041 (packaging and release readiness). Every module in
§2.2 exists except the Next.js UI (T-0040, deliberately deferred, see §2).*

---

## 1. Where we are

**`T-0041` — the graph's leaf task — is done.** So is every task except
`T-0040`, which is **deliberately deferred**: the browser control plane UI is
not built, not deleted, and not silently dropped — `FR-12`'s statement and
`spec.json`'s `out_of_scope` both say so explicitly now, and the task stays
in the graph, state unchanged. A deferred task is not a deleted one.

The full arc exists: `redgear plan` generates a plan read-only, `redgear
approve` gates it behind a named human, `redgear run` drives it, `redgear ui`
serves a read-only FastAPI control plane, and the package builds, installs,
and exposes a working `redgear` command from a clean wheel. **"Done" here
still means the same thing it has meant since T-0002: built and verified by a
human relaying prompts between sessions, not by redgear driving itself.**
That is recorded here and in `CLAUDE.md`, not currently stated as a dedicated
section in the README — see §2's "What's not done yet" entry for why.

| | |
| --- | --- |
| Main is | **green** |
| Test suite | **421 passed, 1 skipped, 1 failing** — the one failure is `test_errors.py`'s `len(documented) == 19`, a documentation-mirror count that must become `20` now that §4.7 declares `E_COMMIT_FAILED`. A frozen file; **awaiting explicit authorization**, not worked around. Every other test passes, including 19 new ones (`test_vcs.py`, `test_commit_boundary.py`). |
| Suite runtime | **Unreliable on this machine — measured 83 s to 463 s for the same tree.** Trust CI, not a Windows laptop. See §5. |
| The skip | `test_gitleaks_clean` — the `gitleaks` binary is not on PATH locally. Its pre-commit config is still asserted. CI runs the real scan. |
| Coverage | **90.35%** (floor 85%) |
| Modules built | `schemas`, `errors`, `paths`, `hashing`, `redact`, `events`, `state_engine`, `locks`, `budget`, `gitctx`, `vcs` (**new — the only git mutator**), `verifier` (**all six gates**), `runner` (protocol **+ Claude Code adapter, verified against a real CLI**), `prompt_engine`, `orchestrator`, `cli`, `planner`, `api/app.py` |
| Test rig | `tests/fake_runner/` — 12 declarative scenarios, no subprocess |
| Golden prompts | `tests/snapshots/` — 5 files. A prompt change is now a reviewable diff. |
| Not built | `ui/` (T-0040, deliberately deferred post-1.0 — see above) |
| Package | Builds clean (`python -m build`), installs from wheel with a working `redgear` entry point, manifest has nothing sensitive. See §2. |
| Spec | `spec-97ee71` (supersedes `spec-dd2914`, history in `.redgear/spec/history/`) — NFR-10 now says 3.12, FR-12 now names the deferred UI. See §2. |
| **First real run** | `redgear run` against a real Claude Code CLI, a throwaway two-task project, real money spent (~$1.60, 6 dispatches). Found two real bugs: `verifier.py`'s aggregate verdict (fixed that session) and the commit-boundary gap (**fixed now**, §2). |
| **Commit boundary** | **Resolved and proven live.** G6 amended, `vcs.py` added. Pinned by `tests/test_commit_boundary.py` (mutation-tested: reverting the fix reproduces the original `out_of_scope_write: tests/test_calc.py` on attempt 1). |
| **THE CROSSOVER** | **REACHED, 2026-08-24.** `redgear run` drove a real two-task project to completion against a live Claude Code CLI (2.1.237), unattended, no human intervention: `complete: 2 iteration(s), 2 verified, 0 escalated`, exit 0, **$0.5677**. Both tasks passed all applicable gates on their **first** attempt. `redgear rebuild` replays the resulting log byte-identically. The one claim this project could never make about itself is now made and evidenced — see §2. |
| Crossover | **Still not reached, and now known to need more than an event log.** No approved plan, no event log behind the 40 done tasks (T-0040 stays deliberately deferred, not counted as done) — see §2's "self-hosting claim" entry and §5. Separately, the first real run just showed that *even with* an event log and an approved plan, today's loop cannot complete two real, genuinely dependent tasks back to back without a human committing between them — see §5, same entry as above. That gap was invisible until this session because redgear's own bootstrap (T-0001–T-0041) was hand-driven, with a human committing between every pair by hand (§4.6.1); nothing had ever asked the automated loop to do that step itself. |

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
| T-0034/35 | test/impl | done | Claude Code adapter. Verified against a real CLI (2.1.229, Windows, 2026-08-19) — see §2 |
| T-0036/37 | test/impl | done | `planner.py` — plan generation + approval gate |
| T-0038/39 | test/impl | done | `api/app.py` — read-only control plane. Also added `state_engine.persist_proof` and wired it into `orchestrator.run` (§2), and the API half of `redgear ui` (§9) |
| T-0040 | scaf | **deferred** | control plane UI (Next.js). Not built, not deleted — FR-12 and `out_of_scope` say so explicitly. The API it would talk to already exists |
| T-0041 | scaf | **done** | packaging and release readiness — the graph's leaf task. Spec amended and re-hashed, README rewritten, wheel built and verified clean, `release.yml` fixed |

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

### Two subprocess paths, two opposite environment rules

Easy to "fix" one into the other, so the reason for each is recorded here.

| Path | Environment | Why |
| --- | --- | --- |
| **Agent CLI** (`runner.py`) | **Full `os.environ`, propagated untouched** | The child cannot authenticate without it. G5's rule is that redgear never *reads* a credential — propagation is not reading. |
| **Harness** (`verifier.py`) | **Scrubbed allowlist** (§7.3) | A test in a target repository is arbitrary code on the user's machine. It must not be able to read `os.environ` for a credential. |

Same syscall, opposite policies, because the threats are opposite: one is
about letting a trusted child authenticate, the other about denying an
untrusted child the means to steal. `argv.json` records environment **key
names only** — the values are never read, not redacted after the fact.

### `AgentTurnReport` is a separate model from `TurnResult`, and that is a G1 property

The JSON schema handed to the agent via `--json-schema` is generated from
`AgentTurnReport`, which carries only the six fields the *agent* supplies.
The runner-populated fields — `exit_code`, `session_id`, `num_turns`,
`cost_usd_estimate`, `parse_ok` — are absent by construction.

Generating the schema from `TurnResult` instead would invite the agent to
report **its own exit code and whether its own output parsed**, which is
precisely the class of self-reported fact G1 exists to refuse. A test asserts
those keys are missing from the schema.

### A timeout returns a `TurnResult`; it does not raise

§6.5 says a timeout is recorded and counted, never an escaping exception —
one slow turn must not abort the run and lose the proof. So the adapter kills
the process tree and returns `parse_ok=False`.

**Note the interaction with the orchestrator**, which is not obvious: its
`_dispatch_with_one_retry` retries on `not parse_ok`, so a timeout is retried
once and a second timeout ends the run as `runner_error`. That is defensible
— a CLI that always times out is an integration problem, and the task's
attempt budget is never charged for it — but it is emergent from two modules
rather than stated anywhere, which is why it is written down here.

### The planner needs a second dispatch shape: `PlanRunner`

§3.2 says the planner goes "through `runner.py`" like the loop does, and it
does — but it **cannot reuse `Runner.dispatch`**. That returns a `TurnResult`,
whose only free-text field is `summary`, capped at 1500 characters. A 41-node
plan does not fit, and widening `TurnResult` to carry one would pollute the
loop's type for the planner's benefit.

So `runner.py` grew a second protocol, `PlanRunner.dispatch_json`, and
`ClaudeCodeRunner` implements both. The two dispatches differ genuinely: a task
turn reports an outcome and edits files; a planning turn returns a document and
edits nothing. Same justification §6.1 gives for `Runner` — the second
implementation (the test fake) exists on day one.

`Runner.dispatch`'s signature could not be widened anyway: a frozen test
compares it against `FakeRunner`'s parameters, and `tests/fake_runner/` is
frozen too.

### The agent supplies a plan, never the plan's *status*

`normalise_plan` computes rather than reads: the spec hash (§3.5's content
addressing), the spec id, the graph's `draft` state, and every node's mutable
run state. Whatever the document claimed about those is discarded.

Each of those is a hole if taken on trust. An agent-chosen spec hash is content
addressing in name only. An agent-declared `"state": "active"` skips the only
human gate in the system — a test feeds exactly that payload and asserts the
plan still lands as `draft`. And a node arriving with `attempts: 2` would make
replay diverge from the moment it was written.

### Claude Code does not commit

The human commits, every time. This is G6 (`redgear` never commits in the target
repo) applied to the humans-driving-Claude-Code phase. Claude Code leaves work
uncommitted and reports what changed plus suggested commit messages.

### Proof artifacts were never persisted; `state_engine.persist_proof` closes that at T-0039

`verifier.run_gates` has always computed a `Proof` and handed it back to the
caller in memory. Nothing wrote it to disk. Section 2.3 documents
`runs/<run_id>/iterations/<NNNN>/proof/{verdict.json,diff.patch}` as part of
the normative layout, and FR-12 AC-4 ("Proof artifacts including the raw diff
are retrievable per attempt") cannot be true against a system that never
writes them — this was found while building T-0038's tests, not invented for
this task.

`persist_proof(repo_root, run_id, iteration, proof, *, diff)` writes both
files under the *same* iteration directory `persist_prompt` already uses for
that attempt's dispatch, and is called from `orchestrator.run` right after a
proof is computed, for both the pass and fail paths. No new event type: the
same relationship `prompt_dispatched`'s `prompt_path` has to `prompt.txt`
applies here — `task_verified`/`task_rejected` already carry `proof_id`, and
these are the artifacts that id points at.

The diff has to be captured **at verification time**, not recomputed later.
redgear never commits (G6), so the working tree keeps accumulating uncommitted
changes across tasks within one run; a diff against `base_commit` computed
after the fact would include whatever later tasks wrote too, or the baseline
commit itself may no longer mean what it meant when this attempt ran. Capture
now is the only way "retrievable per attempt" is honest.

`_dispatch_with_one_retry` now returns `tuple[TurnResult, int] | None` instead
of `TurnResult | None`, so the caller knows *which* of up to two dispatch
tries actually produced the result being verified, and can persist the proof
in that same iteration's directory rather than the first (parse-failed) try's.
It is a private function with one call site; no test imports it directly.

### `mark_verified`'s `task_verified.attempt` is off by one — found, not fixed

`state_engine.mark_verified` writes `"attempt": node.attempts` from *before*
the transition it is recording — the count of prior rejections, not the
number of the attempt just verified. On a first-try pass that is `0`; after
one rejection it is `1`, one behind what `prompt_dispatched` and
`turn_completed` both recorded for the identical turn (`task.attempts + 1`,
computed in `orchestrator.run`). This is a real defect in already-verified
T-0014/15 code, discovered while building T-0039's proof-lookup logic — no
existing test pins the numeric value (`test_state_write.py` only asserts
event *kinds* and ordering, never `task_verified.attempt`'s value), so it was
silently wrong rather than frozen-correct.

**Not fixed here.** `state_engine.py` is not otherwise in scope for an
`api/app.py` task, no test currently depends on the wrong value (so nothing
would force a defect edit under §6's escape hatch), and the blast radius of a
fix is unknown without checking every reader of this field. `redgear/api/app.py`
works around it instead: a verified attempt's number is derived from how many
`task_rejected` events preceded it for that task, which is what every other
event already agrees on. See §5 for the open question of whether to fix
`state_engine.py` itself.

### The amended G5 and the empty `docs/adr/` — both authorized, not discovered this session

Two changes landed by explicit human instruction rather than as findings:

* **G5's socket bullet** now reads "never open an **outbound** connection"
  rather than "never open a socket" — the literal old wording forbade the
  listening socket `redgear ui` (§9) has always been specified to bind.
  `CLAUDE.md` §1.4 now names the control plane as the sole, deliberate
  exception: it accepts local connections, initiates none.
* **`docs/adr/`** sat empty through 37 tasks while ~30 real decisions landed
  in this section instead. Rather than backfilling thirty ADR files,
  `docs/adr/0001-progress-md-records-decisions.md` records that PROGRESS §2
  *is* the decision record for this project, and `CLAUDE.md` §11.3 now points
  here instead of at `docs/adr/`. See the ADR itself for the full reasoning
  and what is lost by not using per-decision files.

### The Claude Code adapter's manual verification — what was run, what it found, what changed

Not a graph task — maintenance on already-verified modules (T-0034/35,
T-0032/33), so the two-phase protocol does not apply. Existing frozen tests
were treated as frozen throughout: none needed editing, because none
contradicted the real payloads (recorded explicitly, since it was checked
rather than assumed).

**What was run.** `docs/agents/claude-code.md`'s procedure, against
**claude 2.1.229, Windows, MSIX/Claude-Desktop install, 2026-08-19**. Three
dispatches against a throwaway repo, `--output-format json` stdout captured
verbatim: an unauthenticated failure (exit 1), a plain success with no
`--json-schema` (exit 0), and a success *with* `--json-schema` (exit 0). Full
account, including the exact payloads: `tests/fixtures/claude_payloads/
README.md`.

**What it found — five things, all about parsing, none about `build_argv`:**

1. **`subtype` reads `"success"` even on the hard authentication failure.**
   Useless as a signal. The adapter never read it — checked by re-reading the
   source, not re-asserted from memory — and `CLAUDE.md` §6.4 now names
   `is_error`/`terminal_reason` as the real discriminators explicitly (rule 7).
2. **Exit code and payload agreed in both samples that had one to check
   against.** §6.4 rule 1's wording read as though disagreement were the
   norm; softened to say they can disagree in either direction and the
   payload is authoritative regardless — the adapter's actual behaviour
   (record the exit code, never branch on it) needed no change.
3. **`structured_output` is a real, separate JSON object; `result` carries
   the same content JSON-encoded as a string.** The adapter already read
   `structured_output` directly and never fell back to `result` — confirmed,
   not changed. The real `--json-schema` capture used a minimal test schema,
   not redgear's own `agent_report_schema()`, so its content does not
   validate against `AgentTurnReport` — expected, and documented as such
   rather than treated as a mismatch to paper over.
4. **`stop_reason: "tool_use"` appeared on a completely ordinary successful,
   multi-turn dispatch.** Every real dispatch uses tools; nothing reads
   `stop_reason`, confirmed, and §6.4 rule 8 now says so explicitly.
5. **`permission_denials` was empty in all three samples** (none of the three
   manual dispatches attempted a disallowed tool) **and `TurnResult` has no
   field for one.** Not fixed — see §5. A regression test
   (`test_permission_denials_are_observed_but_not_yet_surfaced`) pins down
   the current, honest state so a future change here is deliberate.

**What changed because of it:**

- `tests/fixtures/claude_payloads/`: the three real payloads added verbatim
  (`real_unauthenticated_failure.json`, `real_plain_success.json`,
  `real_json_schema_dispatch.json`); `error_zero_exit.json` and
  `no_structured_output.json` now **are** two of those three, copied in
  unmodified, because they already stood in for exactly those roles;
  `completed.json`/`blocked.json` got a real, observed *envelope* (`stop_reason`,
  `permission_denials`, `usage`, `modelUsage`, etc.) around their existing,
  necessarily-illustrative `structured_output` — no real capture demonstrates
  a redgear-schema-conformant result, so the fixtures say so rather than
  pretending otherwise. Every value an existing test asserts exactly
  (`num_turns`, `duration_ms`, `total_cost_usd`, `changed_files`,
  `session_id`) is unchanged, so no existing test's meaning shifted.
- Five new tests in `tests/test_claude_adapter.py`, each pinning one finding
  above against the real fixture content rather than against a synthetic
  stand-in.
- `redgear/runner.py`'s `# Verified against:` comment now names the version,
  date, platform, install method, and — explicitly — what was *not*
  exercised (`--bare`, `--mcp-config`, `--max-spend-usd`).
- `CLAUDE.md` §6.2 gained the observed-field list and the executable
  resolution note (below); §6.4 gained rules 7 and 8 and rule 1's softened
  wording.
- `docs/agents/claude-code.md`: §0 records what was actually found instead of
  predicting a changed flag; §1 explains the MSIX-not-on-PATH gap;
  §6 documents that PowerShell mangles a `--json-schema` argument's quoting
  and `cmd /c` or `subprocess.run(..., shell=False)` must be used instead
  when invoking `claude` directly (not a redgear issue — its own subprocess
  calls are immune, §11.1 rule 1); §7's template reflects what "verified"
  now actually names.

### The configurable executable — a second, independent defect, fixed in the same session

`ClaudeCodeConfig.executable` defaulted to the bare string `"claude"` and
`cli.py`'s two call sites (`run`, `plan`) never overrode it — confirmed by a
direct diagnostic in the previous session (`where.exe claude` finds nothing on
this machine; `CLAUDE_CODE_EXECPATH` in the environment names the real binary,
under `%LOCALAPPDATA%\Packages\<PackageFamilyName>\LocalCache\Roaming\Claude\
claude-code\<version>\claude.exe` — a normal MSIX/Claude-Desktop install,
deliberately not on PATH). A pipx user with Claude Desktop and nothing else
could not run `redgear run` at all without editing Python.

Fixed with the precedence the diagnostic recommended: `--executable` flag on
`run` and `plan`, then `config.json → runner.executable` (the first real
reader `config.json` has ever had in this codebase — nothing previously
parsed it at all, despite `paths.config_path` and several docstrings assuming
it existed), then `ClaudeCodeConfig`'s own `"claude"` default. `redgear doctor`
now resolves and reports whichever one is actually configured, rather than
unconditionally reporting `shutil.which("claude")` — the old behaviour would
say "not on PATH" on a machine where the CLI is installed and correctly
configured, which is exactly the state this fix exists for.

Four new tests in `tests/test_cli.py` cover precedence, a malformed or absent
`config.json` (must not crash — a user setting this up for the first time will
pass through every one of those states), and `doctor` naming the resolved
executable rather than a hardcoded `claude`.

### T-0041 — the spec amendment, carried out exactly per §3.5

Open since T-0021: `spec.json`'s `NFR-10` said Python 3.11; `pyproject.toml`,
CI, and `CLAUDE.md` §1.1 all said 3.12. Resolved by amending the spec rather
than leaving the contradiction live, per the recommendation the T-0037
orientation pass made and this task's own instruction confirmed.

**What changed, and why each piece:**

- `NFR-10`'s statement and first acceptance criterion now say 3.12. The
  rationale gained a sentence explaining *why* 3.12 specifically: it is a
  floor, not the development version — local development on this project runs
  3.14, CI runs 3.12 deliberately to keep the floor honest, and nothing in the
  codebase uses a 3.13+ feature, so raising it further would lock out users
  for no benefit.
- `FR-12`'s statement gained a sentence recording that the API half is
  implemented (T-0038/T-0039) and the browser front end is deferred post-1.0
  — folded into the same amendment rather than a second one, since both are
  spec changes and §3.5 says nothing about batching them separately.
- `out_of_scope` gained "Browser front end for the control plane (the
  read-only API is implemented; the UI is deferred)".
- Hash recomputed via `redgear.hashing.compute_spec_hash` directly (not
  reimplemented by hand) against the mutated requirements/out_of_scope:
  `sha256:97ee71867c3867b80290dfd89c89d4c1dcb8843a8271ba4052b00c60e61ab0c6`,
  `spec-97ee71`. `spec_id_from_hash` likewise, not hand-derived.
- The pre-amendment spec, byte-identical to what shipped through T-0040,
  written to `.redgear/spec/history/spec-dd2914.json` before the working
  file was overwritten — order matters for the same crash-safety reason
  `persist_plan` writes spec-then-graph-then-event (docs/PROGRESS.md's own
  earlier entry on that): a missing history file is an obviously incomplete
  amendment, a spec pointing at a `supersedes` id nothing on disk backs up
  looks complete and is not.
- `spec.json`'s own `supersedes` field set to `spec-dd2914`; `created_at`
  updated to this session's date.
- `task_graph.json`'s top-level `spec_hash` and **all 41 nodes'** `spec_hash`
  updated to the new value, by script rather than by hand — a 41-node file
  is not something to hand-edit when byte-exact hash matching is the pass
  condition.

**Verified, not assumed:** `tests/test_smoke.py`'s hash-recomputation tests
(`test_spec_hash_recomputes`, `test_spec_id_derives_from_hash`,
`test_graph_spec_hash_matches_spec`, `test_every_node_carries_current_spec_hash`)
all passed against the new values on the first try — these tests exist
precisely to catch a hand-edit that gets the hash wrong, and they did their
job by staying green rather than needing to fire.

**One frozen-file edit this forced**, recorded in §4: `tests/test_hashing.py`
hardcodes `REAL_SPEC_HASH`/`REAL_SPEC_ID` as "the recorded, load-bearing
value" of the *real* `spec.json` on disk — its own docstring says the point
of these tests is to track the real file, not a frozen historical snapshot.
Updating the two constants to the new value is the test doing its job, the
same way `test_smoke.py`'s tests did; leaving them at the old value would
make a correct amendment look like a bug.

**One thing this task found but did not fix, out of scope:** `CLAUDE.md`
§12.1's table still named `spec-dd2914` and the old hash. `CLAUDE.md` was not
in T-0041's writable scope (`pyproject.toml`, `.github/**`, `README.md`,
`docs/**`), and unlike `task_graph.json` — which Fix 1's own numbered steps
require touching — nothing about updating the spec instructed touching
`CLAUDE.md`. Reverted an initial edit there rather than keep it unauthorized.
**Resolved in a later session** (the README-cleanup entry below), once
"update the living docs" gave explicit authorization to touch `CLAUDE.md`
directly.

### The self-hosting claim — decided, not fabricated

The four options this file previously listed for the T-0033 crossover gap
(fabricate history, seed one labelled import event, start the log from here,
let redgear re-execute from T-0001) are now moot. The decision made this
task is none of them: **state plainly, in the README, that the loop has never
driven itself end to end** — this codebase was hand-built through T-0041 with
a human relaying every prompt between sessions, and say so without hedging.

The reasoning is the same one the whole project is built on turned back on
itself: a tool whose entire pitch is "an unverified claim is worthless" cannot
ship with a fabricated audit trail as its own first artifact. Seeding
`.redgear/events.jsonl` with `imported_from_manual_phase` events — the
least-bad of the four original options — would still be redgear's own state
directory asserting a history that did not happen through the mechanism it
exists to audit. At the time this decision was made, the README stated it
directly in a dedicated section; that section was later removed for
presentation reasons, and the entry immediately below this one records why
and where the fact now lives instead.

**This does not close the underlying gap** — there is still no event log
behind the 40 done tasks, and `redgear` pointed at its own repository today
would still select T-0001 and try to bootstrap an already-bootstrapped tree.
What changed is that the project no longer has an *open question* about what
to say about this; it has a decided, stated answer. See §5 for what would
actually need to happen for the crossover to become real (an approved plan
and a genuine event log — no shortcut removes that).

### The README's "What's not done yet" section was removed, by explicit request

The entry above records why that section existed: a tool whose pitch is "an
unverified claim is worthless" cannot make one about itself, so the gap had
to be stated somewhere public rather than only in this internal file. A later
session asked for the section to be removed from the README outright, for
presentation reasons (a cleaner, punchier front page), and it was removed on
that explicit instruction after the tension was flagged once.

**What did not change: the underlying facts, or where they are recorded.**
The loop has still never driven a project end to end unattended, and the
browser control plane is still not built. Both remain true and both remain
recorded here (§1, §2 above) and in `CLAUDE.md` where relevant — this file is
the one place in the project that is not allowed to only say what looks good.
What moved is which document carries the caveat prominently: previously the
README did, now only the internal docs do. If a future session wants that
tension resolved differently (a one-line status note in the README rather
than a full section, for instance), that is a presentation choice to make
deliberately, not a fact to rediscover.

### `verifier.py`'s aggregate verdict ignored a gate's own legitimate skip — found by the first real run, fixed the same session

The first real, live `redgear run` (throwaway two-task project, a real Claude
Code CLI, `--executable` pointed at the MSIX install) found a real bug on its
very first dispatch. The agent wrote a correct, fully in-scope failing test
for a `test_authoring` task — nothing outside `tests/**`, the right assertion,
reported `completed` honestly. redgear rejected it anyway. The proof
(`verdict.json`) showed every gate that ran had genuinely **passed**:
`scope_check`, `frozen_hash_check`, `lint`, `tests_pass` (with
`"red_via_failure: 1 of 1 target test(s) fail"` — correct red, for the right
reason), `criteria_coverage`. `coverage_delta` was correctly `skipped` as
`not_applicable` — CLAUDE.md §7.2 and §4.5 both say a `test_authoring` task
is not measured for coverage, on purpose; its suite is red by design. Despite
every gate agreeing the work was correct, the top-level verdict said `fail`,
and the `task_rejected` event named no gate as the cause.

**On the retry, the agent read its own attempt-1 proof, correctly diagnosed
the exact bug** (every gate says passed, verdict says fail, nothing names
why), correctly concluded there was nothing left in its own scope to fix, and
reported `blocked` rather than guessing — G3 working exactly as designed,
independently confirming the root cause a human reading the same file would
reach.

**Root cause:** `run_gates`' aggregate check,
`passed_everything = all(gate.status is GateStatus.PASSED for gate in results)`,
treated *every* non-`PASSED` status as a reason to fail, including a gate
that was `SKIPPED` because it legitimately does not apply to this task type.
Since `coverage_delta` skips unconditionally for every `scaffold` and
`test_authoring` task the moment a real `HarnessConfig` is supplied, and
two-phase TDD means every `implementation` task depends on a verified
`test_authoring` task first, **no `test_authoring` task could ever pass
verification with a real harness** — not a one-off, a systemic block on the
loop's core function. Confirmed by temporarily reverting the fix and
re-running the new regression test: it reproduces the exact real-run symptom
(`verdict.json` all-passed, `Proof.verdict` still `FAIL`).

**Why 403 passing tests never caught it:** the only test in the whole suite
that runs the real six-gate pipeline end to end
(`test_orchestrator.py::test_real_gates_end_to_end`) starts its
`test_authoring` node **pre-verified** and only actually dispatches the
`implementation` node, where `coverage_delta` genuinely measures rather than
skips. Nothing exercised the intersection of "real harness" + "a
`test_authoring` task actually runs" + "check the aggregate verdict."

**The fix**, in `redgear/verifier.py`: a new module constant
`_COVERAGE_NOT_APPLICABLE = frozenset({"scaffold", "test_authoring"})`, now
the single source both `coverage_delta_check` and `run_gates`' aggregate
check read, so the two cannot drift apart the way the bug let them. The
aggregate check now treats `coverage_delta` as satisfied when it is `SKIPPED`
*and* the task type is in that set — every other skip reason (short-circuit
after an earlier failure, or no harness configured at all) still counts
against the verdict exactly as before. Verified this does not weaken the "no
harness" footgun (§7.3): in that case `lint`/`tests_pass`/`criteria_coverage`
are *also* skipped for every task type, and none of those three gained an
exemption, so the aggregate check still correctly fails.

One regression test added,
`tests/test_gates_coverage.py::test_a_correct_test_authoring_turn_passes_the_real_pipeline`
— calls `run_gates` directly (not the individual gate function) against a
real repo with a genuinely correct, in-scope `test_authoring` change, and
asserts `Proof.verdict is Verdict.PASS`. No frozen-file edit needed: nothing
existing asserted the old (buggy) aggregate behaviour, only individual gates'
own `SKIPPED` status in isolation.

**Confirmed by rerunning the real test after the fix**, not just by the unit
test: same throwaway project, fresh repo (the first attempt's task had
already escalated, and there is no "un-escalate" command by design — starting
over cleanly is the honest path, not hand-editing state). T-0001 verified on
its first real attempt this time, all six gates recorded correctly. See the
next entry for what happened to T-0002.

### The loop drove a real project end to end, unattended — 2026-08-24

**The claim this project spent its whole life unable to make about itself.**
`redgear run` against a live Claude Code CLI (2.1.237, MSIX install), a
two-task plan with a genuine dependency, no human in the loop:

```
complete: 2 iteration(s), 2 verified, 0 escalated      exit 0      $0.5677
```

| | dispatch | gates | commit |
| --- | --- | --- | --- |
| T-0001 `test_authoring` | 14 turns, $0.3291 | 5 passed, `coverage_delta` skipped `not_applicable` | `f15781e`, 9 files |
| T-0002 `implementation` | 12 turns, $0.2386 | **all 6 passed** | `9caa6a5`, 9 files |

Both on their **first attempt**. `tests_pass` recorded
`red_via_failure: 1 of 1 target test(s) fail` for T-0001 — correct inverted
polarity — and `lint` recorded `ignored_out_of_scope: 1 violation(s) outside
this task's scope were not counted`, which is the scope-filtered lint gate
working exactly as §7.2 specifies rather than blaming an agent for
pre-existing dirt.

**What this specifically proves, beyond "it ran":**

- T-0002 claimed with `base_commit=f15781e` — its predecessor's commit, not
  the pre-run one. That single field is the whole fix: it is what the old
  design got wrong and what made the previous attempt impossible.
- `redgear rebuild` replays the 13-event log onto the plan and reproduces the
  projection byte-identically (G4).
- The tree ended with exactly one dirty path, `.redgear/events.jsonl` — the
  documented trailing `task_committed` lag, harmless because `.redgear/` is
  excluded from the dirty-tree check.

**What it does not prove.** One run, two tasks, a trivial `add()`. It does not
establish that the loop handles a long plan, a retry, an escalation, or a
genuinely hard task. The bootstrap graph (41 nodes) remains undriven, and the
self-hosting gap in §5 is unchanged — this was a throwaway project, not
redgear's own repository.

### Two blockers found by pre-flighting that run, before spending anything

Both were caught by dry-running and probing rather than by paying for a real
dispatch, which is the entire argument for `--dry-run` and `doctor` existing.

**`coverage_source` was hardcoded and the CLI read no harness config at all.**
§7.3 has always said harness commands come from `config.json` only;
`HarnessConfig(` appeared exactly once in `cli.py`, inside `_default_harness`,
with `coverage_source=["src"]` and no configuration lookup anywhere. Against a
project laid out as `calc/`, coverage collected nothing and `coverage_delta`
failed with `harness_error: no coverage data was produced by the harness` —
on every attempt, escalating the task after three *paid* dispatches for a
reason having nothing to do with the agent's work. Confirmed by running the
full loop against a copy with the real default harness before spending
anything.

Fixed: `cli._load_harness` reads `commands` and `gates` from `config.json`,
`redgear init` writes one with the real commands filled in, and `doctor`
reports whether the configured source exists.

**Why the coverage source is required in configuration rather than derived
from the plan.** Deriving it from a task's `writable_globs` was the obvious
convenience and is rejected: the plan is generated by an agent, so that would
route an agent-authored value into a subprocess argv, which §7.3 ("no
agent-supplied value reaches `cmd`") and §11.1 rule 2 both forbid outright.
Deriving it from `project.root_globs` was the other suggestion, and that field
**does** exist — on `Spec`, not on `TaskGraph`. It is rejected for the same
reason, not for absence: `spec.json` is written by `redgear plan`, so its
`root_globs` are as agent-authored as a task's scope globs, and routing them
into a `coverage --source` argv is the same violation. (An earlier note here
claimed the field did not exist at all. It does; the decision is unchanged,
because the argv argument was always the load-bearing one.)

So: configured, validated against the repository, and seeded at `init` so
nobody meets the error.

**Adapter drift: the installed CLI had moved 2.1.229 → 2.1.237.** Two flag
findings, one of them latent since T-0035:

- **`--max-spend-usd` does not exist.** The CLI documents `--max-budget-usd`.
  This adapter had guessed the name; it was never caught because nothing ever
  populated `per_turn_usd`, so the flag was never emitted. It would have
  fired on the first run that set a spend cap.
- **`--max-turns` is accepted but no longer documented** anywhere in the
  242-line help. It is emitted on *every* dispatch, so its eventual removal
  breaks every turn. Settled by one real $0.078 dispatch rather than guessed;
  a zero-cost probe via `--version` was tried first and **proved nothing** (a
  deliberately bogus control flag also exited 0), so it was discarded rather
  than reported as evidence.

### Three defects in the budget cap, all real, all fixed

`Budget.per_turn_usd` was documented, plumbed, and completely inert:

1. It was **never propagated** from `Budget` to `ClaudeCodeConfig` — `cli.run`
   built the config with the executable alone, so `per_turn_usd` stayed
   `None` forever and setting a cap on the Budget did nothing at all.
2. The flag it would have emitted was the wrong name (above).
3. The default was `None`, meaning **no cap**, which is the wrong default for
   an unattended loop that spawns a paid subprocess.

Default now `1.00`, calibrated against measured cost rather than guessed:
observed dispatches have come in at $0.065, $0.218, $0.239, $0.281 and
$0.329. A $1.00 ceiling is ~3x the observed per-dispatch cost and caps a
two-task three-attempt worst case near $6 instead of at infinity. `redgear
run` now prints the cap in its banner.

### Three traps found while implementing the commit boundary

Each cost real debugging time and none was predictable from the design.

**A `:(exclude)` pathspec naming a gitignored path makes `git add` fail
outright.** The first design used both `.redgear/.gitignore` (for `locks/`
and `STOP`) *and* `git add -A -- . ':(exclude).redgear/locks'`. Together they
break every commit: git reports "The following paths are ignored by one of
your .gitignore files … use -f" and exits 1. The ignore rule covers the
untracked case on its own; the already-tracked case (which is real — the e2e
repo has a committed `run.lock`) is handled by staging everything and then
unstaging the exceptions.

**`git diff --cached` does not show intent-to-add entries.** An `-N` file is
in the index but absent from the cached diff, which is precisely why it is
easy to miss that `git clean` cannot see it either. `git ls-files` is what
shows it.

**`git diff --cached` and intent-to-add, again.** See the entry above; the
same blind spot bit twice in one session, once in the revert and once in the
commit's staged-file listing.

### Reverting a task commit conflicts on `events.jsonl` — a deliberate trade, not a gap

**Decided, not merely observed.** A future session reading only "revert
conflicts" will try to fix it, so the reasoning is recorded here and in
`CLAUDE.md` §7.6.

**What happens.** Each task commit carries the event log as appended at that
point; every later commit appends to the same file. So `git revert` of an
earlier task commit has to *delete* log lines that later entries were written
on top of, and git reports a content conflict in `.redgear/events.jsonl`.

**Why that is correct.** §11.1 rule 5: lines are appended, never edited,
reordered, or deleted. A clean revert would violate it. The conflict is the
audit trail refusing to lose history — the mechanism working, not failing.

**The alternative, and why it was rejected.** Excluding `events.jsonl` and
`task_graph.json` from task commits would make every task commit cleanly
revertible. It would also put the work in one commit and the evidence for it
in another — **the exact split-brain this whole change exists to close.** A
commit that contains verified work but not the proof that it was verified is
the thing redgear is built to prevent, and buying `git revert` ergonomics with
it is a bad trade.

**So: keep it.** The documented undo is
`git checkout <sha>^ -- <writable globs>` — restore the task's work paths and
leave the log alone. The log then records that the task was verified and later
undone, and **both statements stay true**, which is the entire point of an
append-only log.

One consequence, accepted: the README cannot promise "revert with one
command", and deliberately does not. It says one commit per verified task,
each carrying the proof that justifies it.

### The spec's `out_of_scope` line on auto-committing — read, not amended

`.redgear/spec/spec.json` says *"Auto-committing, pushing, opening pull
requests, merging, or deploying **on behalf of the user**"*. G6's amendment
sits against that line, and the deliberate decision was **not to amend the
spec.**

The reading: "on behalf of the user" most naturally scopes the whole list to
operations against **shared history** — it is the same register as pull
requests, merging and deploying, none of which are local. A local commit of
work redgear itself verified, in a repository the user explicitly pointed it
at and which it never pushes, is defensible as outside that boundary.

That is a judgement call and is recorded as one rather than asserted as
certain. What tipped it is the cost of the alternative: amending the spec
means a new content hash, `spec/history/`, all 41 nodes rewritten, `CLAUDE.md`
§12.1, and an authorized `test_hashing.py` edit — the full §3.5 procedure — to
clarify a line that arguably already permits this.

**If it later reads as a stretch, amend then** — and bundle it with the two
known defects below (`mark_verified`'s `attempt` and `gates_passed`), so one
hash change fixes three things instead of three hash changes fixing one each.

### Nothing commits between tasks — RESOLVED, see §2

The rerun above got further, but T-0002 (the dependent `implementation` task)
escalated after three real attempts, on `scope_check` every time, over
`tests/test_calc.py` — a file it never touched. Confirmed directly against
the real repo: that file was **never committed**. T-0001 verified it, but
redgear never commits (G6, by design — "the human commits"), and nothing else
committed it either in an unattended `redgear run`. So when T-0002 claims and
computes its own `base_commit` fresh (`git rev-parse HEAD`, per CLAUDE.md
§7.4), that is still the pre-T-0001 commit, because HEAD genuinely never
moved. `scope_check` diffs against that stale baseline, so T-0001's own
legitimate, already-verified output looks like an out-of-scope write for
T-0002 — permanently, on every attempt, regardless of what T-0002's agent
does. **The agent diagnosed this correctly on its third attempt** —
`git log --oneline -- tests/test_calc.py` shows no commit ever added that
file... I cannot fix it without staging or committing a `tests/**` path,
which is outside my writable/creatable scope for this task — and reported
`blocked` rather than retrying blindly. G3 working as designed, again; the
task itself is genuinely unfixable from inside it.

**Why nothing caught this either.** Every existing test that runs two
dependent tasks in one `orchestrator.run()` call either pre-verifies the
`test_authoring` node without ever actually dispatching it
(`test_real_gates_end_to_end`, `test_pass_advances_and_continues`, and every
other `test_orchestrator.py` scenario), or has both tasks write to
non-overlapping paths that are never frozen for each other. Nothing in the
suite exercises "a real `test_authoring` task actually writes a new file,
gets verified, and its real dependent `implementation` task claims next" —
because that needs two genuine dispatches in sequence, which no test does.
And redgear's own bootstrap (T-0001 through T-0041) never hit it either,
because a **human** committed between every pair by hand, exactly as §4.6.1
documents: *"Complete both phases of a pair in one session... commit once
when green."* That instruction was written for the human-driven manual phase.
Nothing says who or what performs the equivalent step inside an unattended
`redgear run`, and the answer, checked directly, is: nobody does.

**RESOLVED: option 1, with G6 reworded.** The decision, made explicitly by
the human: G6 was never actually about commits, it was about *destructive*
git operations, and the original wording conflated the two. A local commit
destroys nothing and is trivially undoable. The amended guarantee:

> redgear commits verified work to the local repository and does nothing else
> to git. It never pushes, rebases, resets, force-updates, cherry-picks, or
> rewrites history. Every commit is one verified task and is trivially
> undoable.

That is a more precise version of the same protection, not a retreat. The two
alternatives — changing what `scope_check` diffs against, or pausing the loop
for a human commit — were rejected: the first is more invasive to
`verifier.py` and needs care that a mimicked diff cannot smuggle an unverified
change past scope; the second ends "continuous, unattended" as a true
description for any plan with more than one task, which is every real plan.

**Three behaviours, decided together**, because each one's safety depends on
the other two. Implemented in `redgear/vcs.py` and documented normatively in
`CLAUDE.md` §7.6:

1. **On verification: commit.** One commit per verified task, immediately
   after the proof is written and `task_verified` is appended.
2. **On rejection with attempts remaining: revert the tree to HEAD.**
   Otherwise attempt 2 starts on top of attempt 1's failed code and the agent
   inherits half-finished work it did not write. Each attempt becomes an
   independent, auditable experiment.
3. **On escalation: commit nothing, revert nothing.** A human has to diagnose
   it; reverting destroys the evidence and committing enshrines broken work.
   `redgear status` now names the dirty paths and prints the discard command.

**The knock-on, checked rather than assumed.** If every attempt reverts on
rejection and escalation always ends the run, then every dispatch begins from
a clean tree at a known commit, `base_commit` is a true baseline again, and
option 2's snapshot machinery is unnecessary. That reasoning holds — the
inductive step is real, and the reason escalation cannot break it is that
*every* escalation path in `orchestrator.run` returns `finish("blocked")`
immediately, so there is never a next dispatch to inherit the dirty tree.

**But it had two holes**, and one was open in the code:

- **The parse-failure retry.** `_dispatch_with_one_retry` dispatches twice
  with nothing in between, so dispatch 1 could write files, fail to parse, and
  leave them in dispatch 2's diff. Now reverted between attempts (reason
  `unparseable_result`), on the grounds that an unparseable result carries no
  `changed_files` claim and no verdict, so that work is unverifiable by
  construction. Not reverted after the *last* try: the run ends there as
  `runner_error` and a human needs the wreckage.
- **A human editing during a run.** §8.4 only checked at run start. The tree
  is now asserted clean **before every claim**, and unexpected dirt raises
  `E_DIRTY_TREE` and reverts *nothing*. This is what licenses the revert at
  all: if the tree is clean when a turn begins, everything dirty when it ends
  belongs to that turn. It also closes the §7.4 documentation gap a previous
  session recorded but did not act on.

**What the revert deliberately does not bound itself to.** Scoping it to the
task's `writable_globs` was considered and rejected: an out-of-scope write is
*by definition* outside those globs, so a scope-bounded revert would leave
behind exactly the poison it exists to remove. A file the agent created
outside its scope is removed, and the evidence survives because
`persist_proof` (including `diff.patch`) runs **before** the revert. That
ordering is load-bearing and is asserted in `test_commit_boundary.py`.

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
POSIX `killpg` branch is exercised only in CI.

### `killpg` without `start_new_session` signals the runner itself — CI-only, and it happened

This is what the "unverified locally" warning above was hiding, and it is
worth reading before adding any new `Popen` call.

**Symptom:** every job green on Windows; the **pytest job fails in CI** with
no useful summary. Coverage fine, lint fine, the same command passing locally.

**Cause:** `terminate_process_tree` kills a process *group*
(`os.killpg(os.getpgid(child.pid), SIGTERM)`), but neither `Popen` call in the
package created one. On POSIX a child inherits its parent's process group, so
`getpgid(child)` returned **pytest's own group** — and killing it on timeout
sent `SIGTERM` to the test runner. Two tests reach that path deliberately
(`test_gates_tests.py::test_timeout_is_gate_failure` and
`::test_run_command_returns_a_result_on_timeout`).

Invisible on Windows because that branch uses `taskkill /F /T /PID`, which
walks the tree by parent id and touches nothing else.

**Fix:** `start_new_session=True` on both `Popen` calls (`verifier.run_command`
and `ClaudeCodeRunner._spawn`). Accepted and inert on Windows, `setsid()` on
POSIX. `terminate_process_tree`'s docstring now states the precondition,
because the coupling is otherwise invisible from either side.

**The general lesson:** a platform branch nobody can run locally is not
"probably fine". This one was wrong from T-0019 and stayed wrong through six
pairs, because the only signal was a CI job that fails without saying why.

### gitleaks needs `fetch-depth: 0`

`actions/checkout` defaults to a depth-1 shallow clone. `gitleaks-action@v2`
scans history, and on a shallow clone it cannot resolve the commit range — it
fails with a config-shaped error rather than a leak report, which reads like a
secret was found when none was. Only the secrets job needs the full clone;
every other job is happy shallow.

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

### `pyproject.toml` never gained the dependencies §2.1 always specified — invisible for 37 tasks

**Symptom:** `pip install -e ".[dev]"` succeeded and the full suite stayed
green through T-0001–T-0037, even though `CLAUDE.md` §2.1 has specified
`fastapi >= 0.111 + uvicorn` ("Control plane API") since the contract was
first written. Nothing failed until T-0038 tried to `import fastapi` for the
first time and it was not installed.

**Cause:** T-0001 (repository bootstrap) is the task that scaffolds
`pyproject.toml`, and it built the dependency list from what the *first nine
modules* needed (`pydantic`, `typer`, `rich`), not from the full §2.1 table —
reasonably, since the control plane was 37 tasks away. But nothing ever
diffed the manifest against the contract's stack table afterward, so the gap
sat there, silent, because nothing exercised it. A scaffold task's job is
partly to encode a *promise* about what the project will need; a promise
nothing checks is invisible right up until the moment something needs it,
and then it looks like a surprise rather than a known gap.

**Fix:** added at T-0038/T-0039, when `api/app.py` first needed `fastapi` and
`uvicorn` (runtime) and `httpx` (dev, for `fastapi.testclient.TestClient` —
pinned exactly, per the T-0024 version-drift trap above, not floored like the
other runtime deps).

**General lesson:** a scaffold task that declares a stack in the contract but
not in the manifest fails silently until something needs it. Worth a periodic
check — `pyproject.toml`'s dependency list against `CLAUDE.md` §2.1's table —
rather than waiting for the next unbuilt module to notice on its own.

### `git`'s output is UTF-8 regardless of locale; Windows text-mode decode is not

**Symptom:** `state_engine.persist_proof` received `diff=None` and crashed
with `AttributeError: 'NoneType' object has no attribute 'encode'` — deep
inside a call to `gitctx.diff_patch` that, by its own source, cannot return
`None`. Reproducible for specific fixture repos and specific test runs, not
others; the byte position in the reported `UnicodeDecodeError` moved between
runs, which was the first sign this had nothing to do with the diff content
itself.

**Cause:** `gitctx._run_git`'s `subprocess.run(..., text=True)` did not pass
an explicit `encoding=`, so Python decoded git's stdout using
`locale.getpreferredencoding()` — **`cp1252`** on this machine, a plain
Windows install. Git itself emits UTF-8 regardless of host locale. When a
byte git wrote (e.g. as part of a right-quote character in some git-generated
text) has no mapping in cp1252 (`0x9d` is one of several undefined codepoints
in that table), the decode raises **inside `subprocess`'s internal reader
thread** — invisible to the caller as an exception, because `capture_output=True`
spawns a background thread to read one stream while the main thread reads the
other, and an exception in that thread does not propagate to
`subprocess.run`'s return. `check=True` only inspects the exit code, which was
`0` (git succeeded). The net effect: `result.stdout` silently ends up `None`
even though the git command itself worked, and every caller downstream is
unprepared for a "successful" git call returning no output at all.

This was latent in `gitctx.py` since T-0021 — every `_run_git` call was
exposed to it — and simply had not been hit by content that happened to
trigger it until `persist_proof`'s new, unconditional `diff_patch` call in
`orchestrator.run` (T-0039) started running real git diffs on every task
attempt in tests that previously used a stubbed verifier and never touched
real git output at that volume.

**Fix:** `gitctx._run_git` now passes `encoding="utf-8", errors="replace"`
explicitly, matching git's actual output encoding rather than trusting the
host locale, with `errors="replace"` as the fallback for the (now much
smaller) set of bytes that still cannot decode — diagnostic text must never
raise or silently vanish over one unrepresentable byte (§1.4 G7's reasoning
about harness output applies equally to a diff).

**Not yet applied:** `state_engine.py`'s own private `_git` helper (used by
`claim_task` for `rev-parse HEAD` and the frozen-digest file listing) has the
same `text=True` pattern without `encoding=`. It has not been observed to
trigger this — commit hashes and typical file paths are plain ASCII — but it
is the same latent shape. §5's open item about replacing `state_engine._git`
with `gitctx`'s functions (deferred since T-0021) would fix this as a side
effect; noted here so it is not forgotten if that replacement keeps being
deferred.

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

**T-0038/T-0039 required no frozen-file edits.** `tests/test_api.py` builds
its fixture state entirely through `state_engine`'s existing public write
functions (`claim_task`, `persist_prompt`, `record_turn`, `reject_task`,
`mark_verified`) rather than hand-assembled JSON, and the two proof artifacts
`persist_proof` did not yet exist to write are constructed by hand in the
exact shape §2.3 already documents — so Phase 2 could implement both the
reader and the (previously-missing) writer without needing to touch a single
already-frozen test to make the pair pass. `pyproject.toml`'s dependency
addition for this pair (Fix 1, `fastapi`/`uvicorn`/`httpx`) happened *before*
Phase 1 began, under separate, explicit authorization — not as a Phase-2
frozen-file edit.

| `tests/test_errors.py` | `len(documented) == 19` → `== 20` (one line, plus its explanatory comment) | *Consequent* edit of the authorized §4.7 amendment adding `E_COMMIT_FAILED`. Exactly the same category as this file's two earlier entries: the same constant went `20` → `19` at T-0031 when `E_NO_READY_TASK` was struck, and `test_hashing.py`'s `REAL_SPEC_HASH`/`REAL_SPEC_ID` moved at T-0041 when the spec was re-hashed. The assertion is a parse sanity check mirroring §4.7's row count — its own comment says *"update it with the table"* — so leaving it at 19 would make an authorized contract amendment unlandable. Explicitly authorized in-session. |
| `tests/test_events.py` | Two payloads added (`task_committed`, `working_tree_reverted`); the round-trip count `14` → `16` | *Consequent* edit of the authorized §3.6 amendment (G6's commit boundary). Without it the two new types would be the only ones in the taxonomy with no round-trip coverage, and the test's own docstring ("All 14 section 3.6 types") would be false. Explicitly authorized in-session. |
| `tests/test_orchestrator.py` | Comment in `test_termination_reasons_and_exit_codes` (comment only, no assertion touched) | It read that a second run "would refuse to start on the tree the first one left dirty". No longer true: a completed run now commits its work and leaves the tree clean. The assertions were and remain correct. Explicitly authorized in-session. |
| `tests/test_hashing.py` | `REAL_SPEC_HASH`/`REAL_SPEC_ID` constants updated to the new spec's values (`spec-97ee71`) | Direct, mechanical consequence of the T-0041 spec amendment (§2) — the same *consequent-edit* category as the `test_errors.py` count above, not a defect correction. The module's own docstring states the tests exist to track the *real* `spec.json` on disk, not a frozen historical snapshot, so leaving the constants at the superseded value would have made the authorized amendment look like a bug rather than reflecting it. |

---

## 5. Open questions — need a human decision

### The corrective-retry path has still never run against a real agent

**Two live runs, four tasks, four first-attempt passes.** The loop's retry
behaviour — reject, revert, recompose with the §5.5 failure excerpt, succeed —
is covered by the fake runner and by `test_orchestrator.py`, but the fake
never *reads* the prompt, so nothing has yet shown that a real agent receives a
useful excerpt and acts on it. That is the claim the README makes ("the next
prompt for it carries the actual failure excerpt") and it remains unevidenced.

**The second experiment was designed to provoke it honestly and failed to.**
The task was `money.round_half_up(value, places)` — round half away from zero —
which is the canonical Python float trap: verified beforehand that naive
`round()` returns 2.67/-2.67/1.0/2.0 for the four required cases and fails all
four, while `Decimal(str(v))` with `ROUND_HALF_UP` passes all four. The
implementing agent went **straight to `Decimal(str(value)).quantize(...,
ROUND_HALF_UP)` on its first attempt**, with `--allowedTools
Read,Glob,Grep,Edit,Write` — no `Bash`, so it could not run the tests and did
not iterate its way there. It simply knew the trap.

**A third experiment targeted `lint` instead, and also passed first time.**
The repository carried a `ruff.toml` selecting `["E", "F", "EM"]`, where
EM101 forbids `raise ValueError("literal")` — the form nearly everyone
writes. Verified beforehand in both directions: the idiomatic implementation
passes all three tests but trips EM101 twice (so `lint`, gate 3,
short-circuits before `tests_pass` ever runs), while the `msg = ...` form
passes both. The agent wrote the `msg = ...` form immediately, and its own
summary says why:

> "Used an intermediate msg variable before each raise to satisfy the EM
> (flake8-errmsg) ruff rules enabled in **ruff.toml**."

**It read the lint configuration.** That is the finding, and it invalidates
the reasoning that picked this experiment: there is no such thing as a gate
whose *configuration* is hidden from the agent, because `--allowedTools`
grants `Read, Glob, Grep` over the whole repository. An earlier note in this
file claimed the ruff config "is a real constraint that the prompt does not
carry" and was therefore invisible — the prompt indeed does not carry it, but
the repository does, and the agent can read the repository.

**What this actually says about the system, and it is not a negative.** By
the time a task is dispatched the agent has: the exact acceptance criteria,
read access to the frozen tests (i.e. the answer), read access to every
config file that governs every gate, the exact commands that will be run, and
an explicit statement that verification is independent. **The prompt is good
enough that a competent agent rarely fails**, which is the product working as
designed. It also means a first-attempt failure cannot be provoked by hiding
something, because nothing is hidden.

Worth recording from the same turn: the agent declared in `known_gaps` that
it could not execute ruff or pytest itself (no `Bash` in its allowlist) and
had verified by review instead. An honest, unprompted declaration of a real
limitation — G3's mechanism doing exactly what §5.3 asks of it.

**What is left, if this is pursued.** `coverage_delta` is the only gate whose
outcome is genuinely not readable from the repository: it depends on which
lines the tests actually execute under instrumentation, which cannot be
determined by reading and — with no `Bash` — cannot be measured by the agent
either. §10.5's `undercovers` scenario is exactly this. Everything else the
agent can look up.

**Spend so far on provoking a retry: $1.49** across two runs (float-rounding
$1.1062, lint $0.3832), against a $0.60-1.00 estimate for the first. Stopped
rather than starting a fourth attempt on momentum.

### Nothing commits between tasks — a real multi-task `redgear run` cannot currently complete unattended

**Resolved.** G6 was amended, `redgear/vcs.py` added, and a two-task plan now
completes unattended. The full account — the decision, the three behaviours,
the knock-on check and its two holes — is in §2. Kept as a pointer because
this was the file's biggest open question for two sessions and a reader
scanning §5 for it should find where it went.

The historical account below is left for context.

**The short version:** G2's two-phase freeze checks a task's scope by diffing
against `base_commit`. G6 says redgear never commits — "the human commits."
Nothing currently plays that role inside an unattended `redgear run`, so a
verified task's own output (real, uncommitted, sitting in the working tree)
permanently looks like an out-of-scope write to the very next task that
depends on it. Found by the first real run this project has ever made
against a live agent CLI, on a plan with exactly two dependent tasks — as
plain a case as the mechanism can produce, and the loop could not get past
it. Confirmed by reading the real repo directly (`git log -- <file>` showed
no commit ever added it), and independently confirmed by the real agent's own
diagnosis on its third attempt, reaching the identical conclusion.

**Three shapes a fix could take**, detailed in §2, none chosen: let redgear
commit narrowly between tasks (rewrites G6 and the README's "never commits"
claim); change what `scope_check` diffs against, using each verified task's
own recorded `proof/diff.patch` instead of git commit boundaries (keeps G6,
more invasive to `verifier.py`); or pause the loop for a human commit between
tasks, the way it already pauses for `blocked` (keeps every current
guarantee, but ends "continuous, unattended" as a true description for any
plan with more than one task — which is every real plan). Each one changes
either a headline guarantee or the loop's core behaviour, which is why this
sits here rather than being decided in the session that found it.

### `redgear/hashing.py`'s docstring still names the superseded spec

T-0041 amended `spec.json` (§2: NFR-10, FR-12, the new hash `spec-97ee71`).
`CLAUDE.md` §12.1's table had the same staleness (`spec-dd2914`, the old
hash, "10 out-of-scope boundaries") and was fixed in the session that removed
the README's "What's not done yet" section, once "update the living docs"
gave explicit authorization to touch `CLAUDE.md`.

`redgear/hashing.py`'s module docstring still illustrates the reference
implementation with *"the recorded spec digest ``sha256:dd2914...``"*.
Cosmetic (an example inside a comment; nothing reads or asserts it), but
stale for the same reason, and `redgear/**` was outside both the T-0041 task
that introduced the staleness and the README-cleanup task that fixed
`CLAUDE.md`. Worth folding into whatever session next touches `hashing.py`
for an unrelated reason.

### `release.yml`'s GitHub/PyPI setup — still needs doing by hand

The workflow itself is correct for PyPI trusted publishing: `id-token: write`
on the `publish` job, no stored token anywhere, `pypa/gh-action-pypi-publish
@release/v1`. **Neither side of the actual trust relationship exists yet**,
and nothing in this repository can create either — both are configured on a
website, by a human with the right account access:

1. **On GitHub**: create an Environment named `release` on this repository
   (Settings → Environments) — the workflow's `environment: release` refers
   to it, and it does not exist until someone creates it. Environment
   protection rules (required reviewers, restricted branches) are optional
   but are exactly what an environment gate is for on a publish step.
2. **On PyPI**: register `redgear` as a project (if not already claimed) and
   add a **trusted publisher** for it (PyPI project → Settings → Publishing)
   naming this exact repository, the `release.yml` workflow filename, and the
   `release` environment. Trusted publishing has no token to generate or
   store — the whole point is that the OIDC exchange at publish time replaces
   one — but the publisher relationship itself has to be declared on PyPI's
   side before the first publish can succeed.

Until both exist, a GitHub Release publish event will reach the `publish` job
and fail at the OIDC exchange, not silently succeed or silently no-op.

### The Claude Code adapter's real-CLI verification — RESOLVED, within a stated scope

Was an open question through T-0039; resolved by the manual verification
session recorded in §2 above (claude 2.1.229, Windows, 2026-08-19). §12's
prediction held — *"Milestone 10 is an adapter, and adapter bugs are
integration bugs"* — and every one found was in parsing, not in `build_argv`.

**What "resolved" does and does not mean here**, so a future reader does not
read more into it than three dispatches can support:

- Verified: an unauthenticated failure, a plain success with no
  `--json-schema`, a success *with* `--json-schema`. All three exit-code /
  `is_error` combinations that matter, and the `structured_output` mechanism.
- **Not verified this session:** `--bare`, `--mcp-config`, `--max-spend-usd`,
  a `blocked`/`scope_insufficient` outcome from a real agent, or any
  redgear-schema-conformant `structured_output` from a real dispatch (the
  manual test used a minimal test schema, not `agent_report_schema()`) — see
  the two open items below for the two of these that matter most in practice.
- **Not verified on any platform other than Windows**, and not against any
  install method other than the MSIX/Claude-Desktop one.

Two smaller items fell out of this that are still open:

#### `permission_denials` — surface it, or leave it invisible?

All three real captures had `permission_denials: []`. `TurnResult` has no
field for one. If a real dispatch ever attempts a disallowed tool, the
orchestrator currently cannot know — it is silent information loss, not a
crash or a wrong verdict, but it means a class of `--allowedTools` violation
that G6/§8.2 cares about a great deal is not visible anywhere in the event
log.

Adding it would mean: a new `TurnResult`/`AgentTurnReport`-adjacent field (it
is runner-populated, not agent-supplied, so it belongs with `exit_code` and
`session_id`, not with `outcome`/`summary`), and a decision about whether it
should ever affect a gate verdict (G1 says no field an adapter populates may
influence a gate — a denial is redgear's own observation of the *agent's*
constrained behaviour, arguably different in kind from the agent's self-report,
but this wants a deliberate reading of G1 before landing, not an assumption).
Not done this session — no real sample to test against, and it touches
`schemas.py`, which is genuinely out of scope for an adapter-parsing fix.

#### `Budget.per_turn_usd`'s default — RESOLVED, and it was three defects not one

Fixed 2026-08-24: never propagated to the runner config, wrong flag name, and
no cap by default. See §2. Default is now `1.00`.

#### Superseded cost note

Real costs observed: $0.065 for a near-empty 2-token prompt (cache-creation
dominated), $0.218 for a 4-turn dispatch reading one 10-byte file. A real
redgear dispatch carries up to an 8,000-character prompt (§5.6's cap) and
plausibly several turns of tool use — almost certainly more expensive than
either sample. Nobody has checked `Budget.per_turn_usd`'s default against this
yet. Worth doing before trusting it as a real ceiling on an unattended run;
not done this session, since it needs a cost model rather than a parsing fix.

### The self-hosting crossover — the decision is made (§2); the underlying gap is not closed

§4.6 said "From `T-0033` onward redgear can drive its own remaining tasks."
It still cannot, and by T-0041 all three of the original blockers have a
final status:

1. **Still open, and it is the one that always mattered most: there is no
   event log.** `.redgear/` contains `spec/` and `task_graph.json` only;
   every node still sits in its pristine initial state (`T-0001` `ready`,
   everything else `blocked`). redgear pointed at its own repository today
   would select T-0001 and try to bootstrap a tree that has already been
   bootstrapped for 40 done tasks.
2. **RESOLVED at T-0034/35, and now more resolved than it was**: the adapter
   is not just a concrete `Runner` — it has been checked against a real CLI
   (claude 2.1.229, Windows, 2026-08-19; see the T-0038/39-era §2 entry).
3. **RESOLVED at T-0036/37.** `redgear approve` and `POST /plan/approve` both
   move the graph `draft → active` through `planner.approve_plan`, with a
   real `plan_approved` event.

**What T-0041 changed is not #1 itself — it is what the project says about
#1.** The previous version of this entry framed "what to do about the missing
event log" as an open decision with four unattractive options. It no longer
is one: §2's "self-hosting claim" entry records the decision made this
task — state the gap plainly in the README, fabricate nothing. That is a
different question from "how do we make the crossover real," which is still
genuinely unsolved and has exactly one honest answer: an approved plan and a
real event log, built the way every other task in this graph was — no
shortcut removes that. If that work happens, it happens as its own task, not
as a retroactive fix to this file.

**What IS true now:** the full command surface exists, works end to end, and
is verified against a real agent CLI — `redgear plan`, `redgear approve`,
`redgear run`, `redgear ui`. Every refusal on the path to a real self-hosted
run fires correctly and in the right order. The engine is complete and its
one remaining unverified claim — "the loop can drive a real project
unattended" — is stated as unverified, not implied otherwise.

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

### `state_engine.py` still carries a private git helper

T-0015 added `_git`, `_repo_files` and `_frozen_digest_map` to
`state_engine.py` as a stand-in, because `gitctx` did not exist yet. **It does
now (T-0021)**, and those three can be replaced by `gitctx.head_commit` and
`gitctx.tracked_and_untracked`.

Still not done, deliberately: it is a refactor of a module whose tests are
frozen, so it deserves to be a considered step rather than a side effect. The
duplication is small and harmless until then, but it means two code paths ask
git the same questions — and only one of them has the repository-root guard
described above, and (as of T-0039, §3) only one of them decodes git's output
as UTF-8 rather than the host locale. `state_engine._git` has the same
`text=True`-without-`encoding` shape `gitctx._run_git` had; it just has not
been observed to trip it yet, because the values it reads (commit hashes,
typical file paths) are plain ASCII in every fixture so far. Replacing it
would fix that as a side effect, which is one more reason this keeps coming
back up rather than a new one.

### `E_INVALID_CLAIM` and `E_LEASE_EXPIRED` are declared but unimplemented

Both are in §4.7. `locks.py` raises `RunLockedError` for a wrong-token release
(where `E_INVALID_CLAIM` arguably belongs) and treats lease expiry as an
*event*, not an error, so neither class exists yet. Consistent with the
"registry grows as raisers land" rule; still nobody has needed either badly
enough to add it.

### `task_verified.gates_passed` is a hardcoded list and says every gate passed on every task — a defect

`state_engine.mark_verified` writes `gates_passed = [gate.value for gate in
GateName]`. That is not derived from the proof; it is the full six-gate
enumeration, unconditionally, for every task that ever verifies.

**So the event log currently asserts that all six gates passed on every
verified task, including tasks where a gate was legitimately skipped.** Every
`test_authoring` and `scaffold` task skips `coverage_delta` by design (§4.5,
§7.2) — and for those, the log says it passed. It did not; it did not run.

For a project whose entire pitch is "an unverified claim is worthless", the
audit trail carrying a false statement about verification is a defect and not
a quirk. It is the same shape as the `attempt` off-by-one below (a
`mark_verified` field written from something other than the fact it names),
found the same way — while building something that needed to read it — and it
wants the same fix window.

Found while designing the commit message (§2). The commit message deliberately
routes **around** this: `vcs.build_commit_message` derives its `gates:` line
from the `Proof`, lists only gates whose status is actually `PASSED`, and
states a skipped one on its own `skipped:` line. So the commit message tells
the truth even though the event does not, and a test
(`test_commit_message_states_a_skipped_gate_rather_than_padding_it`) pins that.
Not fixed at the source: `state_engine.py` was outside this change's scope,
and a fix changes what every future replay of an already-recorded log reports
for past verified tasks — the same live concern the `attempt` fix carries.

**Recommendation: fix both together**, taking the real gate list and the real
attempt number as parameters from `orchestrator.run`, which has both in scope
at the call site. Checked directly: no frozen test in `test_state_write.py`
pins either value, so neither needs an authorized frozen edit.

### `mark_verified`'s `task_verified.attempt` field is wrong — fix, or leave documented?

Found at T-0039 while building `api/app.py`'s proof lookup (§2, §3's frozen-file
note). `state_engine.mark_verified` records `node.attempts` — the count of
prior *rejections* — as the event's `attempt`, which is one behind the real
attempt number every other event for the same turn agrees on
(`prompt_dispatched`, `turn_completed`, and the `proof_id` string itself all
use `task.attempts + 1`, computed in `orchestrator.run`). On a first-try pass
this event says `attempt: 0`.

Nothing currently reads `task_verified.attempt` except the new `api/app.py`,
which works around it (§2) rather than trusting it. Two ways to close this
properly:

- **Fix `mark_verified`** to take the real attempt number as a parameter, the
  way `reject_task` effectively does via its own `attempts + 1` computation,
  and pass it from `orchestrator.run` (which already has `attempt` in scope
  at the call site). Correct, small, and — checked directly — no frozen test
  in `test_state_write.py` pins the wrong value, so this would not require an
  authorized frozen-file edit. The risk is unknown readers: nothing greps for
  `task_verified.attempt` today, but a fix changes what every *future* replay
  of an *already-recorded* log reports for past verified tasks, which is a
  live concern for exactly the kind of audit trail this project is.
- **Leave it and keep working around it in readers.** Cheaper today, and the
  workaround is small, but it means every future reader of `task_verified`
  events has to know this field lies and rederive the real number the same
  way `api/app.py` does, or repeat the bug.

Recommendation: fix `mark_verified` directly — it is a one-parameter change,
nothing currently depends on the wrong value, and "the log says something
false" is a worse property to carry forward than "the log briefly disagreed
with the code that reads it correctly." But this wants a decision, not an
assumption, because of the "changes what past log entries mean" concern above.

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
