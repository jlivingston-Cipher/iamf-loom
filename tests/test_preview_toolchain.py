"""R9 (doc 46) executed accepts, toolchain-gated (E-Y4/E-Y5/E-Y6): the
binaural render is real (not a stereo downmix), structurally exact,
deterministic, cache/batch-safe, and composes with normalize."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from loom.compiler import compile_manifest
from loom.executor import Executor
from loom.manifest import load_manifest
from loom.wavinfo import read_wav_info

from .conftest import needs_toolchain, toolchain_root, write_wav
from .test_parity_toolchain import FRAMES

PREVIEW_714 = (
    "loom: 0\ntitle: prev\n"
    "sources:\n  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
    "elements:\n  bed: { from: main }\n"
    "presentations:\n"
    "  - id: main\n"
    "    elements: [ { ref: bed, headphones: binaural } ]\n"
    "targets:\n  - { format: preview, out: review/a.wav }\n"
)


def run_manifest(tmp_path, text, wavs, frames=FRAMES):
    for rel, ch in wavs.items():
        write_wav(tmp_path / rel, ch, frames=frames)
    mf = tmp_path / "manifest.yaml"
    mf.write_text(text)
    m = load_manifest(mf)
    plan = compile_manifest(m)
    ex = Executor(plan, m.manifest_dir, tmp_path / "out", tmp_path / "work",
                  toolchain=str(toolchain_root()),
                  validate_policy=m.policy.validate)
    return m, plan, ex, ex.run()


# -- E-Y4: the wav preview, structurally exact + gated on the intermediate ----

@needs_toolchain
def test_preview_wav_render_accept(tmp_path):
    _m, _plan, ex, res = run_manifest(tmp_path, PREVIEW_714,
                                      {"main.wav": 12})
    assert res.ok, res.failures
    out = tmp_path / "out/review/a.wav"
    wi = read_wav_info(out)
    assert wi.channels == 2
    assert wi.sample_rate == 48000
    assert wi.frames == FRAMES          # sample-exact (WP1 precedent)

    prev = list(ex.ledger["preview"].values())
    assert len(prev) == 1 and prev[0]["ok"]
    assert prev[0]["il_lufs"] > -70.0
    assert len(prev[0]["per_channel_peak_dbfs"]) == 2
    for pk in prev[0]["per_channel_peak_dbfs"]:
        assert pk > -90.0               # both channels carry program
    assert "F10" in prev[0]["oracle_note"]

    gate = ex.ledger["gate"]["review/a.wav"]
    assert gate["gated_path"].endswith(".iamf")
    assert "render of gate-validated bytes" in gate["note"]
    assert gate.get("fail_ids") in (None, [],)  # 0 FAIL findings


# -- E-Y4/D-Y7: binaural differs from the 2.0 downmix -------------------------

@needs_toolchain
def test_binaural_is_not_a_stereo_downmix(tmp_path):
    _m, _plan, ex, res = run_manifest(tmp_path, PREVIEW_714,
                                      {"main.wav": 12})
    assert res.ok, res.failures
    binaural = tmp_path / "out/review/a.wav"
    intermediate = Path(str(ex.ledger["gate"]["review/a.wav"]["gated_path"])
                        .replace("$WORK", str(tmp_path / "work")))
    assert intermediate.is_file()
    downmix = tmp_path / "downmix20.wav"
    root = toolchain_root()
    r = subprocess.run(
        [str(root / "src/build-iamf/decoder_main"),
         f"--input_filename={intermediate}",
         f"--output_filename={downmix}", "--output_layout=2.0"],
        capture_output=True, text=True)
    assert r.returncode == 0, (r.stderr or r.stdout)[-400:]
    assert downmix.is_file() and downmix.stat().st_size > 0   # F8
    # same source, same intermediate, two renders: an HRTF render cannot
    # equal an ITU downmix on 7.1.4 identifier sines (D-Y7 — run-failing)
    assert binaural.read_bytes() != downmix.read_bytes()


# -- E-Y4: the opus preview ---------------------------------------------------

@needs_toolchain
def test_preview_opus_accept(tmp_path):
    text = PREVIEW_714.replace("review/a.wav", "review/a.opus")
    _m, _plan, ex, res = run_manifest(tmp_path, text, {"main.wav": 12})
    assert res.ok, res.failures
    out = tmp_path / "out/review/a.opus"
    assert out.is_file() and out.stat().st_size > 0

    # decodes to 2ch/48k, duration within one Opus frame of the essence
    dec = tmp_path / "dec.wav"
    ff = toolchain_root() / "bin/ffmpeg-install/bin/ffmpeg"
    r = subprocess.run([str(ff), "-y", "-hide_banner", "-i", str(out),
                        str(dec)], capture_output=True, text=True)
    assert r.returncode == 0, (r.stderr or r.stdout)[-400:]
    wi = read_wav_info(dec)
    assert wi.channels == 2 and wi.sample_rate == 48000
    assert abs(wi.frames - FRAMES) <= 960   # one Opus frame @48k

    # -bitexact: no version-numbered vendor/encoder tag in the deliverable
    blob = out.read_bytes()
    assert not re.search(rb"Lav[cf]\d|GPAC|libavformat \d", blob), \
        "version-bearing tag survived -bitexact"

    # the verify record bound on the pre-encode WAV (container noted)
    prev = list(ex.ledger["preview"].values())
    assert prev[0]["container"] == "opus" and prev[0]["ok"]


# -- E-Y5: determinism + cache/batch ------------------------------------------

@needs_toolchain
@pytest.mark.parametrize("ext", ["wav", "opus"])
def test_preview_deterministic_across_runs(tmp_path, ext):
    text = PREVIEW_714.replace("review/a.wav", f"review/a.{ext}")
    outs = []
    for sub in ("r1", "r2"):
        d = tmp_path / sub
        d.mkdir()
        _m, _plan, _ex, res = run_manifest(d, text, {"main.wav": 12})
        assert res.ok, res.failures
        outs.append((d / f"out/review/a.{ext}").read_bytes())
    assert outs[0] == outs[1]


@needs_toolchain
def test_preview_batch_cache_replay(tmp_path):
    (tmp_path / "wavs").mkdir(parents=True)
    write_wav(tmp_path / "wavs/main.wav", 12, frames=FRAMES)
    (tmp_path / "tpl.yaml").write_text(
        "loom: 0\ntitle: \"{title}\"\n"
        "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "presentations:\n"
        "  - id: main\n"
        "    elements: [ { ref: bed, headphones: binaural } ]\n"
        "targets:\n"
        "  - out: \"review/{title}.binaural.wav\"\n"
        "    format: preview\n")
    (tmp_path / "batch.yaml").write_text(
        "loom_batch: 0\nmanifest: tpl.yaml\n"
        "defaults: { out_dir: \"dist/{title}\" }\n"
        "jobs:\n"
        "  - { id: a, vars: { title: ta } }\n"
        "  - { id: b, vars: { title: tb } }\n")
    import json as _json

    from loom.batch import load_batch, run_batch
    state = tmp_path / "state"
    spec = load_batch(tmp_path / "batch.yaml")
    rc1, _lp = run_batch(spec, workers=2, state_dir=state,
                         toolchain=str(toolchain_root()))
    assert rc1 == 0
    outs = sorted((tmp_path / "dist").rglob("*.binaural.wav"))
    assert len(outs) == 2
    first = {p.name: p.read_bytes() for p in outs}
    # wipe outputs + journal, keep cache: every target must replay as a hit
    for p in outs:
        p.unlink()
    (state / "journal.jsonl").unlink()
    rc2, lp = run_batch(spec, workers=2, state_dir=state,
                        toolchain=str(toolchain_root()))
    assert rc2 == 0
    ledger = _json.loads(lp.read_text())
    assert ledger["totals"]["cache_hits"] == 2
    assert ledger["totals"]["cache_misses"] == 0
    for job in ledger["jobs"]:
        assert all(v == "hit" for v in job["cache"].values())
        for gate in job["gate"].values():
            assert gate.get("cached") is True
            assert "render of gate-validated bytes" in gate.get("note", "")
    for p in sorted((tmp_path / "dist").rglob("*.binaural.wav")):
        assert p.read_bytes() == first[p.name]


# -- E-Y6: normalize composition ----------------------------------------------

@needs_toolchain
def test_preview_renders_normalized_program(tmp_path):
    text = (
        "loom: 0\ntitle: prevnorm\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: \"7.1.4\" }\n"
        "elements:\n  bed: { from: main }\n"
        "presentations:\n"
        "  - id: main\n"
        "    elements: [ { ref: bed, headphones: binaural } ]\n"
        "policy:\n  loudness: { mode: measure, normalize: -16 }\n"
        "targets:\n  - { format: preview, out: review/a.wav }\n"
    )
    _m, _plan, ex, res = run_manifest(tmp_path, text, {"main.wav": 12})
    assert res.ok, res.failures
    ver = [v for v in ex.ledger["normalize"].values()
           if "within_tolerance" in v]
    assert ver and ver[0]["within_tolerance"]     # existing machinery held
    prev = list(ex.ledger["preview"].values())[0]
    assert prev["ok"]
    # recorded as an actual, never pinned: binaural IL != anchor IL by design
    assert isinstance(prev["il_lufs"], float)
