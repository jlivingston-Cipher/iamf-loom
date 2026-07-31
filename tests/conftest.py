"""Shared fixtures: deterministic WAV factory + manifest helpers.

Fixture WAVs carry a unique identifier sine per channel (440 + 60*ch Hz at
-18 dBFS) — the WP1 essence idea in miniature — so any downstream channel
reorder/loss is detectable by FFT alone (the F4 detector).
"""

from __future__ import annotations

import math
import os
import struct
import sys
from pathlib import Path

import pytest

LOOM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LOOM_ROOT.parent


def find_oss_source() -> Path | None:
    """Locate an `iamf-sentinel` SOURCE checkout carrying `fixtures/`.

    `fixtures/build.py` ships in the core's source tree, not in the installed
    wheel, and one module here (`test_executor_units`) builds real IAMF bytes
    with it. Before doc 97 the sibling directory name `sentinel-oss` — the
    internal monorepo layout — was assumed, so a standalone clone of this repo
    failed at *collection* rather than running. Order: `$IAMF_SENTINEL_SRC`,
    sibling `sentinel-oss/` (internal), sibling `iamf-sentinel/` (public).
    """
    cands = []
    env = os.environ.get("IAMF_SENTINEL_SRC")
    if env:
        cands.append(Path(env))
    cands += [REPO_ROOT / "sentinel-oss", REPO_ROOT / "iamf-sentinel"]
    for c in cands:
        if (c / "fixtures" / "build.py").is_file():
            return c.resolve()
    return None


OSS_SRC = find_oss_source()

NO_OSS_SRC_REASON = (
    "iamf-sentinel source checkout not found — clone it beside this repo or "
    "set $IAMF_SENTINEL_SRC (the fixture builders live in the core's source "
    "tree, not in the installed wheel)"
)

for p in (LOOM_ROOT, OSS_SRC, REPO_ROOT / "sentinel-oss",
          REPO_ROOT / "sentinel-pro"):
    if p is None:
        continue
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

AMP = 0.125  # -18 dBFS
FREQ0, FSTEP = 440.0, 60.0


def channel_freq(ch: int) -> float:
    return FREQ0 + FSTEP * ch


def write_wav(path: Path, channels: int, frames: int = 4800,
              rate: int = 48000, bits: int = 24) -> Path:
    """Deterministic little-endian integer PCM WAV (format tag 1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_per = bits // 8
    block = channels * bytes_per
    data = bytearray()
    scale = float(2 ** (bits - 1) - 1)
    for n in range(frames):
        t = n / rate
        for ch in range(channels):
            v = int(round(AMP * scale * math.sin(2 * math.pi
                                                 * channel_freq(ch) * t)))
            if bits == 24:
                data += struct.pack("<i", v)[:3]
            else:
                data += struct.pack("<h", v)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * block, block, bits)
    body = b"fmt " + struct.pack("<I", 16) + fmt
    body += b"data" + struct.pack("<I", len(data)) + bytes(data)
    path.write_bytes(hdr + body)
    return path


def _box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


def fake_mp4(duration_s: float = 0.1, fourcc: bytes = b"avc1",
             handler: bytes = b"vide") -> bytes:
    """A minimal, structurally valid MP4 with one track — just enough for the
    Phase-2 compile-time video probe (hdlr + mdhd + stsd). Fixture WAVs are
    0.1 s (4800 frames), so the default duration matches them."""
    hdlr = _box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 12 + b"\x00")
    mdhd = _box(b"mdhd", b"\x00" * 12 + struct.pack(">I", 1000)
                + struct.pack(">I", round(duration_s * 1000)) + b"\x00" * 4)
    entry = struct.pack(">I", 16) + fourcc + b"\x00" * 8
    stsd = _box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + entry)
    minf = _box(b"minf", _box(b"stbl", stsd))
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    moov = _box(b"moov", _box(b"trak", mdia))
    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 4 + b"isom")
    return ftyp + moov


@pytest.fixture
def project(tmp_path: Path):
    """A manifest project dir factory: writes WAVs + a manifest file."""

    def make(manifest_text: str, wavs: dict[str, int],
             frames: int = 4800, extra_files: dict[str, bytes] | None = None,
             name: str = "manifest.yaml") -> Path:
        for rel, ch in wavs.items():
            write_wav(tmp_path / rel, ch, frames=frames)
        for rel, content in (extra_files or {}).items():
            fp = tmp_path / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(content)
        mf = tmp_path / name
        # newline="\n" is load-bearing, not style: `manifest_sha256` is taken
        # over the file's RAW BYTES (manifest.py), and the committed plan
        # goldens carry the LF-form hash. Default text mode writes CRLF on
        # Windows, which changes the hash and fails all 20 golden plans and
        # every explain golden (measured: doc 97 §3.2, 40 failures).
        mf.write_text(manifest_text, newline="\n")
        return mf

    return make


def compile_text(manifest_path: Path):
    from loom.compiler import compile_manifest
    from loom.manifest import load_manifest

    return compile_manifest(load_manifest(manifest_path))


def toolchain_root() -> Path | None:
    root = Path(os.environ.get("LOOM_TOOLCHAIN")
                or os.environ.get("SENTINEL_TOOLCHAIN")
                or "/home/claude/iamf-wp1")
    enc = root / "src/build-iamf/encoder_main"
    return root if enc.is_file() and os.access(enc, os.X_OK) else None


needs_toolchain = pytest.mark.skipif(
    toolchain_root() is None,
    reason="IAMF toolchain not present (build per wp3-scripts + addendum)",
)
