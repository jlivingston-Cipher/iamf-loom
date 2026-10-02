"""The toolchain requirement switch (conftest, LOOM_REQUIRE_TOOLCHAIN).

Every encode test sits behind `needs_toolchain` and skips when no encoder
resolves, and the suite still exits 0. On CI that is the design; on a machine
that was meant to test encodes it is a green run that tested none. The switch
turns that state into a hard error, and only when asked.

Four directions are pinned, because a guard is only as trustworthy as the
evidence that it fires, that it stays out of the way, that it is satisfied by
the thing it asks for, and that CI's own flag does not switch it on:

- switch on, no encoder anywhere        -> the run FAILS and says where it looked
- switch off (unset or falsey)           -> the run is green, nothing said
- switch on, a complete toolchain found  -> the run is green
- CI's shape (full collection demanded, no toolchain, switch unset) -> green

An encoder is not a complete toolchain. With the switch on the run is also
refused when the root has its encoder but lacks the decoder, the loudness
kernel, FFmpeg, MP4Box, a video donor or numpy -- each hidden in turn below,
each named in the refusal with the place it was looked for -- because a suite
that passes with the switch on is meant to have run every encode path.

Method, as in test_collection_guard.py: pytest runs in a subprocess against a
synthetic repo root carrying a copy of the real conftest and one probe test,
so this repo's siblings and the caller's environment cannot leak in. The child
environment names its toolchain explicitly every time; the one test that
exercises the built-in default is skipped where that default really exists.
The child's PATH is an empty directory, so a kernel or an MP4Box installed on
the host cannot stand in for one hidden from the root, and its numpy is a stub
this file writes, so the result does not depend on whether the host has numpy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import (
    DECODER_REL,
    DEFAULT_TOOLCHAIN,
    ENCODER_REL,
    FFMPEG_REL,
    INCOMPLETE_TOOLCHAIN_ERROR,
    KERNEL_REL,
    MISSING_TOOLCHAIN_ERROR,
    MP4BOX_REL,
    REQUIRE_FULL_COLLECTION_ENV,
    REQUIRE_TOOLCHAIN_ENV,
    VIDEO_ENV,
    fake_mp4,
    incomplete_toolchain_error,
    missing_components,
    missing_toolchain_error,
    require_toolchain,
    toolchain_root,
)

_REAL_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_PROBE = "def test_probe():\n    assert True\n"

_REFUSAL_HEAD = MISSING_TOOLCHAIN_ERROR.splitlines()[0].format(
    var=REQUIRE_TOOLCHAIN_ENV)

# The incomplete-toolchain refusal: the end of its first line, which no other
# message carries.
_INCOMPLETE_MARK = INCOMPLETE_TOOLCHAIN_ERROR.splitlines()[0].split("{root} ")[1]

# Every part past the encoder: where it lives under the root (None when it does
# not live there), and the name the refusal gives it.
_PARTS = {
    "decoder": (DECODER_REL, "decoder_main"),
    "kernel": (KERNEL_REL, "sentinel-dsp, the loudness kernel"),
    "ffmpeg": (FFMPEG_REL, "ffmpeg"),
    "mp4box": (MP4BOX_REL, "MP4Box"),
    "video": (None, "a video donor"),
    "numpy": (None, "numpy"),
}

_UNSET = object()


def _synthetic_repo(tmp_path: Path, *, with_core_source: bool = False) -> Path:
    """A repo root with one passing probe test and a copy of the real conftest."""
    root = tmp_path / "loom"
    (root / "tests").mkdir(parents=True)
    shutil.copy2(_REAL_CONFTEST, root / "tests" / "conftest.py")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_probe.py").write_text(_PROBE, encoding="utf-8")
    if with_core_source:
        fixtures = tmp_path / "iamf-sentinel" / "fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "build.py").write_text("", encoding="utf-8")
    return root


def _tool(path: Path) -> Path:
    """An executable stand-in for a toolchain binary. It is never run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _toolchain(tmp_path: Path, *, encoder: bool, without=()) -> Path:
    """A toolchain root. With `encoder`, an executable encoder_main and every
    other binary the encode tests use, minus the parts named in `without`."""
    root = tmp_path / "toolchain"
    root.mkdir()
    if encoder:
        _tool(root / ENCODER_REL)
        for part, (rel, _name) in _PARTS.items():
            if rel is not None and part not in without:
                _tool(root / rel)
    return root


