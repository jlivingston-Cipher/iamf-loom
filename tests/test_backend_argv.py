"""E-L7 (compile side): the WP1 sharp-edge rules are visible in the emitted
argvs, and the F4 arrangement is unrepresentable."""

from __future__ import annotations

from .conftest import compile_text, fake_mp4

V = {"v.mp4": fake_mp4()}


def _step(plan, sid_suffix):
    return next(s for s in plan.steps if s.id.endswith(sid_suffix))


def _oneshot_714(project, route="auto", manifest_order="normal"):
    if manifest_order == "adversarial":
        # Sources/elements/presentations written in the F4-tempting order
        # (center/LFE first); the grammar has no field that can reorder
        # substreams, so this MUST compile to the identical stream order.
        text = (
            "loom: 0\ntitle: A\n"
            "presentations:\n  - { id: main, elements: [ { ref: bed } ] }\n"
            "elements:\n  bed: { from: main }\n"
            "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
            "targets:\n  - { format: mp4, out: x.mp4, video: v.mp4 }\n"
        )
    else:
        text = (
            "loom: 0\ntitle: A\n"
            "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
            "elements:\n  bed: { from: main }\n"
            "presentations:\n  - { id: main, elements: [ { ref: bed } ] }\n"
            "targets:\n  - { format: mp4, out: x.mp4, video: v.mp4 }\n"
        )
    mf = project(text, {"wavs/main.wav": 12}, extra_files=V)
    return compile_text(mf)


F4_ORDER = [
    "[0:a]pan=stereo|c0=c0|c1=c1[frontLR]",
    "[0:a]pan=stereo|c0=c4|c1=c5[sideLR]",
    "[0:a]pan=stereo|c0=c6|c1=c7[backLR]",
    "[0:a]pan=stereo|c0=c8|c1=c9[topfLR]",
    "[0:a]pan=stereo|c0=c10|c1=c11[topbLR]",
    "[0:a]pan=mono|c0=c2[center]",
    "[0:a]pan=mono|c0=c3[lfe]",
]


def test_f4_substream_order_derived(project):
    plan = _oneshot_714(project)
    p1 = _step(plan, "-p1")
    fc = p1.argv[p1.argv.index("-filter_complex") + 1]
    assert fc.split(";") == F4_ORDER


def test_f4_adversarial_manifest_identical_order(project):
    """The F4 arrangement cannot be written: declaration order is irrelevant."""
    normal = _oneshot_714(project, manifest_order="normal")
    advers = _oneshot_714(project, manifest_order="adversarial")
    n1 = _step(normal, "-p1").argv
    a1 = _step(advers, "-p1").argv
    assert n1 == a1


def test_f1_parameter_rate_per_parameter_id(project):
    plan = _oneshot_714(project)
    p2 = _step(plan, "-p2")
    mp = p2.argv[[i for i, a in enumerate(p2.argv)
                  if a == "-stream_group"][1] + 1]
    assert "parameter_id=100:parameter_rate=48000" in mp
    assert "parameter_id=101:parameter_rate=48000" in mp


def test_f6_streamids_with_video(project):
    plan = _oneshot_714(project)
    p2 = _step(plan, "-p2")
    ids = [p2.argv[i + 1] for i, a in enumerate(p2.argv) if a == "-streamid"]
    # 7 audio substreams 0..6 (= substream ids) + video 7
    assert ids == [f"{i}:{i}" for i in range(8)]
    assert "-c:v" in p2.argv and p2.argv[p2.argv.index("-c:v") + 1] == "copy"
    assert "+faststart" in p2.argv


def test_f2_f3_scene_layer_syntax(project):
    mf = project(
        "loom: 0\ntitle: S\n"
        "sources:\n  amb: { path: wavs/amb.wav, kind: ambisonics }\n"
        "elements:\n  a: { from: amb }\n"
        "targets:\n  - { format: mp4, out: x.mp4, video: v.mp4 }\n",
        {"wavs/amb.wav": 4},
        extra_files=V,
    )
    plan = compile_text(mf)
    p1 = _step(plan, "-p1")
    ae = p1.argv[p1.argv.index("-stream_group") + 1]
    # ambisonics_mode INSIDE the layer entry, with explicit `ambisonic N`
    assert "layer=ch_layout=ambisonic 1:ambisonics_mode=mono" in ae
    assert ",ambisonics_mode" not in ae.replace(
        "layer=ch_layout=ambisonic 1:ambisonics_mode=mono", "")


def test_measure_tokens_in_pass2(project):
    plan = _oneshot_714(project)
    p2 = _step(plan, "-p2")
    mp = p2.argv[[i for i, a in enumerate(p2.argv)
                  if a == "-stream_group"][1] + 1]
    assert "${measure:" in mp and ":il}" in mp
    # pass 1 must NOT carry loudness fields (it is never a deliverable)
    p1 = _step(plan, "-p1")
    mp1 = p1.argv[[i for i, a in enumerate(p1.argv)
                   if a == "-stream_group"][1] + 1]
    assert "integrated_loudness" not in mp1
    assert "integrated_loudness" in mp


def test_textproto_no_loudness_fields_and_correct_arithmetic(project):
    mf = project(
        "loom: 0\ntitle: X\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: x.iamf }\n",
        {"wavs/main.wav": 12},
    )
    plan = compile_text(mf)
    cfg = _step(plan, "-cfg")
    tp = cfg.content
    assert "substream_count: 7" in tp and "coupled_substream_count: 5" in tp
    assert "LOUDSPEAKER_LAYOUT_7_1_4_CH" in tp
    assert "CHANNEL_LABEL_LSS_7" in tp  # WAV order labels
    # ADR-5: no typed loudness anywhere; encoder measures natively (G2b)
    assert "integrated_loudness" not in tp
    assert "info_type_bit_masks: []" in tp


def test_textproto_profile_and_gain(project):
    mf = project(
        "loom: 0\ntitle: Y\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"5.1\" }\n"
        "elements:\n  bed: { from: main }\n"
        "presentations:\n"
        "  - { id: quiet, elements: [ { ref: bed, gain_db: -2 } ] }\n"
        "targets:\n"
        "  - { format: mp4, out: y.mp4, video: v.mp4, preset: youtube }\n",
        {"wavs/main.wav": 6},
        extra_files=V,
    )
    plan = compile_text(mf)
    tp = _step(plan, "-cfg").content
    assert "PROFILE_VERSION_BASE" in tp          # youtube floor (G9/G11)
    assert "default_mix_gain: -512" in tp        # -2 dB in Q7.8
    assert "SOUND_SYSTEM_B_0_5_0" in tp          # native 5.1 loudness layout


def test_lpcm_codec_block(project):
    mf = project(
        "loom: 0\ntitle: P\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: lpcm }\n"
        "targets:\n  - { format: iamf, out: p.iamf }\n",
        {"wavs/main.wav": 2},
    )
    plan = compile_text(mf)
    tp = _step(plan, "-cfg").content
    assert "CODEC_ID_LPCM" in tp and "sample_size: 24" in tp
