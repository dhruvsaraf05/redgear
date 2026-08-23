# redgear

Your coding agent says it's done. Nobody checked.

That's the failure redgear exists to close. Coding agents are good at
writing code and bad at grading their own work. They report success whether
or not the tests pass, whether or not they touched files they weren't asked
to, whether or not the change does what it claims. Someone has to
independently verify each turn before the next one starts, and today that
someone is a human reading diffs at 11pm.

redgear is that someone, automated. It's an orchestrator, not an agent: it
never writes code itself. It decides what to work on next, writes the prompt
that says so, hands it to a coding agent CLI (Claude Code is the reference
implementation), and then, before trusting a word of the agent's report,
independently re-derives the truth: runs the real test suite itself,
re-hashes every file the agent was told not to touch, recomputes the real
`git diff` instead of reading the agent's claimed file list. If the agent
says "done" and the evidence disagrees, the run doesn't advance. If the agent
says "I'm blocked," that costs nothing: an honest stop should always be
cheaper than a lie.

## Install

```bash
pipx install redgear
```

Needs Python 3.12+, `git`, and a [Claude Code](https://claude.com/claude-code)
install. See [Requirements](#requirements) below: the Claude Code binary
resolution has one gotcha worth reading before your first run.

## A worked example

```bash
cd my-project
redgear init
```

Scaffolds `.redgear/`. The whole audit trail lives here and is committed to
your repo like any other file.

```bash
redgear plan --from docs/PRD.md
```

Dispatches a **read-only** agent turn (`Read`, `Glob`, `Grep` only, it
cannot edit a single file) to turn your requirements doc into a task graph:
which pieces of work, in what order, with what acceptance criteria. It lands
in `.redgear/task_graph.json` in state `draft` and **`redgear run` refuses to
touch a draft plan.** The plan defines what "correct" means for every task
that follows. A bad plan produces confidently verified wrong software, and
no amount of gate rigor downstream catches that. A human has to look at it
first. There is no flag that skips this.

```bash
redgear status
```

```
┌────────┬───────────────┬─────────┬─────┬────────────┐
│ task   │ type          │ state   │ att │ blocked by │
├────────┼───────────────┼─────────┼─────┼────────────┤
│ T-0001 │ test_authoring│ ready   │ 0/3 │ -          │
│ T-0002 │ implementation│ blocked │ 0/3 │ T-0001     │
└────────┴───────────────┴─────────┴─────┴────────────┘
```

Read the plan, then approve it explicitly. This records *who* approved
*which* version of the spec:

```bash
redgear approve --by "your name"
```

```bash
redgear run --dry-run
```

Composes and prints every prompt the loop would send, **dispatching
nothing**. Costs nothing. Use it constantly: it's the fastest way to catch
a badly-scoped task before it costs a real agent turn.

```bash
redgear run
```

```
redgear run
  stop with: redgear stop  (or create .redgear/STOP)

  agent CLI: claude (2.1.229)

complete: 6 iteration(s), 6 verified, 0 escalated
```

That's the whole interface for a clean run: a banner naming the brake, the
resolved agent CLI, and one summary line at the end. Everything else lives in
the audit trail, because the point isn't a chatty console. It's a record
you can actually check.

### What the audit trail shows when something goes wrong

A task that fails a gate doesn't die. It goes back into the queue, and the
next prompt for it carries the actual failure excerpt, so the retry is
corrective rather than a blind repeat. `redgear status` shows this as an
attempt count climbing against the cap:

```
│ T-0004 │ implementation│ rejected│ 1/3 │ -          │
```

If it exhausts its attempts, or the agent honestly reports itself blocked or
under-scoped, the run stops there rather than pushing forward on an
assumption:

```
│ T-0007 │ implementation│ escalated│ 2/3 │ -         │

escalated: T-0007 (needs a human)
```

Reporting "blocked" costs an agent nothing: no attempt is consumed. Claiming
completion it can't support does; verification runs independently either way.
Every one of these transitions is one line in `redgear log`, redacted,
readable, and reconstructible from `.redgear/events.jsonl` alone. That file,
not the console output, is the actual source of truth.

## The seven guarantees

Every design decision in this project traces back to one of these:

1. **It runs the tests itself.** No field the agent reports ever decides a
   verdict. Only real exit codes and a real `git diff`, recomputed by
   redgear after the agent's process has already exited.
2. **Tests are frozen during implementation, and code is frozen during test
   authoring.** SHA-256-enforced. An agent that could edit both the tests and
   the code they check would be grading its own homework.
3. **It's free to say "I'm stuck."** An agent whose only options are "pass"
   or "fail and retry" is structurally pushed toward faking a pass. Reporting
   blocked or under-scoped costs nothing.
4. **Every verdict has a receipt.** `.redgear/events.jsonl` is an
   append-only log; every other state file is a projection that can be
   rebuilt from it byte-for-byte. Nothing is asserted that isn't reconstructible.
5. **redgear holds no API key and makes no outbound network call.** All
   inference is delegated to your own agent CLI subprocess, authenticated with
   whatever you already configured. redgear never touches a credential, never
   calls a model API directly, and adds zero egress of its own. All spend
   belongs to your agent CLI session, not to redgear.
6. **Every run is bounded, and interruptible.** Hard caps on iterations,
   wall-clock time, and consecutive failures; a stop file honored between
   iterations; a process-tree kill on timeout. It never commits, pushes, or
   rewrites history in your repository. That stays yours.
7. **Untrusted text is never treated as an instruction.** Test output, diffs,
   and source documents are explicitly delimited in every prompt as data to
   diagnose, not commands to follow.

## Requirements

- **Python 3.12+**, a floor, not a target. Nothing in redgear needs a newer
  interpreter; this just keeps the requirement honest against what's actually
  tested.
- **git**, with a clean working tree before every run. Without a clean
  baseline, the diff audit redgear runs is fiction.
- **[Claude Code](https://claude.com/claude-code)**, the reference agent CLI
  adapter. Other conforming CLIs are architecturally supported but untested.

**If Claude Code is installed as the Desktop app (Windows, MSIX-packaged):
the `claude` binary is deliberately not on `PATH`.** `redgear run` and
`redgear plan` will fail to find it by default. Point redgear at it directly:

```bash
redgear run --executable "C:\Users\<you>\AppData\Local\Packages\<PackageFamilyName>\LocalCache\Roaming\Claude\claude-code\<version>\claude.exe"
```

or persist it once in `.redgear/config.json`:

```json
{ "runner": { "executable": "C:\\...\\claude.exe" } }
```

`redgear doctor` reports whichever one is actually configured, and whether it
resolves. Run it first if a run fails with "not installed or not on PATH."

## The plan

The build plan this project is executing on itself (task graph, spec,
architectural contract) lives in
[`.redgear/task_graph.json`](.redgear/task_graph.json) and
[`.redgear/spec/spec.json`](.redgear/spec/spec.json). The contract itself is
[`CLAUDE.md`](CLAUDE.md); read it before touching any code here.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff format --check .
ruff check .
mypy
pytest -q
```
