# Recorded Claude Code payloads

**The adapter has now been verified against a real CLI.** Manual integration
procedure (`docs/agents/claude-code.md`) run against **Claude Code 2.1.229**,
**Windows**, **MSIX/Claude-Desktop install**, on **2026-08-19**. Three
dispatches, verbatim `--output-format json` stdout, no editing beyond
pretty-printing for readability (`json.dumps(..., indent=2)`; no key was
added, removed, or renamed). No secret, credential, or anything else sensitive
appears in any of these payloads — `session_id` and `uuid` are harmless
per-invocation identifiers and are left as captured.

Commands used, against a throwaway git repo containing one file
(`calc.py`, `def add(a, b): return 0`):

```
claude -p "..." --output-format json                                        # unauthenticated
claude -p "..." --output-format json                                        # after /login
claude -p "..." --output-format json --json-schema '{"type":"object","properties":{"outcome":{"type":"string"}},"required":["outcome"]}'
```

## The three real captures

| File | What it is |
| --- | --- |
| `real_unauthenticated_failure.json` | Dispatch against a session with no credential. Exit code 1. |
| `real_plain_success.json` | A successful dispatch, no `--json-schema`. Exit code 0. |
| `real_json_schema_dispatch.json` | A successful dispatch **with** `--json-schema`, proving `structured_output` is a real, separate JSON object. Exit code 0. |

These three are kept byte-for-byte as captured (only re-indented). They are
the primary evidence for everything below and for `docs/PROGRESS.md`'s
account of this session.

## What they proved, and what changed because of it

* **`subtype` is useless as a success signal.** It reads `"success"` in *all
  three* captures, including the hard authentication failure. The adapter has
  never read it (confirmed by re-reading `runner.py` at this session, not
  merely re-asserted) — `is_error` (bool) and `terminal_reason` (`"api_error"`
  vs. `"completed"`) are the real discriminators, and `is_error` is what
  `_parse` and `dispatch_json` already branch on. `test_subtype_is_never_a_
  success_signal` in `tests/test_claude_adapter.py` pins this down explicitly
  now, using `real_unauthenticated_failure.json`.
* **Exit code and payload agreed here.** Exit 1 came with `is_error: true`;
  exit 0 came with `is_error: false`, both times. CLAUDE.md §6.4 rule 1 is
  *not* wrong, but its wording read as though disagreement were the norm.
  Amended to say they can disagree in either direction and the payload is
  authoritative — the adapter's existing behaviour (record the exit code,
  never branch on it) needed no code change, only the contract's wording did.
* **`structured_output` is a real JSON object; `result` carries the same
  content JSON-encoded as a string.** `real_json_schema_dispatch.json` shows
  both side by side. The adapter has always read `structured_output` directly
  (`_as_map(payload.get("structured_output"))`) and never fallen back to
  parsing `result` — confirmed, not changed. Note this capture used a minimal
  test schema (`{"outcome": {"type": "string"}}`), not redgear's own
  `agent_report_schema()`, so its `structured_output.outcome` is free text and
  will **not** validate against `AgentTurnReport` (`outcome` must be one of
  `completed`/`blocked`/`scope_insufficient`). That is expected and is not a
  bug in either the CLI or the adapter — it proves the *mechanism*, not a
  redgear-shaped result. `test_structured_output_is_a_real_object_...` checks
  the raw JSON shape directly for this reason, without routing it through
  `_parse`.
* **`stop_reason` can be `"tool_use"` on a normal, successful, multi-turn
  dispatch** (`real_json_schema_dispatch.json`: `stop_reason: "tool_use"`,
  `num_turns: 4` — the agent read `calc.py` before answering). The adapter has
  never branched on `stop_reason` (confirmed). `completed.json` and
  `blocked.json` (below) now carry a real observed `stop_reason` so this stays
  true under test, not just under inspection.
* **New fields, observed and mostly still unused by design:** `is_error`,
  `duration_api_ms`, `stop_reason`, `terminal_reason`, `permission_denials`,
  `api_error_status`, `fast_mode_state`, `fast_mode_disabled_reason`,
  `ttft_ms`, `ttft_stream_ms`, `time_to_request_ms`, `uuid`, `usage`
  (nested token/cache/service-tier detail), `modelUsage` (per-model cost
  breakdown). `TurnResult` intentionally does not carry most of these — see
  CLAUDE.md §6.2 for the recorded list and `docs/PROGRESS.md` for the one that
  is an open question rather than a settled "no": **`permission_denials`**,
  empty in all three captures here, is currently invisible to the
  orchestrator if it is ever non-empty. Nothing in this session added a field
  for it; see PROGRESS §5.
* **Cost calibration.** $0.065 for a near-empty 2-token prompt (cache
  creation dominates the bill), $0.218 for a 4-turn dispatch reading one
  10-byte file. A real redgear dispatch (an ~8,000-character prompt, §5.6's
  cap) will cost more than either. Worth checking `Budget.per_turn_usd`'s
  default against this before trusting it as a real ceiling — not changed
  here; see PROGRESS §5.

## `completed.json` / `blocked.json` — hybrid, and labelled as such

Unlike the three real captures above, these two are **not** verbatim real
payloads. No real capture demonstrates redgear's own schema-conformant
`structured_output` (`outcome` as one of the three `TurnOutcome` values, plus
`summary`/`changed_files`/`known_gaps`) — the manual verification used a
minimal test schema, not `agent_report_schema()`, and never asked the agent to
report itself `blocked`. Producing genuinely real examples of those would mean
running redgear's actual prompts against a real CLI, which is a further,
separate verification this session did not do.

So these two carry a **real, observed envelope** (`stop_reason`,
`terminal_reason`, `permission_denials`, `usage`, `modelUsage`, `ttft_ms`, and
so on — the shape from `real_json_schema_dispatch.json` and
`real_plain_success.json`) around **illustrative** `structured_output`
content shaped the way a real redgear dispatch's would be. Every field a test
in `tests/test_claude_adapter.py` asserts an exact value for
(`num_turns`, `duration_ms`, `total_cost_usd`, `changed_files`, `session_id`)
is unchanged from before this session, so no existing test's meaning shifted —
only the envelope around it got more realistic.

## `error_zero_exit.json` / `no_structured_output.json` — now real, verbatim

These two now **are** `real_unauthenticated_failure.json` and
`real_plain_success.json` respectively, copied in unmodified. They already
played exactly the right role in the existing test suite (a payload that
fails to parse because `is_error` is true; a payload that fails to parse
because `structured_output` is absent) — real evidence slotted into the
scenario it was already standing in for, no test logic changed.

## Before this session

Every file here was synthetic, constructed from CLAUDE.md §6.2's documented
shape rather than captured — no Claude Code CLI was installed on the machine
where the adapter was written. That gap is what this session closed.
