"""T-0001 smoke checks.

These are the four ``smoke::`` criteria declared on T-0001 in
``.redgear/task_graph.json``, plus self-validation of the two state files
(``.redgear/spec/spec.json`` and ``.redgear/task_graph.json``). The
state-file checks exist so a hand edit that breaks an invariant fails CI
immediately instead of failing silently later, once real tasks depend on
those invariants holding.

T-0001 is a ``scaffold`` task: two-phase TDD does not apply to packaging
metadata (CLAUDE.md section 4.5), so this file is written directly rather
than through a test_authoring / implementation pair.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / ".redgear" / "spec" / "spec.json"
GRAPH_PATH = REPO_ROOT / ".redgear" / "task_graph.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC-1: pip_install_editable
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_pip_install_editable() -> None:
    """The editable install succeeds and exposes the redgear command.

    "Exposes" means the console-script entry point is registered against
    the installed distribution -- not that invoking it succeeds. redgear.cli
    does not exist yet (it arrives at T-0033); pyproject.toml is frozen after
    this task (CLAUDE.md section 4.5 / task_graph.json T-0001 scope), so the
    entry point must be declared now even though its target module does not
    exist yet. Entry-point registration does not import the target module,
    so this holds regardless.
    """
    import redgear

    assert isinstance(redgear.__version__, str)
    assert redgear.__version__

    entry_points = importlib.metadata.entry_points(group="console_scripts")
    matches = [ep for ep in entry_points if ep.name == "redgear"]
    assert matches, "no 'redgear' console_script entry point is registered"
    assert matches[0].value == "redgear.cli:app"


# ---------------------------------------------------------------------------
# AC-2: ruff_and_mypy_run
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_ruff_and_mypy_run() -> None:
    """Strict type checking and linting both execute with zero errors."""
    ruff_result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ruff_result.returncode == 0, (
        f"ruff check failed:\nstdout={ruff_result.stdout}\nstderr={ruff_result.stderr}"
    )

    mypy_result = subprocess.run(
        [sys.executable, "-m", "mypy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mypy_result.returncode == 0, (
        f"mypy failed:\nstdout={mypy_result.stdout}\nstderr={mypy_result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC-3: ci_workflow_valid
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_ci_workflow_valid() -> None:
    """CI runs lint, types, tests, and the egress grep gates on push."""
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.exists()

    workflow = yaml.safe_load(ci_path.read_text(encoding="utf-8"))

    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = workflow.get(True, workflow.get("on"))
    assert "push" in triggers

    jobs = workflow["jobs"]
    job_steps_text = {
        job_name: json.dumps(job_def.get("steps", [])) for job_name, job_def in jobs.items()
    }
    all_text = json.dumps(jobs)

    assert "ruff check" in all_text
    assert "ruff format" in all_text
    assert any("mypy" in text for text in job_steps_text.values())
    assert any("pytest" in text for text in job_steps_text.values())

    egress_jobs = [
        job_def
        for job_def in jobs.values()
        if "ANTHROPIC_API_KEY" in json.dumps(job_def) and "httpx" in json.dumps(job_def)
    ]
    assert egress_jobs, "no CI job greps for credential env vars and network imports"


# ---------------------------------------------------------------------------
# AC-4: gitleaks_clean
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_gitleaks_clean() -> None:
    """The gitleaks pre-commit hook is installed and passes on a clean tree."""
    config_path = REPO_ROOT / ".pre-commit-config.yaml"
    assert config_path.exists()

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    hook_ids = {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}
    assert "gitleaks" in hook_ids

    gitleaks_bin = subprocess_which("gitleaks")
    if gitleaks_bin is None:
        pytest.skip("gitleaks binary not on PATH; config presence verified above")

    result = subprocess.run(
        [gitleaks_bin, "detect", "--no-banner", "--source", str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"gitleaks found findings on a supposedly clean tree:\n{result.stdout}"
    )


def subprocess_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


# ---------------------------------------------------------------------------
# State-file self-validation: spec.json
# ---------------------------------------------------------------------------


def _drop_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def compute_spec_hash(spec: dict[str, Any]) -> str:
    """Recompute spec.json's content-addressed hash.

    Canonical algorithm (CLAUDE.md section 3.5, added after this file first
    reverse-engineered it): sorted keys, tight separators, requirements
    sorted by id, None fields dropped, out_of_scope sorted, metadata
    excluded. Only ``requirements`` and ``out_of_scope`` are hashed --
    schema_version, project, created_at, spec_id, hash, and supersedes are
    provenance/identity fields, not content, and reordering or timestamping
    them must not change the content address. Verified byte-for-byte
    against section 3.5's own reference implementation.
    """
    requirements = sorted((_drop_none(r) for r in spec["requirements"]), key=lambda r: r["id"])
    out_of_scope = sorted(spec["out_of_scope"])
    payload = {"requirements": requirements, "out_of_scope": out_of_scope}
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return f"sha256:{digest}"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return _load_json(SPEC_PATH)  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def graph() -> dict[str, Any]:
    return _load_json(GRAPH_PATH)  # type: ignore[no-any-return]


@pytest.mark.smoke
def test_spec_hash_recomputes(spec: dict[str, Any]) -> None:
    assert compute_spec_hash(spec) == spec["hash"]


@pytest.mark.smoke
def test_spec_id_derives_from_hash(spec: dict[str, Any]) -> None:
    digest = spec["hash"].removeprefix("sha256:")
    assert spec["spec_id"] == f"spec-{digest[:6]}"


# ---------------------------------------------------------------------------
# State-file self-validation: task_graph.json
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_graph_spec_hash_matches_spec(spec: dict[str, Any], graph: dict[str, Any]) -> None:
    assert graph["spec_hash"] == spec["hash"]


@pytest.mark.smoke
def test_every_node_carries_current_spec_hash(spec: dict[str, Any], graph: dict[str, Any]) -> None:
    for node in graph["nodes"]:
        assert node["spec_hash"] == spec["hash"], node["id"]


@pytest.mark.smoke
def test_graph_is_acyclic(graph: dict[str, Any]) -> None:
    """Kahn's algorithm: a topological sort exists iff the graph is acyclic."""
    nodes = {n["id"] for n in graph["nodes"]}
    depends_on = {n["id"]: list(n["depends_on"]) for n in graph["nodes"]}

    in_degree = dict.fromkeys(nodes, 0)
    successors: dict[str, list[str]] = {n: [] for n in nodes}
    for node_id, deps in depends_on.items():
        in_degree[node_id] = len(deps)
        for dep in deps:
            successors[dep].append(node_id)

    queue = sorted(n for n, deg in in_degree.items() if deg == 0)
    visited: list[str] = []
    remaining = dict(in_degree)
    while queue:
        current = queue.pop(0)
        visited.append(current)
        for succ in successors[current]:
            remaining[succ] -= 1
            if remaining[succ] == 0:
                queue.append(succ)
        queue.sort()

    if len(visited) != len(nodes):
        cyclic = sorted(nodes - set(visited))
        pytest.fail(f"cycle detected among nodes: {cyclic}")


