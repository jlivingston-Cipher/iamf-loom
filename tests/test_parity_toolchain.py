"""E-L6/E-L7/E-L8 (toolchain-gated): end-to-end parity on a real toolchain.

Skips cleanly when the toolchain is absent (the Sentinel pattern). Success is
judged on outputs (F8), declared loudness is read back from the bitstream via
the sentinel clean-room parser (stdlib), and channel identity is verified
spectrally when numpy is present (the F4 detector).

The A/V one-shot and YouTube-preset tests need a stream-copyable video donor
(ADR-4: video is always stream-copied from user-supplied files — the shipped
FFmpeg is lean/LGPL-class with no H.264 encoder). Set $LOOM_TEST_VIDEO to any
MP4 whose video track can be copied; without one those tests skip.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from loom.compiler import compile_manifest
from loom.executor import Executor
from loom.manifest import load_manifest

from .conftest import channel_freq, needs_toolchain, toolchain_root, write_wav

try:
    import numpy as _np
except ImportError:  # spectral checks skip; the rest still runs
    _np = None

FRAMES = 3 * 48000  # 3 s: enough signal for a stable BS.1770 integration
BEDS = {"stereo": 2, "5.1": 6, "7.1.4": 12}
NATIVE_DECODE = {"stereo": "2.0", "5.1": "5.1", "7.1.4": "7.1.4"}


# --- helpers -----------------------------------------------------------------

def video_donor(tmp_path: Path) -> Path | None:
    """A video file whose track can be stream-copied into A/V targets.

    Phase 2: trimmed to the test-essence duration by stream copy, so the
    compile-time video probe (M-411, the S-406 class) accepts it — the
    duration mismatch the doc-42 evidence run tolerated at runtime is now a
    compile error by design (doc 43 D-P5)."""
    src = os.environ.get("LOOM_TEST_VIDEO")
    if src and Path(src).is_file():
        root = toolchain_root()
        out = tmp_path / "v.mp4"
        r = subprocess.run(
            [str(root / "bin/ffmpeg-install/bin/ffmpeg"), "-y", "-hide_banner",
             "-i", src, "-an", "-c:v", "copy",
             "-t", f"{FRAMES / 48000:.3f}", str(out)],
            capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return out
    return None


def run_manifest(tmp_path: Path, text: str, wavs: dict[str, int]):
    for rel, ch in wavs.items():
        write_wav(tmp_path / rel, ch, frames=FRAMES)
    mf = tmp_path / "manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "out", tmp_path / "work",
                  toolchain=str(toolchain_root()))
    return m, plan, ex, ex.run()


def read_pcm(path: Path):
    """WAV -> float64 [frames, ch] (16/24/32-bit int, incl. extensible)."""
    from loom.wavinfo import read_wav_info

    wi = read_wav_info(path)
    raw = path.read_bytes()
    pos, payload = 12, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        csize = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        if cid == b"data":
            payload = raw[pos + 8:pos + 8 + csize]
            break
        pos += 8 + csize + (csize & 1)
    assert payload is not None, "no data chunk"
    bp = wi.bits_per_sample // 8
    n = len(payload) // (bp * wi.channels)
    if bp == 2:
        d = _np.frombuffer(payload[:n * wi.channels * 2], dtype="<i2")
        a = d.reshape(n, wi.channels).astype(_np.float64) / 2**15
    elif bp == 3:
        b = _np.frombuffer(payload[:n * wi.channels * 3],
                           dtype=_np.uint8).reshape(-1, 3)
        d = (b[:, 0].astype(_np.int32) | (b[:, 1].astype(_np.int32) << 8)
             | (b[:, 2].astype(_np.int32) << 16))
        d = _np.where(d >= 1 << 23, d - (1 << 24), d)
        a = d.reshape(n, wi.channels).astype(_np.float64) / 2**23
    else:
        d = _np.frombuffer(payload[:n * wi.channels * 4], dtype="<i4")
        a = d.reshape(n, wi.channels).astype(_np.float64) / 2**31
    return a, wi


def assert_channel_identity(wav: Path, channels: int):
    """Every decoded channel carries its own identifier sine (F4 detector)."""
    if _np is None:
        pytest.skip("numpy not available for the spectral check")
    a, wi = read_pcm(wav)
    assert wi.channels == channels
    for ch in range(channels):
        x = a[:, ch]
        rms = float(_np.sqrt((x ** 2).mean()))
        assert rms > 1e-3, f"channel {ch} is silent (F4 class)"
        spec = _np.abs(_np.fft.rfft(x * _np.hanning(len(x))))
        got = float(_np.argmax(spec) * wi.sample_rate / len(x))
        want = channel_freq(ch)
        assert abs(got - want) < 15.0, (
            f"channel {ch}: dominant {got:.0f} Hz, expected {want:.0f} Hz "
            "(channel scramble — the F4 class)")


def declared_loudness(iamf_path: Path):
    """Read declared loudness back from the bitstream (sentinel parser)."""
    from sentinel.parser import parse_bytes

    model = parse_bytes(iamf_path.read_bytes(), source=str(iamf_path))
    return [(lay.integrated_loudness, lay.digital_peak)
            for mp in model.mix_presentations
            for sm in mp.sub_mixes
            for lay in sm.layouts]


def sentinel_errors(path: Path) -> list[str]:
    repo = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, "-m", "sentinel", "validate", str(path),
         "--format", "json"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ,
             "PYTHONPATH": f"{repo / 'sentinel-oss'}:{repo / 'sentinel-pro'}"})
    assert r.stdout.strip(), f"sentinel produced no report: {r.stderr[-400:]}"
    report = json.loads(r.stdout)
    return [f["id"] for f in report["findings"] if f["severity"] == "ERROR"]


def decode_native(iamf: Path, layout: str, out_wav: Path):
    root = toolchain_root()
    r = subprocess.run(
        [str(root / "src/build-iamf/decoder_main"),
         f"--input_filename={iamf}", f"--output_filename={out_wav}",
         f"--output_layout={NATIVE_DECODE[layout]}"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stderr or r.stdout)[-400:]
    assert out_wav.is_file() and out_wav.stat().st_size > 0  # F8


# --- iamf-tools path (E-L6) ---------------------------------------------------

@needs_toolchain
@pytest.mark.parametrize("layout,ch", list(BEDS.items()))
def test_iamftools_bed_raw_iamf(tmp_path, layout, ch):
    _m, _p, _e, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: parity\n"
        f"sources:\n  main: {{ path: main.wav, kind: bed, layout: \"{layout}\" }}\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf }\n",
        {"main.wav": ch},
    )
    assert res.ok, res.failures
    out = tmp_path / "out/dist/a.iamf"

    # loudness measured natively (G2b), embedded non-zero (the anti-F7 gate)
    declared = declared_loudness(out)
    assert declared, "no loudness layouts parsed from the bitstream"
    for il, _dp in declared:
        assert il is not None and il < -1.0, f"IL {il} looks unmeasured (F7)"

    dec = tmp_path / "dec.wav"
    decode_native(out, layout, dec)
    assert_channel_identity(dec, ch)

    errs = sentinel_errors(out)
    assert not errs, f"sentinel ERRORs: {errs}"


@needs_toolchain
@pytest.mark.parametrize("order,ch", [(1, 4), (3, 16)])
def test_iamftools_ambisonics_raw_iamf(tmp_path, order, ch):
    _m, _p, _e, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: parity\n"
        "sources:\n  amb: { path: amb.wav, kind: ambisonics }\n"
        "elements:\n  scene: { from: amb }\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf }\n",
        {"amb.wav": ch},
    )
    assert res.ok, res.failures
    errs = sentinel_errors(tmp_path / "out/dist/a.iamf")
    assert not errs, f"sentinel ERRORs: {errs}"


@needs_toolchain
def test_iamftools_lpcm(tmp_path):
    _m, _p, _e, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: parity\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  codec: { name: lpcm }\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf }\n",
        {"main.wav": 2},
    )
    assert res.ok, res.failures
    errs = sentinel_errors(tmp_path / "out/dist/a.iamf")
    assert not errs, f"sentinel ERRORs: {errs}"


# --- FFmpeg one-shot path (E-L7) -----------------------------------------------

@needs_toolchain
@pytest.mark.parametrize("layout,ch", list(BEDS.items()))
def test_ffmpeg_oneshot_two_pass(tmp_path, layout, ch):
    donor = video_donor(tmp_path)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    _m, plan, ex, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: parity\n"
        f"sources:\n  main: {{ path: main.wav, kind: bed, layout: \"{layout}\" }}\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: mp4, out: dist/a.mp4, video: v.mp4 }\n",
        {"main.wav": ch},
    )
    assert res.ok, res.failures
    assert plan.targets[0].backend == "ffmpeg_oneshot"
    out = tmp_path / "out/dist/a.mp4"
    assert out.stat().st_size > 0

    # two-pass landed real measured values in the ledger
    assert ex.measured, "no BS.1770 measurements recorded"
    for vals in ex.measured.values():
        assert vals["il"] < -1.0, f"measured IL {vals['il']} implausible"

    # F4: the encoded stream order is spectrally correct (pass-1 bitstream —
    # identical stream construction to pass 2, minus loudness fields)
    pass1 = next(Path(tmp_path / "work/os").glob("*.pass1.iamf"))
    dec = tmp_path / "dec.wav"
    decode_native(pass1, layout, dec)
    assert_channel_identity(dec, ch)


@needs_toolchain
def test_youtube_preset_shape(tmp_path):
    """E-L8: brands isom/mp42/iso6/iamf, fast-start, RFC6381 iamf.001.001."""
    donor = video_donor(tmp_path)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    _m, plan, _e, res = run_manifest(
        tmp_path,
        "loom: 0\ntitle: parity\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: \"5.1\" }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: mp4, out: dist/yt.mp4, video: v.mp4, preset: youtube }\n",
        {"main.wav": 6},
    )
    assert res.ok, res.failures
    assert plan.targets[0].profile == "base"
    out = tmp_path / "out/dist/yt.mp4"
    raw = out.read_bytes()

    # ftyp brands
    assert raw[4:8] == b"ftyp"
    ftyp_size = struct.unpack(">I", raw[:4])[0]
    brands = {raw[i:i + 4] for i in range(8, ftyp_size, 4)}
    for want in (b"isom", b"iso6", b"iamf", b"mp42"):
        assert want in brands, f"brand {want!r} missing from ftyp"

    # fast-start: moov before mdat
    assert raw.find(b"moov") < raw.find(b"mdat"), "not fast-start"

    # RFC6381 audio string via MP4Box -info (F12: gpac lowercases 'opus')
    from loom.toolchain import binary, resolve_root
    mp4box = binary(resolve_root(str(toolchain_root())), "mp4box")
    r = subprocess.run([str(mp4box), "-info", str(out)],
                       capture_output=True, text=True, encoding="utf-8")
    blob = r.stdout + r.stderr
    assert "iamf.001.001" in blob.lower(), "RFC6381 iamf codec string missing"
