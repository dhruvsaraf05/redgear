# Recorded Claude Code payloads

**These payloads are SYNTHETIC.** They were constructed from CLAUDE.md §6.2's
documented shape, not captured from a live `claude -p` invocation: no Claude
Code CLI was installed on the machine where T-0034 was written (`claude` was
not on PATH, verified before writing them).

That matters, and it is why it is stated here rather than buried:

* They are correct against the **contract**, which is what the adapter is
  written to. They are not evidence about any **installed release**.
* The first time someone runs the manual procedure in
  `docs/agents/claude-code.md` against a real CLI, these should be replaced
  with genuinely captured output and this notice deleted.
* If a field here disagrees with a real payload, the real payload is right
  (§2.4: "Where this section and the installed CLI disagree, the CLI is
  right").

Every file is one complete stdout payload as `--output-format json` emits it.
