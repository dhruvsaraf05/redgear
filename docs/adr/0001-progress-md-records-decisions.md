# ADR-0001: `docs/PROGRESS.md` §2 is the decision record for this project

## Status

Accepted.

## Context

CLAUDE.md §11.3 and §2.2 both require an ADR in `docs/adr/` for any design
decision not covered by the contract. By T-0037, roughly thirty such
decisions had been made — spec-hash edge cases, event-schema amendments,
platform-specific subprocess behaviour, frozen-file edit justifications — and
every one of them was recorded in `docs/PROGRESS.md` §2 instead. `docs/adr/`
sat empty the entire time. Nobody flagged the deviation until an orientation
pass across the whole repository surfaced it as a live gap between the
contract and actual practice.

## Decision

`docs/PROGRESS.md` §2 ("Decisions taken (with reasoning)") is the decision
record for this project. New non-trivial decisions not covered by CLAUDE.md
go there, not into a new file under `docs/adr/`. The existing ~30 decisions
already recorded in PROGRESS §2 are not being backfilled into individual ADR
files — that would rewrite working documentation into a less useful shape for
no benefit to any reader.

**Why one running document beat thirty files, for this project specifically:**

- This is a solo build with heavy session handoff — a new session's first act
  is reading one document front to back to reload context, not browsing a
  directory of files to reassemble a timeline. PROGRESS.md already exists for
  that purpose (§1 status, §3 traps, §5 open questions); decisions are one
  more section of the same read, not a second document to cross-reference.
- Cross-referencing cost matters here more than the audit trail ADRs
  optimise for. A decision in PROGRESS §2 sits next to the trap it fixed and
  the frozen-file edit it justified, in the same file, often the same
  session's edit. Splitting it into a separate ADR file would mean a reader
  chasing one thread opens two or three files instead of scrolling one.
- Thirty near-identical files (title, status, context, decision, three
  sentences each) is overhead a single human maintaining both the contract
  and the code does not recoup. An ADR directory earns its keep when multiple
  people need to find "why was this decided" without asking the person who
  decided it — that person doesn't exist yet here.

## What is lost

- **No per-decision immutability.** An ADR file, once accepted, is meant to
  never be edited — a change supersedes it with a new file. PROGRESS.md is
  explicitly the opposite: "Keep it current, not cumulative. Update entries
  in place; delete what stops being true." A decision recorded there can be
  edited or removed if it stops applying, with no supersession chain marking
  what it replaced.
- **No supersession chain.** ADRs form a linkable history — ADR-0012
  supersedes ADR-0007. PROGRESS §2 entries do not reference each other by id,
  so reconstructing how a decision evolved across sessions means reading
  history in git log or trusting that the current entry's reasoning still
  covers the original motivation.
- **No standalone citability.** An ADR has a stable path and number a PR or
  another document can reference. A PROGRESS §2 entry has a heading, not an
  id, and the entry can move or be deleted as the section is edited in place.

## When to revisit

If this project gains contributors beyond one person driving Claude Code
through a session at a time, revisit this decision. Multiple contributors are
exactly the case ADRs are built for — someone who did not make a decision
needs to find out why it was made without asking the person who made it, and
a single continuously-edited document does not serve a reader who was not
there for the edit.
