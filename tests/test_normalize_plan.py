"""Phase 2 (doc 43 D-P2/E-P2): R3 normalize — plan-level structure, tokens,
and gain arithmetic (no toolchain needed)."""

from __future__ import annotations


from loom.compiler import compile_manifest
from loom.executor import Executor
from loom.manifest import load_manifest
from loom.plan import gain_token

from .conftest import compile_text


def _manifest(project, targets: str, normalize: float = -16,
              wavs=None, extra=None):
    wavs = wavs or {"wavs/main.wav": 2}
    return project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        f"policy:\n  loudness: {{ normalize: {normalize} }}\n"
        f"targets:\n{targets}",
        wavs, extra_files=extra,
    )


def test_normalize_iamftools_chain_shape(project):
    mf = _manifest(project, "  - { format: iamf, out: x.iamf }\n")
    plan = compile_text(mf)
    kinds = [s.kind for s in plan.steps]
    # verify sits BEFORE the delivery copy: a missed normalize never ships
    assert kinds == ["stage_input", "write_config", "encode", "render",
                     "measure_bs1770", "gain_ride", "write_config", "encode",
                     "verify_loudness", "copy"]
    ride = next(s for s in plan.steps if s.kind == "gain_ride")
    # the ride reads the staged WAV and its gain comes from the pre-measure
    assert any("${gain:" in a for a in ride.argv)
    assert ride.argv[-1].endswith("main.wav") and "/wavn/" in ride.argv[-1]
    # the FINAL encode reads the ridden WAV
    enc_final = [s for s in plan.steps if s.kind == "encode"][-1]
    assert any("/wavn/" in a for a in enc_final.argv)
    # config for the final encode describes 24-bit (ridden) input
    cfg_final = [s for s in plan.steps if s.kind == "write_config"][-1]
    assert "sample_size: 24" not in cfg_final.content  # opus: no pcm block
    verify = next(s for s in plan.steps if s.kind == "verify_loudness")
    assert verify.params["method"] == "bitstream"
    assert verify.params["anchor"] == "stereo"
    assert verify.params["target"] == -16.0


def test_normalize_oneshot_chain_shape(project):
    from .conftest import fake_mp4
    mf = _manifest(project,
                   "  - { format: mp4, out: x.mp4, video: v.mp4 }\n",
                   normalize=-14, extra={"v.mp4": fake_mp4()})
    plan = compile_text(mf)
    kinds = [s.kind for s in plan.steps]
    # pre-chain (ffmpeg pass-A) + ride, then the standard two-pass on the
    # ridden WAV: three ffmpeg encodes total (D-P2)
    assert kinds[:6] == ["stage_input", "encode", "render", "measure_bs1770",
                        "gain_ride", "encode"]
    assert kinds[-1] == "verify_loudness"
    encodes = [s for s in plan.steps if s.kind == "encode"]
    assert len(encodes) == 3 and all(s.tool == "ffmpeg" for s in encodes)
    # pass-B and final read the ridden WAV; pass-A reads the staged one
    assert any("/wav/" in a and "/wavn/" not in a for a in encodes[0].argv)
    for enc in encodes[1:]:
        assert any("/wavn/" in a for a in enc.argv)
    verify = plan.steps[-1]
    assert verify.params["method"] == "measured"
    assert verify.params["measure_step"].endswith("-measure-stereo")


def test_normalize_plans_deterministic(project):
    mf = _manifest(project, "  - { format: iamf, out: x.iamf }\n")
    assert compile_text(mf).dumps() == compile_text(mf).dumps()


def test_no_normalize_plans_unchanged(project):
    """Without normalize: no new step kinds anywhere (golden stability)."""
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    plan = compile_text(mf)
    kinds = {s.kind for s in plan.steps}
    assert "gain_ride" not in kinds and "verify_loudness" not in kinds


def test_gain_token_resolution_and_ledger(project, tmp_path):
    """Executor arithmetic: ${gain:sid:target} -> target - measured il."""
    mf = _manifest(project, "  - { format: iamf, out: x.iamf }\n")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "o", tmp_path / "w")
    sid = "t00-x-measure-anchor-pre"
    ex.measured[sid] = {"il": -8.7, "dp": -18.0, "tp": -17.9}
    got = ex._resolve(gain_token(sid, -16.0))
    assert got == "-7.30"
    rec = ex.ledger["normalize"][sid]
    assert rec["applied_gain_db"] == -7.3
    assert rec["pre_ride_il"] == -8.7
    assert rec["target_lufs"] == -16.0


def test_verify_loudness_measured_method(project, tmp_path):
    from loom.plan import Step
    mf = _manifest(project, "  - { format: iamf, out: x.iamf }\n")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "o", tmp_path / "w")
    ex.measured["ms"] = {"il": -16.2, "dp": -8.0, "tp": -7.9}
    ok_step = Step(id="v1", kind="verify_loudness", tool="internal",
                   params={"method": "measured", "measure_step": "ms",
                           "anchor": "stereo", "target": -16.0,
                           "tolerance": 0.3})
    rec = ex._run_step(ok_step)
    assert rec["ok"] and abs(rec["delta_lu"] + 0.2) < 1e-9
    bad_step = Step(id="v2", kind="verify_loudness", tool="internal",
                    params={"method": "measured", "measure_step": "ms",
                            "anchor": "stereo", "target": -17.0,
                            "tolerance": 0.3})
    rec2 = ex._run_step(bad_step)
    assert not rec2["ok"] and "normalize missed" in rec2["error"]
