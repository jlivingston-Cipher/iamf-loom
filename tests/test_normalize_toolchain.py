"""Phase 2 toolchain-gated E2E (doc 43 E-P3/E-P4/E-P5): R3 normalize hits
±0.3 LU on both routes; the clip guard refuses to clip; the FLAC/archive
mezzanine round-trips; and the mutation-catch accept test — an injected
F4-style backend regression encodes rc 0 but the Sentinel gate fails the run.
"""

from __future__ import annotations

import struct

import pytest

from loom.compiler import compile_manifest
from loom.executor import Executor
from loom.manifest import load_manifest

from .conftest import needs_toolchain, toolchain_root, write_wav
from .test_parity_toolchain import (FRAMES, assert_channel_identity,
                                    declared_loudness, decode_native,
                                    video_donor)

TOL = 0.3  # PRD R3 accept


def run_manifest(tmp_path, text, wavs, frames=FRAMES, expect_ok=True):
    for rel, ch in wavs.items():
        write_wav(tmp_path / rel, ch, frames=frames)
    mf = tmp_path / "manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "out", tmp_path / "work",
                  toolchain=str(toolchain_root()),
                  validate_policy=m.policy.validate)
    return m, plan, ex, ex.run()


# -- R3: normalize on the iamf-tools route (bitstream-verified) ---------------

@needs_toolchain
@pytest.mark.parametrize("layout,ch,target", [("stereo", 2, -16.0),
                                              ("7.1.4", 12, -20.0)])
def test_normalize_iamftools_hits_target(tmp_path, layout, ch, target):
    _m, plan, ex, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: norm\n"
        f"sources:\n  main: {{ path: main.wav, kind: bed, layout: \"{layout}\" }}\n"
        "elements:\n  bed: { from: main }\n"
        f"policy:\n  loudness: {{ normalize: {target} }}\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf }\n",
        {"main.wav": ch},
    )
    assert res.ok, res.failures
    out = tmp_path / "out/dist/a.iamf"

    # ledger: the full audit trail (target, gain, verdict)
    ver = [v for v in ex.ledger["normalize"].values()
           if "within_tolerance" in v]
    assert ver and ver[0]["within_tolerance"]
    assert abs(ver[0]["post_ride_il"] - target) <= TOL
    gains = [v for v in ex.ledger["normalize"].values()
             if "applied_gain_db" in v]
    assert gains and gains[0]["target_lufs"] == target

    # independent read-back: encoder_main's OWN measurement of the ridden
    # audio, declared in the bitstream, is within tolerance of the target
    declared = declared_loudness(out)
    assert declared
    anchor_il = declared[0][0]  # first layout = the anchor (stereo)
    assert anchor_il is not None and abs(anchor_il - target) <= TOL

    # and the ride didn't scramble anything (F4 detector still green)
    dec = tmp_path / "dec.wav"
    decode_native(out, layout, dec)
    assert_channel_identity(dec, ch)


# -- R3: normalize on the FFmpeg one-shot route -------------------------------

@needs_toolchain
def test_normalize_oneshot_hits_target(tmp_path):
    donor = video_donor(tmp_path)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    target = -14.0
    _m, plan, ex, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: norm\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: \"5.1\" }\n"
        "elements:\n  bed: { from: main }\n"
        f"policy:\n  loudness: {{ normalize: {target} }}\n"
        "targets:\n  - { format: mp4, out: dist/a.mp4, video: v.mp4 }\n",
        {"main.wav": 6},
    )
    assert res.ok, res.failures
    assert plan.targets[0].backend == "ffmpeg_oneshot"
    ver = [v for v in ex.ledger["normalize"].values()
           if "within_tolerance" in v]
    assert ver and ver[0]["within_tolerance"], ver
    assert abs(ver[0]["post_ride_il"] - target) <= TOL
    # three ffmpeg encodes in the chain (D-P2): pre + pass-B + final
    encs = [s for s in plan.steps if s.kind == "encode"]
    assert len(encs) == 3


# -- R3: the clip guard -------------------------------------------------------

