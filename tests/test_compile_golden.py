"""E-L2 + E-L4: the WP1 matrix compiles from manifests with zero hand-typed
metadata; plans are deterministic (byte-identical across compiles) and match
committed golden snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import compile_text, fake_mp4

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
MANIFEST_DIR = Path(__file__).parent / "fixtures" / "manifests"

# (name, wavs, extra_files) — the WP1 matrix expressed as manifests
CASES = [
    ("stereo_opus_iamf", {"wavs/main.wav": 2}, {}),
    ("5dot1_opus_iamf", {"wavs/main.wav": 6}, {}),
    ("7dot1dot4_opus_iamf", {"wavs/main.wav": 12}, {}),
    ("foa_opus_iamf", {"wavs/amb.wav": 4}, {}),
    ("3oa_opus_iamf", {"wavs/amb.wav": 16}, {}),
    ("stereo_lpcm_iamf", {"wavs/main.wav": 2}, {}),
    ("stereo_opus_mp4_oneshot", {"wavs/main.wav": 2}, {"v.mp4": fake_mp4()}),
    ("5dot1_opus_mp4_oneshot", {"wavs/main.wav": 6}, {"v.mp4": fake_mp4()}),
    ("7dot1dot4_opus_mp4_oneshot", {"wavs/main.wav": 12}, {"v.mp4": fake_mp4()}),
    ("5dot1_youtube_mp4", {"wavs/main.wav": 6}, {"v.mp4": fake_mp4()}),
    ("7dot1dot4_multi_presentation", {"wavs/main.wav": 12}, {}),
    # Phase 2 (doc 43): R3 normalize chains + the R4 archive mezzanine
    ("normalize_7dot1dot4_iamf", {"wavs/main.wav": 12}, {}),
    ("normalize_5dot1_oneshot", {"wavs/main.wav": 6}, {"v.mp4": fake_mp4()}),
    ("archive_flac_5dot1_iamf", {"wavs/main.wav": 6}, {}),
    # R9 (doc 46): binaural preview targets — appended, so the 14 existing
    # goldens' bytes and parametrization order are untouched (D-Y1)
    ("preview_7dot1dot4_wav", {"wavs/main.wav": 12}, {}),
    ("preview_stereo_opus", {"wavs/main.wav": 2}, {}),
    ("preview_normalize_7dot1dot4", {"wavs/main.wav": 12}, {}),
    # R8 (doc 47): multi-language presentation expansion — appended, so the
    # 17 existing goldens' bytes and parametrization order are untouched
    ("multilang_3lang_iamf",
     {"wavs/main.wav": 12, "wavs/vo_en.wav": 2, "wavs/vo_de.wav": 2,
      "wavs/vo_fr.wav": 2}, {}),
    ("multilang_preview_wav",
     {"wavs/main.wav": 12, "wavs/vo_en.wav": 2, "wavs/vo_de.wav": 2}, {}),
    ("multilang_normalize_iamf",
     {"wavs/main.wav": 12, "wavs/vo_en.wav": 2, "wavs/vo_de.wav": 2}, {}),
]


def _compile_case(project, name, wavs, extra):
    text = (MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    mf = project(text, wavs, extra_files=extra, name=f"{name}.yaml")
    return compile_text(mf)


@pytest.mark.parametrize("name,wavs,extra", CASES)
def test_golden_plan(project, name, wavs, extra):
    plan = _compile_case(project, name, wavs, extra)
    got = plan.dumps()
    golden_path = GOLDEN_DIR / f"{name}.plan.json"
    assert golden_path.is_file(), f"golden missing: {golden_path.name}"
    assert got == golden_path.read_text(encoding="utf-8"), (
        f"plan for {name} deviates from the golden snapshot"
    )


def test_manifest_fixture_materializes_lf_only(project):
    """Named regression (doc 97 §3.2) — cross-platform golden identity.

    `manifest_sha256` is taken over the manifest file's RAW BYTES, and every
    committed golden carries the LF-form hash. Default text-mode writes emit
    CRLF on Windows, which silently re-hashes the manifest: under a simulated
    Windows write, 40 of the 51 golden/explain assertions failed. The fixture
    therefore pins the newline explicitly, and this test is the guard that
    keeps it pinned.
    """
    name = CASES[0][0]
    text = (MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    mf = project(text, CASES[0][1], name=f"{name}.yaml")
    raw = mf.read_bytes()
    assert b"\r" not in raw, (
        "manifest fixture wrote CR bytes — manifest_sha256 hashes raw bytes, "
        "so the committed plan goldens would not reproduce on this platform"
    )


@pytest.mark.parametrize("name,wavs,extra", CASES[:3])
def test_determinism_compile_twice(project, name, wavs, extra):
    text = (MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    mf = project(text, wavs, extra_files=extra, name=f"{name}.yaml")
    assert compile_text(mf).dumps() == compile_text(mf).dumps()


def test_zero_hand_typed_metadata_in_manifests():
    """The manifests themselves contain no loudness numbers, no substream
    ids/orders, no codec strings beyond policy names (ADR-5, E-L2)."""
    for name, _w, _e in CASES:
        text = (MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8").lower()
        for forbidden in ("integrated_loudness", "digital_peak", "true_peak",
                          "substream", "streamid", "iamf.001",
                          "parameter_rate"):
            assert forbidden not in text, f"{name}: hand-typed {forbidden}"


def test_golden_plans_have_no_absolute_paths():
    """Plans are environment-portable: only $-tokens and relative paths."""
    for name, _w, _e in CASES:
        doc = json.loads((GOLDEN_DIR / f"{name}.plan.json").read_text(encoding="utf-8"))
        for step in doc["steps"]:
            for a in step.get("argv", []) + step.get("argv_secondary", []):
                assert not a.startswith("/"), f"{name}/{step['id']}: {a}"