def _numpy_stub(tmp_path: Path, *, importable: bool) -> Path:
    """A directory whose `numpy` imports cleanly, or refuses to import."""
    d = tmp_path / ("numpy-present" if importable else "numpy-hidden")
    (d / "numpy").mkdir(parents=True, exist_ok=True)
    (d / "numpy" / "__init__.py").write_text(
        "" if importable else "raise ImportError('numpy hidden by the test')\n",
        encoding="utf-8")
    return d


def _run(root: Path, *, switch, toolchain, full_collection=None,
         video: bool = True, numpy: bool = True, extra_env=None):
    """Run the synthetic suite with exactly the environment named here."""
    tmp = root.parent
    env = dict(os.environ)
    env["IAMF_SENTINEL_SRC"] = ""          # never inherit a real core checkout
    for var in (REQUIRE_TOOLCHAIN_ENV, REQUIRE_FULL_COLLECTION_ENV,
                "LOOM_TOOLCHAIN", "SENTINEL_TOOLCHAIN", "SENTINEL_DSP", VIDEO_ENV):
        env.pop(var, None)
    empty = tmp / "empty-path"
    empty.mkdir(exist_ok=True)
    env["PATH"] = str(empty)               # no host kernel, no host MP4Box
    stub = _numpy_stub(tmp, importable=numpy)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(stub)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    if video:
        donor = tmp / "donor.mp4"
        donor.write_bytes(fake_mp4())
        env[VIDEO_ENV] = str(donor)
    if switch is not None:
        env[REQUIRE_TOOLCHAIN_ENV] = switch
    if toolchain is not _UNSET:
        env["LOOM_TOOLCHAIN"] = str(toolchain)
    if full_collection is not None:
        env[REQUIRE_FULL_COLLECTION_ENV] = full_collection
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    return proc, proc.stdout + proc.stderr


def test_switch_refuses_a_run_with_no_encoder(tmp_path):
    """Switch on, toolchain root without an encoder: FAIL, naming the path."""
    tc = _toolchain(tmp_path, encoder=False)
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=tc)

    assert proc.returncode != 0, (
        f"{REQUIRE_TOOLCHAIN_ENV}=1 with no encoder exited 0 -- the switch did "
        f"not fire:\n{out}")
    assert " passed" not in out, f"a 'passed' summary survived the switch:\n{out}"
    assert _REFUSAL_HEAD in out, out
    assert str(tc / ENCODER_REL) in out, out
    assert "$LOOM_TOOLCHAIN" in out, out


@pytest.mark.skipif(toolchain_root({}) is not None,
                    reason="the built-in default toolchain exists on this machine")
def test_switch_names_the_default_when_nothing_is_set(tmp_path):
    """The macOS case as measured: no variable set, so the Linux default is used."""
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=_UNSET)

    assert proc.returncode != 0, out
    assert str(Path(DEFAULT_TOOLCHAIN) / ENCODER_REL) in out, out
    assert "neither $LOOM_TOOLCHAIN nor $SENTINEL_TOOLCHAIN is set" in out, out


@pytest.mark.parametrize("switch", [None, "0", "false"])
def test_switch_is_inert_unless_asked(tmp_path, switch):
    """Unset or falsey, a run without a toolchain is unchanged: green, silent."""
    tc = _toolchain(tmp_path, encoder=False)
    proc, out = _run(_synthetic_repo(tmp_path), switch=switch, toolchain=tc)

    assert proc.returncode == 0, f"switch={switch!r} broke a toolchain-less run:\n{out}"
    assert "1 passed" in out, out
    assert _REFUSAL_HEAD not in out, out


