"""Credential redaction for log lines and event records.

Leaf module (CLAUDE.md section 2.2): imports nothing else from ``redgear``.

Implements G5 (section 1.4): "redact any variable matching
``(?i)(key|token|secret|password|credential)`` from every log line and every
event record", and section 10.5's global invariant 6: "No event record and no
line contains a value from an auth environment variable."

Redaction has two halves, and the second is the load-bearing one:

* **By name** -- a mapping key matching the credential pattern has its value
  replaced. This catches the ordinary case of an environment dump.
* **By value** -- a known secret string is replaced wherever it appears in
  free text. This is what catches a credential that leaked into a stack
  trace, a subprocess error message, or an argv echo, where there is no key
  to match against at all. Name-only redaction misses every one of those.

This module emits nothing. It has no output side effects of any kind, by
construction: the one place a credential must never appear in cleartext is
the very code responsible for removing it.

Note on G5 and this module: nothing here ever *reads* an auth variable from
the process environment. Callers pass a mapping in; propagation without
inspection remains the rule (section 1.4 G5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

#: G5 names this pattern literally. Unanchored and case-insensitive, so it
#: matches a fragment anywhere in a variable name. Kept as a string rather
#: than a compiled pattern so callers can pass it to ``re`` with their own
#: flags.
CREDENTIAL_KEY_PATTERN = r"(?i)(key|token|secret|password|credential)"

#: Visible marker, so a reader can tell a value was removed rather than
#: assuming it was empty or that the key was absent.
REDACTED = "[REDACTED]"

#: Values shorter than this are never used as VALUE-redaction targets.
#: Substring-replacing a 3-character value would corrupt unrelated words --
#: a "secret" of "abc" would mangle "abcdef" into "[REDACTED]def". Such a
#: value is still redacted by NAME, which is what actually identifies it.
MIN_SECRET_LENGTH = 8

_CREDENTIAL_KEY_RE = re.compile(CREDENTIAL_KEY_PATTERN)

#: JSON-shaped data. Spelled out recursively rather than as ``Any``, which
#: section 11.2 rule 1 permits only in ``hashing.canonical_json``.
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


def is_credential_key(name: str) -> bool:
    """True if a variable name looks like it holds a credential.

    The match is an unanchored substring test, so ``MONKEY_COUNT`` matches
    on "key" and will be redacted. That false positive is deliberate and is
    the correct direction to fail: over-redacting costs one line of
    debugging output, under-redacting leaks a credential.
    """
    return _CREDENTIAL_KEY_RE.search(name) is not None


def collect_secrets(mapping: Mapping[str, str]) -> frozenset[str]:
    """Gather the values worth hunting for in free text.

    Only credential-named keys contribute, and only values long enough to be
    matched safely (see ``MIN_SECRET_LENGTH``). Empty and whitespace-only
    values are dropped: replacing the empty string would corrupt every
    character boundary in the text, and replacing "   " would mangle
    ordinary indentation.
    """
    secrets: set[str] = set()
    for name, value in mapping.items():
        if not is_credential_key(name):
            continue
        if not value or not value.strip():
            continue
        if len(value) < MIN_SECRET_LENGTH:
            continue
        secrets.add(value)
    return frozenset(secrets)


def redact_text(text: str, secrets: Iterable[str] = frozenset()) -> str:
    """Replace every occurrence of every known secret in free text.

    Secrets are applied longest-first. If one secret is a prefix of another,
    replacing the shorter one first would consume the prefix and leave the
    remainder of the longer secret stranded in the output.

    Idempotent: the marker contains no secret, so a second pass is a no-op.
    Lossless: text containing no secret is returned unchanged.
    """
    if not text:
        return text
    result = text
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) < MIN_SECRET_LENGTH:
            continue
        result = result.replace(secret, REDACTED)
    return result


def redact_mapping(
    mapping: Mapping[str, str], secrets: Iterable[str] = frozenset()
) -> dict[str, str]:
    """Redact a flat string mapping by key name, then by value.

    Keys are preserved. Redaction hides values, not the shape of the record
    -- an auditor must still be able to see that a variable was present.
    """
    result: dict[str, str] = {}
    for name, value in mapping.items():
        if is_credential_key(name):
            result[name] = REDACTED
        else:
            result[name] = redact_text(value, secrets)
    return result


def redact_value(value: JsonValue, secrets: Iterable[str] = frozenset()) -> JsonValue:
    """Recursively redact a JSON-shaped record.

    Event records nest (section 3.6: ``run_started`` carries a whole budget
    object), so a flat top-level pass would miss a credential one level down.

    Non-string scalars pass through untouched and keep their exact type --
    ``bool`` is checked implicitly by falling through, never coerced to
    ``int``, so ``parse_ok`` stays ``True`` rather than becoming ``1``.
    """
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        redacted: dict[str, JsonValue] = {}
        for name, item in value.items():
            if is_credential_key(name):
                redacted[name] = REDACTED
            else:
                redacted[name] = redact_value(item, secrets)
        return redacted
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    return value
