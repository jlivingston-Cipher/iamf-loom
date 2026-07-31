"""Phase 2 (doc 43 D-P5/E-P6): the compile-time video probe — the S-406 class
caught at compile, M-410/M-411/M-412."""

from __future__ import annotations

import pytest

from loom.diagnostics import CompileError
from loom.manifest import load_manifest
from loom.videoprobe import probe_video

from .conftest import fake_mp4

MANIFEST_AV = (
    "loom: 0\ntitle: T\n"
    "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: mp4, out: x.mp4, video: v.mp4 }\n"
)
MANIFEST_YT = MANIFEST_AV.replace("video: v.mp4 }",
                                  "video: v.mp4, preset: youtube }")


def _codes(exc) -> list[str]:
    return exc.value.codes()


# -- probe unit level ---------------------------------------------------------

def test_probe_reads_crafted_mp4(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(fake_mp4(duration_s=2.5, fourcc=b"avc1"))
    vp = probe_video(p)
    assert vp.ok and vp.fourcc == "avc1"
    assert vp.duration_s == pytest.approx(2.5, abs=0.01)


def test_probe_rejects_garbage(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"\x00" * 64)
    vp = probe_video(p)
    assert not vp.ok and "moov" in (vp.error or "")


def test_probe_no_video_track(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(fake_mp4(handler=b"soun"))
    vp = probe_video(p)
    assert not vp.ok and "vide" in (vp.error or "")


# -- compile level ------------------------------------------------------------

def test_m410_no_video_track(project):
    mf = project(MANIFEST_AV, {"wavs/main.wav": 2},
                 extra_files={"v.mp4": fake_mp4(handler=b"soun")})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-410" in _codes(e)


def test_m410_not_an_mp4(project):
    mf = project(MANIFEST_AV, {"wavs/main.wav": 2},
                 extra_files={"v.mp4": b"\x00" * 64})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-410" in _codes(e)


def test_m411_duration_mismatch_s406_class(project):
    """The doc-42 S-406 fixture case (short essence vs 10 s donor), now a
    compile error naming both durations."""
    mf = project(MANIFEST_AV, {"wavs/main.wav": 2},
                 extra_files={"v.mp4": fake_mp4(duration_s=10.0)})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-411" in _codes(e)
    msg = str(e.value)
    assert "10.00" in msg and "0.10" in msg  # both sides named


def test_m411_tolerance_allows_padding(project):
    # 0.1 s audio vs 0.9 s video: inside the 1.0 s tolerance
    mf = project(MANIFEST_AV, {"wavs/main.wav": 2},
                 extra_files={"v.mp4": fake_mp4(duration_s=0.9)})
    m = load_manifest(mf)
    assert m.targets[0].video == "v.mp4"


def test_m412_youtube_requires_h264(project):
    mf = project(MANIFEST_YT, {"wavs/main.wav": 2},
                 extra_files={"v.mp4": fake_mp4(fourcc=b"hev1")})
    with pytest.raises(CompileError) as e:
        load_manifest(mf)
    assert "M-412" in _codes(e)


def test_hevc_fine_without_preset(project):
    mf = project(MANIFEST_AV, {"wavs/main.wav": 2},
                 extra_files={"v.mp4": fake_mp4(fourcc=b"hev1")})
    m = load_manifest(mf)
    assert m.targets[0].video == "v.mp4"
