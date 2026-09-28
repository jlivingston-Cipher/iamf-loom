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
- switch on, an executable encoder found -> the run is green
- CI's shape (full collection demanded, no toolchain, switch unset) -> green

Method, as in test_collection_guard.py: pytest runs in a subprocess against a
synthetic repo root carrying a copy of the real conftest and one probe test,
so this repo's siblings and the caller's environment cannot leak in. The child
environment names its toolchain explicitly every time; the one test that
exercises the built-in default is skipped where that default really exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import (
    DEFAULT_TOOLCHAIN,
    ENCODER_REL,
    MISSING_TOOLCHAIN_ERROR,
    REQUIRE_FULL_COLLECTION_ENV,
    REQUIRE_TOOLCHAIN_ENV,
    missing_toolchain_error,
    require_toolchain,
    toolchain_root,
)

_REAL_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_PROBE = "def test_probe():\n    assert True\n"

_REFUSAL_HEAD = MISSING_TOOLCHAIN_ERROR.splitlines()[0].format(
    var=REQUIRE_TOOLCHAIN_ENV)

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


def _toolchain(tmp_path: Path, *, encoder: bool) -> Path:
    """A toolchain root, with or without an executable encoder_main in it."""
    root = tmp_path / "toolchain"
    root.mkdir()
    if encoder:
        enc = root / ENCODER_REL
        enc.parent.mkdir(parents=True)
        enc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        enc.chmod(0o755)
    return root


def _run(root: Path, *, switch, toolchain, full_collection=None):
    """Run the synthetic suite with exactly the environment named here."""
    env = dict(os.environ)
    env["IAMF_SENTINEL_SRC"] = ""          # never inherit a real core checkout
    for var in (REQUIRE_TOOLCHAIN_ENV, REQUIRE_FULL_COLLECTION_ENV,
                "LOOM_TOOLCHAIN", "SENTINEL_TOOLCHAIN"):
        env.pop(var, None)
    if switch is not None:
        env[REQUIRE_TOOLCHAIN_ENV] = switch
    if toolchain is not _UNSET:
        env["LOOM_TOOLCHAIN"] = str(toolchain)
    if full_collection is not None:
        env[REQUIRE_FULL_COLLECTION_ENV] = full_collection
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


def test_switch_is_satisfied_by_an_executable_encoder(tmp_path):
    """Switch on and the encoder present: the guard stays out of the way."""
    tc = _toolchain(tmp_path, encoder=True)
    proc, out = _run(_synthetic_repo(tmp_path), switch="1", toolchain=tc)

    assert proc.returncode == 0, f"the switch refused a run that had its encoder:\n{out}"
    assert "1 passed" in out, out
    assert _REFUSAL_HEAD not in out, out


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