def test_switch_is_satisfied_by_a_complete_toolchain(tmp_path):
    """Switch on and every part present: the guard stays out of the way."""
    tc = _toolchain(tmp_path, encoder=True)
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=tc)

    assert proc.returncode == 0, f"the switch refused a complete toolchain:\n{out}"
    assert "1 passed" in out, out
    assert _REFUSAL_HEAD not in out, out
    assert _INCOMPLETE_MARK not in out, out


@pytest.mark.parametrize("hidden", list(_PARTS))
def test_switch_refuses_a_toolchain_missing_one_part(tmp_path, hidden):
    """Switch on, encoder present, one other part hidden: FAIL, naming that
    part and where it was looked for, and no part that is present."""
    tc = _toolchain(tmp_path, encoder=True, without=(hidden,))
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=tc,
                     video=hidden != "video", numpy=hidden != "numpy")

    assert proc.returncode != 0, (
        f"{REQUIRE_TOOLCHAIN_ENV}=1 without {hidden} exited 0 -- the switch did "
        f"not fire:\n{out}")
    assert " passed" not in out, f"a 'passed' summary survived the switch:\n{out}"
    assert _INCOMPLETE_MARK in out, out
    assert _REFUSAL_HEAD not in out, out          # the encoder is there
    for part, (rel, name) in _PARTS.items():
        if part == hidden:
            assert f"Missing: {name}." in out, out
            if rel is not None:
                assert str(tc / rel) in out, out
        else:
            assert f"Missing: {name}." not in out, out
    if hidden == "video":
        assert f"${VIDEO_ENV} (unset)" in out, out


def test_an_encoder_alone_does_not_satisfy_the_switch(tmp_path):
    """The state this guard exists for: a root with an encoder and nothing
    else. One refusal names every missing part, not one per run."""
    tc = _toolchain(tmp_path, encoder=True, without=tuple(_PARTS))
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=tc,
                     video=False, numpy=False)

    assert proc.returncode != 0, out
    assert " passed" not in out, out
    for _rel, name in _PARTS.values():
        assert f"Missing: {name}." in out, out


@pytest.mark.parametrize("switch", [None, "0"])
def test_missing_parts_are_not_demanded_unless_asked(tmp_path, switch):
    """Switch off, an encoder and nothing else: green and silent, as before."""
    tc = _toolchain(tmp_path, encoder=True, without=tuple(_PARTS))
    proc, out = _run(_synthetic_repo(tmp_path), switch=switch, toolchain=tc,
                     video=False, numpy=False)

    assert proc.returncode == 0, f"switch={switch!r} demanded a full toolchain:\n{out}"
    assert "1 passed" in out, out
    assert _INCOMPLETE_MARK not in out, out


def test_ci_flag_does_not_switch_it_on(tmp_path):
    """CI's shape: full collection demanded and met, no toolchain, switch unset."""
    tc = _toolchain(tmp_path, encoder=False)
    root = _synthetic_repo(tmp_path, with_core_source=True)
    proc, out = _run(root, switch=None, toolchain=tc, full_collection="1")

    assert proc.returncode == 0, (
        f"{REQUIRE_FULL_COLLECTION_ENV}=1 without a toolchain failed -- CI, which "
        f"installs none, would go red:\n{out}")
    assert "1 passed" in out, out
    assert _REFUSAL_HEAD not in out, out


def test_both_refusals_are_reported_together(tmp_path):
    """Both guards failing: one run names both fixes, not one at a time."""
    tc = _toolchain(tmp_path, encoder=False)
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=tc,
                     full_collection="1")

    assert proc.returncode != 0, out
    assert _REFUSAL_HEAD in out, out
    assert "IAMF_SENTINEL_SRC" in out, out


