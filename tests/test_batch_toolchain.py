"""R6 executed half (toolchain-gated): the doc-44 E-Q4…E-Q7 accepts.

The primary catalog: 8 jobs over one template (stereo/5.1/7.1.4 mix), every
job producing a raw .iamf AND a youtube-preset A/V MP4 (trimmed donor), all
under `normalize: -16` — so the batch exercises both backends, the remux
route, the R3 chains, and the R5 gate on every output. A smaller
stereo-only batch drives the induced-kill resume test (E-Q6), where target
variety adds nothing and wall-clock does.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loom.batch import REQUIRED_LEDGER_KEYS, load_batch, run_batch

from .conftest import needs_toolchain, toolchain_root, write_wav
from .test_parity_toolchain import FRAMES, video_donor

LAYOUT_CH = {"stereo": 2, "5.1": 6, "7.1.4": 12}

TPL_FULL = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: \"{layout}\" }\n"
    "elements:\n  bed: { from: main }\n"
    "policy:\n  loudness: { normalize: -16 }\n"
    "targets:\n"
    "  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
    "  - { format: mp4, out: \"dist/{title}.mp4\", video: v.mp4, "
    "preset: youtube }\n"
)

# (title, layout) — the 8-job synthetic catalog (E-Q4)
JOBS = [("s01", "stereo"), ("s02", "stereo"), ("s03", "stereo"),
        ("s04", "stereo"), ("f01", "5.1"), ("f02", "5.1"),
        ("h01", "7.1.4"), ("h02", "7.1.4")]

TPL_SMALL = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
)
SMALL_TITLES = [f"k{i:02d}" for i in range(6)]


def _batch_yaml(jobs: list[tuple[str, str]] | list[str],
                template: str = "tpl.yaml") -> str:
    lines = ["loom_batch: 0", f"manifest: {template}",
             "defaults: { out_dir: \"out/{title}\" }", "jobs:"]
    for j in jobs:
        if isinstance(j, tuple):
            t, lay = j
            lines.append(f"  - {{ id: {t}, vars: {{ title: {t}, "
                         f"layout: \"{lay}\" }} }}")
        else:
            lines.append(f"  - {{ id: {j}, vars: {{ title: {j} }} }}")
    return "\n".join(lines) + "\n"


def _make_full_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tpl.yaml").write_text(TPL_FULL)
    for t, lay in JOBS:
        write_wav(root / "wavs" / f"{t}.wav", LAYOUT_CH[lay], frames=FRAMES)
    donor = video_donor(root)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    (root / "batch.yaml").write_text(_batch_yaml(JOBS))
    return root / "batch.yaml"


def _make_small_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tpl.yaml").write_text(TPL_SMALL)
    for t in SMALL_TITLES:
        write_wav(root / "wavs" / f"{t}.wav", 2, frames=FRAMES)
    (root / "batch.yaml").write_text(_batch_yaml(SMALL_TITLES))
    return root / "batch.yaml"


def _out_files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted((root).rglob("dist/*"))
            if p.is_file()}


@pytest.fixture(scope="module")
def full_batch(tmp_path_factory):
    """The primary E-Q4 run: 8 jobs, --workers 2. Shared by the cache and
    tamper tests (they re-enter the same state dir by design)."""
    root = tmp_path_factory.mktemp("batch-full")
    bf = _make_full_project(root)
    spec = load_batch(bf)
    rc, ledger_path = run_batch(spec, workers=2,
                                toolchain=str(toolchain_root()))
    return root, bf, spec, rc, json.loads(ledger_path.read_text())


# ---- E-Q4: the 8-job catalog --------------------------------------------------

@needs_toolchain
def test_eq4_batch_completes(full_batch):
    root, _bf, spec, rc, led = full_batch
    assert rc == 0
    assert led["totals"] == {
        "jobs": 8, "ok": 8, "failed": 0, "skipped_resume": 0,
        "cache_hits": 0, "cache_misses": 16,
        "seconds": led["totals"]["seconds"]}
    for job in led["jobs"]:
        assert job["status"] == "ok"
        assert set(job["outputs"]) == {
            f"dist/{job['vars']['title']}.iamf",
            f"dist/{job['vars']['title']}.mp4"}
        for out, gate in job["gate"].items():
            assert gate.get("passed"), f"{job['job_id']}/{out}: gate failed"
            assert "cached" not in gate
        # R3 landed: normalize records verified within tolerance
        assert any(v.get("within_tolerance")
                   for v in job["normalize"].values()), job["job_id"]
    # contract keys (the executed-side pin; the schema pin is test_batch.py)
    for k in REQUIRED_LEDGER_KEYS["batch"]:
        assert k in led
    # both workers actually took jobs
    assert len({j["worker"] for j in led["jobs"]}) >= 2


@needs_toolchain
def test_eq4_worker_count_invariance(full_batch, tmp_path):
    """The same catalog at --workers 1 (fresh project copy, cache off) must
    produce byte-identical outputs — the doc-13 open-question-2 determinism
    check on this pinned toolchain."""
    root, _bf, _spec, _rc, _led = full_batch
    copy = tmp_path / "control"
    shutil.copytree(root, copy,
                    ignore=shutil.ignore_patterns(".loom-batch", "out"))
    spec = load_batch(copy / "batch.yaml")
    rc, _lp = run_batch(spec, workers=1, no_cache=True,
                        toolchain=str(toolchain_root()))
    assert rc == 0
    a, b = _out_files(root / "out"), _out_files(copy / "out")
    assert set(a) == set(b) and len(a) == 16
    diff = [k for k in a if a[k] != b[k]]
    assert not diff, f"outputs differ across worker counts: {diff}"


# ---- E-Q5: cache hits ---------------------------------------------------------

@needs_toolchain
def test_eq5_cache_hits_after_outputs_deleted(full_batch):
    root, bf, spec, _rc, first_led = full_batch
    state = root / ".loom-batch" / spec.spec_hash[:8]
    before = _out_files(root / "out")
    shutil.rmtree(root / "out")
    (state / "journal.jsonl").unlink()

    t0 = time.monotonic()
    rc, lp = run_batch(spec, workers=2, toolchain=str(toolchain_root()))
    wall = time.monotonic() - t0
    assert rc == 0
    led = json.loads(lp.read_text())
    assert led["totals"]["cache_hits"] == 16
    assert led["totals"]["cache_misses"] == 0
    for job in led["jobs"]:
        assert job["status"] == "ok"
        assert not job.get("failures")
        for gate in job["gate"].values():
            assert gate.get("passed") and gate.get("cached") is True
    # per-job run ledgers: no steps executed at all
    for jdir in sorted((root / "out").iterdir()):
        run_led = json.loads((jdir / "loom-run.json").read_text())
        assert run_led["steps"] == [], f"{jdir.name} ran steps on a full hit"
        assert all(v == "hit" for v in run_led["cache"].values())
    after = _out_files(root / "out")
    assert before == after, "cache replay is not byte-identical"
    assert wall < first_led["totals"]["seconds"], (
        f"hit run ({wall:.1f}s) not faster than build run "
        f"({first_led['totals']['seconds']:.1f}s)")


# ---- E-Q6 tail: tampered output re-earns its skip -----------------------------

@needs_toolchain
def test_eq6_tampered_output_reruns(full_batch):
    root, bf, spec, _rc, _led = full_batch
    victim_rel = "s01/dist/s01.iamf"
    victim = root / "out" / victim_rel
    good = victim.read_bytes()
    raw = bytearray(good)
    raw[len(raw) // 2] ^= 0xFF
    victim.write_bytes(bytes(raw))

    rc, lp = run_batch(spec, workers=2, toolchain=str(toolchain_root()))
    assert rc == 0
    led = json.loads(lp.read_text())
    st = {j["job_id"]: j["status"] for j in led["jobs"]}
    assert st["s01"] == "ok", "tampered job must RE-RUN (hash-earned skip)"
    assert all(v == "skipped-resume" for k, v in st.items() if k != "s01")
    assert victim.read_bytes() == good, "re-run did not restore the output"


# ---- E-Q6: induced SIGKILL + resume -------------------------------------------

@needs_toolchain
def test_eq6_sigkill_resume(tmp_path):
    root = tmp_path / "proj"
    bf = _make_small_project(root)
    control = tmp_path / "control"
    shutil.copytree(root, control)

    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ,
           "PYTHONPATH": f"{repo/'loom'}:{repo/'sentinel-oss'}:"
                         f"{repo/'sentinel-pro'}",
           "LOOM_TOOLCHAIN": str(toolchain_root())}
    argv = [sys.executable, "-m", "loom", "batch", str(bf), "--workers", "2"]
    spec = load_batch(bf)
    journal = root / ".loom-batch" / spec.spec_hash[:8] / "journal.jsonl"

    # launch, wait for >=2 journal lines, SIGKILL the whole process group
    proc = subprocess.Popen(argv, env=env, cwd=root,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    deadline = time.monotonic() + 120
    killed = False
    while time.monotonic() < deadline:
        if journal.is_file() and len(journal.read_text().splitlines()) >= 2:
            os.killpg(proc.pid, signal.SIGKILL)
            killed = True
            break
        if proc.poll() is not None:   # finished before we could kill it
            break
        time.sleep(0.05)
    proc.wait(timeout=30)
    if not killed:
        pytest.fail("batch finished before the kill window — enlarge the "
                    "batch; the induced-kill accept did not exercise resume")
    n_journaled = len(journal.read_text().splitlines())
    assert 2 <= n_journaled < len(SMALL_TITLES)

    # relaunch, same command: journaled jobs skip, the rest complete
    r = subprocess.run(argv, env=env, cwd=root, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-400:]
    led = json.loads(
        (journal.parent / "batch-ledger.json").read_text())
    statuses = [j["status"] for j in led["jobs"]]
    assert statuses.count("skipped-resume") >= 2
    assert statuses.count("ok") == len(SMALL_TITLES) - \
        statuses.count("skipped-resume")
    assert led["totals"]["failed"] == 0

    # uninterrupted control: byte-identical output set
    crc, _clp = run_batch(load_batch(control / "batch.yaml"), workers=2,
                          toolchain=str(toolchain_root()))
    assert crc == 0
    a, b = _out_files(root / "out"), _out_files(control / "out")
    assert set(a) == set(b) and len(a) == len(SMALL_TITLES)
    diff = [k for k in a if a[k] != b[k]]
    assert not diff, f"killed+resumed differs from control: {diff}"


# ---- E-Q7: failure isolation, executed ----------------------------------------

@needs_toolchain
def test_eq7_failed_job_isolation_executed(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "tpl.yaml").write_text(TPL_SMALL)
    for t in ("a01", "a02"):
        write_wav(root / "wavs" / f"{t}.wav", 2, frames=FRAMES)
    (root / "batch.yaml").write_text(
        _batch_yaml(["a01", "a02", "broken"]))   # no wavs/broken.wav
    rc, lp = run_batch(load_batch(root / "batch.yaml"), workers=2,
                       toolchain=str(toolchain_root()))
    assert rc == 2
    led = json.loads(lp.read_text())
    st = {j["job_id"]: j for j in led["jobs"]}
    assert st["broken"]["status"] == "failed"
    assert any("M-301" in f for f in st["broken"]["failures"])
    for t in ("a01", "a02"):
        assert st[t]["status"] == "ok", "title #713 must cost only #713"
        assert all(g.get("passed") for g in st[t]["gate"].values())


# ---- doc 49 E-V4: the mixed-policy multi-template accept ----------------------

TPL_P = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
)
TPL_N = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: \"5.1\" }\n"
    "elements:\n  bed: { from: main }\n"
    "policy:\n  loudness: { normalize: -16 }\n"
    "targets:\n"
    "  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
    "  - { format: mp4, out: \"dist/{title}.mp4\", video: v.mp4, "
    "preset: youtube }\n"
)
TPL_A = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: \"7.1.4\" }\n"
    "elements:\n  bed: { from: main }\n"
    "policy:\n  codec: { name: flac }\n"
    "targets:\n"
    "  - { format: iamf, out: \"dist/{title}.iamf\", preset: archive }\n"
)

MIXED_BATCH = (
    "loom_batch: 0\n"
    "manifest: tpl_plain.yaml\n"
    "defaults: { out_dir: \"out/{title}\" }\n"
    "jobs:\n"
    "  - { id: p01, vars: { title: p01 } }\n"
    "  - { id: p02, vars: { title: p02 } }\n"
    "  - { id: n01, manifest: tpl_norm_av.yaml, vars: { title: n01 } }\n"
    "  - { id: n02, manifest: tpl_norm_av.yaml, vars: { title: n02 } }\n"
    "  - { id: a01, manifest: tpl_arch.yaml, vars: { title: a01 } }\n"
    "  - { id: a02, manifest: tpl_arch.yaml, vars: { title: a02 } }\n"
    "  - { id: d01, manifest: tpl_plain_copy.yaml, vars: { title: p01 },\n"
    "      out_dir: \"dedup/p01\" }\n"
)
MIXED_CH = {"p01": 2, "p02": 2, "n01": 6, "n02": 6, "a01": 12, "a02": 12}


def _make_mixed_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tpl_plain.yaml").write_text(TPL_P)
    (root / "tpl_plain_copy.yaml").write_text(TPL_P)   # byte-identical content
    (root / "tpl_norm_av.yaml").write_text(TPL_N)
    (root / "tpl_arch.yaml").write_text(TPL_A)
    for t, ch in MIXED_CH.items():
        write_wav(root / "wavs" / f"{t}.wav", ch, frames=FRAMES)
    donor = video_donor(root)
    if donor is None:
        pytest.skip("no stream-copyable video donor ($LOOM_TEST_VIDEO)")
    (root / "batch.yaml").write_text(MIXED_BATCH)
    return root / "batch.yaml"


@pytest.fixture(scope="module")
def mixed_batch(tmp_path_factory):
    """The doc-49 E-V4 run: 7 jobs over 4 templates, --workers 2."""
    root = tmp_path_factory.mktemp("batch-mixed")
    bf = _make_mixed_project(root)
    spec = load_batch(bf)
    rc, ledger_path = run_batch(spec, workers=2,
                                toolchain=str(toolchain_root()))
    return root, bf, spec, rc, json.loads(ledger_path.read_text())


@needs_toolchain
def test_ev4_mixed_policy_batch(mixed_batch):
    """One batch, one journal, one ledger, one rc — normalize, plain, and
    archive-FLAC jobs together: the doc-44 deviation-3 limit, expressed."""
    root, _bf, spec, rc, led = mixed_batch
    assert rc == 0
    assert led["totals"]["jobs"] == 7 and led["totals"]["ok"] == 7
    assert led["totals"]["failed"] == 0
    by_id = {j["job_id"]: j for j in led["jobs"]}
    # per-job template provenance recorded (D-V4)
    assert by_id["p01"]["manifest"] == "tpl_plain.yaml"
    assert by_id["n01"]["manifest"] == "tpl_norm_av.yaml"
    assert by_id["a02"]["manifest"] == "tpl_arch.yaml"
    assert by_id["d01"]["manifest"] == "tpl_plain_copy.yaml"
    for j in led["jobs"]:
        assert j["manifest_sha256"] == spec.manifests[j["manifest"]]
        assert j["status"] == "ok"
        for out, gate in j["gate"].items():
            assert gate.get("passed"), f"{j['job_id']}/{out}: gate failed"
    # normalize records present EXACTLY for the normalize template's jobs
    for jid, j in by_id.items():
        if jid.startswith("n"):
            assert any(v.get("within_tolerance")
                       for v in j["normalize"].values()), jid
        else:
            assert not j["normalize"], f"{jid} has normalize records"
    # A/V outputs only from the norm template; FLAC mezzanines from arch
    assert set(by_id["n01"]["outputs"]) == {"dist/n01.iamf", "dist/n01.mp4"}
    assert set(by_id["a01"]["outputs"]) == {"dist/a01.iamf"}
    assert set(by_id["d01"]["outputs"]) == {"dist/p01.iamf"}


@needs_toolchain
def test_ev4_worker_invariance_and_dedup(mixed_batch, tmp_path):
    """--workers 1 into a fresh state: byte-identical outputs across all
    templates; and (deterministic order) the d01 dedup job — a
    byte-identical template under another name, same source — replays as
    cache hits within the same run: the key excludes template identity."""
    root, _bf, _spec, _rc, _led = mixed_batch
    copy = tmp_path / "control"
    shutil.copytree(root, copy,
                    ignore=shutil.ignore_patterns(".loom-batch", "out",
                                                  "dedup"))
    spec = load_batch(copy / "batch.yaml")
    rc, lp = run_batch(spec, workers=1, toolchain=str(toolchain_root()))
    assert rc == 0
    led = json.loads(lp.read_text())
    by_id = {j["job_id"]: j for j in led["jobs"]}
    # deterministic sequential order: p01 built (miss), d01 replayed (hit)
    assert all(v == "hit" for v in by_id["d01"]["cache"].values())
    assert by_id["d01"]["cache"], "d01 has no cache records"
    assert any(v == "miss" for v in by_id["p01"]["cache"].values())
    for base in ("out", "dedup"):
        a = _out_files(root / base)
        b = _out_files(copy / base)
        assert set(a) == set(b) and a, f"{base}: output sets differ"
        diff = [k for k in a if a[k] != b[k]]
        assert not diff, f"{base}: differs across worker counts: {diff}"
    # the dedup job's replayed bytes equal the built original's
    assert (root / "dedup/p01/dist/p01.iamf").read_bytes() == \
           (root / "out/p01/dist/p01.iamf").read_bytes()
