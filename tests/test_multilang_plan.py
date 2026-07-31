"""R8 (doc 47): multi-language presentation expansion — compile-level tests.

D-Z2 (multi-element grammar + M-415/M-416), D-Z3 (`languages:` expansion),
D-Z4 (emitter allocation), D-Z5 (per-stem staging/riding), D-Z6/D-Z7
(preview mix selection + the binaural requirement following it), D-Z8
(routing blocker). Toolchain-free by construction.
"""

from __future__ import annotations

import re

import pytest

from loom.diagnostics import CompileError
from loom.manifest import load_manifest

from .conftest import compile_text, write_wav

BASE_SOURCES = (
    "sources:\n"
    "  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
    "  vo_en: { path: wavs/vo_en.wav, kind: bed, layout: stereo }\n"
    "  vo_de: { path: wavs/vo_de.wav, kind: bed, layout: stereo }\n"
)
BASE_WAVS = {"wavs/main.wav": 12, "wavs/vo_en.wav": 2, "wavs/vo_de.wav": 2}
BASE_ELEMENTS = (
    "elements:\n"
    "  bed: { from: main }\n"
    "  vo_en: { from: vo_en }\n"
    "  vo_de: { from: vo_de }\n"
)
LANG_BLOCK = (
    "presentations:\n"
    "  - id: \"main-{lang}\"\n"
    "    languages:\n"
    "      - { lang: en-us, vo: vo_en, label: \"English\" }\n"
    "      - { lang: de-de, vo: vo_de, label: \"Deutsch\" }\n"
    "    elements:\n"
    "      - { ref: bed }\n"
    "      - { ref: \"{vo}\" }\n"
)
RAW_TARGET = "targets:\n  - { format: iamf, out: dist/x.iamf }\n"


def _mf(project, body: str, wavs=None, **kw):
    return project("loom: 0\ntitle: T\n" + body, wavs or dict(BASE_WAVS), **kw)


def _codes(excinfo) -> list[str]:
    return excinfo.value.codes()


# ---- D-Z3: expansion semantics ---------------------------------------------

def test_expansion_produces_one_presentation_per_row(project):
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK + RAW_TARGET)
    m = load_manifest(mf)
    assert [pr.id for pr in m.presentations] == ["main-en-us", "main-de-de"]
    # Derived annotations: {lang: label}, computed never typed.
    assert m.presentations[0].annotations == {"en-us": "English"}
    assert m.presentations[1].annotations == {"de-de": "Deutsch"}
    # Row-bound element refs resolved per language.
    assert [pe.ref for pe in m.presentations[0].elements] == ["bed", "vo_en"]
    assert [pe.ref for pe in m.presentations[1].elements] == ["bed", "vo_de"]


def test_expansion_label_defaults_to_expanded_id(project):
    block = (
        "presentations:\n"
        "  - id: \"main-{lang}\"\n"
        "    languages:\n"
        "      - { lang: en-us, vo: vo_en }\n"
        "    elements: [ { ref: bed }, { ref: \"{vo}\" } ]\n"
    )
    m = load_manifest(_mf(project, BASE_SOURCES + BASE_ELEMENTS + block
                          + RAW_TARGET))
    assert m.presentations[0].annotations == {"en-us": "main-en-us"}


def test_row_bindings_shadow_global_vars(project):
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK + RAW_TARGET)
    # A global binding for `vo` must NOT override the row's binding.
    m = load_manifest(mf, variables={"vo": "bed"})
    assert [pe.ref for pe in m.presentations[0].elements] == ["bed", "vo_en"]


def test_global_vars_still_resolve_inside_expanded_blocks(project):
    block = (
        "presentations:\n"
        "  - id: \"main-{lang}\"\n"
        "    languages:\n"
        "      - { lang: en-us, vo: vo_en, label: \"{title_txt} (EN)\" }\n"
        "    elements: [ { ref: bed }, { ref: \"{vo}\" } ]\n"
    )
    m = load_manifest(_mf(project, BASE_SOURCES + BASE_ELEMENTS + block
                          + RAW_TARGET), variables={"title_txt": "Movie"})
    assert m.presentations[0].annotations == {"en-us": "Movie (EN)"}


