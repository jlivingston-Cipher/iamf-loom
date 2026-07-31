"""Item 13 (doc 84): the F32 in-product `stts` repair.

Two tiers:

- Plan-structure tests (always run): every mp4box-routed target compiles
  with a `repair_stts` step immediately after its remux; ffmpeg-oneshot
  routes (ADR-1 encode path — correct, doc 57) compile without one.
- Archived-fixture tests, skip-gated on the doc-60/wp1 evidence roots
  (`/tmp/sentinel_build/f32/f32-samples` from f32-artifacts.zip,
  `/tmp/sentinel_build/wp1/wp1-samples`, `/tmp/sentinel_build/f31/
  f31-samples` from f31-artifacts.zip). The discriminator is doc 60's own
  matrix: repairing MP4Box's `fresh` output must reproduce the archived
  `conform` variant byte-for-byte — the row proven sample-exact in BOTH
  reference ecosystems.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from loom.repair import RepairError, repair_stts

from .conftest import fake_mp4

F32_ROOT = Path("/tmp/sentinel_build/f32/f32-samples")
WP1_ROOT = Path("/tmp/sentinel_build/wp1/wp1-samples")
F31_ROOT = Path("/tmp/sentinel_build/f31/f31-samples")

needs_f32 = pytest.mark.skipif(
    not F32_ROOT.is_dir(), reason="f32-artifacts fixtures not staged")
needs_wp1 = pytest.mark.skipif(
    not (WP1_ROOT / "youtube_candidate_5dot1.mp4").is_file(),
    reason="wp1-samples not staged")
needs_f31 = pytest.mark.skipif(
    not (F31_ROOT / "ffmpeg-remux.mp4").is_file(),
    reason="f31-samples not staged")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _finding_ids(path: Path) -> set[str]:
    from sentinel.engine import validate
    return {f.check_id for f in validate(str(path)).findings}


def _extracted_stream(path: Path) -> bytes:
    from sentinel.container.mp4 import extract_iamf_stream, parse_mp4
    data = path.read_bytes()
    return extract_iamf_stream(data, parse_mp4(data))


# ---- plan structure (always runs) ------------------------------------------

def _steps_for(plan, out: str):
    tp = next(t for t in plan.targets if t.out == out)
    by_id = {s.id: s for s in plan.steps}
    return [by_id[sid] for sid in tp.step_ids]


def test_mp4box_route_compiles_repair_step(project):
    from .conftest import compile_text
    mf = project(
        (Path(__file__).parent / "fixtures" / "manifests"
         / "5dot1_youtube_mp4.yaml").read_text(encoding="utf-8"),
        {"wavs/main.wav": 6}, extra_files={"v.mp4": fake_mp4()})
    plan = compile_text(mf)
    steps = _steps_for(plan, "dist/youtube_candidate.mp4")
    kinds = [s.kind for s in steps]
    assert "repair_stts" in kinds, kinds
    i = kinds.index("repair_stts")
    assert kinds[i - 1] == "remux", kinds
    rep = steps[i]
    assert rep.tool == "internal"
    assert rep.params["out"] == "dist/youtube_candidate.mp4"
    assert rep.params["path"].endswith("dist/youtube_candidate.mp4")
    # in-place: the repair writes exactly what the remux wrote
    assert rep.writes == steps[i - 1].writes


def test_oneshot_route_has_no_repair_step(project):
    from .conftest import compile_text
    mf = project(
        (Path(__file__).parent / "fixtures" / "manifests"
         / "5dot1_opus_mp4_oneshot.yaml").read_text(encoding="utf-8"),
        {"wavs/main.wav": 6}, extra_files={"v.mp4": fake_mp4()})
    plan = compile_text(mf)
    kinds = {s.kind for s in plan.steps}
    # ADR-1: the FFmpeg encode path writes the trim correctly (doc 57);
    # nothing to repair, so nothing may claim to.
    assert "repair_stts" not in kinds


def test_executor_surfaces_repair_error(tmp_path):
    """Doc-35 discipline: a RepairError is a failed step, never silence."""
    from loom.executor import ExecutionError, Executor
    from loom.plan import Step
    bad = tmp_path / "not-an-mp4.mp4"
    bad.write_bytes(b"\x00" * 64)
    ex = Executor.__new__(Executor)          # handler-only harness
    ex.toolchain = None
    ex.work_dir = tmp_path
    ex.out_dir = tmp_path
    ex.manifest_dir = tmp_path
    ex.ledger = {"stts_repair": {}}
    step = Step(id="t-repair", kind="repair_stts", tool="internal",
                params={"out": "x.mp4", "path": str(bad)})
    with pytest.raises(ExecutionError, match="stts repair"):
        ex._step_repair_stts(step, {})


# ---- archived fixtures (doc 60 matrix / wp1 / f31) --------------------------

@needs_f32
def test_repair_fresh_reproduces_conform_byte_for_byte(tmp_path):
    """The doc-60 discriminator: repaired `fresh` == archived `conform`."""
    p = tmp_path / "fresh.mp4"
    shutil.copyfile(F32_ROOT / "mp4box-fresh.mp4", p)
    rec = repair_stts(p)
    assert rec["repaired"] is True
    assert (rec["sum_before"], rec["model_sum"]) == (479_352, 480_312)
    assert (rec["start_trim"], rec["end_trim"]) == (312, 648)
    assert _sha(p) == _sha(F32_ROOT / "mp4box-conform.mp4")


@needs_f32
def test_repair_is_idempotent_and_noops_on_conformant(tmp_path):
    """The removal-trigger property: a conformant table (gpac fixed, or a
    second pass) is a recorded no-op, byte-identical in and out."""
    p = tmp_path / "conform.mp4"
    shutil.copyfile(F32_ROOT / "mp4box-conform.mp4", p)
    before = _sha(p)
    rec = repair_stts(p)
    assert rec["repaired"] is False
    assert _sha(p) == before


@needs_f32
def test_repair_refuses_missing_elst(tmp_path):
    """Trimmed essence without an elst is S-407 territory (gate FAIL),
    not a table repair — assert loudly, touch nothing."""
    p = tmp_path / "noedts.mp4"
    shutil.copyfile(F32_ROOT / "mp4box-noedts.mp4", p)
    before = _sha(p)
    with pytest.raises(RepairError, match="no elst"):
        repair_stts(p)
    assert _sha(p) == before        # refused = untouched


@needs_f32
def test_repaired_output_is_sentinel_clean(tmp_path):
    p = tmp_path / "fresh.mp4"
    shutil.copyfile(F32_ROOT / "mp4box-fresh.mp4", p)
    assert "S-408" in _finding_ids(p)
    repair_stts(p)
    ids = _finding_ids(p)
    assert "S-408" not in ids
    assert "S-407" not in ids


@needs_wp1
def test_repair_video_bearing_wp1_candidate(tmp_path):
    """The wp1 candidate carries an h264 track: exercises iacb-trak
    selection and the mvhd max() coherence; essence must be untouched."""
    src = WP1_ROOT / "youtube_candidate_5dot1.mp4"
    p = tmp_path / "yt.mp4"
    shutil.copyfile(src, p)
    stream_before = _extracted_stream(p)
    rec = repair_stts(p)
    assert rec["repaired"] is True
    assert (rec["sum_before"], rec["model_sum"]) == (479_352, 480_312)
    assert p.stat().st_size == src.stat().st_size      # size-preserving
    assert _extracted_stream(p) == stream_before        # essence untouched
    ids = _finding_ids(p)
    assert "S-408" not in ids and "S-407" not in ids


@needs_f31
def test_repair_refuses_ffmpeg_stripped_remux(tmp_path):
    """FFmpeg's remux stripped the START-trim (F31) but the end-trim rides
    `discard_padding` and survives the copy (doc 57) — so the essence
    declares (0, 648) against a uniform 501x960 table. Fixing THAT shape
    needs a second stts run (+8 bytes): not byte-size-preserving, outside
    the adjudicated F32 form, and not a file Loom ever produces (its remux
    is MP4Box). Loud refusal, bytes untouched — S-408/S-409 stay the
    flags for what FFmpeg destroyed."""
    p = tmp_path / "ffmpeg-remux.mp4"
    shutil.copyfile(F31_ROOT / "ffmpeg-remux.mp4", p)
    before = _sha(p)
    with pytest.raises(RepairError, match="end-trim remainder"):
        repair_stts(p)
    assert _sha(p) == before        # refused = untouched
