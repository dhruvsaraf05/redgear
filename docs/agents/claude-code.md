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

> **Verified against a real CLI: claude 2.1.229, Windows, MSIX/Claude-Desktop
> install, 2026-08-19.** Three real dispatches, `--output-format json` stdout
> captured verbatim under `tests/fixtures/claude_payloads/` (see that
> directory's README for the full account, and `docs/PROGRESS.md` for the
> reasoning behind every change it drove).
>
> §0 used to predict "expect to find at least one flag that differs" — that
> is not what happened. **Every flag `build_argv` sends already matched what
> the real CLI accepted; `build_argv` did not change.** What the real payloads
> found instead was five things the *parsing* side either assumed or had
> never been tested against:
>
> 1. `subtype` reads `"success"` even on a hard authentication failure —
>    useless as a signal, and the adapter never read it (confirmed, not
>    assumed, by re-reading the source).
> 2. Exit code and payload agreed in every sample observed (`is_error`
>    matched the exit code both times) — CLAUDE.md §6.4 rule 1 read as though
>    disagreement were the norm; it is not, the rule exists for when it
>    happens, and the payload is authoritative either way.
> 3. `structured_output` is a real, separate JSON object; `result` carries
>    the same content JSON-encoded as a string. The adapter already read
>    `structured_output` directly and never fell back to `result` — confirmed,
>    not changed.
> 4. `stop_reason: "tool_use"` appeared on a completely normal, successful,
>    multi-turn dispatch that read a file before answering. Every real
>    dispatch uses tools; nothing here reads `stop_reason`, confirmed.
> 5. `permission_denials` was empty in all three samples (none of the manual
>    dispatches attempted a disallowed tool), and `TurnResult` has no field
>    for it — so a real denial is currently invisible to the orchestrator.
>    Open question, not yet resolved either way (`docs/PROGRESS.md` §5).
>
> Two things not exercised this session and still open: `--bare`,
> `--mcp-config`, `--max-spend-usd`, and anything about a *second* Claude Code
> installation method (native installer, npm global) — this machine only ever
> had the one, MSIX-packaged install to test against.
>
> Run this procedure again — and update everything below in the same way —
> the next time any precondition in the list above this section is true.

---

## 1. Preconditions

```bash
claude --version
```

If that fails, stop: nothing below will work. Install Claude Code first.

**`claude --version` failing here does not necessarily mean Claude Code is not
installed.** On Windows, a Claude Desktop install (MSIX-packaged) is
deliberately not placed on PATH — `where.exe claude` finds nothing even though
the binary is real and working. Check the running Claude Code session's own
environment before concluding the CLI is absent:

```powershell
echo $env:CLAUDE_CODE_EXECPATH
```

That variable, when set, names the real binary directly (observed on this
machine at `%LOCALAPPDATA%\Packages\<PackageFamilyName>\LocalCache\Roaming\
Claude\claude-code\<version>\claude.exe`). Once you have found it, either add
it to PATH for this shell or use `--executable <path>` / `config.json →
runner.executable` (§6.2) — `redgear doctor` reports which one is actually
configured and whether it resolves.

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

Confirm the `agent CLI (...)` row — the name in parentheses is whatever is
actually configured (§1's `--executable`/`config.json`, or the bare `claude`
default) — shows a real version, not `not found`. This is the fastest way to
tell a PATH/configuration problem from a flag problem, and they look identical
from inside a failed run.

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

Scrub any session id or path you would rather not commit — though check first
whether it is actually sensitive; `session_id` and `uuid` are per-invocation
identifiers with nothing to leak and are fine to leave, which is what the
2026-08-19 session did. Delete the "these payloads are SYNTHETIC" notice from
`tests/fixtures/claude_payloads/README.md` and record what you captured
instead — see that file for the shape a real writeup takes; it now documents
five real findings (§0) rather than a single "synthetic" notice.

Run the suite. If `tests/test_claude_adapter.py` now fails, **the adapter was
wrong and the test just caught it** — that is the procedure working. (It did
not, this run: every existing test passed unchanged against the real payloads,
and five new tests were added to pin the new findings down — see the fixtures
README for which.)

**Capturing more than one dispatch shape needs more than `--max-iterations
1`.** A single real redgear turn gives you one payload shape from whatever the
plan happens to dispatch next. To deliberately capture a *specific* shape —
an unauthenticated failure, a plain success, a success with a custom
`--json-schema` — invoke `claude` directly against the throwaway repo from §1,
outside of `redgear run` entirely:

```bash
claude -p "describe calc.py" --output-format json \
  --json-schema '{"type":"object","properties":{"outcome":{"type":"string"}},"required":["outcome"]}'
```

**On Windows, do this from `cmd /c` or a real POSIX shell, not PowerShell.**
PowerShell re-quotes and re-escapes arguments before they ever reach the child
process's argv, and a JSON string containing `{`, `}`, and nested `"`
characters does not survive that unmangled — the `--json-schema` argument
`claude` receives is not the string you typed. This is a shell-quoting
problem with PowerShell's argument parsing, not with `redgear` (its own
subprocess calls use `shell=False`, per §11.1 rule 1, and are immune to this
entirely) or with `claude` itself. Confirmed directly on this machine: the
same command that mangled under PowerShell worked unmodified from `cmd /c` and
from a Python `subprocess.run([...], shell=False)` call.

---

## 7. Update the verification comment

In `redgear/runner.py`, replace the `Verified against:` block with what you
actually observed. As of 2026-08-19 it reads:

```python
# Verified against: claude 2.1.229 on 2026-08-19 (Windows, MSIX/Claude-Desktop
#   install, OAuth via the desktop app, bare=false).
```

Record the version, the date, the platform, the install method, and the
authentication mode — bare mode takes a different credential path (§6.3), so
"it worked" means something different under each, and an MSIX/Desktop install
resolves the binary differently than a native installer or an npm global does
(§1's PATH note). Also record **what was actually exercised**, not just that
something was: this session's comment names the three dispatch shapes tested
(unauthenticated failure, plain success, success with `--json-schema`) and
lists `--bare`/`--mcp-config`/`--max-spend-usd` as not yet exercised, because
"verified" without a scope invites the next reader to trust flags nobody
actually ran.

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