def test_escapes_resolve_exactly_once(project):
    block = (
        "presentations:\n"
        "  - id: \"main-{lang}\"\n"
        "    languages:\n"
        "      - { lang: en-us, vo: vo_en, label: \"lit {{lang}} brace\" }\n"
        "    elements: [ { ref: bed }, { ref: \"{vo}\" } ]\n"
    )
    m = load_manifest(_mf(project, BASE_SOURCES + BASE_ELEMENTS + block
                          + RAW_TARGET))
    # {{lang}} is an escape owned by the global pass: one literal {lang}.
    assert m.presentations[0].annotations == {"en-us": "lit {lang} brace"}


def test_unbound_ref_in_template_is_m413(project):
    block = (
        "presentations:\n"
        "  - id: \"main-{lang}\"\n"
        "    languages:\n"
        "      - { lang: en-us }\n"
        "    elements: [ { ref: bed }, { ref: \"{vo}\" } ]\n"
    )
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, BASE_SOURCES + BASE_ELEMENTS + block
                          + RAW_TARGET))
    assert "M-413" in _codes(e)


# ---- D-Z3: M-414 negatives --------------------------------------------------

@pytest.mark.parametrize("block,frag", [
    ("    languages: {}\n", "non-empty list"),
    ("    languages: []\n", "non-empty list"),
    ("    languages: [ 42 ]\n", "mapping"),
    ("    languages:\n      - { vo: vo_en }\n", "lang"),
    ("    languages:\n      - { lang: en-us, vo: vo_en }\n"
     "      - { lang: en-us, vo: vo_de }\n", "duplicate"),
    ("    languages:\n      - { lang: en-us, gain: 3 }\n", "string"),
])
def test_m414_block_schema(project, block, frag):
    body = (BASE_SOURCES + BASE_ELEMENTS
            + "presentations:\n  - id: \"main-{lang}\"\n" + block
            + "    elements: [ { ref: bed } ]\n" + RAW_TARGET)
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, body))
    assert "M-414" in _codes(e)
    msg = "; ".join(str(d) for d in e.value.diagnostics)
    assert frag in msg


def test_expanded_id_collision_is_m204(project):
    block = (
        "presentations:\n"
        "  - id: static-id\n"
        "    languages:\n"
        "      - { lang: en-us, vo: vo_en }\n"
        "      - { lang: de-de, vo: vo_de }\n"
        "    elements: [ { ref: bed }, { ref: \"{vo}\" } ]\n"
    )
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, BASE_SOURCES + BASE_ELEMENTS + block
                          + RAW_TARGET))
    assert "M-204" in _codes(e)


def test_empty_elements_is_m201(project):
    body = (BASE_SOURCES + BASE_ELEMENTS
            + "presentations:\n  - { id: main, elements: [] }\n" + RAW_TARGET)
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, body))
    assert "M-201" in _codes(e)


# ---- D-Z2: M-415 / M-416 ----------------------------------------------------

def test_m415_frame_count_mismatch_names_both(project, tmp_path):
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK + RAW_TARGET)
    # Re-write one stem with a different frame count.
    write_wav(mf.parent / "wavs/vo_de.wav", 2, frames=2400)
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert "M-415" in _codes(e)
    msg = "; ".join(str(d) for d in e.value.diagnostics)
    assert "4800" in msg and "2400" in msg  # both sides named


def test_m415_bits_mismatch_under_flac(project):
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK
             + "policy:\n  codec: { name: flac }\n" + RAW_TARGET)
    write_wav(mf.parent / "wavs/vo_de.wav", 2, bits=16)
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert "M-415" in _codes(e)
    msg = "; ".join(str(d) for d in e.value.diagnostics)
    assert "24-bit" in msg and "16-bit" in msg


def test_m416_exceeds_every_profile(project):
    body = (
        "sources:\n"
        "  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "  amb: { path: wavs/amb.wav, kind: ambisonics }\n"
        "  vo: { path: wavs/vo.wav, kind: bed, layout: stereo }\n"
        "elements:\n"
        "  bed: { from: main }\n  amb: { from: amb }\n  vo: { from: vo }\n"
        "presentations:\n"
        "  - id: main\n"
        "    elements: [ { ref: bed }, { ref: amb }, { ref: vo } ]\n"
        + RAW_TARGET
    )
    wavs = {"wavs/main.wav": 12, "wavs/amb.wav": 16, "wavs/vo.wav": 2}
    with pytest.raises(CompileError) as e:
        compile_text(_mf(project, body, wavs))
    assert "M-416" in _codes(e)
    msg = "; ".join(str(d) for d in e.value.diagnostics)
    assert "30 channels" in msg and "base_enhanced 28/28" in msg


# ---- D-Z2: profile arithmetic ----------------------------------------------

