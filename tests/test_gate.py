"""Phase 2 (doc 43 D-P4/E-P5): the Sentinel gate — unit level.

The mutation-catch E2E accept test lives in test_normalize_toolchain.py
(toolchain-gated); here: policy plumbing, verdict mapping, tier recording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.compiler import compile_manifest
from loom.executor import Executor
from loom.gate import GateResult, run_gate
from loom.manifest import load_manifest


def _executor(project, tmp_path, validate="fail_on_error"):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        f"policy:\n  validate: {validate}\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    m = load_manifest(mf)
    plan = compile_manifest(m)
    return m, Executor(plan, m.manifest_dir, tmp_path / "o", tmp_path / "w",
                       validate_policy=m.policy.validate)


def test_gate_result_verdicts():
    ok = GateResult(path="x", tier="l1l2", findings=[
        {"id": "S-403", "severity": "WARN", "message": "case quirk"}])
    assert ok.passed
    bad = GateResult(path="x", tier="l1l2+l3", fail_ids=["S-320"], findings=[
        {"id": "S-320", "severity": "FAIL", "message": "duplication"}])
    assert not bad.passed
    err = GateResult(path="x", tier="error", execution_error="boom")
    assert not err.passed
    assert bad.to_json()["fail_ids"] == ["S-320"]


def test_gate_on_valid_bitstream_sample(monkeypatch):
    """L1/L2 tier on a known-good WP1 sample: no FAIL findings.

    (Tier forced to L1/L2: Toolchain.discover falls through to the canonical
    root/$PATH by design, so the tier is pinned for determinism here; the
    l1l2+l3 tier is exercised by the toolchain-gated E2E tests.)"""
    sample = Path("/tmp/sentinel_build/wp1/wp1-samples/stereo_iamftools.iamf")
    if not sample.is_file():
        pytest.skip("wp1 sample tree not staged")
    monkeypatch.setattr("loom.gate._l3_toolchain", lambda root: None)
    gr = run_gate(sample, Path("/nonexistent-toolchain"))
    assert gr.tier == "l1l2"
    assert gr.passed, gr.fail_ids


def test_gate_execution_error_on_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr("loom.gate._l3_toolchain", lambda root: None)
    p = tmp_path / "junk.iamf"
    p.write_bytes(b"\x00" * 32)
    gr = run_gate(p, Path("/nonexistent-toolchain"))
    # whatever sentinel reports (parse-level FAIL findings or an execution
    # error), the gate must not pass a garbage file
    assert not gr.passed


def test_validate_off_skips_gate(project, tmp_path, monkeypatch):
    m, ex = _executor(project, tmp_path, validate="off")
    calls = []
    monkeypatch.setattr("loom.executor.run_gate",
                        lambda *a, **k: calls.append(a))
    # only run the internal steps (no toolchain in unit tests): simulate by
    # marking every target complete with a fake output
    out = tmp_path / "o" / "x.iamf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x")
    monkeypatch.setattr(ex, "_run_step",
                        lambda step: {"id": step.id, "kind": step.kind,
                                      "tool": step.tool, "ok": True})
    res = ex.run()
    assert res.ok
    assert not calls, "gate ran despite validate: off"
    assert ex.ledger["gate"]["x.iamf"]["tier"] == "skipped"


def test_gate_fail_fails_run(project, tmp_path, monkeypatch):
    m, ex = _executor(project, tmp_path)
    out = tmp_path / "o" / "x.iamf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x")
    monkeypatch.setattr(ex, "_run_step",
                        lambda step: {"id": step.id, "kind": step.kind,
                                      "tool": step.tool, "ok": True})
    monkeypatch.setattr(
        "loom.executor.run_gate",
        lambda *a, **k: GateResult(path=str(out), tier="l1l2+l3",
                                   fail_ids=["S-320"],
                                   findings=[{"id": "S-320",
                                              "severity": "FAIL",
                                              "message": "dup"}]))
    res = ex.run()
    assert not res.ok
    assert any("gate[x.iamf]" in f for f in res.failures)
    assert ex.ledger["gate"]["x.iamf"]["fail_ids"] == ["S-320"]


def test_gate_warn_passes_run(project, tmp_path, monkeypatch):
    m, ex = _executor(project, tmp_path)
    out = tmp_path / "o" / "x.iamf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x")
    monkeypatch.setattr(ex, "_run_step",
                        lambda step: {"id": step.id, "kind": step.kind,
                                      "tool": step.tool, "ok": True})
    monkeypatch.setattr(
        "loom.executor.run_gate",
        lambda *a, **k: GateResult(path=str(out), tier="l1l2",
                                   findings=[{"id": "S-403",
                                              "severity": "WARN",
                                              "message": "case"}]))
    res = ex.run()
    assert res.ok
    assert ex.ledger["gate"]["x.iamf"]["passed"]
