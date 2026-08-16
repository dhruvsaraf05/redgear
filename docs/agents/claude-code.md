# Claude Code adapter — manual integration check

**This procedure is never run in CI** (§10.6, §10.4). It needs a real
`claude` CLI, a credential, and it costs money — three things a test suite
must never require. It exists to catch **adapter drift**: third-party CLI
flags change between releases, and `redgear/runner.py` is where that breaks
first.

Run it when any of these is true:

- you have upgraded Claude Code,
- `redgear run` fails with `E_RUNNER_ERROR` and you do not know why,
- you are about to trust an unattended run for the first time on a machine,
- the "Verified against" comment at the top of the adapter section in
  `redgear/runner.py` is stale, missing, or says it was never verified.

It should be runnable by someone who did not write the adapter. If a step is
ambiguous, that is a bug in this document — fix it here.

---

## 0. Current verification status

> **The adapter has never been checked against a real CLI.**
>
> It was written at T-0035 on a machine with no `claude` on PATH. Every flag
> comes from CLAUDE.md §6.2, and every payload in
> `tests/fixtures/claude_payloads/` is **synthetic**, constructed from the
> documented shape rather than captured.
>
> **You are the first person to run this.** Expect to find at least one flag
> that differs. That is the normal outcome of a first integration, not a
> failure of the design — and §2.4 is explicit that where this repository and
> the installed CLI disagree, **the CLI is right**.

---

## 1. Preconditions

```bash
claude --version
```

If that fails, stop: nothing below will work. Install Claude Code first.

You also need a **throwaway git repository** to point at. Never run this
against a repository whose contents you care about — the agent has `Edit` and
`Write` permissions inside it and the whole point is to find out whether the
constraints hold.

```bash
mkdir /tmp/redgear-probe && cd /tmp/redgear-probe
git init -b main && git commit --allow-empty -m "baseline"
redgear init
```

---

## 2. Check what the adapter believes

```bash
redgear doctor
```

Confirm the `agent CLI (claude)` row shows a path, not `not on PATH`. This is
the fastest way to tell a PATH problem from a flag problem, and they look
identical from inside a failed run.

---

## 3. Read a prompt before sending one

```bash
redgear run --dry-run
```

Costs nothing and dispatches nothing. Read the prompt in full. Specifically
check:

- the **FROZEN** list is present and correct — its absence is the failure that
  produces a scope violation three turns later,
- the **Required outcome** block is intact, including the sentence saying that
  reporting `blocked` does not count against the attempt budget,
- no absolute path from your machine appears anywhere.

If the prompt is wrong, stop here. Fixing `prompt_engine.py` is cheaper than
debugging an agent that was briefed badly.

---

## 4. Send exactly one turn

This is the step that costs money. Cap it:

```bash
redgear run --max-iterations 1
```

Watch for the banner naming `redgear stop`. Then, while it runs, confirm the
brake works from another terminal:

```bash
redgear stop
```

The run should finish its current turn and exit 0 — not abandon the turn, and
not leave a lock behind.

---

## 5. Inspect what actually crossed the boundary

Everything the adapter did is on disk. This is the part that catches drift.

```bash
ls .redgear/runs/agent/turn-0000/
cat .redgear/runs/agent/turn-0000/argv.json
```

**Check the argv record:**

| Look for | Why |
| --- | --- |
| `-p` followed by the prompt as **one** element | §6.5 — a concatenated prompt is a shell-injection surface |
| `--output-format json` | without it there is no structured result at all |
| `--json-schema` present | this is what makes the outcome contract mechanical |
| `--allowedTools` with no bare `Bash` | §8.2 — a bare shell defeats every scope guarantee at once |
| every `Bash(... *)` has a **space before the star** | §6.2 — `Bash(git diff*)` also matches `git diff-index` |
| **no** `--dangerously-skip-permissions` | §11.1 rule 3 |
| `env_keys` lists names only, **no values** | G5 — values are never read, not merely redacted |

Then the raw payload:

```bash
cat .redgear/runs/agent/turn-0000/agent_stdout.log
```

**This is the file that matters most.** Compare its shape against
`tests/fixtures/claude_payloads/completed.json`. Check that it has:

- a top-level `structured_output` object (not nested somewhere else, not a
  string containing JSON),
- `session_id`, `num_turns`, `duration_ms`, `total_cost_usd`,
- `is_error` present when the run failed internally.

---

## 6. Replace the synthetic fixtures

If the real payload differs from the synthetic ones **in any way**, the real
one wins:

```bash
cp .redgear/runs/agent/turn-0000/agent_stdout.log \
   <redgear-repo>/tests/fixtures/claude_payloads/completed.json
```

Scrub any session id or path you would rather not commit, then delete the
"these payloads are SYNTHETIC" notice from
`tests/fixtures/claude_payloads/README.md` and record what you captured
instead.

Run the suite. If `tests/test_claude_adapter.py` now fails, **the adapter was
wrong and the test just caught it** — that is the procedure working.

---

## 7. Update the verification comment

In `redgear/runner.py`, replace the `Verified against: NOT VERIFIED AGAINST A
REAL CLI` block with what you actually observed:

```python
# Verified against: claude 1.2.3 on 2026-09-04 (macOS, OAuth, bare=false)
```

Record the version, the date, and the authentication mode — bare mode takes a
different credential path (§6.3), so "it worked" means something different
under each.

**Do not write a version you did not run.** An unverified claim here is worse
than an admission, because the next person will trust it.

---

## When a flag has changed

You will know because the CLI rejects the argv, or accepts it and returns
something the parser cannot use. Both surface as `E_RUNNER_ERROR` after two
attempts, with the raw stdout preserved.

1. **Read `agent_stdout.log` first.** A usage error names the offending flag.
2. **Check `claude --help`** for the current spelling. §2.4: the CLI is right.
3. **Fix `build_argv` in `redgear/runner.py`.** Adapter differences live in
   that module only — never branch on agent identity in `orchestrator.py` or
   `prompt_engine.py` (§2.4, NFR-8).
4. **Add or update a fixture** capturing the new shape, so the change is
   covered by the suite rather than only by memory.
5. **Update the verification comment.**

If the CLI has lost a capability the adapter needs — non-interactive dispatch,
tool constraints, a turn cap, or machine-readable output — say so and stop.
§2.4: "A CLI that cannot do all four is not supportable... say so and stop
rather than half-supporting it." A half-supported adapter produces runs whose
guarantees are quietly weaker than the ones the README promises.

---

## What this procedure deliberately does not do

- **It does not run in CI.** §10.4 forbids any test invoking a real agent CLI,
  under any marker or condition. A suite that needs a credential stops working
  the moment someone else clones the repository.
- **It does not test the gates.** Those have 43 tests of their own and need no
  agent. If a gate misbehaves here, reproduce it in the suite instead.
- **It does not validate the plan.** A bad plan produces confidently verified
  wrong software, and no amount of adapter checking catches that — the human
  review gate in §3.3 is the only thing that does.
