"""T-0006: failing tests for redgear.hashing -- canonical JSON and content
addressing.

``redgear/hashing.py`` does not exist yet, so the import block below fails at
COLLECTION with ``ModuleNotFoundError``. That is the correct red state
(CLAUDE.md section 10.4): no try/except, no ``importorskip``, no mock.

This node's own graph note: "Determinism here is load-bearing. If the hash
drifts, every downstream guarantee is theatre." Every test below is written
against that standard -- the hash is asserted against the *real*
``.redgear/spec/spec.json`` and its recorded digest, not against a fixture
that could drift with the implementation.

CLAUDE.md section 3.5 is normative and carries the reference implementation.
These tests pin that behaviour exactly: same ``json.dumps`` kwargs, same
sort rules, same field exclusions.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from redgear.hashing import (
    canonical_json,
    compute_spec_hash,
    digest_map,
    file_digest,
    spec_id_from_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / ".redgear" / "spec" / "spec.json"

# The recorded, load-bearing values. If either of these changes, every task
# in the graph has been invalidated (section 3.5 drift handling).
#
# Updated at T-0041: NFR-10 (Python floor) and FR-12 (deferred UI) both
# changed, which moved the spec hash from spec-dd2914 to spec-97ee71 --
# exactly the "recorded value changed" event section 3.5 defines, carried
# out per its own mechanical steps (recompute, supersede, mark task_graph.json
# with the new hash). This file's own docstring says the point of these tests
# is to track the *real* spec.json, not a frozen historical one, so updating
# the constant to match is the test doing its job, not a frozen-file defect
# edit -- see docs/PROGRESS.md for the full account.
REAL_SPEC_HASH = "sha256:97ee71867c3867b80290dfd89c89d4c1dcb8843a8271ba4052b00c60e61ab0c6"
REAL_SPEC_ID = "spec-97ee71"


@pytest.fixture(scope="module")
def real_spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# AC-1: canonical encoding is byte-stable across process restarts and key
# reordering.
# ---------------------------------------------------------------------------


def test_canonical_json_stable() -> None:
    """Byte-stable under key reordering, and byte-identical across separate
    interpreter processes started with different PYTHONHASHSEED values.

    The cross-process half is the one that matters: a canonical encoder that
    is stable only within one process (because it happened to iterate a dict
    or set in insertion order) would still pass a same-process test and then
    produce a different digest on the user's machine.
    """
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_json(a) == canonical_json(b)

    # Tight separators, sorted keys, UTF-8 bytes out.
    assert canonical_json(a) == b'{"a":2,"b":1,"c":{"y":2,"z":1}}'
    assert isinstance(canonical_json(a), bytes)

    # ensure_ascii=False: non-ASCII is emitted as raw UTF-8, not \uXXXX.
    assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode()

    # allow_nan=False: NaN/Infinity are not valid JSON and must be refused
    # rather than silently emitted as bare NaN tokens.
    with pytest.raises(ValueError):
        canonical_json({"k": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"k": float("inf")})

    # Cross-process determinism under varied hash seeds.
    payload = {"requirements": [{"id": "FR-2"}, {"id": "FR-1"}], "s": "café", "n": 3}
    code = (
        "import json,sys;"
        "from redgear.hashing import canonical_json;"
        "sys.stdout.write(canonical_json(json.loads(sys.argv[1])).hex())"
    )
    outputs = set()
    for seed in ("0", "1", "42", "random"):
        result = subprocess.run(
            [sys.executable, "-c", code, json.dumps(payload)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={**_clean_env(), "PYTHONHASHSEED": seed},
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"canonical_json differed across processes: {outputs}"
    assert bytes.fromhex(outputs.pop()) == canonical_json(payload)


def _clean_env() -> dict[str, str]:
    """Minimal environment for a subprocess -- PATH so the interpreter
    resolves its own DLLs on Windows, SYSTEMROOT likewise, and nothing
    resembling a credential (G5)."""
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT", "VIRTUAL_ENV")
    return {k: os.environ[k] for k in keep if k in os.environ}


def test_canonical_json_is_the_only_encoder() -> None:
    """CLAUDE.md section 3.5: canonical_json is 'the only encoder used for
    hashing'. A second json.dumps call site inside the module -- even a
    well-meaning one -- is a latent second encoder with different kwargs,
    and would silently produce a different digest for the same input."""
    source = (REPO_ROOT / "redgear" / "hashing.py").read_text(encoding="utf-8")
    assert source.count("json.dumps") == 1, (
        "json.dumps must appear exactly once in hashing.py, inside canonical_json"
    )
    # And every hash in the module must be fed by it: no direct encode() of
    # a payload straight into sha256.
    assert "json.dump(" not in source


# ---------------------------------------------------------------------------
# AC-2 / AC-3: requirement ordering is irrelevant; requirement content is not.
# ---------------------------------------------------------------------------


def test_requirement_order_irrelevant(real_spec: dict[str, Any]) -> None:
    """Shuffling the requirements list never changes the hash, and neither
    does reordering out_of_scope. Run repeatedly with a seeded shuffler so
    the property is exercised, not just one lucky permutation."""
    baseline = compute_spec_hash(real_spec)
    assert baseline == REAL_SPEC_HASH

    # S311 is suppressed below: this is a seeded shuffler exercising a
    # determinism property, not a cryptographic random source.
    rng = random.Random(20260812)  # noqa: S311
    for _ in range(25):
        shuffled = dict(real_spec)
        requirements = list(real_spec["requirements"])
        rng.shuffle(requirements)
        shuffled["requirements"] = requirements

        out_of_scope = list(real_spec["out_of_scope"])
        rng.shuffle(out_of_scope)
        shuffled["out_of_scope"] = out_of_scope

        assert compute_spec_hash(shuffled) == baseline

    # Reversal is the adversarial permutation; assert it explicitly rather
    # than trusting the shuffler to have produced it.
    reversed_spec = dict(real_spec)
    reversed_spec["requirements"] = list(reversed(real_spec["requirements"]))
    reversed_spec["out_of_scope"] = list(reversed(real_spec["out_of_scope"]))
    assert compute_spec_hash(reversed_spec) == baseline


def test_requirement_change_alters_hash(real_spec: dict[str, Any]) -> None:
    """Changing ANY requirement's statement always changes the hash --
    checked for every requirement in the real spec, not a sample."""
    baseline = compute_spec_hash(real_spec)
    seen: set[str] = {baseline}

    for index in range(len(real_spec["requirements"])):
        mutated = dict(real_spec)
        requirements = [dict(r) for r in real_spec["requirements"]]
        requirements[index]["statement"] = requirements[index]["statement"] + " (amended)"
        mutated["requirements"] = requirements

        digest = compute_spec_hash(mutated)
        assert digest != baseline, f"requirement index {index} changed but the hash did not"
        assert digest not in seen, "two distinct mutations collided"
        seen.add(digest)

    # acceptance list order IS semantic (section 3.5 rule 3): reordering it
    # must change the hash, unlike reordering requirements or out_of_scope.
    reordered_acceptance = dict(real_spec)
    requirements = [dict(r) for r in real_spec["requirements"]]
    target = next(r for r in requirements if len(r["acceptance"]) > 1)
    target["acceptance"] = list(reversed(target["acceptance"]))
    reordered_acceptance["requirements"] = requirements
    assert compute_spec_hash(reordered_acceptance) != baseline

    # Adding and removing a requirement both move the hash.
    added = dict(real_spec)
    added["requirements"] = [
        *real_spec["requirements"],
        {
            "id": "FR-99",
            "kind": "functional",
            "statement": "A new requirement.",
            "rationale": "Because.",
            "acceptance": ["It holds."],
            "priority": "must",
        },
    ]
    assert compute_spec_hash(added) != baseline

    removed = dict(real_spec)
    removed["requirements"] = list(real_spec["requirements"][1:])
    assert compute_spec_hash(removed) != baseline

    # out_of_scope content (as opposed to order) is hashed.
    changed_scope = dict(real_spec)
    changed_scope["out_of_scope"] = [*real_spec["out_of_scope"], "Something new"]
    assert compute_spec_hash(changed_scope) != baseline


# ---------------------------------------------------------------------------
# AC-4: metadata fields are excluded from the hashed payload.
# ---------------------------------------------------------------------------


def test_metadata_excluded_from_hash(real_spec: dict[str, Any]) -> None:
    """section 3.5 names exactly six excluded fields: spec_id, hash,
    created_at, supersedes, project, schema_version. Renaming the project
    must not invalidate all 41 tasks."""
    baseline = compute_spec_hash(real_spec)

    metadata_mutations: dict[str, Any] = {
        "spec_id": "spec-ffffff",
        "hash": "sha256:" + "0" * 64,
        "created_at": "2099-01-01T00:00:00Z",
        "supersedes": "spec-000000",
        "project": {"name": "renamed-project", "root_globs": ["somewhere/**"]},
        "schema_version": 99,
    }
    for field, value in metadata_mutations.items():
        mutated = dict(real_spec)
        mutated[field] = value
        assert compute_spec_hash(mutated) == baseline, (
            f"metadata field {field!r} leaked into the hashed payload"
        )

    # All six changed at once still yields the recorded hash.
    all_mutated = {**real_spec, **metadata_mutations}
    assert compute_spec_hash(all_mutated) == baseline

    # An unknown extra top-level key is also not hashed -- only requirements
    # and out_of_scope are.
    with_extra = dict(real_spec)
    with_extra["some_future_field"] = ["anything"]
    assert compute_spec_hash(with_extra) == baseline

    # None-valued requirement fields are omitted (section 3.5 rule 2), so a
    # requirement carrying an explicit null is identical to one without it.
    with_null = dict(real_spec)
    requirements = [dict(r) for r in real_spec["requirements"]]
    requirements[0]["some_optional_field"] = None
    with_null["requirements"] = requirements
    assert compute_spec_hash(with_null) == baseline


def test_real_spec_hash_and_id_match_recorded_values(real_spec: dict[str, Any]) -> None:
    """The load-bearing assertion, against the real file on disk."""
    assert compute_spec_hash(real_spec) == REAL_SPEC_HASH
    assert spec_id_from_hash(REAL_SPEC_HASH) == REAL_SPEC_ID
    assert spec_id_from_hash(compute_spec_hash(real_spec)) == REAL_SPEC_ID

    # And against the values the file records about itself.
    assert real_spec["hash"] == REAL_SPEC_HASH
    assert real_spec["spec_id"] == REAL_SPEC_ID


def test_spec_id_derivation() -> None:
    """spec-<first 6 hex chars>, prefix stripped."""
    assert spec_id_from_hash("sha256:abcdef1234567890") == "spec-abcdef"
    # Tolerates a bare digest with no prefix (removeprefix is a no-op).
    assert spec_id_from_hash("abcdef1234567890") == "spec-abcdef"


# ---------------------------------------------------------------------------
# AC-5: file digests are stable and detect single-byte modifications.
# ---------------------------------------------------------------------------


def test_file_digest_detects_byte_change(tmp_path: Path) -> None:
    """Digests are algorithm-prefixed, stable, and flip on one byte."""
    target = tmp_path / "sample.py"
    target.write_bytes(b"def f():\n    return 1\n")

    first = file_digest(target)
    second = file_digest(target)

    assert first == second, "digest is not stable for unchanged content"
    assert first.startswith("sha256:"), "digest must be algorithm-prefixed, never bare hex"
    assert len(first) == len("sha256:") + 64
    assert first == "sha256:" + hashlib.sha256(b"def f():\n    return 1\n").hexdigest()

    # One byte changes: 1 -> 2.
    target.write_bytes(b"def f():\n    return 2\n")
    assert file_digest(target) != first

    # A single trailing byte appearing is also caught.
    target.write_bytes(b"def f():\n    return 1\n\n")
    assert file_digest(target) != first


def test_file_digest_is_binary_not_text(tmp_path: Path) -> None:
    """Digests must be computed over exact bytes.

    If the file were opened in text mode, Python's universal-newline
    translation would collapse CRLF to LF on read, so a CRLF file and an LF
    file with the same logical content would digest identically. On Windows
    that would make the G2 frozen-hash gate blind to a real byte-level
    change, and would make a digest taken on Windows disagree with the same
    file digested on Linux. This is the specific bug this test exists for.
    """
    crlf = tmp_path / "crlf.txt"
    lf = tmp_path / "lf.txt"
    crlf.write_bytes(b"line one\r\nline two\r\n")
    lf.write_bytes(b"line one\nline two\n")

    assert file_digest(crlf) != file_digest(lf), (
        "CRLF and LF files digested identically -- file_digest is reading in "
        "text mode and translating newlines"
    )
    assert file_digest(crlf) == "sha256:" + hashlib.sha256(b"line one\r\nline two\r\n").hexdigest()
    assert file_digest(lf) == "sha256:" + hashlib.sha256(b"line one\nline two\n").hexdigest()


def test_file_digest_handles_large_and_empty_files(tmp_path: Path) -> None:
    """Chunked reading must produce the same digest as a single read --
    a chunk-boundary bug would only show on files larger than the buffer."""
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert file_digest(empty) == "sha256:" + hashlib.sha256(b"").hexdigest()

    blob = bytes(range(256)) * 4096  # 1 MiB, spans many chunks
    large = tmp_path / "large.bin"
    large.write_bytes(blob)
    assert file_digest(large) == "sha256:" + hashlib.sha256(blob).hexdigest()


def test_file_digest_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        file_digest(tmp_path / "does-not-exist.txt")


def test_digest_map_builds_the_frozen_hash_map(tmp_path: Path) -> None:
    """The G2 frozen-hash map: repo-relative POSIX keys -> prefixed digests.

    Keys must be POSIX-relative regardless of host so a map taken on Windows
    is comparable with one taken on Linux (CLAUDE.md G2 / section 7.2).
    """
    repo = tmp_path / "repo"
    (repo / "redgear").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "redgear" / "a.py").write_bytes(b"a\n")
    (repo / "redgear" / "b.py").write_bytes(b"b\n")
    (repo / "tests" / "t.py").write_bytes(b"t\n")

    result = digest_map(repo, ["redgear/a.py", "redgear/b.py", "tests/t.py"])

    assert set(result) == {"redgear/a.py", "redgear/b.py", "tests/t.py"}
    assert all("\\" not in key for key in result), "keys must be POSIX-separated"
    assert all(value.startswith("sha256:") for value in result.values())
    assert result["redgear/a.py"] == "sha256:" + hashlib.sha256(b"a\n").hexdigest()
    assert result["redgear/a.py"] != result["redgear/b.py"]

    # Stable across calls, and order-independent in the input.
    assert digest_map(repo, ["tests/t.py", "redgear/b.py", "redgear/a.py"]) == result
    assert digest_map(repo, []) == {}