@needs_toolchain
def test_clip_guard_refuses_to_clip(tmp_path):
    """A crest-heavy program (quiet continuous bed + near-full-scale spikes)
    whose ride to a legal target would exceed full scale must fail with named
    values — Loom refuses to clip, it does not limit."""
    import math
    path = tmp_path / "main.wav"
    rate, frames, bits = 48000, FRAMES, 24
    scale = float(2 ** (bits - 1) - 1)
    data = bytearray()
    for n in range(frames):
        t = n / rate
        bed = 0.01 * math.sin(2 * math.pi * 440 * t)      # ~-40 dBFS bed
        spike = 0.891 if n % 4800 == 0 else 0.0            # ~-1 dBFS peaks
        hot = int(round(min(1.0, bed + spike) * scale))
        quiet = int(round(0.01 * scale * math.sin(2 * math.pi * 500 * t)))
        data += struct.pack("<i", hot)[:3] + struct.pack("<i", quiet)[:3]
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 6, 6, bits)
    path.write_bytes(hdr + b"fmt " + struct.pack("<I", 16) + fmt
                     + b"data" + struct.pack("<I", len(data)) + bytes(data))

    mf = tmp_path / "manifest.yaml"
    mf.write_text(
        "loom: 0\ntitle: crest\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  loudness: { normalize: -5 }\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf }\n", encoding="utf-8")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "out", tmp_path / "work",
                  toolchain=str(toolchain_root()),
                  validate_policy=m.policy.validate)
    res = ex.run()
    assert not res.ok
    assert any("clip guard" in f for f in res.failures), res.failures
    assert not (tmp_path / "out/dist/a.iamf").exists()  # nothing shipped


# -- R4: the FLAC/archive mezzanine -------------------------------------------

@needs_toolchain
def test_archive_flac_roundtrip(tmp_path):
    _m, plan, ex, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: mezz\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: \"5.1\" }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: flac }\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf, preset: archive }\n",
        {"main.wav": 6},
    )
    assert res.ok, res.failures
    out = tmp_path / "out/dist/a.iamf"

    # loudness measured natively on the flac path too (G2b, anti-F7)
    declared = declared_loudness(out)
    assert declared
    for il, _dp in declared:
        assert il is not None and il < -1.0, f"IL {il} looks unmeasured"

    # decode round-trip channel identity (F4 detector)
    dec = tmp_path / "dec.wav"
    decode_native(out, "5.1", dec)
    assert_channel_identity(dec, 6)

    # gate ran and passed (it is part of res.ok, but assert the ledger too)
    gate = ex.ledger["gate"]["dist/a.iamf"]
    assert gate["passed"], gate


# -- R5: the mutation-catch accept test (PRD R5) ------------------------------

def _f4_tempting_split(source):
    """The natural-but-wrong reading: pairs NOT first, C/LFE not last —
    exactly the F4 arrangement WP1 proved encodes silently (doc 42's
    negative control, now injected as a live backend regression)."""
    from loom.layouts import BEDS
    bed = BEDS[source.layout]
    subs = []
    for ss in bed.substreams:
        if ss.coupled:
            a, b = ss.wav_channels
            subs.append((ss.label, f"stereo|c0=c{a}|c1=c{b}", True))
        else:
            (a,) = ss.wav_channels
            subs.append((ss.label, f"mono|c0=c{a}", False))
    pairs = [s for s in subs if s[2]]
    monos = [s for s in subs if not s[2]]
    return [pairs[0]] + monos + pairs[1:]   # (L/R)(C)(LFE)(remaining pairs)


@needs_toolchain
def test_mutation_catch_gate_fails_run(tmp_path, monkeypatch):
    """Inject the F4 mutation into the FFmpeg backend: the encode must
    succeed rc 0 (the silent-corruption class) and the Sentinel gate must
    fail the run with the S-320/S-321 signatures in the ledger."""
    donor = video_donor(tmp_path)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    import loom.backends.ffmpeg as ffb
    monkeypatch.setattr(ffb, "substream_split", _f4_tempting_split)

    _m, plan, ex, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: mut\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: mp4, out: dist/a.mp4, video: v.mp4 }\n",
        {"main.wav": 12},
    )
    # the encodes themselves succeeded (rc 0 — nothing upstream noticed)
    enc_recs = [r for r in ex.ledger["steps"]
                if r["kind"] == "encode" and r["id"].endswith("-p2")]
    assert enc_recs and enc_recs[0]["rc"] == 0, "mutated encode should be rc 0"
    # ...but the gate caught it
    assert not res.ok, "the gate MUST fail the mutated run"
    gate = ex.ledger["gate"]["dist/a.mp4"]
    assert not gate["passed"]
    assert set(gate["fail_ids"]) & {"S-320", "S-321"}, gate["fail_ids"]
    esc = [f for f in gate["findings"] if f.get("escalated")]
    assert esc, "escalation must be marked in the ledger, never silent"


@needs_toolchain
def test_mutation_control_run_passes(tmp_path):
    """The unmutated control: same manifest, gate passes."""
    donor = video_donor(tmp_path)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    _m, _p, ex, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: mut\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: mp4, out: dist/a.mp4, video: v.mp4 }\n",
        {"main.wav": 12},
    )
    assert res.ok, res.failures
    gate = ex.ledger["gate"]["dist/a.mp4"]
    assert gate["passed"], gate
    assert gate["tier"] == "l1l2+l3"
