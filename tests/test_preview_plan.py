"""R9 (doc 46) compile-side: the preview target surface, negatives, routing,
plan shape, and explain content. Toolchain-free by construction (E-Y2/E-Y3).
"""

from __future__ import annotations

import json

import pytest

from loom.compiler import compile_manifest
from loom.diagnostics import CompileError
from loom.explain import render_explain
from loom.manifest import load_manifest

from .conftest import compile_text

BASE = (
    "loom: 0\ntitle: T\n"
    "sources:\n  main: {{ path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }}\n"
    "elements:\n  bed: {{ from: main }}\n"
    "presentations:\n"
    "  - id: main\n"
    "    elements: [ {{ ref: bed, headphones: binaural }} ]\n"
    "{policy}"
    "targets:\n{targets}"
)


def _codes(exc: pytest.ExceptionInfo) -> list[str]:
    return exc.value.codes()


def _mf(project, targets: str, policy: str = "", wavs=None):
    return project(BASE.format(targets=targets, policy=policy),
                   wavs or {"wavs/main.wav": 12})


# -- negatives (E-Y2) ---------------------------------------------------------

def test_preview_bad_extension_m203(project):
    mf = _mf(project, "  - { format: preview, out: review/a.mp4 }\n")
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-203" in _codes(e)
    assert ".wav" in str(e.value) and ".opus" in str(e.value)


def test_preview_video_m402(project):
    mf = _mf(project,
             "  - { format: preview, out: review/a.wav, video: v.mp4 }\n")
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-402" in _codes(e)
    assert "audio-only" in str(e.value)


def test_preview_preset_m402(project):
    mf = _mf(project,
             "  - { format: preview, out: review/a.wav, preset: youtube }\n")
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-402" in _codes(e)
    assert "preset" in str(e.value)


def test_preview_without_binaural_element_m402(project):
    """Probe-forced (doc 46 deviation 1): headphones STEREO renders the
    stereo downmix under a Binaural layout — a mislabeled preview dies at
    compile, naming the remedy."""
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "presentations:\n"
        "  - id: main\n"
        "    elements: [ { ref: bed } ]\n"       # headphones: stereo (default)
        "targets:\n  - { format: preview, out: review/a.wav }\n",
        {"wavs/main.wav": 12})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-402" in _codes(e)
    msg = str(e.value)
    assert "headphones: binaural" in msg and "stereo downmix" in msg


@pytest.mark.parametrize("route", ["oneshot", "remux"])
def test_preview_route_m403(project, route):
    mf = _mf(project,
             f"  - {{ format: preview, out: review/a.wav, route: {route} }}\n")
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert "M-403" in _codes(e)
    assert "exactly one route" in str(e.value)


# -- routing + plan shape (E-Y3) ----------------------------------------------

def test_preview_routes_iamftools_with_r9_rationale(project):
    plan = compile_text(_mf(project,
                            "  - { format: preview, out: review/a.wav }\n"))
    tp = plan.targets[0]
    assert tp.backend == "iamftools" and tp.muxer is None
    for needle in ("R9", "obr", "ADR-4", "F10", "Binaural"):
        assert needle in tp.rationale, f"rationale misses {needle}"


def test_preview_gate_path_names_intermediate(project):
    plan = compile_text(_mf(project,
                            "  - { format: preview, out: review/a.wav }\n"))
    tp = plan.targets[0]
    assert tp.gate_path and tp.gate_path.endswith(".iamf")
    assert tp.gate_path.startswith("$WORK/")
    # emitted in JSON only when set; non-preview targets never carry the key
    doc = json.loads(plan.dumps())
    assert doc["targets"][0]["gate_path"] == tp.gate_path


def test_non_preview_targets_carry_no_gate_path(project):
    plan = compile_text(_mf(project,
                            "  - { format: iamf, out: dist/a.iamf }\n"))
    doc = json.loads(plan.dumps())
    assert "gate_path" not in doc["targets"][0]


def test_preview_wav_chain_shape(project):
    plan = compile_text(_mf(project,
                            "  - { format: preview, out: review/a.wav }\n"))
    kinds = [next(s for s in plan.steps if s.id == sid).kind
             for sid in plan.targets[0].step_ids]
    assert kinds == ["stage_input", "write_config", "encode", "render",
                     "measure_bs1770", "verify_preview", "copy"]
    render = next(s for s in plan.steps if s.kind == "render")
    assert "--output_layout=Binaural" in render.argv
    verify = next(s for s in plan.steps if s.kind == "verify_preview")
    assert verify.params["expect_channels"] == 2
    assert verify.params["container"] == "wav"


def test_preview_opus_chain_uses_bitexact(project):
    plan = compile_text(_mf(
        project, "  - { format: preview, out: review/a.opus }\n"))
    kinds = [next(s for s in plan.steps if s.id == sid).kind
             for sid in plan.targets[0].step_ids]
    assert kinds[-1] == "encode"          # opus encode delivers, no copy
    opus = next(s for s in plan.steps
                if s.kind == "encode" and s.tool == "ffmpeg")
    assert "-bitexact" in opus.argv and "libopus" in opus.argv
    verify = next(s for s in plan.steps if s.kind == "verify_preview")
    assert verify.params["container"] == "opus"
    # the frame-exactness contract binds on the pre-encode WAV (D-Y6)
    assert verify.params["wav"].endswith("binaural.wav")


def test_preview_under_normalize_renders_ridden_program(project):
    plan = compile_text(_mf(
        project, "  - { format: preview, out: review/a.wav }\n",
        policy="policy:\n  loudness: { mode: measure, normalize: -16 }\n"))
    ids = plan.targets[0].step_ids
    kinds = [next(s for s in plan.steps if s.id == sid).kind for sid in ids]
    assert "gain_ride" in kinds and "verify_loudness" in kinds
    # the preview's final encode reads the RIDDEN wav, not the staged one
    enc = next(s for s in plan.steps
               if s.id.endswith("-enc") and not s.id.endswith("-enc-pre"))
    assert any("/wavn/" in r for r in enc.reads), enc.reads
    # ride precedes the binaural render (by id — the pre-ride chain has its
    # own anchor render, so kind-index alone is ambiguous)
    ride_i = next(i for i, sid in enumerate(ids) if sid.endswith("-ride"))
    render_i = next(i for i, sid in enumerate(ids)
                    if sid.endswith("-render-binaural"))
    verify_i = next(i for i, sid in enumerate(ids) if sid.endswith("-verify"))
    assert ride_i < render_i < verify_i


# -- explain (E-Y3) -----------------------------------------------------------

def test_explain_renders_preview_target(project):
    mf = _mf(project, "  - { format: preview, out: review/a.wav }\n")
    m = load_manifest(mf)
    text = render_explain(m, compile_manifest(m))
    # E-Y3: the Binaural render is visible (it lives in the step rationale,
    # which explain prints verbatim), alongside the R9/F10 reasoning
    for needle in ("format: preview", "R9", "F10", "verify_preview",
                   "--output_layout=Binaural"):
        assert needle in text, f"explain misses {needle}"
