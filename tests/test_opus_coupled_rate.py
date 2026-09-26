"""The stereo-pair rate: `policy.codec.bitrate_coupled` reaches the iamf-tools
encoder, and a rate that encoder would refuse is refused at compile (M-417).

The pinned encoder resolves each Opus substream's rate (iamf-tools
proto_conversion/codec_config_utils.cc, GetSanitizedBitrate) from a
per-substream override if one names it, else from `target_bitrate_per_channel`
— once for a one-channel substream, and times 2 x `coupling_rate_adjustment`
(default 1.0) for a two-channel one. Loom's iamf-tools backend used to write
the per-channel rate alone, so every stereo pair was coded at twice
`bitrate_uncoupled` whatever `bitrate_coupled` said — while the FFmpeg backend
honoured it and `loom explain` printed it "as resolved". Now each pair whose
rate differs from 2 x uncoupled gets an override naming bitrate_coupled.
"""

from __future__ import annotations

import random
import struct
from pathlib import Path

import pytest

from loom.backends import iamftools as it
from loom.diagnostics import CODES, CompileError
from loom.layouts import BEDS

from .conftest import compile_text, fake_mp4, needs_toolchain, toolchain_root

V = {"v.mp4": fake_mp4()}
CH = {"stereo": 2, "5.1": 6, "7.1.4": 12}


def _step(plan, sid_suffix):
    return next(s for s in plan.steps if s.id.endswith(sid_suffix))


def _manifest(layout="5.1", kind="bed", codec="", targets=None, binaural=False):
    src = (f'  main: {{ path: wavs/main.wav, kind: bed, layout: "{layout}" }}\n'
           if kind == "bed" else
           "  main: { path: wavs/main.wav, kind: ambisonics }\n")
    return ("loom: 0\ntitle: R\n"
            "sources:\n" + src +
            "elements:\n  bed: { from: main }\n"
            + ("presentations:\n  - { id: main, elements: "
               "[ { ref: bed, headphones: binaural } ] }\n" if binaural else "")
            + (f"policy:\n  codec: {{ {codec} }}\n" if codec else "")
            + "targets:\n"
            + "".join(f"  - {t}\n" for t in
                      (targets or ["{ format: iamf, out: dist/r.iamf }"])))


def _cfg(project, codec, layout="5.1"):
    mf = project(_manifest(layout, codec=codec), {"wavs/main.wav": CH[layout]})
    return _step(compile_text(mf), "-cfg").content


def _overrides(tp: str) -> dict[int, int]:
    out = {}
    for line in tp.splitlines():
        line = line.strip()
        if line.startswith("substream_id_to_bitrate_override"):
            body = line.split("{", 1)[1].rsplit("}", 1)[0].split()
            kv = dict(zip(body[0::2], body[1::2]))
            out[int(kv["key:"])] = int(kv["value:"])
    return out


def _pairs(layout):
    return [i for i, s in enumerate(BEDS[layout].substreams) if s.coupled]


def _codes(e):
    return e.value.codes()


# ---- the pairs get the rate the manifest wrote -----------------------------------

def test_each_pair_is_overridden_to_the_coupled_rate(project):
    """96k pairs beside 64k singles. Before the rule the block carried the
    per-channel rate alone and the pairs were coded at 128k."""
    tp = _cfg(project, "bitrate_coupled: 96k, bitrate_uncoupled: 64k")
    assert "target_bitrate_per_channel: 64000" in tp
    assert _overrides(tp) == {0: 96000, 1: 96000}        # 5.1: L/R, Ls/Rs (F4: pairs first)
    assert "coupling_rate_adjustment" not in tp


@pytest.mark.parametrize("layout", ["stereo", "7.1.4"])
def test_every_bed_overrides_exactly_its_pairs(project, layout):
    tp = _cfg(project, "bitrate_coupled: 80k, bitrate_uncoupled: 64k", layout)
    assert _overrides(tp) == {i: 80000 for i in _pairs(layout)}
    assert _pairs(layout)


