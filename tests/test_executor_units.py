"""Executor unit tests — the pure/in-process halves that need no toolchain:
kernel-JSON parsing, token-resolution error paths, the copy step, the
verify_loudness bitstream read-back arm, and the unconfigured-toolchain
contract. The subprocess halves live in the *_toolchain suites.
"""

from __future__ import annotations

import pytest

from loom.executor import ExecutionError, Executor, _parse_kernel_json
from loom.plan import Plan, Step

# The core's fixture builders make real .iamf bytes for the read-back arm;
# they live in the iamf-sentinel SOURCE tree, not in the installed wheel.
from .conftest import NO_OSS_SRC_REASON, OSS_SRC       # noqa: E402

if OSS_SRC is None:                                    # pragma: no cover
    pytest.skip(NO_OSS_SRC_REASON, allow_module_level=True)
from fixtures.build import build, channel_spec        # noqa: E402


def _executor(tmp_path, toolchain=None):
    plan = Plan(title="T", manifest_sha256="0" * 64)
    return Executor(plan, tmp_path, tmp_path / "out", tmp_path / "work",
                    toolchain=toolchain)


# ---------------------------------------------------------- kernel JSON parse

def test_parse_kernel_json_happy():
    doc = ('{"integrated_lufs": -18.5, "digital_peak_dbfs": -6.0, '
           '"true_peak_dbtp": -5.5}')
    assert _parse_kernel_json(doc) == {"il": -18.5, "dp": -6.0, "tp": -5.5}


def test_parse_kernel_json_unparsable():
    with pytest.raises(ExecutionError, match="unparsable JSON"):
        _parse_kernel_json("not json at all")


def test_parse_kernel_json_missing_field():
    with pytest.raises(ExecutionError, match="missing measure field"):
        _parse_kernel_json('{"integrated_lufs": -18.5}')


# ------------------------------------------------------------- token errors

def test_measure_token_before_measure_step_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOM_TOOLCHAIN", raising=False)
    ex = _executor(tmp_path)
    with pytest.raises(ExecutionError, match="has not run"):
        ex._resolve("${measure:s1:il}")


def test_gain_token_before_measure_step_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOM_TOOLCHAIN", raising=False)
    ex = _executor(tmp_path)
    with pytest.raises(ExecutionError, match="gain token"):
        ex._resolve("volume=${gain:s1:-16}dB")


# ---------------------------------------------- unconfigured-toolchain contract

def test_unconfigured_toolchain_is_none_in_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOM_TOOLCHAIN", raising=False)
    monkeypatch.delenv("SENTINEL_TOOLCHAIN", raising=False)
    ex = _executor(tmp_path)
    assert ex.toolchain is None
    assert ex.ledger["toolchain"] is None
    # $TOOLCHAIN stays literal rather than becoming the string "None"
    assert "$TOOLCHAIN" in ex._resolve("$TOOLCHAIN/bin/ffmpeg")


def test_configured_toolchain_substitutes(tmp_path, monkeypatch):
    monkeypatch.delenv("SENTINEL_TOOLCHAIN", raising=False)
    monkeypatch.setenv("LOOM_TOOLCHAIN", "/tc-root")
    ex = _executor(tmp_path)
    assert ex.ledger["toolchain"] == "/tc-root"
    assert ex._resolve("$TOOLCHAIN/bin/ffmpeg") == "/tc-root/bin/ffmpeg"


# ------------------------------------------------------------------ copy step

def test_copy_step_creates_parents_and_records_ok(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"payload")
    ex = _executor(tmp_path)
    rec = ex._run_step(Step(id="s1", kind="copy", tool="internal",
                            src=str(src), dst=str(tmp_path / "deep/dir/out.bin")))
    assert rec["ok"] is True
    assert (tmp_path / "deep/dir/out.bin").read_bytes() == b"payload"


# ----------------------------------------- verify_loudness (bitstream arm)

def _iamf_file(tmp_path):
    p = tmp_path / "declared.iamf"
    p.write_bytes(build(channel_spec("stereo")))       # declares −18 LKFS stereo
    return p


def test_verify_loudness_bitstream_within_tolerance(tmp_path):
    ex = _executor(tmp_path)
    rec = ex._run_step(Step(
        id="v1", kind="verify_loudness", tool="internal",
        params={"method": "bitstream", "path": str(_iamf_file(tmp_path)),
                "target": -18.0, "tolerance": 0.5, "anchor": "stereo"}))
    assert rec["ok"] is True
    assert rec["method"].startswith("bitstream read-back")
    led = ex.ledger["normalize"]["v1"]
    assert led["within_tolerance"] is True and led["post_ride_il"] == -18.0


def test_verify_loudness_bitstream_miss_reports_delta(tmp_path):
    ex = _executor(tmp_path)
    rec = ex._run_step(Step(
        id="v1", kind="verify_loudness", tool="internal",
        params={"method": "bitstream", "path": str(_iamf_file(tmp_path)),
                "target": -14.0, "tolerance": 0.5, "anchor": "stereo"}))
    assert rec["ok"] is False
    assert "normalize missed" in rec["error"]
    assert ex.ledger["normalize"]["v1"]["within_tolerance"] is False


def test_verify_loudness_missing_anchor_layout_errors(tmp_path):
    ex = _executor(tmp_path)
    with pytest.raises(ExecutionError, match="no 7.1.4"):
        ex._run_step(Step(
            id="v1", kind="verify_loudness", tool="internal",
            params={"method": "bitstream", "path": str(_iamf_file(tmp_path)),
                    "target": -18.0, "tolerance": 0.5, "anchor": "7.1.4"}))


def test_verify_loudness_measured_arm_requires_measure_step(tmp_path):
    ex = _executor(tmp_path)
    with pytest.raises(ExecutionError, match="has not run"):
        ex._run_step(Step(
            id="v1", kind="verify_loudness", tool="internal",
            params={"method": "measured", "measure_step": "m1",
                    "target": -18.0, "tolerance": 0.5, "anchor": "stereo"}))
