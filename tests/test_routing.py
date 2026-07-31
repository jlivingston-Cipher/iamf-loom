"""E-L5: backend selection matches the ADR-1/ADR-2 decision table."""

from __future__ import annotations

import pytest

from loom.diagnostics import CompileError

from .conftest import compile_text, fake_mp4

STEREO_SRC = (
    "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
)


def _plan(project, targets: str, policy: str = "", extra_pres: str = ""):
    mf = project(
        "loom: 0\ntitle: R\n" + STEREO_SRC + extra_pres + policy
        + "targets:\n" + targets,
        {"wavs/main.wav": 2},
        extra_files={"v.mp4": fake_mp4()},
    )
    return compile_text(mf)


def test_raw_iamf_routes_iamftools(project):
    plan = _plan(project, "  - { format: iamf, out: x.iamf }\n")
    t = plan.targets[0]
    assert t.backend == "iamftools" and t.muxer is None
    assert "ADR-1" in t.rationale


def test_av_opus_oneshot_routes_ffmpeg(project):
    plan = _plan(project, "  - { format: mp4, out: x.mp4, video: v.mp4 }\n")
    t = plan.targets[0]
    assert t.backend == "ffmpeg_oneshot" and t.muxer == "ffmpeg"
    assert "two-pass" in t.rationale


def test_mp4_without_video_routes_mp4box(project):
    plan = _plan(project, "  - { format: mp4, out: x.mp4 }\n")
    t = plan.targets[0]
    assert t.backend == "iamftools" and t.muxer == "mp4box"
    # ADR-2 post-61 grounding: repairability asymmetry, not the refuted F5
    assert "F31" in t.rationale and "F32" in t.rationale
    assert "F5" not in t.rationale


def test_youtube_preset_forces_iamftools_mp4box(project):
    """G11: base-profile signaling exists only on the iamf-tools route."""
    plan = _plan(project,
                 "  - { format: mp4, out: x.mp4, video: v.mp4, preset: youtube }\n")
    t = plan.targets[0]
    assert t.backend == "iamftools" and t.muxer == "mp4box"
    assert t.profile == "base"
    assert "G11" in t.rationale


def test_route_remux_honored_even_when_oneshot_possible(project):
    plan = _plan(project,
                 "  - { format: mp4, out: x.mp4, video: v.mp4, route: remux }\n")
    t = plan.targets[0]
    assert t.backend == "iamftools" and t.muxer == "mp4box"


def test_route_oneshot_honored(project):
    plan = _plan(project,
                 "  - { format: mp4, out: x.mp4, video: v.mp4, route: oneshot }\n")
    assert plan.targets[0].backend == "ffmpeg_oneshot"


def test_route_oneshot_on_raw_iamf_is_m403(project):
    with pytest.raises(CompileError) as e:
        _plan(project, "  - { format: iamf, out: x.iamf, route: oneshot }\n")
    assert "M-403" in e.value.codes()


def test_binaural_presentation_blocks_oneshot(project):
    pres = ("presentations:\n"
            "  - id: main\n"
            "    elements: [ { ref: bed, headphones: binaural } ]\n")
    plan = _plan(project, "  - { format: mp4, out: x.mp4, video: v.mp4 }\n",
                 extra_pres=pres)
    t = plan.targets[0]
    assert t.backend == "iamftools" and t.muxer == "mp4box"


def test_multi_presentation_blocks_oneshot(project):
    pres = ("presentations:\n"
            "  - { id: a, elements: [ { ref: bed } ] }\n"
            "  - { id: b, elements: [ { ref: bed, gain_db: -2 } ] }\n")
    plan = _plan(project, "  - { format: mp4, out: x.mp4, video: v.mp4 }\n",
                 extra_pres=pres)
    assert plan.targets[0].backend == "iamftools"


def test_every_target_carries_rationale(project):
    plan = _plan(project,
                 "  - { format: iamf, out: a.iamf }\n"
                 "  - { format: mp4, out: b.mp4, video: v.mp4 }\n"
                 "  - { format: mp4, out: c.mp4 }\n")
    assert all(t.rationale for t in plan.targets)
    assert all(s.rationale for s in plan.steps)