def _profile_of(project, pres_block, wavs=None, extra_sources="",
                extra_elements=""):
    body = (BASE_SOURCES + extra_sources + BASE_ELEMENTS + extra_elements
            + pres_block + RAW_TARGET)
    return compile_text(_mf(project, body, wavs)).targets[0].profile


def test_profile_two_elements_is_base(project):
    p = _profile_of(project,
                    "presentations:\n  - id: m\n"
                    "    elements: [ { ref: bed }, { ref: vo_en } ]\n")
    assert p == "base"


def test_profile_28_channels_is_base_enhanced(project):
    extra_s = "  amb: { path: wavs/amb.wav, kind: ambisonics }\n"
    extra_e = "  amb: { from: amb }\n"
    wavs = dict(BASE_WAVS, **{"wavs/amb.wav": 16})
    p = _profile_of(project,
                    "presentations:\n  - id: m\n"
                    "    elements: [ { ref: bed }, { ref: amb } ]\n",
                    wavs, extra_s, extra_e)
    assert p == "base_enhanced"  # 2 elements, 28 ch > base's 18


def test_profile_is_max_over_presentations(project):
    # A simple-profile mix next to a base-profile mix -> base for the file.
    p = _profile_of(project,
                    "presentations:\n"
                    "  - id: solo\n    elements: [ { ref: bed } ]\n"
                    "  - id: duo\n"
                    "    elements: [ { ref: bed }, { ref: vo_en } ]\n")
    assert p == "base"


def test_profile_policy_floor_still_raises(project):
    body = (BASE_SOURCES + BASE_ELEMENTS
            + "presentations:\n  - id: m\n    elements: [ { ref: bed } ]\n"
            + "policy:\n  profile: base_enhanced\n" + RAW_TARGET)
    assert compile_text(_mf(project, body)).targets[0].profile == "base_enhanced"


# ---- D-Z4: emitter allocation ----------------------------------------------

def test_emitter_allocation_multilang(project):
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK + RAW_TARGET)
    plan = compile_text(mf)
    cfg = next(s for s in plan.steps if s.kind == "write_config").content
    subs = re.findall(r"audio_substream_ids: \[([^\]]*)\]", cfg)
    flat = [int(x) for grp in subs for x in grp.split(",") if x.strip()]
    assert flat == list(range(9))          # globally unique, dense (7.1.4=7+2 VOs)
    els = re.findall(r"audio_element_id: (\d+)", cfg)
    assert set(els) == {"300", "301", "302"}
    # One frame block per element, each naming its own staged wav.
    assert cfg.count("audio_frame_metadata {") == 3
    # Running param ids: 3 per presentation (2 elements + output), 2 mixes.
    pids = [int(x) for x in re.findall(r"parameter_id: (\d+)", cfg)]
    assert pids == list(range(1000, 1006))
    # Per-source staging steps.
    stages = [s.id for s in plan.steps if s.kind == "stage_input"]
    assert stages == ["s00-stage-0-main", "s00-stage-1-vo_en",
                      "s00-stage-2-vo_de"]


def test_normalize_rides_every_stem_with_one_gain(project):
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK
             + "policy:\n  loudness: { normalize: -16 }\n" + RAW_TARGET)
    plan = compile_text(mf)
    rides = [s for s in plan.steps if s.kind == "gain_ride"]
    assert len(rides) == 3                  # one per stem
    gain_args = {a for s in rides for a in s.argv if "${gain:" in a}
    assert len(gain_args) == 1              # the SAME anchor-derived gain
    # The encode reads the ridden stems.
    enc = next(s for s in plan.steps if s.kind == "encode"
               and s.tool == "encoder_main" and not s.id.endswith("-pre"))
    assert sum("wavn" in r for r in enc.reads) == 3


# ---- D-Z6/D-Z7: preview selection ------------------------------------------

PREVIEW_LANG_BLOCK = (
    "presentations:\n"
    "  - id: \"dub-{lang}\"\n"
    "    languages:\n"
    "      - { lang: en-us, vo: vo_en }\n"
    "      - { lang: de-de, vo: vo_de }\n"
    "    elements:\n"
    "      - { ref: bed, headphones: binaural }\n"
    "      - { ref: \"{vo}\", headphones: binaural }\n"
)


def test_preview_selection_pins_mix_id(project):
    body = (BASE_SOURCES + BASE_ELEMENTS + PREVIEW_LANG_BLOCK
            + "targets:\n  - { format: preview, out: r/d.wav, "
              "presentation: dub-de-de }\n")
    plan = compile_text(_mf(project, body))
    rend = next(s for s in plan.steps if s.id.endswith("render-binaural"))
    assert "--mix_id=43" in rend.argv       # idx 1 -> 42+1
    assert "dub-de-de" in rend.rationale


