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


# ------------------------------------------------- collection-completeness
#
# `test_executor_units.py` skips at MODULE level when OSS_SRC is None, so its
# 12 tests are never collected and the suite still exits 0 (measured doc 128;
# the same class as iamf-sentinel-pro's 17-test collapse, doc 127 §4). That is
# the right behaviour for a contributor who cloned this repo alone, and a
# silent lie on CI, where ci.yml supplies the sibling checkout — remove,
# rename or silently fail that step and every leg goes on reporting success
# with a module missing.
#
# IAMF_SENTINEL_REQUIRE_FULL_COLLECTION=1 (one flag for the whole stack; CI
# sets it) turns the absent checkout into a hard error. It lives in
# `pytest_configure`, which runs BEFORE any test module is imported, so it
# covers any future module that depends on OSS_SRC and not merely today's one.

REQUIRE_FULL_COLLECTION_ENV = "IAMF_SENTINEL_REQUIRE_FULL_COLLECTION"

_FALSEY = {"", "0", "false", "no", "off"}


def require_full_collection(environ=None) -> bool:
    """True when the caller has demanded a fully-collected run.

    Split out from the hook so the policy is testable without a subprocess.
    """
    env = os.environ if environ is None else environ
    return env.get(REQUIRE_FULL_COLLECTION_ENV, "").strip().lower() not in _FALSEY


INCOMPLETE_COLLECTION_ERROR = (
    "{var}=1 demands a fully-collected run, but the iamf-sentinel SOURCE "
    "checkout was not found.\n"
    "  Effect: tests/test_executor_units.py skips at MODULE level, so 12 "
    "tests are never collected and the suite would still exit 0.\n"
    "  Fix: clone iamf-sentinel beside this repo (CI does this with a second "
    "actions/checkout step), or set $IAMF_SENTINEL_SRC to a source checkout "
    "that carries fixtures/build.py.\n"
    "  If you are a contributor running this repo standalone, unset {var} "
    "and the module will skip cleanly as designed."
).format(var=REQUIRE_FULL_COLLECTION_ENV)


# ------------------------------------------------------ toolchain requirement
#
# Every test that runs a real encode sits behind `needs_toolchain`, keyed on an
# executable `src/build-iamf/encoder_main` under `$LOOM_TOOLCHAIN` (else
# `$SENTINEL_TOOLCHAIN`, else a Linux default that exists only in the workspace
# this suite was first written in). When none resolves, those tests SKIP and
# the suite exits 0. That is right for CI, which installs no toolchain, and for
# a contributor without one. It is a silent lie on a machine that was meant to
# test encodes: measured on macOS with the toolchain built but the variable
# unset, a plain run read 300 passed / 45 skipped / exit 0 with 21 encode tests
# never run, among them the stereo-pair rate test.
#
# LOOM_REQUIRE_TOOLCHAIN=1 says "this run is meant to test encodes": a toolchain
# that does not resolve becomes a hard error before collection. Opt-in, and
# deliberately NOT implied by IAMF_SENTINEL_REQUIRE_FULL_COLLECTION, because CI
# sets that flag and has no toolchain.

REQUIRE_TOOLCHAIN_ENV = "LOOM_REQUIRE_TOOLCHAIN"
DEFAULT_TOOLCHAIN = "/home/claude/iamf-wp1"
ENCODER_REL = Path("src/build-iamf/encoder_main")


def require_toolchain(environ=None) -> bool:
    """True when the caller has demanded a run that tests real encodes."""
    env = os.environ if environ is None else environ
    return env.get(REQUIRE_TOOLCHAIN_ENV, "").strip().lower() not in _FALSEY


_DEFAULT_SOURCE = ("the built-in default; neither $LOOM_TOOLCHAIN nor "
                   "$SENTINEL_TOOLCHAIN is set")


def toolchain_candidate(environ=None) -> tuple[Path, str]:
    """The toolchain root this run would use, and where that answer came from."""
    env = os.environ if environ is None else environ
    for var in ("LOOM_TOOLCHAIN", "SENTINEL_TOOLCHAIN"):
        if env.get(var):
            return Path(env[var]), "$" + var
    return Path(DEFAULT_TOOLCHAIN), _DEFAULT_SOURCE


MISSING_TOOLCHAIN_ERROR = (
    "{var}=1 demands a run that tests real encodes, but no IAMF toolchain was "
    "found.\n"
    "  Looked for: {encoder} (root from {source}), an executable file.\n"
    "  Effect: every toolchain test would skip and the suite would still exit "
    "0 with no encode tested.\n"
    "  Fix: set $LOOM_TOOLCHAIN to the toolchain root, the directory that "
    "holds src/build-iamf/encoder_main.\n"
    "  If this run is not meant to test encodes, unset {var} and those tests "
    "will skip as designed."
)


def missing_toolchain_error(environ=None) -> str | None:
    """The refusal text when the switch is on and no toolchain resolves."""
    if not require_toolchain(environ) or toolchain_root(environ) is not None:
        return None
    root, source = toolchain_candidate(environ)
    return MISSING_TOOLCHAIN_ERROR.format(
        var=REQUIRE_TOOLCHAIN_ENV, encoder=root / ENCODER_REL, source=source)


def pytest_configure(config):                     # noqa: ARG001
    """Refuse a run that cannot do what the caller asked of it.

    Both guards are checked before anything is refused, so a run that fails
    both says so once rather than one fix at a time.
    """
    errors = []
    if require_full_collection() and OSS_SRC is None:
        errors.append(INCOMPLETE_COLLECTION_ERROR)
    toolchain_error = missing_toolchain_error()
    if toolchain_error is not None:
        errors.append(toolchain_error)
    if errors:
        raise pytest.UsageError("\n\n".join(errors))

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
        mf.write_text(manifest_text, newline="\n", encoding="utf-8")
        return mf

    return make


def compile_text(manifest_path: Path):
    from loom.compiler import compile_manifest
    from loom.manifest import load_manifest

    return compile_manifest(load_manifest(manifest_path))


def toolchain_root(environ=None) -> Path | None:
    root, _ = toolchain_candidate(environ)
    enc = root / ENCODER_REL
    return root if enc.is_file() and os.access(enc, os.X_OK) else None


needs_toolchain = pytest.mark.skipif(
    toolchain_root() is None,
    reason="IAMF toolchain not found: set $LOOM_TOOLCHAIN to the root that "
           "holds src/build-iamf/encoder_main (and $LOOM_REQUIRE_TOOLCHAIN=1 "
           "to make its absence a failure)",
)
