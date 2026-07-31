"""R6 / D-Q2/D-Q6/D-Q7, toolchain-free half: batch spec loading (M-420/421),
collision compile-abort, journal spec-hash guard, the ledger-keys contract,
and failed-job isolation at the compile level. The executed half (workers,
cache hits, induced kill) lives in test_batch_toolchain.py."""

from __future__ import annotations

import json

import pytest

from loom.batch import (
    BatchError, REQUIRED_LEDGER_KEYS, load_batch, run_batch,
)
from loom.diagnostics import CompileError

from .conftest import write_wav

TPL = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
)


def _project(tmp_path, batch_text, titles=("ep01", "ep02")):
    (tmp_path / "tpl.yaml").write_text(TPL, encoding="utf-8")
    for t in titles:
        write_wav(tmp_path / "wavs" / f"{t}.wav", 2)
    bf = tmp_path / "batch.yaml"
    bf.write_text(batch_text, encoding="utf-8")
    return bf


BATCH = (
    "loom_batch: 0\n"
    "manifest: tpl.yaml\n"
    "defaults: { out_dir: \"out/{title}\" }\n"
    "jobs:\n"
    "  - { vars: { title: ep01 } }\n"
    "  - { vars: { title: ep02 } }\n"
)


# ---- load_batch (M-420 / M-421) ---------------------------------------------

def test_load_batch_ok(tmp_path):
    spec = load_batch(_project(tmp_path, BATCH))
    assert [j.vars["title"] for j in spec.jobs] == ["ep01", "ep02"]
    assert spec.jobs[0].out_dir == "out/ep01"        # defaults + substitution
    assert spec.jobs[0].id.startswith("job00-")
    # derived ids are deterministic
    spec2 = load_batch(_project(tmp_path, BATCH))
    assert [j.id for j in spec.jobs] == [j.id for j in spec2.jobs]
    assert spec.spec_hash == spec2.spec_hash


@pytest.mark.parametrize("mutate,needle", [
    (lambda t: t.replace("loom_batch: 0", "loom_batch: 1"), "M-420"),
    (lambda t: t.replace("manifest: tpl.yaml\n", ""), "M-420"),
    (lambda t: t.replace("manifest: tpl.yaml", "manifest: nope.yaml"), "M-420"),
    (lambda t: t.split("jobs:")[0] + "jobs: []\n", "M-420"),
    (lambda t: t.replace("defaults: { out_dir: \"out/{title}\" }\n", ""),
     "M-420"),   # no out_dir anywhere
])
def test_load_batch_schema_errors(tmp_path, mutate, needle):
    bf = _project(tmp_path, mutate(BATCH))
    with pytest.raises(CompileError) as ei:
        load_batch(bf)
    assert needle in ei.value.codes()


def test_missing_batch_file(tmp_path):
    with pytest.raises(CompileError) as ei:
        load_batch(tmp_path / "nope.yaml")
    assert ei.value.codes() == ["M-420"]


def test_duplicate_job_ids_m421(tmp_path):
    text = BATCH.replace("- { vars: { title: ep01 } }",
                         "- { id: same, vars: { title: ep01 } }") \
                .replace("- { vars: { title: ep02 } }",
                         "- { id: same, vars: { title: ep02 } }")
    with pytest.raises(CompileError) as ei:
        load_batch(_project(tmp_path, text))
    assert "M-421" in ei.value.codes()
    assert "same" in str(ei.value)


def test_output_collision_m421_names_both_jobs(tmp_path):
    # two jobs, same title -> same out path (derived ids differ via index)
    text = ("loom_batch: 0\nmanifest: tpl.yaml\n"
            "defaults: { out_dir: out }\n"
            "jobs:\n"
            "  - { id: a, vars: { title: ep01 } }\n"
            "  - { id: b, vars: { title: ep01 } }\n")
    bf = _project(tmp_path, text, titles=("ep01",))
    spec = load_batch(bf)
    with pytest.raises(CompileError) as ei:
        run_batch(spec)
    assert "M-421" in ei.value.codes()
    assert "'a'" in str(ei.value) and "jobs[b]" in str(ei.value)


# ---- journal guard + ledger contract ----------------------------------------

def test_journal_spec_mismatch_is_hard_error(tmp_path):
    bf = _project(tmp_path, BATCH)
    spec = load_batch(bf)
    state = tmp_path / "state"
    state.mkdir()
    (state / "journal.jsonl").write_text(json.dumps(
        {"spec": "f" * 64, "job_id": "x", "ok": True, "outputs": {}}) + "\n", encoding="utf-8")
    with pytest.raises(BatchError) as ei:
        run_batch(spec, state_dir=state)
    assert spec.spec_hash[:12] in str(ei.value)


def test_ledger_contract_and_failed_job_isolation(tmp_path):
    """Jobs that fail COMPILE (missing source) are `failed` with diagnostics
    in the ledger; the batch still writes the full R6 contract; rc 2.
    (Toolchain-free: compile-failure isolation; the executed ok-path half of
    the #713 story is in test_batch_toolchain.py.)"""
    text = BATCH + "  - { vars: { title: missing } }\n"   # no wavs/missing.wav
    bf = _project(tmp_path, text)
    spec = load_batch(bf)
    rc, ledger_path = run_batch(spec, state_dir=tmp_path / "state",
                                no_cache=True)
    assert rc == 2
    led = json.loads(ledger_path.read_text(encoding="utf-8"))
    for k in REQUIRED_LEDGER_KEYS["batch"]:
        assert k in led, f"batch ledger missing {k}"
    for job in led["jobs"]:
        for k in REQUIRED_LEDGER_KEYS["job"]:
            assert k in job, f"job entry missing {k}"
    for k in REQUIRED_LEDGER_KEYS["totals"]:
        assert k in led["totals"], f"totals missing {k}"
    m301 = [j for j in led["jobs"]
            if any("M-301" in f for f in j["failures"])]
    assert len(m301) == 1 and m301[0]["status"] == "failed"
    assert m301[0]["vars"] == {"title": "missing"}
    # the other jobs were not aborted by the bad one: they RAN — in this
    # toolchain-free half they then fail only at the encoder boundary
    # (execution failures, never M-301); the executed rc-0-for-others half
    # of the #713 story is test_batch_toolchain.py's.
    others = [j for j in led["jobs"] if j not in m301]
    assert len(others) == 2
    assert all(not any("M-301" in f for f in j["failures"]) for j in others)
    assert all(j["seconds"] > 0 or j["status"] == "ok" for j in others)


def test_ledger_echoes_vars_and_tool_identities(tmp_path):
    bf = _project(tmp_path, BATCH)
    spec = load_batch(bf)
    _rc, ledger_path = run_batch(spec, state_dir=tmp_path / "state",
                                 no_cache=True)
    led = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led["jobs"][0]["vars"] == {"title": "ep01"}
    assert set(led["tool_identities"]) == {
        "decoder_main", "encoder_main", "ffmpeg", "mp4box", "sentinel-dsp"}
    assert led["cache_policy"] == "off (--no-cache)"
    assert led["spec_hash"] == spec.spec_hash
