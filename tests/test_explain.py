"""R10 `loom explain` (doc 45): golden snapshots, output contracts, content
assertions, CLI surface, error paths. All toolchain-free by construction
(rendering is a pure function of the compiled artifacts, D-X3)."""

from __future__ import annotations

import re

import pytest

from loom.compiler import compile_manifest
from loom.explain import render_explain
from loom.manifest import load_manifest

from .conftest import fake_mp4
from .test_compile_golden import CASES, GOLDEN_DIR, MANIFEST_DIR

EXPLAIN_DIR = GOLDEN_DIR.parent / "golden-explain"


def _explain_case(project, name, wavs, extra) -> str:
    text = (MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    mf = project(text, wavs, extra_files=extra, name=f"{name}.yaml")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    a = render_explain(m, plan)
    assert a == render_explain(m, plan), "render not deterministic"
    return a


@pytest.mark.parametrize("name,wavs,extra", CASES)
def test_explain_golden(project, name, wavs, extra):
    """E-X2: every golden manifest renders byte-identically to its committed
    explain snapshot (render-twice determinism asserted inside)."""
    got = _explain_case(project, name, wavs, extra)
    gp = EXPLAIN_DIR / f"{name}.explain.txt"
    assert gp.is_file(), f"explain golden missing: {gp.name}"
    assert got == gp.read_text(encoding="utf-8"), (
        f"explain for {name} deviates from the golden snapshot")


def test_output_contracts():
    """E-X4: no version string, no absolute paths (only $-tokens), and every
    target block carries backend + non-empty routing rationale (E-X3)."""
    from loom import __version__
    for name, _w, _e in CASES:
        text = (EXPLAIN_DIR / f"{name}.explain.txt").read_text(encoding="utf-8")
        assert __version__ not in text, f"{name}: version string leaked"
        assert not re.search(r"(?i)loom \d+\.\d+", text), (
            f"{name}: version-shaped string leaked")
        for token in re.split(r"\s+", text):
            if token == "/":       # the "$TOOLCHAIN / $WORK / …" notes line
                continue
            assert not token.startswith("/"), (
                f"{name}: absolute path leaked: {token}")
        assert "backend:" in text and "why this route:" in text
        # every TARGET block has a non-empty rationale paragraph
        for block in re.split(r"\nTARGET ", text)[1:]:
            why = block.split("why this route:\n", 1)[1]
            assert why.splitlines()[0].strip(), f"{name}: empty rationale"


def test_content_assertions():
    """E-X3 spot checks on the golden set: routing reasons, blocker lists,
    the R3 chain, tokens + legend."""
    yt = (EXPLAIN_DIR / "5dot1_youtube_mp4.explain.txt").read_text(encoding="utf-8")
    assert "G11" in yt and "F31" in yt         # why iamf-tools+MP4Box, not oneshot
    assert "F5" not in yt                      # doc-56-refuted framing stays out
    assert "backend: iamftools + mp4box mux" in yt

    nz = (EXPLAIN_DIR / "normalize_5dot1_oneshot.explain.txt").read_text(encoding="utf-8")
    assert "normalize: -14 LUFS" in nz and "+/-0.3 LU" in nz
    assert "${gain:" in nz and "${measure:" in nz
    assert "gain_ride" in nz and "verify_loudness" in nz
    assert "resolves at execution:" in nz
    assert "data-dependent plan edges" in nz    # token legend present

    yt_steps = yt.split("steps (execution order):", 1)[1]
    assert "[remux, mp4box]" in yt_steps        # ADR-2 route visible as a step

    ar = (EXPLAIN_DIR / "archive_flac_5dot1_iamf.explain.txt").read_text(encoding="utf-8")
    assert "codec: flac" in ar
    assert "raw .iamf: iamf-tools primary" in ar
    assert "preset: archive" in ar

    simple = (EXPLAIN_DIR / "stereo_opus_iamf.explain.txt").read_text(encoding="utf-8")
    assert "data-dependent plan edges" not in simple   # no tokens -> no legend
    assert "$TOOLCHAIN / $WORK / $OUTDIR / $SRCDIR" in simple  # env note always

    # the gate policy line renders on every fixture (validate defaults on)
    assert "Sentinel gate" in simple and "S-320/S-321" in simple


TEMPLATE = """\
loom: 0
title: "{title} explain"
sources:
  main: { path: wavs/main.wav, kind: bed, layout: stereo }
elements:
  bed: { from: main }
presentations:
  - id: main
    elements: [ { ref: bed } ]
targets:
  - { format: iamf, out: "dist/{title}.iamf" }
"""

MULTI_PRES_MP4 = """\
loom: 0
title: multi-pres AV
sources:
  main: { path: wavs/main.wav, kind: bed, layout: "5.1" }
elements:
  bed: { from: main }
presentations:
  - id: main
    elements: [ { ref: bed } ]
  - id: alt
    annotations: { en-us: "Alt Mix" }
    elements: [ { ref: bed, gain_db: -2 } ]
targets:
  - { format: mp4, out: dist/av.mp4, video: v.mp4 }
"""


def test_blocker_list_named_in_explain(project):
    """E-X3: an mp4 target that routing declined the one-shot for carries the
    blocker list -- here, the presentation count -- in its rationale."""
    mf = project(MULTI_PRES_MP4, {"wavs/main.wav": 6},
                 extra_files={"v.mp4": fake_mp4()}, name="mp.yaml")
    m = load_manifest(mf)
    plan = compile_manifest(m)
    text = render_explain(m, plan)
    assert "2 presentations declared" in text
    assert "backend: iamftools + mp4box mux" in text


def test_var_equivalence(project, tmp_path):
    """E-X4: a {var}-bound template and its literally-substituted twin render
    identically apart from the manifest sha256 line (different manifest
    bytes, same compiled semantics)."""
    mf_t = project(TEMPLATE, {"wavs/main.wav": 2}, name="tpl.yaml")
    m_t = load_manifest(mf_t, variables={"title": "ep01"})
    text_lit = TEMPLATE.replace("{title}", "ep01")
    mf_l = project(text_lit, {"wavs/main.wav": 2}, name="lit.yaml")
    m_l = load_manifest(mf_l)

    def strip_sha(s: str) -> str:
        return "\n".join(ln for ln in s.splitlines()
                         if not ln.startswith("manifest sha256:"))

    a = strip_sha(render_explain(m_t, compile_manifest(m_t)))
    b = strip_sha(render_explain(m_l, compile_manifest(m_l)))
    assert a == b


def test_cli_explain(project, tmp_path, capsys):
    """D-X2: stdout by default; -o writes the file; --var binds."""
    from loom.cli import main
    text = (MANIFEST_DIR / "stereo_opus_iamf.yaml").read_text(encoding="utf-8")
    mf = project(text, {"wavs/main.wav": 2}, name="m.yaml")
    rc = main(["explain", str(mf)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("LOOM EXPLAIN -- ")
    dst = tmp_path / "out.txt"
    rc = main(["explain", str(mf), "-o", str(dst)])
    assert rc == 0
    assert dst.read_text(encoding="utf-8") == out


def test_cli_explain_errors(project, capsys):
    """E-X5: compile rejections surface identically to `loom compile` --
    same M- codes, rc 2, zero tracebacks, nothing rendered."""
    from loom.cli import main
    # garbage YAML -> M-102
    mf = project(":\n  - ][", {}, name="bad.yaml")
    rc = main(["explain", str(mf)])
    cap = capsys.readouterr()
    assert rc == 2 and "M-102" in cap.err and cap.out == ""
    # unbound {variable} -> M-413 naming variable + dotted path
    mf2 = project(TEMPLATE, {"wavs/main.wav": 2}, name="tpl.yaml")
    rc = main(["explain", str(mf2)])
    cap = capsys.readouterr()
    assert rc == 2 and "M-413" in cap.err and "title" in cap.err
    assert cap.out == ""
