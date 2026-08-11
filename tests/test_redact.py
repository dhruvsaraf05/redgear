"""T-0008: failing tests for redgear.redact -- credential redaction.

``redgear/redact.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state.

This module implements G5's redaction requirement (CLAUDE.md section 1.4):
"redact any variable matching ``(?i)(key|token|secret|password|credential)``
from every log line and every event record", and section 10.5's global
invariant 6: "No event record and no log line contains a value from an auth
environment variable."

Two halves, and the second is the load-bearing one:

* **By name** -- a mapping key matching the credential pattern has its value
  replaced. Catches the common case.
* **By value** -- a known secret appearing anywhere in free text is replaced.
  This is what catches a credential that leaked into a stack trace, a
  subprocess error message, or an argv echo, where there is no key to match
  on at all. Name-only redaction would miss every one of those.
"""

from __future__ import annotations

import re

import pytest
from redgear.redact import (
    CREDENTIAL_KEY_PATTERN,
    MIN_SECRET_LENGTH,
    REDACTED,
    collect_secrets,
    is_credential_key,
    redact_mapping,
    redact_text,
    redact_value,
)

# Fake credentials for redaction subjects. Deliberately NOT shaped like any
# real provider's token format (no `sk-ant-`, no `ghp_`, etc.): gitleaks runs
# as a CI gate (section 10.3) and pattern-matches those prefixes, so a
# realistic-looking fixture would fail the build on a value that is not a
# secret at all. Long and unique is all these need to be.
# S105 is suppressed: these are redaction test subjects, not credentials.
FAKE_SECRET = "FAKE-VALUE-AAAA-0000-NOT-A-REAL-CREDENTIAL"  # noqa: S105
OTHER_SECRET = "FAKE-VALUE-BBBB-1111-NOT-A-REAL-CREDENTIAL"  # noqa: S105


# ---------------------------------------------------------------------------
# AC-1: variables whose names match credential patterns are redacted.
# ---------------------------------------------------------------------------


def test_credential_named_keys_redacted() -> None:
    """Every key matching G5's pattern has its value replaced; every other
    key passes through untouched."""
    env = {
        "ANTHROPIC_API_KEY": FAKE_SECRET,
        "OPENAI_API_KEY": "sk-fake000000000000000",
        "GITHUB_TOKEN": OTHER_SECRET,
        "MY_SECRET": "hunter2hunter2",
        "DB_PASSWORD": "correct-horse-battery",
        "AWS_CREDENTIAL": "AKIAFAKE0000000000",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/dev",
        "LANG": "C.UTF-8",
        "CI": "1",
    }

    result = redact_mapping(env)

    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "MY_SECRET",
        "DB_PASSWORD",
        "AWS_CREDENTIAL",
    ):
        assert result[key] == REDACTED, f"{key} was not redacted"

    assert result["PATH"] == "/usr/bin:/bin"
    assert result["HOME"] == "/home/dev"
    assert result["LANG"] == "C.UTF-8"
    assert result["CI"] == "1"

    # The keys themselves survive -- redaction hides values, not the shape
    # of the record. An auditor must still see that a key existed.
    assert set(result) == set(env)

    # No secret value survives anywhere in the output.
    rendered = repr(result)
    for secret in (FAKE_SECRET, OTHER_SECRET, "hunter2hunter2", "correct-horse-battery"):
        assert secret not in rendered


def test_is_credential_key_is_case_insensitive() -> None:
    for name in ("API_KEY", "api_key", "ApiKey", "Token", "TOKEN", "secret", "SeCrEt"):
        assert is_credential_key(name) is True
    for name in ("PATH", "HOME", "LANG", "CI", "NO_COLOR", "iterations"):
        assert is_credential_key(name) is False


def test_credential_pattern_matches_g5_exactly() -> None:
    """G5 names the pattern literally. Pin all five alternatives so a later
    edit cannot quietly drop one."""
    for fragment in ("key", "token", "secret", "password", "credential"):
        assert re.search(CREDENTIAL_KEY_PATTERN, f"MY_{fragment.upper()}_VAR", re.I)
        assert is_credential_key(f"some_{fragment}_name") is True


