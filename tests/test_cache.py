"""R6 / D-Q4: content-addressed target cache — key semantics + store.

Key sensitivity is the contract: a key must change when the source bytes,
the plan fragment, or a tool identity change — and must NOT change when
only `rationale` prose changes. The store must self-heal on corruption.
"""

from __future__ import annotations

from pathlib import Path

from loom.cache import Cache, target_key
from loom.compiler import compile_manifest
from loom.manifest import load_manifest

from .conftest import write_wav

MANIFEST = (
    "loom: 0\ntitle: cachetest\n"
    "sources:\n  main: { path: main.wav, kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: dist/a.iamf }\n"
)


def _compile(tmp_path, text=MANIFEST):
    mf = tmp_path / "manifest.yaml"
    mf.write_text(text)
    m = load_manifest(mf)
    return m, compile_manifest(m)


def _fake_toolchain(root: Path, salt: bytes = b"") -> Path:
    """Executable stand-ins so tool identities exist toolchain-free."""
    for rel in ("src/build-iamf/encoder_main", "src/build-iamf/decoder_main",
                "bin/ffmpeg-install/bin/ffmpeg", "bin/MP4Box"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"#!/bin/sh\n# " + rel.encode() + salt + b"\n")
        p.chmod(0o755)
    return root


def test_key_deterministic(tmp_path):
    write_wav(tmp_path / "main.wav", 2)
    tc = _fake_toolchain(tmp_path / "tc")
    _m, plan = _compile(tmp_path)
    _m2, plan2 = _compile(tmp_path)
    k1 = target_key(plan, plan.targets[0], tmp_path, tc)
    k2 = target_key(plan2, plan2.targets[0], tmp_path, tc)
    assert k1 == k2


def test_key_ignores_rationale(tmp_path):
    write_wav(tmp_path / "main.wav", 2)
    tc = _fake_toolchain(tmp_path / "tc")
    _m, plan = _compile(tmp_path)
    k1 = target_key(plan, plan.targets[0], tmp_path, tc)
    for s in plan.steps:
        s.rationale = "totally different prose"
    plan.targets[0].rationale = "likewise"
    assert target_key(plan, plan.targets[0], tmp_path, tc) == k1


def test_key_changes_on_source_bytes(tmp_path):
    write_wav(tmp_path / "main.wav", 2)
    tc = _fake_toolchain(tmp_path / "tc")
    _m, plan = _compile(tmp_path)
    k1 = target_key(plan, plan.targets[0], tmp_path, tc)
    raw = bytearray((tmp_path / "main.wav").read_bytes())
    raw[-1] ^= 0xFF   # one sample byte
    (tmp_path / "main.wav").write_bytes(bytes(raw))
    assert target_key(plan, plan.targets[0], tmp_path, tc) != k1


def test_key_changes_on_plan_fragment(tmp_path):
    write_wav(tmp_path / "main.wav", 2)
    tc = _fake_toolchain(tmp_path / "tc")
    _m, plan = _compile(tmp_path)
    k1 = target_key(plan, plan.targets[0], tmp_path, tc)
    _m2, plan2 = _compile(
        tmp_path, MANIFEST.replace(
            "targets:",
            "policy:\n  codec: { name: lpcm }\n"
            "targets:"))
    assert target_key(plan2, plan2.targets[0], tmp_path, tc) != k1


def test_key_changes_on_tool_identity(tmp_path):
    write_wav(tmp_path / "main.wav", 2)
    _m, plan = _compile(tmp_path)
    tc1 = _fake_toolchain(tmp_path / "tc1")
    tc2 = _fake_toolchain(tmp_path / "tc2", salt=b"v2")
    k1 = target_key(plan, plan.targets[0], tmp_path, tc1)
    k2 = target_key(plan, plan.targets[0], tmp_path, tc2)
    assert k1 != k2


def test_store_roundtrip_and_selfheal(tmp_path):
    cache = Cache(root=tmp_path / "cache")
    art = tmp_path / "out.iamf"
    art.write_bytes(b"IAMF-ish bytes")
    import hashlib
    meta = {"sha256": hashlib.sha256(art.read_bytes()).hexdigest(),
            "output_entry": {"sha256": "x", "bytes": 14},
            "gate": {"passed": True}}
    key = "ab" + "0" * 62
    cache.admit(key, art, meta)
    got = cache.lookup(key)
    assert got is not None and got["artifact"] == "out.iamf"
    assert cache.artifact(key, got).read_bytes() == b"IAMF-ish bytes"

    # corrupt the stored artifact -> lookup self-heals to a miss
    cache.artifact(key, got).write_bytes(b"truncated")
    assert cache.lookup(key) is None
    # re-admission repairs the entry
    cache.admit(key, art, meta)
    assert cache.lookup(key) is not None


def test_store_disabled(tmp_path):
    cache = Cache(root=tmp_path / "cache", enabled=False)
    art = tmp_path / "o"
    art.write_bytes(b"x")
    cache.admit("cd" + "0" * 62, art, {"sha256": "whatever"})
    assert cache.lookup("cd" + "0" * 62) is None
    assert not (tmp_path / "cache").exists()


def test_missing_meta_is_miss(tmp_path):
    cache = Cache(root=tmp_path / "cache")
    assert cache.lookup("ef" + "0" * 62) is None
