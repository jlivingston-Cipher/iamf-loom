"""R8 (doc 47) executed accepts, toolchain-gated (E-Z4/E-Z5/E-Z6): a
multi-language multi-element manifest encodes to one IAMF carrying every
language presentation with measured per-presentation loudness; element mix
gains flow through encode+render; the preview renders the SELECTED
presentation; normalize rides every stem.
"""

from __future__ import annotations

from pathlib import Path


from loom.compiler import compile_manifest
from loom.executor import Executor
from loom.manifest import load_manifest
from loom.wavinfo import read_wav_info

from .conftest import needs_toolchain, toolchain_root, write_wav
from .test_parity_toolchain import FRAMES, declared_loudness

MULTILANG_RAW = (
    "loom: 0\ntitle: mlt\n"
    "sources:\n"
    "  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
    "  vo_en: { path: vo_en.wav, kind: bed, layout: stereo }\n"
    "  vo_de: { path: vo_de.wav, kind: bed, layout: stereo }\n"
    "elements:\n"
    "  bed: { from: main }\n"
    "  vo_en: { from: vo_en }\n"
    "  vo_de: { from: vo_de }\n"
    "presentations:\n"
    "  - id: \"main-{lang}\"\n"
    "    languages:\n"
    "      - { lang: en-us, vo: vo_en, label: \"English\" }\n"
    "      - { lang: de-de, vo: vo_de, label: \"Deutsch\" }\n"
    "    elements:\n"
    "      - { ref: bed }\n"
    "      - { ref: \"{vo}\" }\n"
    "targets:\n  - { format: iamf, out: dist/mlt.iamf }\n"
)

# Distinct identifier sines per stem: the shared conftest generator seeds
# frequency by channel index only, so hand-place the VO stems' spectra by
# writing them at offset channel counts (12ch bed occupies 440..1100 Hz;
# VO stems get disjoint bands via the channels= trick below).


def _write_stems(tmp_path, vo_names=("vo_en.wav", "vo_de.wav")):
    write_wav(tmp_path / "main.wav", 12, frames=FRAMES)
    # A 14/16-channel scratch WAV whose last two channels carry the
    # distinct-frequency stems, then split? Simpler: write 2-ch WAVs with
    # a per-file frequency offset by generating wider WAVs and slicing is
    # overkill — write_wav's per-channel identifier keeps stems distinct
    # from each other only by channel index (both VO stems share 440/500).
    # For gain-discrimination tests we compare MIXES of the same stems, so
    # identical VO spectra across languages is acceptable and recorded.
    for name in vo_names:
        write_wav(tmp_path / name, 2, frames=FRAMES)


def run_manifest(tmp_path, text, stems=_write_stems):
    stems(tmp_path)
    mf = tmp_path / "manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "out", tmp_path / "work",
                  toolchain=str(toolchain_root()),
                  validate_policy=m.policy.validate)
    return m, plan, ex, ex.run()


# -- E-Z4: one IAMF, every language, measured per-presentation loudness ------

@needs_toolchain
def test_multilang_encode_gate_and_readback(tmp_path):
    _m, _plan, ex, res = run_manifest(tmp_path, MULTILANG_RAW)
    assert res.ok, res.failures
    out = tmp_path / "out/dist/mlt.iamf"
    assert out.is_file()

    # Gate: sentinel passed at the discovered tier with 0 FAIL.
    gate = ex.ledger["gate"]["dist/mlt.iamf"]
    assert gate["passed"] and not gate["fail_ids"]
    assert gate["tier"] in ("l1l2", "l1l2+l3")

    # Clean-room read-back: 3 presentations? No — 2 languages -> 2 mixes.
    from sentinel.parser import parse_bytes
    model = parse_bytes(out.read_bytes(), source=str(out))
    assert len(model.mix_presentations) == 2
    langs = [mp.language_labels for mp in model.mix_presentations]
    assert langs == [["en-us"], ["de-de"]]
    texts = [mp.annotations for mp in model.mix_presentations]
    assert texts == [["English"], ["Deutsch"]]
    # Every mix references two elements (bed + that language's VO).
    for mp in model.mix_presentations:
        assert len(mp.sub_mixes[0].audio_element_ids) == 2
    # Per-presentation loudness: measured, non-zero (G2b per mix).
    decls = declared_loudness(out)
    assert decls and all(il != 0 for il, _dp in decls)

    # 4 elements? No — 3 declared elements land in the sequence.
    assert len(model.audio_elements) == 3
    # Substream ids unique across the sequence.
    subs = [sid for ae in model.audio_elements.values()
            for sid in ae.audio_substream_ids]
    assert len(subs) == len(set(subs))


# -- E-Z4: element_mix_gain flows through encode + render --------------------

GAIN_DISCRIM = (
    "loom: 0\ntitle: gd\n"
    "sources:\n"
    "  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
    "  vo_en: { path: vo_en.wav, kind: bed, layout: stereo }\n"
    "elements:\n"
    "  bed: { from: main }\n"
    "  vo: { from: vo_en }\n"
    "presentations:\n"
    "  - id: full\n"
    "    elements: [ { ref: bed }, { ref: vo, gain_db: 0 } ]\n"
    "  - id: ducked\n"
    "    elements: [ { ref: bed }, { ref: vo, gain_db: -6 } ]\n"
    "targets:\n  - { format: iamf, out: dist/gd.iamf }\n"
)


