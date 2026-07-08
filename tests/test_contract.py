"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-billing emits its **own** contract triad — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (generate_flow_docs machine
artifact — empty, this module has no ``@flow_step`` annotations) and
``docs/errors.json`` (generate_error_keys registry, already the etalon) — from
a single-module ``{billing + core}`` Django instance mounted at the canonical
``/billing/api/`` prefix. The frontend codegen consumes these committed
artifacts instead of the monolith aggregate. Copied from stapel-auth's
reference implementation (contract-pipeline.md §7 Wave 1 recipe).

The emitted schema is **byte-identical to the monolith aggregate's billing
slice** (paths under ``/billing/api/`` + their transitive component closure);
see ``test_matches_monolith_billing_slice`` — the guarantee the whole repoint
rests on.

Regenerate after any change to a serializer / view / url / error key:

    make contract        # or: python -m stapel_billing._codegen --out docs

then commit ``docs/{schema,flows,errors}.json``. Without regenerating, the drift
gate below fails — the same byte-stable regenerate-and-diff discipline as
``test_error_keys``.

The harness runs in a **subprocess**: this test process already configured Django
(via conftest, on the test urlconf), and the harness needs its own drf-spectacular
singleton — a clean interpreter is the honest way to exercise exactly what
``make contract`` runs.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")


def _emit(out_dir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "stapel_billing._codegen", "--out", str(out_dir)],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def test_contract_triad_committed():
    for name in TRIAD:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"


def test_contract_has_no_drift(tmp_path):
    """Regenerate into a temp dir; committed triad must match byte-for-byte."""
    _emit(tmp_path)
    for name in TRIAD:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in TRIAD:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    """Schema paths + flow endpoints are /billing/api/*, not bare."""
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/billing/api/") for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /billing/api/ prefix"
    )
    flows = json.loads((DOCS / "flows.json").read_text())
    for flow in flows:
        for step in flow.get("steps", []):
            for ep in step.get("endpoints", []):
                assert ep["path"].startswith("/billing/api/"), (
                    f"flow endpoint {ep['path']} is not canonically prefixed"
                )


def test_flows_empty():
    """This module has no @flow_step annotations — flows.json = [] is valid."""
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == []


# --- Byte-identity regression vs the monolith aggregate's billing slice -------
# Only runs in the workspace (the monolith is a sibling repo, absent in module CI).

_MONO = REPO.parent / "stapel-example-monolith" / "codegen" / "generated" / "schema.json"


def _closure(schema: dict, seeds: set[str]) -> set[str]:
    import re

    comps = schema["components"]["schemas"]
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen or name not in comps:
            continue
        seen.add(name)
        for ref in re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(comps[name])):
            stack.append(ref)
    return seen


def _refs(obj) -> set[str]:
    import re

    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


@pytest.mark.skipif(
    not _MONO.exists() or os.environ.get("STAPEL_SKIP_MONOLITH_IDENTITY"),
    reason="monolith aggregate not present (module CI checks out only this repo)",
)
def test_matches_monolith_billing_slice():
    """docs/schema.json == the monolith aggregate's /billing/api/ slice, byte-for-byte.

    Compares path objects and the transitive component closure — the envelope
    (info/servers) is intentionally not compared (it names billing, not the
    monolith).
    """
    mine = json.loads((DOCS / "schema.json").read_text())
    mono = json.loads(_MONO.read_text())

    mono_paths = {p: v for p, v in mono["paths"].items() if p.startswith("/billing/api/")}
    assert set(mine["paths"]) == set(mono_paths), "path set differs from monolith slice"
    for p in mono_paths:
        assert json.dumps(mine["paths"][p], sort_keys=True) == json.dumps(
            mono_paths[p], sort_keys=True
        ), f"path object {p} differs from monolith slice"

    seeds: set[str] = set()
    for v in mono_paths.values():
        seeds |= _refs(v)
    mono_cl = _closure(mono, seeds)
    my_seeds: set[str] = set()
    for v in mine["paths"].values():
        my_seeds |= _refs(v)
    my_cl = _closure(mine, my_seeds)
    assert mono_cl == my_cl, "component closure differs from monolith slice"
    for c in mono_cl:
        assert json.dumps(mine["components"]["schemas"][c], sort_keys=True) == json.dumps(
            mono["components"]["schemas"][c], sort_keys=True
        ), f"component {c} differs from monolith slice"