@pytest.mark.smoke
def test_depends_on_and_edges_resolve_to_real_nodes(graph: dict[str, Any]) -> None:
    node_ids = {n["id"] for n in graph["nodes"]}
    for node in graph["nodes"]:
        for dep in node["depends_on"]:
            assert dep in node_ids, f"{node['id']} depends_on unknown node {dep}"
    for edge in graph["edges"]:
        assert edge["from"] in node_ids, f"edge from unknown node {edge['from']}"
        assert edge["to"] in node_ids, f"edge to unknown node {edge['to']}"


@pytest.mark.smoke
def test_two_phase_tdd_holds(graph: dict[str, Any]) -> None:
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    for node in graph["nodes"]:
        if node["type"] == "scaffold":
            continue  # CLAUDE.md section 4.4 invariant 8: does not apply.
        if node["type"] == "implementation":
            assert node["acceptance_criteria"] == [], node["id"]
            assert node["inherits_criteria_from"], node["id"]
            for source_id in node["inherits_criteria_from"]:
                source = nodes_by_id[source_id]
                assert source["type"] == "test_authoring", (
                    f"{node['id']} inherits from non-test_authoring node {source_id}"
                )
        elif node["type"] == "test_authoring":
            assert node["inherits_criteria_from"] == [], node["id"]
            assert node["acceptance_criteria"], node["id"]


