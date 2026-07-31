"""Loom CLI contract tests — compile / run / batch / version arms that the
explain tests (test_explain.py) don't reach. Toolchain-free: `run` and `batch`
are exercised only for their error contracts against a bogus toolchain root;
the executed halves live in the *_toolchain suites.
"""

from __future__ import annotations

import pytest

from loom import __version__
from loom.cli import main

from .conftest import write_wav

MANIFEST = (
    "loom: 0\ntitle: T\n"
    "sources:\n  main: { path: wavs/main.wav, kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: dist/x.iamf }\n"
)


@pytest.fixture
def manifest(tmp_path):
    write_wav(tmp_path / "wavs/main.wav", 2)
    mf = tmp_path / "manifest.yaml"
    mf.write_text(MANIFEST, encoding="utf-8")
    return mf


# -------------------------------------------------------------------- compile

def test_cli_compile_to_stdout(manifest, capsys):
    assert main(["compile", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert '"steps"' in out                        # the serialized plan


def test_cli_compile_to_file_reports_shape(manifest, tmp_path, capsys):
    out_path = tmp_path / "plan.json"
    assert main(["compile", str(manifest), "-o", str(out_path)]) == 0
    msg = capsys.readouterr().out
    assert f"plan written: {out_path}" in msg
    assert "steps" in msg and "targets" in msg
    assert out_path.read_text(encoding="utf-8").startswith("{")


def test_cli_compile_error_lists_diagnostics(tmp_path, capsys):
    mf = tmp_path / "manifest.yaml"
    mf.write_text("loom: 0\n", encoding="utf-8")                     # missing everything
    assert main(["compile", str(mf)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "M-" in err                             # stable diagnostic codes


# ------------------------------------------------------------------------ run

def test_cli_run_missing_toolchain_is_actionable(manifest, tmp_path, capsys):
    rc = main(["run", str(manifest), "--out-dir", str(tmp_path / "out"),
               "--toolchain", "/nonexistent-toolchain"])
    cap = capsys.readouterr()
    assert rc == 2
    assert "/nonexistent-toolchain" in cap.err     # names the missing root
    assert "ledger: " in cap.out                   # ledger written even on failure
    ledger_line = [ln for ln in cap.out.splitlines()
                   if ln.startswith("ledger: ")][0]
    from pathlib import Path
    assert Path(ledger_line.removeprefix("ledger: ")).is_file()


def test_cli_run_compile_error_short_circuits(tmp_path, capsys):
    mf = tmp_path / "manifest.yaml"
    mf.write_text("loom: 0\n", encoding="utf-8")
    assert main(["run", str(mf)]) == 2
    cap = capsys.readouterr()
    assert cap.err.startswith("error: ")
    assert "ledger:" not in cap.out                # nothing ran


# ---------------------------------------------------------------------- batch

def test_cli_batch_bad_spec_is_m420(tmp_path, capsys):
    bf = tmp_path / "batch.yaml"
    bf.write_text("jobs: []\n", encoding="utf-8")                    # missing loom_batch header
    assert main(["batch", str(bf)]) == 2
    assert "M-420" in capsys.readouterr().err


def test_cli_batch_missing_toolchain_writes_ledger(tmp_path, capsys):
    (tmp_path / "tpl.yaml").write_text(
        "loom: 0\ntitle: \"{title}\"\n"
        "sources:\n  main: { path: \"wavs/{title}.wav\", kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "targets:\n  - { format: iamf, out: \"dist/{title}.iamf\" }\n", encoding="utf-8")
    write_wav(tmp_path / "wavs/ep01.wav", 2)
    bf = tmp_path / "batch.yaml"
    bf.write_text("loom_batch: 0\nmanifest: tpl.yaml\n"
                  "defaults: { out_dir: \"out/{title}\" }\n"
                  "jobs:\n  - { vars: { title: ep01 } }\n", encoding="utf-8")
    rc = main(["batch", str(bf), "--no-cache",
               "--state-dir", str(tmp_path / "state"),
               "--toolchain", "/nonexistent-toolchain"])
    cap = capsys.readouterr()
    assert rc == 2                                 # the job fails, the run reports
    assert "batch ledger: " in cap.out


# -------------------------------------------------------------------- version

def test_cli_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"loom {__version__}"
