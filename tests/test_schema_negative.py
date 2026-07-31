"""E-L3: every statically checkable error fails at compile time with its own
stable M- code, naming both sides where applicable. Zero tracebacks."""

from __future__ import annotations

import pytest

from loom.diagnostics import CompileError
from loom.manifest import load_manifest

from .conftest import compile_text, fake_mp4


def _codes(exc: pytest.ExceptionInfo) -> list[str]:
    return exc.value.codes()


def test_m303_layout_channel_mismatch(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 10},  # 7.1.4 needs 12
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-303" in _codes(e)
    msg = str(e.value)
    assert "7.1.4" in msg and "12" in msg and "10" in msg  # names both sides


def test_m304_ambisonics_not_square(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  amb: { path: wavs/amb.wav, kind: ambisonics }\n"
        "elements:\n  a: { from: amb }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/amb.wav": 5},  # not (N+1)^2
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-304" in _codes(e)


def test_m205_unknown_reference(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: nosuch }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-205" in _codes(e)


def test_m204_duplicate_presentation_id(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "presentations:\n"
        "  - { id: main, elements: [ { ref: bed } ] }\n"
        "  - { id: main, elements: [ { ref: bed } ] }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-204" in _codes(e)


# -- Phase 2 (R3): normalize is now legal; its NEW negative space -----------
# (replaces the Phase-1 test_m401_normalize_is_phase2, a pre-declared
# test-baseline change — doc 43 D-P2)

def test_normalize_accepted_and_carried(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  loudness: { mode: measure, normalize: -16 }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    m = load_manifest(mf)
    assert m.policy.normalize == -16.0


def test_m409_normalize_out_of_range(project):
    for bad in (-3, -40, 0):
        mf = project(
            "loom: 0\ntitle: T\n"
            "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
            "elements:\n  bed: { from: main }\n"
            f"policy:\n  loudness: {{ normalize: {bad} }}\n"
            "targets:\n  - { format: iamf, out: x.iamf }\n",
            {"wavs/main.wav": 2},
        )
        with pytest.raises(CompileError) as e:
            load_manifest(mf)
        assert "M-409" in _codes(e), f"normalize: {bad}"


def test_m202_normalize_wrong_type(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  loudness: { normalize: loud }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-202" in _codes(e)


def test_m402_normalize_lpcm_conflict(project):
    """The PRD edge: lpcm passthrough must stay bit-transparent."""
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: lpcm }\n  loudness: { normalize: -16 }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    codes = _codes(e)
    assert "M-402" in codes
    assert "lpcm" in str(e.value)


def test_m403_oneshot_route_conflict_lpcm(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: lpcm }\n"
        "targets:\n"
        "  - { format: mp4, out: x.mp4, video: v.mp4, route: oneshot }\n",
        {"wavs/main.wav": 2},
        extra_files={"v.mp4": fake_mp4()},
    )
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert "M-403" in _codes(e)
    assert "Opus" in str(e.value)  # names the ADR-1 rule


def test_m404_youtube_needs_video(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: mp4, out: x.mp4, preset: youtube }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-404" in _codes(e)


def test_m301_missing_source_file(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/nope.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-301" in _codes(e)


def test_m305_unknown_layout(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"9.1.6\" }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 16},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-305" in _codes(e)


def test_m206_unsafe_overrides_reserved(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "unsafe_overrides: { integrated_loudness: -14 }\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-206" in _codes(e)


# --- beyond the pre-registered ten -----------------------------------------

def test_m309_adm_gated_on_r7(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  m: { path: wavs/m.wav, kind: adm }\n"
        "elements:\n  a: { from: m }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/m.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-309" in _codes(e)


# -- R8: multi-element mixes are now legal (replaces
# test_m405_multi_element_is_phase2, a pre-declared test-baseline change —
# doc 47 D-Z2; the doc-43 M-401/M-406 retire-in-place pattern) ---------------
def test_m405_retired_multi_element_compiles(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n"
        "  a: { path: wavs/a.wav, kind: bed, layout: stereo }\n"
        "  b: { path: wavs/b.wav, kind: bed, layout: stereo }\n"
        "elements:\n  ea: { from: a }\n  eb: { from: b }\n"
        "presentations:\n"
        "  - { id: main, elements: [ { ref: ea }, { ref: eb } ] }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/a.wav": 2, "wavs/b.wav": 2},
    )
    plan = compile_text(mf)
    # 2 elements / 4 channels in one mix -> base profile (per-mix caps).
    assert plan.targets[0].profile == "base"
    from loom.diagnostics import CODES
    assert "resolved in R8" in CODES["M-405"]  # retired in place, kept


# -- Phase 2 (R4): flac is now legal (replaces test_m406_flac_is_phase2,
# a pre-declared test-baseline change — doc 43 D-P3) ------------------------

def test_flac_compiles_and_routes_iamftools(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: flac }\n"
        "targets:\n  - { format: iamf, out: x.iamf, preset: archive }\n",
        {"wavs/main.wav": 2},
    )
    plan = compile_text(mf)
    assert plan.targets[0].backend == "iamftools"
    cfg = next(s for s in plan.steps if s.kind == "write_config")
    assert "CODEC_ID_FLAC" in cfg.content
    assert "compression_level: 8" in cfg.content
    # the -1 STREAMINFO quirk (24-bit fixture WAV -> 23)
    assert "bits_per_sample: 23" in cfg.content


def test_m402_archive_preset_needs_flac(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf, preset: archive }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-402" in _codes(e)


def test_m402_archive_preset_iamf_only(project):
    mf = project(
        "loom: 0\ntitle: T\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: flac }\n"
        "targets:\n  - { format: mp4, out: x.mp4, preset: archive }\n",
        {"wavs/main.wav": 2},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-402" in _codes(e)


def test_m308_wrong_sample_rate(project, tmp_path):
    from .conftest import write_wav

    write_wav(tmp_path / "wavs/m.wav", 2, rate=44100)
    mf = tmp_path / "manifest.yaml"
    mf.write_text(
        "loom: 0\ntitle: T\n"
        "sources:\n  m: { path: wavs/m.wav, kind: bed, layout: stereo }\n"
        "elements:\n  a: { from: m }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n"
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-308" in _codes(e)


def test_diagnostics_accumulate(project):
    """One compile reports as many independent errors as possible."""
    mf = project(
        "loom: 0\ntitle: T\n"
        "unsafe_overrides: {}\n"
        "sources:\n  m: { path: wavs/nope.wav, kind: bed, layout: stereo }\n"
        "elements:\n  a: { from: ghost }\n"
        "policy:\n  loudness: { normalize: -99 }\n"
        "targets:\n  - { format: mp4, out: x.mp4, preset: youtube }\n",
        {},
    )
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    codes = set(_codes(e))
    assert {"M-206", "M-301", "M-205", "M-409", "M-404"} <= codes


# -- Error-path table (doc 76): one row per statically checkable branch ------
# Every row was verified against the live validator; the expected code must
# appear in the CompileError (co-fired codes are allowed — a bad section often
# also breaks a reference).

_HDR = "loom: 0\ntitle: T\n"
_SRC = "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
_EL = "elements:\n  bed: { from: main }\n"
_TG = "targets:\n  - { format: iamf, out: x.iamf }\n"


@pytest.mark.parametrize("name,text,code", [
    ("identifier_m207",
     _HDR + 'sources:\n  "my src": { path: wavs/main.wav, kind: bed, layout: stereo }\n'
     'elements:\n  bed: { from: "my src" }\n' + _TG, "M-207"),
    ("missing_title_m201", "loom: 0\n" + _SRC + _EL + _TG, "M-201"),
    ("sources_not_mapping_m202", _HDR + "sources: []\n" + _EL + _TG, "M-202"),
    ("source_scalar_m202", _HDR + "sources:\n  main: 3\n" + _EL + _TG, "M-202"),
    ("element_scalar_m202", _HDR + _SRC + "elements:\n  bed: 3\n" + _TG, "M-202"),
    ("unknown_source_kind_m203",
     _HDR + "sources:\n  main: { path: wavs/main.wav, kind: blob }\n" + _EL + _TG,
     "M-203"),
    ("bed_missing_layout_m306",
     _HDR + "sources:\n  main: { path: wavs/main.wav, kind: bed }\n" + _EL + _TG,
     "M-306"),
    ("bitrate_garbage_m202",
     _HDR + _SRC + _EL + "policy:\n  codec: { bitrate_coupled: high }\n" + _TG,
     "M-202"),
    ("presentations_scalar_m202", _HDR + _SRC + _EL + "presentations: 3\n" + _TG,
     "M-202"),
    ("presentation_entry_scalar_m202",
     _HDR + _SRC + _EL + "presentations:\n  - 3\n" + _TG, "M-202"),
    ("presentation_annotations_m202",
     _HDR + _SRC + _EL +
     "presentations:\n  - { id: p, elements: [ { ref: bed } ], annotations: 3 }\n"
     + _TG, "M-202"),
    ("presentation_element_scalar_m202",
     _HDR + _SRC + _EL + "presentations:\n  - { id: p, elements: [ 3 ] }\n" + _TG,
     "M-202"),
    ("presentation_unknown_ref_m205",
     _HDR + _SRC + _EL +
     "presentations:\n  - { id: p, elements: [ { ref: nosuch } ] }\n" + _TG,
     "M-205"),
    ("gain_db_type_m202",
     _HDR + _SRC + _EL +
     "presentations:\n  - { id: p, elements: [ { ref: bed, gain_db: hot } ] }\n"
     + _TG, "M-202"),
    ("gain_db_range_m408",
     _HDR + _SRC + _EL +
     "presentations:\n  - { id: p, elements: [ { ref: bed, gain_db: 99 } ] }\n"
     + _TG, "M-408"),
    ("headphones_vocab_m203",
     _HDR + _SRC + _EL +
     "presentations:\n  - { id: p, elements: [ { ref: bed, headphones: surround } ] }\n"
     + _TG, "M-203"),
    ("loudness_layouts_scalar_m202",
     _HDR + _SRC + _EL +
     "presentations:\n  - { id: p, elements: [ { ref: bed } ], loudness_layouts: 3 }\n"
     + _TG, "M-202"),
    ("loudness_layout_unknown_m407",
     _HDR + _SRC + _EL +
     'presentations:\n  - { id: p, elements: [ { ref: bed } ], loudness_layouts: [ "22.2" ] }\n'
     + _TG, "M-407"),
    ("policy_scalar_m202", _HDR + _SRC + _EL + "policy: 3\n" + _TG, "M-202"),
    ("codec_scalar_m202", _HDR + _SRC + _EL + "policy:\n  codec: 3\n" + _TG, "M-202"),
    ("loudness_scalar_m202", _HDR + _SRC + _EL + "policy:\n  loudness: 3\n" + _TG,
     "M-202"),
    ("unknown_codec_m203",
     _HDR + _SRC + _EL + "policy:\n  codec: { name: mp3 }\n" + _TG, "M-203"),
    ("unknown_loudness_mode_m203",
     _HDR + _SRC + _EL + "policy:\n  loudness: { mode: guess }\n" + _TG, "M-203"),
    ("unknown_profile_m203",
     _HDR + _SRC + _EL + "policy:\n  profile: mega\n" + _TG, "M-203"),
    ("unknown_validate_m203",
     _HDR + _SRC + _EL + "policy:\n  validate: whatever\n" + _TG, "M-203"),
    ("target_scalar_m202", _HDR + _SRC + _EL + "targets:\n  - 3\n", "M-202"),
    ("unknown_format_m203",
     _HDR + _SRC + _EL + "targets:\n  - { format: wav, out: x.wav }\n", "M-203"),
    ("duplicate_out_m204",
     _HDR + _SRC + _EL + "targets:\n  - { format: iamf, out: x.iamf }\n"
     "  - { format: iamf, out: x.iamf }\n", "M-204"),
    ("unknown_preset_m203",
     _HDR + _SRC + _EL + "targets:\n  - { format: mp4, out: x.mp4, preset: vimeo }\n",
     "M-203"),
    ("unknown_route_m203",
     _HDR + _SRC + _EL + "targets:\n  - { format: mp4, out: x.mp4, route: teleport }\n",
     "M-203"),
    ("presentation_selector_type_m202",
     _HDR + _SRC + _EL + "targets:\n  - { format: iamf, out: x.iamf, presentation: 3 }\n",
     "M-202"),
    ("video_type_m202",
     _HDR + _SRC + _EL + "targets:\n  - { format: mp4, out: x.mp4, video: 3 }\n",
     "M-202"),
    ("video_missing_m301",
     _HDR + _SRC + _EL + "targets:\n  - { format: mp4, out: x.mp4, video: novideo.mp4 }\n",
     "M-301"),
    ("youtube_preset_on_iamf_m404",
     _HDR + _SRC + _EL + "targets:\n  - { format: iamf, out: x.iamf, preset: youtube }\n",
     "M-404"),
])
def test_error_path_table(project, name, text, code):
    mf = project(text, {"wavs/main.wav": 2})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert code in _codes(e), (name, _codes(e))


def test_top_level_scalar_m103(project):
    mf = project("3\n", {})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-103" in _codes(e)


def test_wrong_loom_version_m103(project):
    mf = project("loom: 9\ntitle: T\n" + _SRC + _EL + _TG, {"wavs/main.wav": 2})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-103" in _codes(e)


def test_manifest_missing_file_m101(tmp_path):
    with pytest.raises(CompileError) as e:
        load_manifest(tmp_path / "nope.yaml")
    assert "M-101" in _codes(e)


def test_manifest_bad_json_m102(tmp_path):
    mf = tmp_path / "manifest.json"
    mf.write_text("{not json")
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-102" in _codes(e)


def test_unreadable_wav_m302(project):
    mf = project(_HDR + _SRC + _EL + _TG, {},
                 extra_files={"wavs/main.wav": b"garbage, not RIFF"})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-302" in _codes(e)


def test_json_manifest_happy_path_and_bitrate_forms(project):
    import json as _json
    doc = {"loom": 0, "title": "T",
           "sources": {"main": {"path": "wavs/main.wav", "kind": "bed",
                                "layout": "stereo"}},
           "elements": {"bed": {"from": "main"}},
           "policy": {"codec": {"bitrate_coupled": "128k",
                                "bitrate_uncoupled": 64000}},
           "targets": [{"format": "iamf", "out": "x.iamf"}]}
    mf = project(_json.dumps(doc), {"wavs/main.wav": 2}, name="manifest.json")
    m = load_manifest(mf)
    assert m.title == "T"
    assert m.policy.codec.bitrate_coupled == 128_000     # "128k" form
    assert m.policy.codec.bitrate_uncoupled == 64_000    # int passthrough