SCAFFOLD_REDGEAR_MARKER_EXCEPTIONS = frozenset({"redgear/__init__.py", "redgear/py.typed"})


@pytest.mark.smoke
def test_scaffold_never_writes_behavioral_redgear_code(graph: dict[str, Any]) -> None:
    """CLAUDE.md section 4.5: "A scaffold task may never write under
    redgear/**. Package code is behaviour and goes through the two-phase
    pattern without exception." T-0001 is the sole exception in practice,
    and only for two zero-logic marker files: the __init__.py version
    marker and the py.typed marker. Anything else under redgear/** on a
    scaffold node's writable/creatable globs would be smuggling behavioral
    code past the two-phase gate.
    """
    for node in graph["nodes"]:
        if node["type"] != "scaffold":
            continue
        for glob_list_name in ("writable_globs", "creatable_globs"):
            for pattern in node["scope"][glob_list_name]:
                if pattern == "redgear/**" or (pattern.startswith("redgear/") and "*" in pattern):
                    pytest.fail(
                        f"{node['id']}.{glob_list_name}: {pattern!r} grants scaffold "
                        "write access to a redgear/** wildcard, not just a marker file"
                    )
                if (
                    pattern.startswith("redgear/")
                    and pattern not in SCAFFOLD_REDGEAR_MARKER_EXCEPTIONS
                ):
                    allowed = sorted(SCAFFOLD_REDGEAR_MARKER_EXCEPTIONS)
                    pytest.fail(
                        f"{node['id']}.{glob_list_name}: {pattern!r} is not one of the "
                        f"documented marker-file exceptions {allowed}"
                    )


def _expand_globs_to_candidates(globs: list[str]) -> set[str]:
    candidates: set[str] = set()
    for pattern in globs:
        if "*" not in pattern:
            candidates.add(pattern)
        else:
            prefix = pattern.split("*")[0].rstrip("/")
            candidates.add(f"{prefix}/__redgear_probe__.py" if prefix else "__redgear_probe__.py")
    return candidates


@pytest.mark.smoke
def test_no_overlapping_writable_and_frozen_globs(graph: dict[str, Any]) -> None:
    for node in graph["nodes"]:
        writable = node["scope"]["writable_globs"]
        frozen = node["scope"]["frozen_globs"]
        if not writable or not frozen:
            continue

        candidates = _expand_globs_to_candidates(writable) | _expand_globs_to_candidates(frozen)
        for candidate in candidates:
            writable_hit = any(fnmatch.fnmatch(candidate, pat) for pat in writable)
            frozen_hit = any(fnmatch.fnmatch(candidate, pat) for pat in frozen)
            assert not (writable_hit and frozen_hit), (
                f"{node['id']}: {candidate!r} matches both writable and frozen globs"
            )


@pytest.mark.smoke
def test_criterion_selectors_are_unique(graph: dict[str, Any]) -> None:
    selectors = [
        criterion["verified_by"]["selector"]
        for node in graph["nodes"]
        for criterion in node["acceptance_criteria"]
    ]
    duplicates = {s for s in selectors if selectors.count(s) > 1}
    assert not duplicates, f"duplicate criterion selectors: {duplicates}"


@pytest.mark.smoke
def test_exactly_one_root_and_one_leaf(graph: dict[str, Any]) -> None:
    nodes = graph["nodes"]
    depended_on = {dep for n in nodes for dep in n["depends_on"]}

    roots = [n["id"] for n in nodes if not n["depends_on"]]
    leaves = [n["id"] for n in nodes if n["id"] not in depended_on]

    assert roots == ["T-0001"]
    assert leaves == ["T-0041"]


@pytest.mark.smoke
def test_graph_ships_in_draft_state(graph: dict[str, Any]) -> None:
    assert graph["state"] == "draft"
