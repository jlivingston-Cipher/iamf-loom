"""Regenerate the golden snapshots (dev tool, not a test).

Plan goldens: used once in the doc-44 cycle for the D-Q8 emitter retag.
Explain goldens (doc 45, D-X4): rendered from the same CASES via
`render_explain`. Compiles/renders every CASES manifest twice in a scratch
project dir (determinism asserted), then rewrites tests/fixtures/golden/
and tests/fixtures/golden-explain/.

    PYTHONPATH=… python -m tests.regen_goldens
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from loom.compiler import compile_manifest
from loom.explain import render_explain
from loom.manifest import load_manifest

from .conftest import write_wav
from .test_compile_golden import CASES, GOLDEN_DIR, MANIFEST_DIR

EXPLAIN_DIR = GOLDEN_DIR.parent / "golden-explain"


def _artifacts(name: str, wavs: dict, extra: dict, root: Path):
    """Compile one CASES manifest in a scratch project; return (plan, explain)."""
    proj = root / name
    for rel, ch in wavs.items():
        write_wav(proj / rel, ch)
    for rel, content in extra.items():
        fp = proj / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
    mf = proj / f"{name}.yaml"
    mf.write_text((MANIFEST_DIR / f"{name}.yaml").read_text())
    m = load_manifest(mf)
    plan = compile_manifest(m)
    return plan.dumps(), render_explain(m, plan)


def _refresh(path: Path, text: str) -> bool:
    old = path.read_text() if path.is_file() else None
    if old != text:
        path.write_text(text)
        print(f"rewrote {path.name}")
        return True
    print(f"unchanged {path.name}")
    return False


def main() -> int:
    EXPLAIN_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        changed = 0
        for name, wavs, extra in CASES:
            plan_a, exp_a = _artifacts(name, wavs, extra, root / "a")
            plan_b, exp_b = _artifacts(name, wavs, extra, root / "b")
            assert plan_a == plan_b, f"{name}: compile not deterministic"
            assert exp_a == exp_b, f"{name}: explain not deterministic"
            changed += _refresh(GOLDEN_DIR / f"{name}.plan.json", plan_a)
            changed += _refresh(EXPLAIN_DIR / f"{name}.explain.txt", exp_a)
        print(f"{changed} golden file(s) rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