@pytest.mark.parametrize("codec,want", [
    # outside the 1x..2x a coupling factor could reach: both are coded exactly
    ("bitrate_coupled: 160k, bitrate_uncoupled: 64k", 160000),
    ("bitrate_coupled: 48k, bitrate_uncoupled: 64k", 48000),
    # only the per-channel rate set: the pairs keep the stated default 128k,
    # as the FFmpeg backend and `loom explain` have always said
    ("bitrate_uncoupled: 48k", 128000),
    ("bitrate_uncoupled: 160k", 128000),
])
def test_any_pair_rate_the_encoder_accepts_compiles(project, codec, want):
    tp = _cfg(project, codec)
    assert _overrides(tp) == {0: want, 1: want}


def test_default_rates_write_no_override(project):
    """At 2:1 the per-channel rate already gives the pairs their rate: nothing
    is written, so every plan at the default rates stays byte-identical."""
    assert _overrides(_cfg(project, "")) == {}
    tp = _cfg(project, "bitrate_coupled: 100k, bitrate_uncoupled: 50k")
    assert _overrides(tp) == {} and "target_bitrate_per_channel: 50000" in tp


def test_ambisonics_has_no_pair_to_override(project):
    mf = project(_manifest(kind="ambisonics",
                           codec="bitrate_coupled: 300k, bitrate_uncoupled: 64k"),
                 {"wavs/main.wav": 4})
    assert _overrides(_step(compile_text(mf), "-cfg").content) == {}


def test_a_pair_in_a_later_element_is_found_by_its_emitted_id(project):
    """One codec config serves every element: a stereo VO after a first-order
    scene (substreams 0..3) is substream 4, and it is the pair."""
    mf = project(
        "loom: 0\ntitle: M\n"
        "sources:\n  amb: { path: wavs/amb.wav, kind: ambisonics }\n"
        "  vo: { path: wavs/vo.wav, kind: bed, layout: stereo }\n"
        "elements:\n  scene: { from: amb }\n  voice: { from: vo }\n"
        "presentations:\n  - { id: main, elements: [ { ref: scene }, { ref: voice } ] }\n"
        "policy:\n  codec: { bitrate_coupled: 96k, bitrate_uncoupled: 64k }\n"
        "targets:\n  - { format: iamf, out: dist/m.iamf }\n",
        {"wavs/amb.wav": 4, "wavs/vo.wav": 2})
    tp = _step(compile_text(mf), "-cfg").content
    assert "audio_substream_ids: [4]" in tp
    assert _overrides(tp) == {4: 96000}


# ---- a rate the encoder would refuse is refused at compile (M-417) ---------------

@pytest.mark.parametrize("codec,path", [
    ("bitrate_coupled: 600k, bitrate_uncoupled: 64k", "policy.codec.bitrate_coupled"),
    ("bitrate_coupled: 5k, bitrate_uncoupled: 64k", "policy.codec.bitrate_coupled"),
    ("bitrate_uncoupled: 3k", "policy.codec.bitrate_uncoupled"),
    ("bitrate_coupled: 1200k, bitrate_uncoupled: 600k", "policy.codec.bitrate_uncoupled"),
])
def test_a_rate_the_encoder_refuses_is_refused(project, codec, path):
    mf = project(_manifest(codec=codec), {"wavs/main.wav": 6})
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert _codes(e) == ["M-417"]
    d = e.value.diagnostics[0]
    assert d.path == path and "6000 .. 512000 bps" in d.message and "targets[0]" in d.message


@pytest.mark.parametrize("layout,codec,want", [
    # the encoder's bounds are inclusive, both ends, for a pair and a single
    ("5.1", "bitrate_coupled: 512000, bitrate_uncoupled: 6000", {0: 512000, 1: 512000}),
    ("5.1", "bitrate_coupled: 6000, bitrate_uncoupled: 512000", {0: 6000, 1: 6000}),
    # 2:1 above the per-substream cap: no override, pairs coded at 2 x 300k
    ("5.1", "bitrate_coupled: 600k, bitrate_uncoupled: 300k", {}),
    # libopus's max (-1) passes through the encoder unchecked
    ("5.1", "bitrate_uncoupled: -1", {0: 128000, 1: 128000}),
])
def test_what_the_encoder_accepts_compiles(project, layout, codec, want):
    assert _overrides(_cfg(project, codec, layout)) == want