def test_the_policy_reads_the_environment(tmp_path):
    """The switch and the refusal, without a subprocess."""
    for falsey in ("", "0", "false", "FALSE", "no", "off", "  0  "):
        assert require_toolchain({REQUIRE_TOOLCHAIN_ENV: falsey}) is False
    for truthy in ("1", "true", "yes", "on", "anything"):
        assert require_toolchain({REQUIRE_TOOLCHAIN_ENV: truthy}) is True
    assert require_toolchain({}) is False

    bare = _toolchain(tmp_path, encoder=False)
    env = {REQUIRE_TOOLCHAIN_ENV: "1", "LOOM_TOOLCHAIN": str(bare)}
    assert toolchain_root(env) is None
    assert str(bare / ENCODER_REL) in missing_toolchain_error(env)
    assert missing_toolchain_error({**env, REQUIRE_TOOLCHAIN_ENV: "0"}) is None

    # $LOOM_TOOLCHAIN wins over $SENTINEL_TOOLCHAIN, as the executor resolves it.
    env = {REQUIRE_TOOLCHAIN_ENV: "1", "LOOM_TOOLCHAIN": str(bare),
           "SENTINEL_TOOLCHAIN": "/elsewhere"}
    assert "$LOOM_TOOLCHAIN" in missing_toolchain_error(env)


def test_the_kernel_and_mp4box_may_live_outside_the_root(tmp_path):
    """The product finds the kernel through $SENTINEL_DSP or PATH and MP4Box on
    PATH; the guard must accept what the product would use, and must not take
    `SENTINEL_DSP=off` -- the numpy escape hatch of another package -- for a
    kernel."""
    tc = _toolchain(tmp_path, encoder=True, without=("kernel", "mp4box"))
    bindir = tmp_path / "elsewhere"
    kernel = _tool(bindir / "sentinel-dsp")
    _tool(bindir / "MP4Box")
    root = _synthetic_repo(tmp_path)

    proc, out = _run(root, switch="1", toolchain=tc,
                     extra_env={"SENTINEL_DSP": str(kernel), "PATH": str(bindir)})
    assert proc.returncode == 0, out
    assert "1 passed" in out, out

    proc, out = _run(root, switch="1", toolchain=tc,
                     extra_env={"SENTINEL_DSP": "off"})
    assert proc.returncode != 0, out
    assert "Missing: sentinel-dsp, the loudness kernel." in out, out
    assert "Missing: MP4Box." in out, out


def test_the_missing_parts_policy_reads_the_environment(tmp_path):
    """The list and the refusal, without a subprocess."""
    tc = _toolchain(tmp_path, encoder=True)
    donor = tmp_path / "donor.mp4"
    donor.write_bytes(fake_mp4())
    env = {REQUIRE_TOOLCHAIN_ENV: "1", "LOOM_TOOLCHAIN": str(tc),
           VIDEO_ENV: str(donor), "PATH": ""}
    assert missing_components(env, numpy_ok=True) == []
    assert incomplete_toolchain_error(env, numpy_ok=True) is None

    assert [m[0] for m in missing_components(env, numpy_ok=False)] == ["numpy"]
    no_video = {k: v for k, v in env.items() if k != VIDEO_ENV}
    assert [m[0] for m in missing_components(no_video, numpy_ok=True)] == [
        "a video donor"]
    assert [m[0] for m in missing_components(
        {**env, VIDEO_ENV: str(tmp_path / "absent.mp4")}, numpy_ok=True)] == [
        "a video donor"]

    (tc / KERNEL_REL).unlink()
    text = incomplete_toolchain_error(env, numpy_ok=True)
    assert str(tc / KERNEL_REL) in text and "$SENTINEL_DSP" in text
    assert "$LOOM_TOOLCHAIN" in text
    # off the switch, or with no encoder, this refusal has nothing to say
    assert incomplete_toolchain_error({**env, REQUIRE_TOOLCHAIN_ENV: "0"},
                                      numpy_ok=True) is None
    (tc / ENCODER_REL).unlink()
    assert missing_components(env, numpy_ok=True) == []
    assert missing_toolchain_error(env) is not None


def test_the_paths_are_the_products_own():
    """The guard looks where the executor looks: one path map, not two."""
    from loom.toolchain import BINARIES

    assert ENCODER_REL.as_posix() == BINARIES["encoder_main"]
    assert DECODER_REL.as_posix() == BINARIES["decoder_main"]
    assert FFMPEG_REL.as_posix() == BINARIES["ffmpeg"]
    assert MP4BOX_REL.as_posix() == BINARIES["mp4box"]
    assert KERNEL_REL.as_posix() == BINARIES["sentinel-dsp"]