def test_preview_default_selection_is_first_presentation(project):
    body = (BASE_SOURCES + BASE_ELEMENTS + PREVIEW_LANG_BLOCK
            + "targets:\n  - { format: preview, out: r/d.wav }\n")
    plan = compile_text(_mf(project, body))
    rend = next(s for s in plan.steps if s.id.endswith("render-binaural"))
    assert "--mix_id=42" in rend.argv


def test_preview_single_presentation_argv_unchanged(project):
    body = (BASE_SOURCES + BASE_ELEMENTS
            + "presentations:\n"
              "  - id: only\n"
              "    elements: [ { ref: bed, headphones: binaural } ]\n"
            + "targets:\n  - { format: preview, out: r/d.wav }\n")
    plan = compile_text(_mf(project, body))
    rend = next(s for s in plan.steps if s.id.endswith("render-binaural"))
    assert not any(a.startswith("--mix_id") for a in rend.argv)  # R9 bytes


def test_unknown_presentation_selection_is_m205(project):
    body = (BASE_SOURCES + BASE_ELEMENTS + PREVIEW_LANG_BLOCK
            + "targets:\n  - { format: preview, out: r/d.wav, "
              "presentation: nope }\n")
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, body))
    assert "M-205" in _codes(e)


def test_selection_on_non_preview_target_is_m402(project):
    body = (BASE_SOURCES + BASE_ELEMENTS + PREVIEW_LANG_BLOCK
            + "targets:\n  - { format: iamf, out: d.iamf, "
              "presentation: dub-en-us }\n")
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, body))
    assert "M-402" in _codes(e)


def test_binaural_requirement_follows_selection(project):
    # Selected presentation lacks binaural; ANOTHER presentation has it.
    # Phase3l's scan-any latitude would pass this; D-Z7 must fail it.
    body = (
        BASE_SOURCES + BASE_ELEMENTS
        + "presentations:\n"
          "  - id: plain\n"
          "    elements: [ { ref: bed } ]\n"
          "  - id: binaural-mix\n"
          "    elements: [ { ref: bed, headphones: binaural } ]\n"
        + "targets:\n  - { format: preview, out: r/d.wav, "
          "presentation: plain }\n"
    )
    with pytest.raises(CompileError) as e:
        load_manifest(_mf(project, body))
    assert "M-402" in _codes(e)
    msg = "; ".join(str(d) for d in e.value.diagnostics)
    assert "plain" in msg                    # names the selected presentation


def test_binaural_on_selected_presentation_passes(project):
    body = (
        BASE_SOURCES + BASE_ELEMENTS
        + "presentations:\n"
          "  - id: plain\n"
          "    elements: [ { ref: bed } ]\n"
          "  - id: binaural-mix\n"
          "    elements: [ { ref: bed, headphones: binaural } ]\n"
        + "targets:\n  - { format: preview, out: r/d.wav, "
          "presentation: binaural-mix }\n"
    )
    plan = compile_text(_mf(project, body))
    rend = next(s for s in plan.steps if s.id.endswith("render-binaural"))
    assert "--mix_id=43" in rend.argv


# ---- D-Z8: routing ----------------------------------------------------------

def test_multi_element_blocks_oneshot(project):
    body = (BASE_SOURCES + BASE_ELEMENTS
            + "presentations:\n  - id: m\n"
              "    elements: [ { ref: bed }, { ref: vo_en } ]\n"
            + "targets:\n  - { format: mp4, out: d.mp4, video: v.mp4 }\n")
    from .conftest import fake_mp4
    mf = _mf(project, body, extra_files={"v.mp4": fake_mp4(0.1)})
    plan = compile_text(mf)
    t = plan.targets[0]
    assert t.backend == "iamftools" and t.muxer == "mp4box"
    assert "3 elements declared" in t.rationale


def test_multilang_explain_renders_generically(project):
    from loom.explain import render_explain
    mf = _mf(project, BASE_SOURCES + BASE_ELEMENTS + LANG_BLOCK + RAW_TARGET)
    m = load_manifest(mf)
    from loom.compiler import compile_manifest
    plan = compile_manifest(m)
    text = render_explain(m, plan)
    assert text == render_explain(m, plan)   # render-twice identical
    assert "base" in text                    # derived profile visible