def test_a_stereo_pair_on_the_per_channel_rate_is_checked(project):
    """No override (2:1), no one-channel substream: the pair's base is still
    bitrate_uncoupled, and the encoder refuses 3k for it."""
    mf = project(_manifest("stereo", codec="bitrate_coupled: 6k, bitrate_uncoupled: 3k"),
                 {"wavs/main.wav": 2})
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert _codes(e) == ["M-417"]
    assert e.value.diagnostics[0].path == "policy.codec.bitrate_uncoupled"


def test_a_rate_that_does_not_fit_the_field_is_refused_even_unused(project):
    """A stereo bed with its pair overridden never uses the per-channel rate,
    but it is still written, into an int32: 5e9 makes a config the encoder
    cannot parse."""
    mf = project(_manifest("stereo", codec="bitrate_coupled: 96k, bitrate_uncoupled: 5000000000"),
                 {"wavs/main.wav": 2})
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert _codes(e) == ["M-417"] and "32-bit" in e.value.diagnostics[0].message


def test_only_rates_that_reach_the_encoder_are_checked(project):
    """A stereo bed has no one-channel substream and its pair is overridden, so
    the per-channel rate is never used: the encoder accepts it, and so does
    Loom. libopus's -1000 (auto) passes through the encoder unchecked."""
    mf = project(_manifest("stereo", codec="bitrate_coupled: 96k, bitrate_uncoupled: 3k"),
                 {"wavs/main.wav": 2})
    assert _overrides(_step(compile_text(mf), "-cfg").content) == {0: 96000}
    tp = _cfg(project, "bitrate_uncoupled: -1000")
    assert "target_bitrate_per_channel: -1000" in tp


def test_refused_once_however_many_targets_route_there(project):
    mf = project(_manifest(codec="bitrate_coupled: 600k, bitrate_uncoupled: 64k",
                           targets=["{ format: iamf, out: dist/a.iamf }",
                                    "{ format: preview, out: review/a.binaural.wav }",
                                    "{ format: iamf, out: dist/b.iamf }"],
                           binaural=True),
                 {"wavs/main.wav": 6})
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert _codes(e) == ["M-417"]


def test_a_preview_is_checked_too(project):
    """A preview is an iamf-tools encode (R9), so its rates meet the same encoder."""
    mf = project(_manifest(codec="bitrate_coupled: 600k, bitrate_uncoupled: 64k",
                           targets=["{ format: preview, out: review/a.binaural.wav }"],
                           binaural=True),
                 {"wavs/main.wav": 6})
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert _codes(e) == ["M-417"]


def test_the_ffmpeg_one_shot_is_not_checked(project):
    """The one-shot passes both rates to libopus directly (-b:a per stream);
    the iamf-tools encoder's range is not its rule."""
    mf = project(_manifest("stereo", codec="bitrate_coupled: 600k, bitrate_uncoupled: 64k",
                           targets=["{ format: mp4, out: x.mp4, video: v.mp4 }"]),
                 {"wavs/main.wav": 2}, extra_files=V)
    argv = _step(compile_text(mf), "-p2").argv
    assert argv[argv.index("-b:a:0") + 1] == "600000"


def test_a_mixed_manifest_names_the_iamftools_target(project):
    mf = project(_manifest("stereo", codec="bitrate_coupled: 600k, bitrate_uncoupled: 64k",
                           targets=["{ format: mp4, out: x.mp4, video: v.mp4 }",
                                    "{ format: iamf, out: dist/x.iamf }"]),
                 {"wavs/main.wav": 2}, extra_files=V)
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert _codes(e) == ["M-417"]
    assert "targets[1]" in e.value.diagnostics[0].message
    assert "M-417" in CODES