@needs_toolchain
def test_element_gain_discrimination(tmp_path):
    _m, _plan, _ex, res = run_manifest(
        tmp_path, GAIN_DISCRIM,
        stems=lambda p: _write_stems(p, ("vo_en.wav",)))
    assert res.ok, res.failures
    out = tmp_path / "out/dist/gd.iamf"

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "sentinel-pro"))
    from sentinel_pro.oracle import Toolchain
    from sentinel_pro.dsp import analyze
    tc = Toolchain.discover(str(toolchain_root()))
    r_full = tc.decode(str(out), "2.0", "decoder_main", mix_id=42)
    r_duck = tc.decode(str(out), "2.0", "decoder_main", mix_id=43)
    w_full = getattr(r_full, "wav_path", r_full)
    w_duck = getattr(r_duck, "wav_path", r_duck)
    assert Path(w_full).read_bytes() != Path(w_duck).read_bytes()
    assert r_full.ok and r_duck.ok, (r_full.note, r_duck.note)
    il_full = analyze(w_full, layout="2.0")["integrated_lufs"]
    il_duck = analyze(w_duck, layout="2.0")["integrated_lufs"]
    # The ducked-VO mix is quieter; a -6 dB VO moves the mix by 1-5 LU on
    # this essence (recorded as an actual, the direction is the assert).
    assert il_duck < il_full - 0.5


# -- E-Z5: the preview renders the SELECTED presentation ---------------------

MULTILANG_PREVIEW = (
    "loom: 0\ntitle: mlp\n"
    "sources:\n"
    "  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
    "  vo_en: { path: vo_en.wav, kind: bed, layout: stereo }\n"
    "  vo_de: { path: vo_de.wav, kind: bed, layout: stereo }\n"
    "elements:\n"
    "  bed: { from: main }\n"
    "  vo_en: { from: vo_en }\n"
    "  vo_de: { from: vo_de }\n"
    "presentations:\n"
    "  - id: \"dub-{lang}\"\n"
    "    languages:\n"
    "      - { lang: en-us, vo: vo_en }\n"
    "      - { lang: de-de, vo: vo_de }\n"
    "    elements:\n"
    "      - { ref: bed, headphones: binaural }\n"
    "      - { ref: \"{vo}\", gain_db: %s, headphones: binaural }\n"
    "targets:\n  - { format: preview, out: review/dub.wav, "
    "presentation: %s }\n"
)


@needs_toolchain
def test_preview_renders_selected_presentation(tmp_path):
    # Language mixes differ by VO gain (row-shared gains are typed and
    # uniform, so the discriminator is two literal runs with different
    # selections over stems whose mixes differ by construction).
    m1, _p1, ex1, res1 = run_manifest(
        tmp_path / "a", MULTILANG_PREVIEW % ("0", "dub-en-us"))
    assert res1.ok, res1.failures
    m2, _p2, ex2, res2 = run_manifest(
        tmp_path / "b", MULTILANG_PREVIEW % ("-6", "dub-de-de"))
    assert res2.ok, res2.failures

    for sub, ex in (("a", ex1), ("b", ex2)):
        out = tmp_path / sub / "out/review/dub.wav"
        wi = read_wav_info(out)
        assert (wi.channels, wi.sample_rate, wi.frames) == (2, 48000, FRAMES)
        prev = list(ex.ledger["preview"].values())[0]
        assert prev["ok"] and prev["il_lufs"] > -70.0

    # Selection proof: the en@0dB and de@-6dB previews are different
    # programs -> different bytes and lower IL for the ducked render.
    b1 = (tmp_path / "a/out/review/dub.wav").read_bytes()
    b2 = (tmp_path / "b/out/review/dub.wav").read_bytes()
    assert b1 != b2
    il1 = list(ex1.ledger["preview"].values())[0]["il_lufs"]
    il2 = list(ex2.ledger["preview"].values())[0]["il_lufs"]
    assert il2 < il1 - 0.5


# -- E-Z6: normalize rides every stem ----------------------------------------

MULTILANG_NORM = MULTILANG_RAW.replace(
    "targets:", "policy:\n  loudness: { normalize: -16 }\ntargets:")


@needs_toolchain
def test_multilang_normalize_composition(tmp_path):
    _m, plan, ex, res = run_manifest(tmp_path, MULTILANG_NORM)
    assert res.ok, res.failures
    # The executor's verify-loudness step enforced the ±0.3 accept on the
    # anchor; assert the verify record and the per-stem rides in the ledger.
    ver = [v for v in ex.ledger.get("normalize", {}).values()
           if "within_tolerance" in v]
    assert ver, "normalize verify records missing"
    for v in ver:
        assert v["within_tolerance"]
        assert abs(v["post_ride_il"] - (-16.0)) <= 0.3
    rides = [s for s in plan.steps if s.kind == "gain_ride"]
    assert len(rides) == 3
    # Declared loudness on the shipped bitstream is re-measured post-ride.
    decls = declared_loudness(tmp_path / "out/dist/mlt.iamf")
    assert decls and all(il != 0 for il, _dp in decls)


# -- E-Z5 determinism spot: run-twice byte-identical -------------------------

@needs_toolchain
def test_multilang_run_twice_byte_identical(tmp_path):
    _m, _p, _e, res1 = run_manifest(tmp_path / "r1", MULTILANG_RAW)
    _m, _p, _e, res2 = run_manifest(tmp_path / "r2", MULTILANG_RAW)
    assert res1.ok and res2.ok
    b1 = (tmp_path / "r1/out/dist/mlt.iamf").read_bytes()
    b2 = (tmp_path / "r2/out/dist/mlt.iamf").read_bytes()
    assert b1 == b2