def test_substring_match_over_redacts_deliberately() -> None:
    """G5's pattern is an unanchored substring match, so `MONKEY_COUNT`
    matches on "key" and is redacted.

    That is a false positive, and it is the correct direction to fail: over-
    redacting costs a line of debugging output, under-redacting leaks a
    credential. Pinned as intended behaviour so nobody "fixes" it into an
    anchored match without reading G5 first.
    """
    assert is_credential_key("MONKEY_COUNT") is True
    assert redact_mapping({"MONKEY_COUNT": "12"})["MONKEY_COUNT"] == REDACTED


# ---------------------------------------------------------------------------
# AC-2: a known secret value in free text is replaced before emission.
# ---------------------------------------------------------------------------


def test_value_redacted_in_free_text() -> None:
    """The load-bearing half: a credential that leaked into prose, with no
    key to match on, is still removed."""
    secrets = collect_secrets({"ANTHROPIC_API_KEY": FAKE_SECRET})

    traceback_text = (
        "Traceback (most recent call last):\n"
        '  File "runner.py", line 42, in dispatch\n'
        f"    raise RuntimeError('auth failed for {FAKE_SECRET}')\n"
        f"RuntimeError: auth failed for {FAKE_SECRET}\n"
    )

    result = redact_text(traceback_text, secrets)

    assert FAKE_SECRET not in result
    assert result.count(REDACTED) == 2, "every occurrence must be replaced, not just the first"
    # Surrounding context is preserved -- this is a redaction, not a purge.
    assert "Traceback (most recent call last):" in result
    assert "line 42, in dispatch" in result
    assert "RuntimeError: auth failed for" in result


def test_value_redaction_covers_argv_and_subprocess_output() -> None:
    """The specific leak paths G5 cares about: an argv echo and a
    subprocess error string, neither of which has a credential-named key."""
    secrets = collect_secrets({"GITHUB_TOKEN": OTHER_SECRET})

    argv_echo = f"running: git push https://{OTHER_SECRET}@github.com/x/y.git"
    stderr_blob = f"fatal: could not read Password for 'https://{OTHER_SECRET}@github.com'"

    assert OTHER_SECRET not in redact_text(argv_echo, secrets)
    assert OTHER_SECRET not in redact_text(stderr_blob, secrets)
    assert "github.com" in redact_text(argv_echo, secrets)


def test_short_values_are_not_value_redacted() -> None:
    """A 2-3 character 'secret' substring-matched across free text would
    corrupt unrelated words. Short values are excluded from VALUE redaction.

    They are still redacted by NAME -- the key is what identifies them.
    """
    assert MIN_SECRET_LENGTH >= 8

    secrets = collect_secrets({"API_KEY": "abc"})
    assert secrets == frozenset(), "a 3-char value must not become a value-redaction target"

    text = "the abccdef alphabet contains abc and abcdef"
    assert redact_text(text, secrets) == text, "short secret corrupted unrelated text"

    # But name-based redaction still applies regardless of length.
    assert redact_mapping({"API_KEY": "abc"})["API_KEY"] == REDACTED


def test_longest_secret_replaced_first() -> None:
    """When one secret is a prefix of another, replacing the shorter one
    first would leave a fragment of the longer one in the output."""
    short = "prefix-secret-000000"
    long = "prefix-secret-000000-with-more-entropy"
    secrets = collect_secrets({"A_TOKEN": short, "B_TOKEN": long})

    result = redact_text(f"saw {long} here", secrets)

    assert long not in result
    assert short not in result
    assert "-with-more-entropy" not in result, "a fragment of the longer secret survived"


def test_collect_secrets_only_gathers_credential_named_values() -> None:
    secrets = collect_secrets(
        {
            "ANTHROPIC_API_KEY": FAKE_SECRET,
            "PATH": "/usr/bin:/bin/some/long/path/value",
            "HOME": "/home/developer/workspace",
        }
    )
    assert FAKE_SECRET in secrets
    assert "/usr/bin:/bin/some/long/path/value" not in secrets
    assert "/home/developer/workspace" not in secrets


