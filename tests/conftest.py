"""Shared fixtures: deterministic WAV factory + manifest helpers.

Fixture WAVs carry a unique identifier sine per channel (440 + 60*ch Hz at
-18 dBFS) — the WP1 essence idea in miniature — so any downstream channel
reorder/loss is detectable by FFT alone (the F4 detector).
"""

from __future__ import annotations

import math
import os
import shutil
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


# ----------------------------------------------- the rest of the toolchain
#
# The encoder is one tool of six. A root that holds it and nothing else still
# cannot run most routes, and the suite reports that badly in two ways:
#
# - without the loudness kernel, the decoder, FFmpeg or MP4Box the tests that
#   need them FAIL one by one, deep in a plan step, and read like regressions
#   (measured on macOS: 13 failures, every one "sentinel-dsp not found");
# - without a video donor, or without numpy, the tests that need them SKIP and
#   the run can still exit 0 (measured: 13 A/V skips and 4 spectral-check
#   skips, so the FFmpeg one-shot route and the YouTube preset had never run on
#   that machine while its runs read as passing apart from the kernel).
#
# With the switch on, "the suite passed" is meant to say every encode path ran.
# So the switch also refuses, before collection and in one message, a root that
# has an encoder but lacks any of the parts below. The lookup rules are the
# product's own (loom/toolchain.py): the kernel may be named by $SENTINEL_DSP
# or sit on PATH, MP4Box may sit on PATH, everything else lives under the root.

DECODER_REL = Path("src/build-iamf/decoder_main")
FFMPEG_REL = Path("bin/ffmpeg-install/bin/ffmpeg")
MP4BOX_REL = Path("bin/MP4Box")
KERNEL_REL = Path("bin/sentinel-dsp")
VIDEO_ENV = "LOOM_TEST_VIDEO"


def _is_executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _on_path(name: str, env) -> bool:
    return shutil.which(name, path=env.get("PATH", "")) is not None


def numpy_available() -> bool:
    """Whether the spectral channel-identity check can run in this interpreter."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def missing_components(environ=None, *,
                       numpy_ok: bool | None = None) -> list[tuple[str, str, str]]:
    """What an encode-testing run would lack: (part, where it was looked for,
    what its absence costs), one entry per missing part.

    Empty when no encoder resolves: that is the other refusal's to report, and
    a wrong root would otherwise be listed six times over.
    """
    env = os.environ if environ is None else environ
    root = toolchain_root(env)
    if root is None:
        return []
    out = []
    if not _is_executable(root / DECODER_REL):
        out.append(("decoder_main", f"{root / DECODER_REL}",
                    "every render fails, so no loudness is measured and no "
                    "decoded channel is checked"))
    named = env.get("SENTINEL_DSP", "")
    kernel = ((named and named != "off" and _is_executable(Path(named)))
              or _is_executable(root / KERNEL_REL) or _on_path("sentinel-dsp", env))
    if not kernel:
        out.append(("sentinel-dsp, the loudness kernel",
                    f"$SENTINEL_DSP, then {root / KERNEL_REL}, then PATH",
                    "every loudness-measure step fails, test by test"))
    if not _is_executable(root / FFMPEG_REL):
        out.append(("ffmpeg", f"{root / FFMPEG_REL}",
                    "the FFmpeg one-shot route, gain rides and Opus previews "
                    "cannot run, and the video donor cannot be trimmed"))
    if not (_is_executable(root / MP4BOX_REL) or _on_path("MP4Box", env)):
        out.append(("MP4Box", f"{root / MP4BOX_REL}, then PATH",
                    "every MP4 remux fails, the YouTube preset among them"))
    video = env.get(VIDEO_ENV, "")
    if not (video and Path(video).is_file() and Path(video).stat().st_size > 0):
        out.append(("a video donor",
                    f"${VIDEO_ENV} ({video!r})" if video else f"${VIDEO_ENV} (unset)",
                    "every audio-with-video test SKIPS and the run can still "
                    "exit 0 with the FFmpeg one-shot route never run"))
    if not (numpy_available() if numpy_ok is None else numpy_ok):
        out.append(("numpy", "an import in this interpreter",
                    "the spectral channel-identity check SKIPS after the "
                    "encode, taking the rest of its test with it"))
    return out


INCOMPLETE_TOOLCHAIN_ERROR = (
    "{var}=1 demands a run that tests real encodes, and the toolchain at "
    "{root} has an encoder but not everything the encode tests use.\n"
    "{parts}\n"
    "  Effect: the run would fail or skip tests for want of a tool, and a "
    "pass would not mean that every encode path ran.\n"
    "  Fix: add what is missing to the toolchain root (root from {source}), "
    "or name it with the variable shown.\n"
    "  If this run is not meant to test encodes, unset {var} and the "
    "toolchain tests will run or skip as they did before."
)


def incomplete_toolchain_error(environ=None, *,
                               numpy_ok: bool | None = None) -> str | None:
    """The refusal text when the switch is on, an encoder resolves, and some
    other part of an encode-testing run does not."""
    if not require_toolchain(environ):
        return None
    missing = missing_components(environ, numpy_ok=numpy_ok)
    if not missing:
        return None
    root, source = toolchain_candidate(environ)
    parts = "\n".join(
        f"  Missing: {name}. Looked for: {where}.\n    Without it {cost}."
        for name, where, cost in missing)
    return INCOMPLETE_TOOLCHAIN_ERROR.format(
        var=REQUIRE_TOOLCHAIN_ENV, root=root, source=source, parts=parts)


def pytest_configure(config):                     # noqa: ARG001
    """Refuse a run that cannot do what the caller asked of it.

    Every guard is checked before anything is refused, so a run that fails
    more than one says so once rather than one fix at a time.
    """
    errors = []
    if require_full_collection() and OSS_SRC is None:
        errors.append(INCOMPLETE_COLLECTION_ERROR)
    toolchain_error = missing_toolchain_error() or incomplete_toolchain_error()
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
