"""Canonical JSON encoding, spec content addressing, and file digests.

Leaf module (CLAUDE.md section 2.2): imports nothing else from ``redgear``.

The three spec-hashing functions below are CLAUDE.md section 3.5's reference
implementation, reproduced exactly -- same encoder keyword arguments, same
sort rules, same field exclusions. Do not "improve" them: the recorded spec
digest ``sha256:dd2914...`` and every task's stored ``spec_hash`` depend on
this producing byte-identical output forever. Section 3.5 is normative; if
this module and that section ever disagree, this module is wrong.

``file_digest`` and ``digest_map`` are not in section 3.5. They serve the G2
frozen-hash gate (section 1.4, section 7.2), which SHA-256s every file
matching a task's ``frozen_globs`` at claim time and recomputes at
verification.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Read files in chunks so a large blob is never held whole in memory; the
# digest is identical regardless of chunk size.
_CHUNK_SIZE = 1 << 20  # 1 MiB


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding. The only encoder used for hashing.

    ``Any`` is permitted here and nowhere else in the package (section 11.2
    rule 1): this function's whole job is to accept arbitrary JSON-shaped
    data.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def compute_spec_hash(spec: dict[str, Any]) -> str:
    """
    Rules, all load-bearing:
      1. Requirements sorted by `id`, plain lexicographic.
      2. Each requirement dumped with None-valued fields omitted.
      3. `acceptance` lists preserve author order — order is semantic.
      4. `out_of_scope` is sorted — order is NOT semantic.
      5. Keys sorted, separators tight, no NaN, UTF-8.
    """
    requirements = sorted(spec["requirements"], key=lambda r: str(r["id"]))
    normalised = [{k: v for k, v in r.items() if v is not None} for r in requirements]
    payload = {
        "requirements": normalised,
        "out_of_scope": sorted(spec.get("out_of_scope", [])),
    }
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def spec_id_from_hash(spec_hash: str) -> str:
    """spec-<first 6 hex chars of the digest>."""
    return "spec-" + spec_hash.removeprefix("sha256:")[:6]


def file_digest(path: Path) -> str:
    """SHA-256 of a file's exact bytes, algorithm-prefixed.

    Opened in binary mode deliberately. Text mode applies universal-newline
    translation on read, so a CRLF file and an LF file with the same logical
    content would produce the same digest -- which would make the G2
    frozen-hash gate blind to a real byte-level change, and would make a
    digest taken on Windows disagree with the same file digested on Linux.

    Raises FileNotFoundError if the path does not exist; a missing frozen
    file is a gate failure the caller must see, not something to paper over
    with a sentinel digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def digest_map(repo_root: Path, relative_paths: Iterable[str]) -> dict[str, str]:
    """Build the G2 frozen-hash map: repo-relative POSIX path -> digest.

    Keys are normalised to forward slashes so a map recorded on Windows
    compares equal to one recorded on Linux for the same tree. Without that,
    every frozen-hash check would fail across platforms for reasons that
    have nothing to do with the files having changed.
    """
    result: dict[str, str] = {}
    for relative in relative_paths:
        key = relative.replace("\\", "/")
        result[key] = file_digest(repo_root / key)
    return result