# ---------------------------------------------------------------------------
# AC-3: redaction is idempotent and never corrupts non-secret content.
# ---------------------------------------------------------------------------


def test_redaction_idempotent_and_lossless() -> None:
    """Redacting twice equals redacting once; clean content is byte-identical."""
    secrets = collect_secrets({"ANTHROPIC_API_KEY": FAKE_SECRET})

    dirty = f"before {FAKE_SECRET} after"
    once = redact_text(dirty, secrets)
    twice = redact_text(once, secrets)
    assert once == twice
    assert redact_text(twice, secrets) == once

    # Lossless on content containing no secret at all.
    clean = (
        "GATE tests_pass FAILED (1 failed, 47 passed)\n"
        "tests/ledger/test_posting.py:88\n"
        "  E       AssertionError: expected UnbalancedPosting, got IntegrityError\n"
    )
    assert redact_text(clean, secrets) == clean
    assert redact_text("", secrets) == ""
    assert redact_text(clean, frozenset()) == clean

    # Mapping redaction is idempotent too.
    env = {"ANTHROPIC_API_KEY": FAKE_SECRET, "PATH": "/usr/bin"}
    first = redact_mapping(env)
    assert redact_mapping(first) == first

    # And nested-record redaction.
    record = {"event": "run_started", "env": {"ANTHROPIC_API_KEY": FAKE_SECRET}}
    reduced = redact_value(record, secrets)
    assert redact_value(reduced, secrets) == reduced


def test_redact_value_walks_nested_event_records() -> None:
    """Event records nest (section 3.6: run_started carries a whole budget
    object), so redaction must recurse rather than only handling a flat
    top-level mapping."""
    record = {
        "event": "prompt_dispatched",
        "seq": 3,
        "actor": "engine",
        "allowed_tools": ["Read", "Glob", f"Bash(curl -H 'Authorization: {FAKE_SECRET}' *)"],
        "nested": {
            "deeper": {"ANTHROPIC_API_KEY": FAKE_SECRET},
            "note": f"token was {OTHER_SECRET}",
        },
        "parse_ok": True,
        "cost": None,
    }
    secrets = collect_secrets({"K_TOKEN": FAKE_SECRET, "J_SECRET": OTHER_SECRET})

    result = redact_value(record, secrets)
    rendered = repr(result)

    assert FAKE_SECRET not in rendered
    assert OTHER_SECRET not in rendered

    # Structure and non-secret scalars are preserved exactly.
    assert result["event"] == "prompt_dispatched"
    assert result["seq"] == 3
    assert result["parse_ok"] is True
    assert result["cost"] is None
    assert isinstance(result["allowed_tools"], list)
    assert result["allowed_tools"][0] == "Read"


def test_redact_module_never_logs_or_prints() -> None:
    """'Never log the secret while implementing redaction.' A print or a
    logging call inside the redactor is the one place a credential would be
    emitted in cleartext by the very code meant to prevent that."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "redgear" / "redact.py").read_text(
        encoding="utf-8"
    )
    assert "print(" not in source
    assert "logging" not in source
    assert "sys.stdout" not in source
    assert "sys.stderr" not in source


def test_redacted_placeholder_is_a_visible_marker() -> None:
    """The placeholder must be a non-empty, visible marker, so a reader can
    tell redaction happened rather than assuming the value was empty or the
    key was absent."""
    assert isinstance(REDACTED, str)
    assert REDACTED.strip() != ""
    # Redacting text that already contains the placeholder is a no-op --
    # this is what makes repeated redaction safe (see the idempotency test).
    assert redact_text(f"x {REDACTED} y", frozenset()) == f"x {REDACTED} y"


@pytest.mark.parametrize("value", ["", "   "])
def test_collect_secrets_ignores_empty_values(value: str) -> None:
    """An empty or whitespace credential variable must not become a secret.
    Substring-replacing the empty string would corrupt every character
    boundary in the text; replacing "   " would mangle ordinary indentation.
    """
    assert collect_secrets({"ANTHROPIC_API_KEY": value}) == frozenset()
