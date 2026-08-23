"""The command surface (§9). Argument parsing and output formatting **only**.

FR-11: "The tool is distributed as a CLI; the command surface is the product
boundary." So this module is the boundary and nothing more. It calls
``orchestrator``, ``state_engine`` and ``verifier``; it contains no loop
logic, composes no prompt text, and runs no gates. The same rule §2.2 applies
to the orchestrator applies here one level up.

Three commands carry a guarantee rather than a convenience, and each is easy
to weaken into uselessness:

* **``run`` refuses a draft plan** (§3.3). There is deliberately no flag that
  skips it. The plan defines the tests, so it is the only unverified model
  output in the system -- everything the loop does afterwards is gated by
  tests the plan itself specified, which means an unreviewed plan produces
  confidently verified wrong software.
* **``rebuild`` never repairs.** It reports divergence and exits non-zero,
  leaving the file exactly as found. Auto-healing an audit trail destroys the
  thing the audit trail is for, and §1.5 lists automatic repair as an explicit
  non-goal.
* **``log`` never prints a credential** (G5). Routed through ``redact``.

Errors surface as a code and a message, never a traceback (§11.2 rule 4). A
user who sees a stack trace has learned nothing actionable and has no idea
whether their repository is now inconsistent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.text import Text
from redgear import __version__, gitctx, orchestrator, planner, state_engine, verifier
from redgear.api.app import create_app
from redgear.budget import request_stop
from redgear.errors import JsonValue, PlanUnreviewedError, RedgearError
from redgear.events import replay as replay_events
from redgear.paths import config_path, events_path, redgear_dir, stop_path, task_graph_path
from redgear.redact import collect_secrets, redact_value
from redgear.runner import ClaudeCodeConfig, ClaudeCodeRunner
from redgear.schemas import Budget, Claim, HarnessConfig, TaskGraph, Verdict

app = typer.Typer(
    name="redgear",
    help="An autonomous orchestrator that refuses to accept unproven work.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
#: Errors go to stderr so `redgear log > file` keeps the file clean.
err_console = Console(stderr=True)

#: §9's table. `status` is the one command whose codes are not §4.3's, because
#: it reports a *condition* rather than terminating a run.
_STATUS_READY = 0
_STATUS_ALL_TERMINAL = 1
_STATUS_ESCALATED = 2

#: A task that has left the queue and will not re-enter it without a human.
_TERMINAL_STATES = frozenset({"verified", "escalated"})
_CLAIMABLE_STATES = frozenset({"ready", "rejected"})

RepoOption = Annotated[
    Path | None,
    typer.Option("--repo", help="Target repository root. Defaults to the current directory."),
]


def _repo_default() -> Path:
    return Path.cwd()


def _fail(error: RedgearError, code: int = 1) -> typer.Exit:
    """Render a RedgearError as a clean, actionable line.

    The code is printed because it is the stable identifier -- §4.7's table is
    what a user greps for, and the prose may be reworded.
    """
    err_console.print(f"[bold red]{error.code}[/bold red] {error.message}")
    for name, value in sorted(error.detail.items()):
        err_console.print(f"  {name}: {value}")
    return typer.Exit(code)


def _require_state_dir(repo: Path) -> None:
    """Every command that reads state needs the state to exist.

    Without this the user meets a bare ``FileNotFoundError`` from deep inside
    the state engine, which says nothing about what to do next.
    """
    if not task_graph_path(repo).is_file():
        raise _fail(
            RedgearError(
                f"no redgear state in {repo}; run `redgear init` first",
                detail={"expected": str(redgear_dir(repo))},
            )
        )


def _default_harness() -> HarnessConfig:
    """The stock Python harness (§2.1).

    Built from ``sys.executable`` rather than bare tool names because a bare
    name is not resolvable in general -- in this project's own virtualenv
    ``ruff`` is reachable only as ``python -m ruff``. Configuration proper
    arrives with ``config.json``; this is the fallback so the CLI is usable
    before one exists.
    """
    return HarnessConfig(
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache", "."],
        test_cmd=[sys.executable, "-m", "pytest"],
        coverage_cmd=[sys.executable, "-m", "coverage"],
        coverage_source=["src"],
        coverage_floor=0.85,
        timeout_s=900,
    )


def _configured_executable(root: Path, *, flag: str | None) -> str | None:
    """Resolve the agent CLI binary: ``--executable``, then ``config.json``'s
    ``runner.executable``, then ``None`` -- meaning ``ClaudeCodeConfig``'s own
    ``"claude"`` default applies.

    A bare ``"claude"`` only resolves on PATH, and a normal MSIX/Desktop
    install of Claude Code deliberately is not on PATH (confirmed by direct
    diagnostic on this machine: ``where.exe claude`` finds nothing, while
    ``CLAUDE_CODE_EXECPATH`` in the environment names the real binary). Before
    this, a pipx user with Claude Desktop and nothing else could not run
    ``redgear run`` at all without editing Python.

    Returns ``None`` rather than the literal default so callers can tell "use
    the config" from "nothing configured, fall through to ``ClaudeCodeConfig``'s
    own default" -- the two need to behave identically, but only one of them
    should ever be reported as "configured" by ``doctor``.
    """
    if flag is not None:
        return flag

    config_file = config_path(root)
    if not config_file.is_file():
        return None
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    runner_section = data.get("runner")
    if not isinstance(runner_section, dict):
        return None
    executable = runner_section.get("executable")
    return executable if isinstance(executable, str) and executable.strip() else None


# ---------------------------------------------------------------------------
# The `redgear run` banner -- cosmetic, and deliberately only on `run`
# ---------------------------------------------------------------------------


#: One entry per distinct character in ``_WORDMARK``. Each glyph is a tuple of
#: equal-width rows; ``#`` is ink, ``.`` is empty.
#:
#: Generated from a glyph table rather than hand-typed as multi-line strings on
#: purpose: with every glyph the same 7x7 block, alignment is a property of
#: the composition below rather than of one long string a reader has to
#: eyeball. A typo here produces one ragged letter, not a silently skewed
#: banner nobody notices until it reaches a terminal.
#:
#: Strokes are two cells thick, which is what gives the wordmark its weight.
_GLYPHS: dict[str, tuple[str, ...]] = {
    "R": ("######.", "##...##", "##...##", "######.", "##.##..", "##..##.", "##...##"),
    "E": ("#######", "##.....", "##.....", "######.", "##.....", "##.....", "#######"),
    "D": ("######.", "##...##", "##...##", "##...##", "##...##", "##...##", "######."),
    "G": (".######", "##.....", "##.....", "##.####", "##...##", "##...##", ".######"),
    "A": ("..###..", ".##.##.", "##...##", "#######", "##...##", "##...##", "##...##"),
}

_WORDMARK = "REDGEAR"
_TAGLINE = "your coding agent says it's done. redgear checks."

#: Full block. Solid letterforms read as one continuous shape at terminal font
#: sizes; thin box-drawing characters read as broken or disconnected.
_BLOCK = "█"

#: Columns between adjacent letters. One, so the wordmark reads as a single
#: tight logotype rather than seven spaced-out characters. Zero would merge
#: adjacent stems into an unreadable slab.
_GLYPH_GAP = 1

#: One flat red for every letterform cell, with no per-row or per-cell
#: variation: a uniform logotype is the point, so there is nowhere for a
#: gradient or a band to creep in.
_STYLE_MAIN = "bold #dc2828"

#: The background texture: a 2x2 tile of corner glyphs, laid on a lattice
#: behind and around the wordmark.
_TEXTURE_TILE = ("╔╗", "╚╝")

#: Lattice spacing. Sparse on purpose -- the texture is decoration behind a
#: logo, not a second layer competing with it. Halving the horizontal density
#: (4 -> 6) was a deliberate step back: at 4 the full-width bands carried
#: nearly as many glyphs as the wordmark has blocks.
_TEXTURE_PERIOD_X = 6
_TEXTURE_PERIOD_Y = 3

#: How far the textured field extends past the wordmark, so the logotype sits
#: *in* the texture rather than merely beside it. The vertical margin has to
#: clear ``_TEXTURE_CLEARANCE`` twice over, or the bands above and below the
#: wordmark are eaten entirely and the texture collapses into two side panels.
_TEXTURE_MARGIN_X = 8
_TEXTURE_MARGIN_Y = 4

#: Cells of empty space kept clear around every letterform. Without it the
#: tiles crowd the strokes and settle inside the counters of D, R and G, and
#: the wordmark stops being instantly readable -- which is the one thing the
#: texture must not cost.
_TEXTURE_CLEARANCE = 1

#: Very dark red: present against black, but far enough below the wordmark's
#: brightness that the eye reads it as ground rather than figure.
_STYLE_TEXTURE = "#5a1e1e"


def _banner_rows() -> list[str]:
    """The wordmark as plain text rows, one string per row.

    Every glyph contributes the same row index to the same output row, joined
    by a fixed gap, so the letters cannot drift out of alignment -- the width
    of a row is a function of the table, not of anything typed by hand.
    """
    height = len(_GLYPHS[_WORDMARK[0]])
    separator = " " * _GLYPH_GAP
    rows: list[str] = []
    for index in range(height):
        letters = [_GLYPHS[letter][index] for letter in _WORDMARK]
        rows.append(separator.join(letters).replace("#", _BLOCK).replace(".", " "))
    return rows


def _banner_canvas() -> list[str]:
    """The texture field with the wordmark composited on top.

    Order matters and mirrors how the layers read: tiles are laid down first,
    then the letterforms overwrite whatever they land on, so the wordmark is
    never broken up by a glyph showing through it.

    Tiles are placed **atomically** -- all four cells or none. Placing them
    cell by cell leaves orphaned halves wherever the clearance clips a tile,
    which reads as debris rather than as a pattern.
    """
    rows = _banner_rows()
    height, width = len(rows), len(rows[0])
    top, left = -_TEXTURE_MARGIN_Y, -_TEXTURE_MARGIN_X
    canvas_h = height + 2 * _TEXTURE_MARGIN_Y
    canvas_w = width + 2 * _TEXTURE_MARGIN_X

    def is_ink(y: int, x: int) -> bool:
        return 0 <= y < height and 0 <= x < width and rows[y][x] == _BLOCK

    def is_crowded(y: int, x: int) -> bool:
        span = range(-_TEXTURE_CLEARANCE, _TEXTURE_CLEARANCE + 1)
        return any(is_ink(y + dy, x + dx) for dy in span for dx in span)

    canvas = [[" "] * canvas_w for _ in range(canvas_h)]

    for row in range(canvas_h - 1):
        for column in range(canvas_w - 1):
            if row % _TEXTURE_PERIOD_Y or column % _TEXTURE_PERIOD_X:
                continue
            corners = [(row, column), (row, column + 1), (row + 1, column), (row + 1, column + 1)]
            if any(is_crowded(top + y, left + x) for y, x in corners):
                continue
            for y, x in corners:
                canvas[y][x] = _TEXTURE_TILE[y - row][x - column]

    for row in range(canvas_h):
        for column in range(canvas_w):
            if is_ink(top + row, left + column):
                canvas[row][column] = _BLOCK

    return ["".join(row) for row in canvas]


def _banner_wordmark() -> Text:
    """The composited banner: dark texture behind, flat red letterforms on top.

    Every ``_BLOCK`` cell gets the same style object, so the wordmark stays one
    uniform red no matter where it sits on the canvas.
    """
    art = Text()
    canvas = _banner_canvas()
    for index, row in enumerate(canvas):
        for cell in row:
            if cell == _BLOCK:
                art.append(cell, style=_STYLE_MAIN)
            elif cell != " ":
                art.append(cell, style=_STYLE_TEXTURE)
            else:
                art.append(" ")
        if index != len(canvas) - 1:
            art.append("\n")
    return art


def _banner_width() -> int:
    """Columns the banner occupies. Every row is the same length."""
    return len(_banner_canvas()[0])


def _banner_supported(target: Console) -> bool:
    """Should a block-art banner go to this stream at all?

    Four ways the answer is no, and each is a real case rather than a
    defensive guess:

    * **Not a terminal.** ``redgear run`` output gets piped and logged, and
      coloured box-drawing art in a log file is noise.
    * **NO_COLOR.** Colour is what separates the dark texture from the bright
      letterforms; unstyled, the corner glyphs are just clutter around the
      wordmark. Honouring the convention means skipping the banner, not
      printing it grey.
    * **The stream cannot encode the art.** A Windows console on a cp1252 code
      page raises ``UnicodeEncodeError`` on ``█`` -- hit directly while
      building this, so the banner would have crashed a real run rather than
      degrading. Every character the banner can emit is checked, not just the
      block: the texture glyphs have their own encoding story (cp437 carries
      all five, cp1252 none of them).
    * **Too narrow.** Below the banner's width rich wraps the art into
      unreadable fragments.
    """
    if not target.is_terminal or target.no_color:
        return False
    if os.environ.get("NO_COLOR"):
        return False

    encoding = getattr(target.file, "encoding", None)
    art_characters = _BLOCK + "".join("".join(row) for row in _TEXTURE_TILE)
    try:
        art_characters.encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False

    return target.width >= _banner_width()


def _print_banner() -> None:
    """Print the banner if the stream can carry it. Never raises."""
    if _banner_supported(console):
        console.print(_banner_wordmark())
        console.print(_TAGLINE, style="dim")
        console.print()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(repo: RepoOption = None) -> None:
    """Scaffold `.redgear/` in the current repository."""
    root = repo or _repo_default()
    try:
        # Outside a repository there is no baseline to diff against, so every
        # guarantee downstream of the diff audit would be fiction (§8.4).
        gitctx.head_commit(root)
        created = state_engine.scaffold(root)
    except RedgearError as error:
        raise _fail(error) from error

    console.print(f"[green]initialised[/green] {created}")
    console.print("Next: add a plan, review it, then `redgear run`.")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    repo: RepoOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compose and print every prompt; dispatch nothing."),
    ] = False,
    max_iterations: Annotated[
        int | None, typer.Option("--max-iterations", help="Override the iteration cap.")
    ] = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Restrict the dry run to one task id.")
    ] = None,
    executable: Annotated[
        str | None,
        typer.Option(
            "--executable",
            help="Path to the agent CLI binary. Defaults to config.json's "
            'runner.executable, then "claude" resolved on PATH.',
        ),
    ] = None,
) -> None:
    """Run the continuous loop until a terminal condition is reached."""
    root = repo or _repo_default()
    _require_state_dir(root)

    # Only `run` gets this. A banner on every invocation gets old fast, so
    # --help, status, doctor and the rest stay plain.
    _print_banner()

    if dry_run:
        _dry_run(root, only=task)
        return

    budget = Budget() if max_iterations is None else Budget(max_iterations=max_iterations)

    # §8.3: print the brake before doing anything. A user who cannot find it
    # will Ctrl-C repeatedly and leave the state directory inconsistent.
    console.print("[bold]redgear run[/bold]")
    console.print("  stop with: [cyan]redgear stop[/cyan]  (or create .redgear/STOP)")
    console.print()

    # The approval gate is checked FIRST, before any other refusal (§3.3).
    # Ordering matters: an unapproved plan must be reported as unapproved
    # whatever else is missing from the environment, or a user fixes the
    # adapter, runs again, and only then discovers the real problem.
    try:
        graph = state_engine.load_graph(root)
        if graph.state != "active":
            raise PlanUnreviewedError(
                "the plan has not been approved; `redgear run` refuses a draft graph. "
                "A human must review it first: the plan defines the tests.",
                detail={"state": graph.state, "graph": str(task_graph_path(root))},
            )
    except RedgearError as error:
        raise _fail(error) from error

    # The adapter writes its per-turn artifacts under the run directory the
    # state engine owns (§2.3's `agent_stdout.log`, `argv.json`).
    resolved_executable = _configured_executable(root, flag=executable)
    config = (
        ClaudeCodeConfig(executable=resolved_executable)
        if resolved_executable is not None
        else ClaudeCodeConfig()
    )
    agent = ClaudeCodeRunner(
        config=config,
        artifacts_root=redgear_dir(root) / "runs" / "agent",
    )
    console.print(f"  agent CLI: [dim]{config.executable}[/dim] ({agent.version()})")
    console.print()

    try:
        outcome = orchestrator.run(root, runner=agent, budget=budget, harness=_default_harness())
    except RedgearError as error:
        # A missing `claude` surfaces here, named, rather than as a traceback.
        raise _fail(
            error, code=orchestrator.TERMINATION_EXIT_CODES.get(error.code.lower(), 1)
        ) from error

    console.print()
    console.print(
        f"[bold]{outcome.reason}[/bold]: {outcome.iterations} iteration(s), "
        f"{outcome.tasks_verified} verified, {outcome.tasks_escalated} escalated"
    )
    raise typer.Exit(outcome.exit_code)


def _dry_run(root: Path, *, only: str | None) -> None:
    """Print what a real run would send, and nothing else."""
    try:
        composed = orchestrator.compose_dry_run(root, harness=_default_harness())
    except RedgearError as error:
        raise _fail(error) from error

    if only is not None:
        composed = [pair for pair in composed if pair[0] == only]

    console.print("[bold]redgear run --dry-run[/bold]  (composing only, dispatching nothing)")
    console.print("  stop with: [cyan]redgear stop[/cyan]")
    console.print()

    if not composed:
        console.print("[yellow]no ready task[/yellow]: nothing to compose.")
        return

    for index, (task_id, prompt) in enumerate(composed, start=1):
        console.rule(f"[bold cyan]{index}/{len(composed)}  {task_id}[/bold cyan]")
        # soft_wrap keeps the prompt byte-faithful: rich would otherwise
        # re-wrap it to the terminal width and the printed text would no
        # longer be what an agent receives.
        console.print(prompt, soft_wrap=True, highlight=False)
        console.print()

    console.print(f"[green]{len(composed)} prompt(s) composed. Nothing was dispatched.[/green]")


# ---------------------------------------------------------------------------
# plan / approve -- section 3.3's gate, deliberately two commands
# ---------------------------------------------------------------------------


@app.command(name="plan")
def plan_command(
    source: Annotated[Path, typer.Option("--from", help="Requirements document to plan from.")],
    repo: RepoOption = None,
    executable: Annotated[
        str | None,
        typer.Option(
            "--executable",
            help="Path to the agent CLI binary. Defaults to config.json's "
            'runner.executable, then "claude" resolved on PATH.',
        ),
    ] = None,
) -> None:
    """Generate a plan from a requirements document. Leaves it in `draft`."""
    root = repo or _repo_default()
    _require_state_dir(root)

    if not source.is_file():
        raise _fail(
            RedgearError(f"no such document: {source}", detail={"path": str(source)})
        ) from None

    resolved_executable = _configured_executable(root, flag=executable)
    config = (
        ClaudeCodeConfig(executable=resolved_executable)
        if resolved_executable is not None
        else ClaudeCodeConfig()
    )
    agent = ClaudeCodeRunner(
        config=config,
        artifacts_root=redgear_dir(root) / "runs" / "planner",
    )
    console.print("[bold]redgear plan[/bold]")
    console.print("  the planner is dispatched [cyan]read-only[/cyan] (Read, Glob, Grep)")
    console.print()

    try:
        result = planner.generate_plan(
            root, runner=agent, source_document=source.read_text(encoding="utf-8")
        )
    except RedgearError as error:
        raise _fail(error) from error

    console.print(
        f"[green]plan written[/green]: {len(result.graph.nodes)} task(s), "
        f"spec {result.spec.spec_id}, after {result.attempts} attempt(s)"
    )
    console.print()
    # Section 3.3 is a *human* gate. The CLI's job is to make sure nobody
    # mistakes a generated plan for an approved one.
    console.print("[yellow]This plan is a DRAFT and will not run.[/yellow]")
    console.print("Read it, then approve it explicitly:")
    console.print("  [cyan]redgear status[/cyan]                 # see the tasks")
    console.print("  [cyan]redgear approve --by <your name>[/cyan]")


@app.command()
def approve(
    by: Annotated[str, typer.Option("--by", help="Who is approving this plan.")],
    repo: RepoOption = None,
) -> None:
    """Approve a draft plan, recording who approved which spec version."""
    root = repo or _repo_default()
    _require_state_dir(root)

    try:
        graph = planner.approve_plan(root, approved_by=by)
    except RedgearError as error:
        raise _fail(error) from error

    console.print(f"[green]approved[/green] by {by} ({len(graph.nodes)} task(s) now executable)")
    console.print(f"  spec: [dim]{graph.spec_hash}[/dim]")
    console.print("Editing the spec after this invalidates the approval (§3.3).")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(repo: RepoOption = None) -> None:
    """Show every task, its state, and what is blocking it."""
    root = repo or _repo_default()
    _require_state_dir(root)
    try:
        graph = state_engine.recompute_readiness(state_engine.load_graph(root))
    except RedgearError as error:
        raise _fail(error) from error

    verified = {node.id for node in graph.nodes if node.state == "verified"}
    table = Table("task", "type", "state", "att", "blocked by")
    for node in graph.nodes:
        blockers = [dep for dep in node.depends_on if dep not in verified]
        table.add_row(
            node.id,
            node.type,
            node.state,
            f"{node.attempts}/{node.max_attempts}",
            ", ".join(blockers) or "-",
        )
    console.print(table)

    escalated = [node.id for node in graph.nodes if node.state == "escalated"]
    if escalated:
        console.print(f"[red]escalated:[/red] {', '.join(escalated)} (needs a human)")
        raise typer.Exit(_STATUS_ESCALATED)

    if any(node.state in _CLAIMABLE_STATES for node in graph.nodes):
        raise typer.Exit(_STATUS_READY)

    console.print("[green]all tasks are terminal[/green]")
    raise typer.Exit(_STATUS_ALL_TERMINAL)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@app.command()
def stop(repo: RepoOption = None) -> None:
    """Ask a running loop to stop at the next iteration boundary."""
    root = repo or _repo_default()
    request_stop(root)
    console.print(f"[yellow]stop requested[/yellow]: wrote {stop_path(root)}")
    console.print("The run finishes its current turn, releases its lease, and exits 0.")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@app.command()
def verify(
    task_id: Annotated[str, typer.Argument(help="The task to verify.")],
    repo: RepoOption = None,
) -> None:
    """Run the harness against one task. Harness debugging (§9)."""
    root = repo or _repo_default()
    _require_state_dir(root)
    try:
        graph = state_engine.load_graph(root)
        node = next((n for n in graph.nodes if n.id == task_id), None)
        if node is None:
            raise _fail(
                RedgearError(f"no task {task_id} in the graph", detail={"task_id": task_id})
            )

        # A claim is synthesised rather than taken: `verify` is a diagnostic
        # and must not move the task through the state machine.
        claim = node.claim or Claim(
            base_commit=gitctx.head_commit(root),
            frozen_hashes=state_engine.frozen_digests(root, node.scope.frozen_globs),
            allowed_tools=[],
            claimed_at=datetime.now(tz=UTC),
        )
        changed = [entry.path for entry in gitctx.changed_files(root, claim.base_commit)]
        proof = verifier.run_gates(
            root,
            task=node,
            claim=claim,
            declared=changed,
            attempt=node.attempts + 1,
            harness=_default_harness(),
            criteria=orchestrator.criteria_for(graph, node),
        )
    except RedgearError as error:
        raise _fail(error) from error

    table = Table("gate", "status", "reasons")
    for gate in proof.gates:
        table.add_row(gate.name.value, gate.status.value, "\n".join(gate.reasons) or "-")
    console.print(f"[bold]{task_id}[/bold]")
    console.print(table)

    if proof.verdict is Verdict.PASS:
        console.print("[green]PASS[/green]")
        raise typer.Exit(0)
    console.print("[red]FAIL[/red]")
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


@app.command()
def rebuild(repo: RepoOption = None) -> None:
    """Replay the log and compare it against the on-disk projection."""
    root = repo or _repo_default()
    _require_state_dir(root)
    try:
        on_disk_text = task_graph_path(root).read_text(encoding="utf-8")
        graph = TaskGraph.model_validate_json(on_disk_text)
        events = replay_events(events_path(root))
        rebuilt = state_engine.replay_graph(state_engine.plan_definition(graph), events)
    except RedgearError as error:
        raise _fail(error, code=orchestrator.TERMINATION_EXIT_CODES["engine_error"]) from error

    rebuilt_text = state_engine.render_graph(rebuilt)
    if rebuilt_text == on_disk_text:
        console.print(f"[green]projection matches[/green]: {len(events)} event(s) replayed")
        raise typer.Exit(0)

    # Divergence. Name the tasks, and change NOTHING (§1.5, G4).
    on_disk_states = {node.id: node.state for node in graph.nodes}
    diverged = [
        f"{node.id}: on disk {on_disk_states.get(node.id)!r}, replay {node.state!r}"
        for node in rebuilt.nodes
        if on_disk_states.get(node.id) != node.state
    ]
    raise _fail(
        _projection_diverged(diverged, len(events)),
        code=orchestrator.TERMINATION_EXIT_CODES["engine_error"],
    ) from None


def _projection_diverged(diverged: list[str], event_count: int) -> RedgearError:
    from redgear.errors import ProjectionMismatchError

    detail: dict[str, JsonValue] = {
        "events_replayed": event_count,
        "diverging_tasks": "; ".join(diverged) if diverged else "field-level difference only",
        "remedy": "the projection was NOT repaired; investigate before touching it",
    }
    return ProjectionMismatchError(
        "the on-disk projection does not match a replay of the event log",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


@app.command()
def log(
    repo: RepoOption = None,
    tail: Annotated[int | None, typer.Option("--tail", help="Show only the last N events.")] = None,
) -> None:
    """Print the event log, human-readable and redacted."""
    root = repo or _repo_default()
    path = events_path(root)
    if not path.is_file():
        raise _fail(
            RedgearError(
                f"no event log at {path}; run `redgear init` first",
                detail={"expected": str(path)},
            )
        )

    try:
        events = replay_events(path)
    except RedgearError as error:
        raise _fail(error) from error

    if tail is not None:
        events = events[-tail:]

    # G5. Two passes, because a credential reaches the log two ways: as a
    # value that leaked into free text, and as a field whose *name* marks it.
    # `redact_value` handles both; nothing here reimplements the matching.
    secrets = collect_secrets(dict(os.environ))
    for event in events:
        redacted = redact_value(event.model_dump(mode="json"), secrets)
        payload = redacted if isinstance(redacted, dict) else {}
        head = f"[dim]{payload.get('seq')}[/dim] [bold]{payload.get('event')}[/bold]"
        console.print(f"{head} [dim]{payload.get('ts')}[/dim]", soft_wrap=True, highlight=False)
        for name, value in payload.items():
            if name in {"seq", "event", "ts"}:
                continue
            console.print(f"    {name}: {value}", soft_wrap=True, highlight=False)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(repo: RepoOption = None) -> None:
    """Report the environment: agent CLI, harness tools, git, CLAUDE.md."""
    root = repo or _repo_default()
    table = Table("check", "result")
    healthy = True

    table.add_row("redgear", __version__)

    # Agent CLI. Absent is not fatal -- `--dry-run` works without one -- but
    # it is the single most useful thing to know when a run does nothing.
    # Resolved the same way `run`/`plan` resolve it: config.json's
    # `runner.executable`, falling back to the bare "claude" `ClaudeCodeConfig`
    # defaults to. A normal MSIX/Desktop install is deliberately not on PATH,
    # so reporting only `shutil.which("claude")` here would say "not on PATH"
    # on a machine where the CLI is installed and configured correctly.
    resolved_executable = _configured_executable(root, flag=None) or "claude"
    probe_runner = ClaudeCodeRunner(
        config=ClaudeCodeConfig(executable=resolved_executable),
        artifacts_root=redgear_dir(root) / "runs" / "agent",
    )
    # Not folded into `healthy`: a missing agent CLI does not make the
    # environment unhealthy, the same way it was not before this resolved a
    # configured path instead of a bare "claude" -- `--dry-run` works with no
    # agent CLI at all, and reporting it as a health failure would make
    # `doctor` exit non-zero for a perfectly legitimate `--dry-run`-only setup.
    resolved_path = shutil.which(resolved_executable)
    if resolved_path or Path(resolved_executable).is_file():
        table.add_row(f"agent CLI ({resolved_executable})", probe_runner.version())
    else:
        table.add_row(
            f"agent CLI ({resolved_executable})",
            "[yellow]not found -- not on PATH and not an existing file[/yellow]",
        )

    harness = _default_harness()
    for label, cmd in (
        ("harness: lint", harness.lint_cmd),
        ("harness: tests", harness.test_cmd),
        ("harness: coverage", harness.coverage_cmd),
    ):
        ok = _probe(cmd)
        healthy = healthy and ok
        table.add_row(label, "[green]available[/green]" if ok else "[red]missing[/red]")

    try:
        head = gitctx.head_commit(root)
        dirty = gitctx.dirty_paths(root)
        table.add_row("git HEAD", head[:12])
        table.add_row(
            "git tree",
            "[green]clean[/green]" if not dirty else f"[yellow]{len(dirty)} dirty path(s)[/yellow]",
        )
    except RedgearError as error:
        healthy = False
        table.add_row("git", f"[red]{error.code}[/red]")

    # §6.3: with bare mode off the target repo's CLAUDE.md is loaded into every
    # dispatch, so an oversized one silently competes with redgear's prompt.
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file():
        lines = len(claude_md.read_text(encoding="utf-8").splitlines())
        warn = " [yellow](over 500 lines; it shares context with every prompt)[/yellow]"
        table.add_row("target CLAUDE.md", f"{lines} lines{warn if lines > 500 else ''}")
    else:
        table.add_row("target CLAUDE.md", "absent")

    table.add_row("state dir", "present" if task_graph_path(root).is_file() else "absent")
    console.print(table)
    raise typer.Exit(0 if healthy else 1)


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------


@app.command()
def ui(
    repo: RepoOption = None,
    host: Annotated[
        str, typer.Option("--host", help="Bind address. Loopback by default (§1.4 G5).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = 8787,
) -> None:
    """Serve the read-only control plane API (§9, FR-12).

    This is the API half only -- the Next.js web UI is T-0040 and does not
    exist yet. `/docs` on this server is a usable interface in the meantime.

    Binds to loopback by default and nothing else: G5's amended wording
    permits exactly this one listening socket ("accepts local connections,
    initiates none") and no other. Passing `--host 0.0.0.0` is the caller's
    choice to make, not this command's default.
    """
    root = repo or _repo_default()
    _require_state_dir(root)

    console.print("[bold]redgear ui[/bold]  (API only -- the web UI is T-0040, not yet built)")
    console.print(f"  serving [cyan]http://{host}:{port}[/cyan]  (interactive docs at /docs)")
    console.print()

    uvicorn.run(create_app(root), host=host, port=port)


def _probe(cmd: list[str]) -> bool:
    """Can this harness command be launched at all?

    A ``python -m tool`` vector is answered by asking the import system, not
    by spawning anything. That is both faster -- `doctor` should be instant --
    and more accurate: launching ``--version`` conflates "the module is
    missing" with "the module exists and its version flag exits non-zero".
    """
    if not cmd:
        return False
    if len(cmd) >= 3 and cmd[1] == "-m":
        return importlib.util.find_spec(cmd[2]) is not None
    return shutil.which(cmd[0]) is not None