# ---- the rate written is the rate parsed ------------------------------------------

@pytest.mark.parametrize("text,want", [("64.1k", 64100), ("32.3k", 32300),
                                       ("96.5k", 96500), ("128k", 128000)])
def test_a_decimal_k_rate_is_rounded_not_truncated(project, text, want):
    tp = _cfg(project, f'bitrate_coupled: 400k, bitrate_uncoupled: "{text}"')
    assert f"target_bitrate_per_channel: {want}" in tp


def test_a_boolean_is_not_a_bitrate(project):
    mf = project(_manifest(codec="bitrate_uncoupled: true"), {"wavs/main.wav": 6})
    with pytest.raises(CompileError) as e:
        compile_text(mf)
    assert "M-202" in _codes(e)


# ---- the file carries it (toolchain) --------------------------------------------

SECONDS = 3


def _noise_wav(path: Path, channels: int, seconds: int = SECONDS) -> None:
    """Independent white noise per channel at -12 dBFS peak, seeded: a signal
    VBR Opus spends its whole budget on, so the coded size tracks the rate."""
    rng = random.Random(0xC0FFEE)
    frames = 48000 * seconds
    amp = int(0.25 * (2 ** 23 - 1))
    data = bytearray()
    for _ in range(frames * channels):
        data += struct.pack("<i", rng.randint(-amp, amp))[:3]
    path.parent.mkdir(parents=True, exist_ok=True)
    block = channels * 3
    fmt = struct.pack("<HHIIHH", 1, channels, 48000, 48000 * block, block, 24)
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
                     + b"fmt " + struct.pack("<I", 16) + fmt
                     + b"data" + struct.pack("<I", len(data)) + bytes(data))


def _encode(root: Path, codec: str) -> dict[int, int]:
    """Encode a 5.1 noise bed through the plan; return coded bytes per substream."""
    from sentinel.parser import parse_bytes

    from loom.compiler import compile_manifest
    from loom.executor import Executor
    from loom.manifest import load_manifest

    _noise_wav(root / "wavs/main.wav", 6)
    mf = root / "manifest.yaml"
    mf.write_text(_manifest("5.1", codec=codec), newline="\n", encoding="utf-8")
    m = load_manifest(mf)
    res = Executor(compile_manifest(m), m.manifest_dir, root / "out", root / "work",
                   toolchain=str(toolchain_root())).run()
    assert res.ok, res.failures
    out = root / "out/dist/r.iamf"
    model = parse_bytes(out.read_bytes(), source=str(out))
    sizes: dict[int, int] = {}
    for f in model.audio_frames:
        sizes[f.substream_id] = sizes.get(f.substream_id, 0) + f.payload_len
    return sizes


@needs_toolchain
def test_the_encoded_pairs_carry_the_coupled_rate(tmp_path):
    """5.1 = two stereo pairs (substreams 0, 1: pairs first, the F4 order) and
    two singles (2, 3). Against the default (128k pairs, 64k singles): 96k
    pairs shrink to three quarters and 160k pairs — beyond anything a coupling
    factor reaches — grow by a quarter, and the singles do not move. Before
    the rule all three encodes were the same file."""
    ref = _encode(tmp_path / "ref", "")
    for codec, want in (("bitrate_coupled: 96k, bitrate_uncoupled: 64k", 96000),
                        ("bitrate_coupled: 160k, bitrate_uncoupled: 64k", 160000)):
        got = _encode(tmp_path / str(want), codec)
        assert sorted(ref) == sorted(got) == [0, 1, 2, 3]
        for sid in (2, 3):
            assert got[sid] == ref[sid], f"{want}: single substream {sid} changed size"
        for sid in (0, 1):
            ratio = got[sid] / ref[sid]
            assert abs(ratio - want / 128000) < 0.05, (
                f"pair {sid}: {want // 1000}k/128k coded ratio {ratio:.3f}")
            bps = got[sid] * 8 / SECONDS
            assert abs(bps / want - 1) < 0.15, f"pair {sid} coded at {bps:.0f} bps, asked {want}"
