"""Doc 49 (D-V2…D-V5), toolchain-free half: per-job `manifest:` — the
multi-template batch grammar, the legacy-stable/extended spec-hash, the
additive ledger contract, M-420/M-421 arms across templates, and the
journal refusal when a template's bytes change. The executed mixed-policy
accept (E-V4) lives in test_batch_toolchain.py. Cache-key
template-independence (E-V3) lives here via the test_cache harness."""

from __future__ import annotations

import hashlib
import json

import pytest

from loom.batch import (
    BatchError, REQUIRED_LEDGER_KEYS, load_batch, run_batch,
)
from loom.cache import target_key
from loom.compiler import compile_manifest
from loom.diagnostics import CompileError
from loom.manifest import load_manifest

from .conftest import write_wav
from .test_cache import _fake_toolchain

TPL = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: stereo }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
)

TPL_NORM = TPL.replace(
    "targets:", "policy:\n  loudness: { normalize: -16 }\ntargets:")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _project(tmp_path, batch_text, titles=("ep01", "ep02")):
    (tmp_path / "tpl.yaml").write_text(TPL)
    (tmp_path / "tpl_norm.yaml").write_text(TPL_NORM)
    for t in titles:
        write_wav(tmp_path / "wavs" / f"{t}.wav", 2)
    bf = tmp_path / "batch.yaml"
    bf.write_text(batch_text)
    return bf


MIXED = (
    "loom_batch: 0\n"
    "manifest: tpl.yaml\n"
    "defaults: { out_dir: \"out/{title}\" }\n"
    "jobs:\n"
    "  - { id: plain, vars: { title: ep01 } }\n"
    "  - { id: norm, manifest: tpl_norm.yaml, vars: { title: ep02 } }\n"
)

LEGACY = (
    "loom_batch: 0\n"
    "manifest: tpl.yaml\n"
    "defaults: { out_dir: \"out/{title}\" }\n"
    "jobs:\n"
    "  - { vars: { title: ep01 } }\n"
    "  - { vars: { title: ep02 } }\n"
)


# ---- grammar (D-V2) ---------------------------------------------------------

def test_per_job_manifest_resolves(tmp_path):
    spec = load_batch(_project(tmp_path, MIXED))
    plain, norm = spec.jobs
    assert plain.manifest_rel == "tpl.yaml"
    assert norm.manifest_rel == "tpl_norm.yaml"
    assert norm.manifest_path == (tmp_path / "tpl_norm.yaml").resolve()
    tpl_sha = hashlib.sha256((tmp_path / "tpl.yaml").read_bytes()).hexdigest()
    norm_sha = hashlib.sha256(
        (tmp_path / "tpl_norm.yaml").read_bytes()).hexdigest()
    assert spec.manifests == {"tpl.yaml": tpl_sha, "tpl_norm.yaml": norm_sha}
    assert spec.manifest_sha256 == tpl_sha          # the batch-level default
    assert plain.manifest_sha256 == tpl_sha
    assert norm.manifest_sha256 == norm_sha


def test_no_default_template_is_legal(tmp_path):
    text = MIXED.replace("manifest: tpl.yaml\n", "") \
                .replace("- { id: plain, vars: { title: ep01 } }",
                         "- { id: plain, manifest: tpl.yaml, "
                         "vars: { title: ep01 } }")
    spec = load_batch(_project(tmp_path, text))
    assert spec.manifest_path is None
    assert spec.manifest_sha256 is None
    assert set(spec.manifests) == {"tpl.yaml", "tpl_norm.yaml"}


def test_job_without_any_template_m420(tmp_path):
    text = MIXED.replace("manifest: tpl.yaml\n", "")   # `plain` has none
    with pytest.raises(CompileError) as ei:
        load_batch(_project(tmp_path, text))
    assert "M-420" in ei.value.codes()
    assert "plain" in str(ei.value)


@pytest.mark.parametrize("bad", ["manifest: 42", "manifest: \"\""])
def test_job_manifest_bad_type_m420(tmp_path, bad):
    text = MIXED.replace("manifest: tpl_norm.yaml", bad)
    with pytest.raises(CompileError) as ei:
        load_batch(_project(tmp_path, text))
    assert "M-420" in ei.value.codes()


def test_job_manifest_missing_file_m420(tmp_path):
    text = MIXED.replace("tpl_norm.yaml", "nope.yaml")
    with pytest.raises(CompileError) as ei:
        load_batch(_project(tmp_path, text))
    assert "M-420" in ei.value.codes()
    assert "nope.yaml" in str(ei.value) and "norm" in str(ei.value)


def test_unreferenced_default_must_still_exist(tmp_path):
    # the default is spec text: declared-but-missing is an error even when
    # every job names its own template
    text = ("loom_batch: 0\nmanifest: nope.yaml\n"
            "defaults: { out_dir: \"out/{title}\" }\n"
            "jobs:\n"
            "  - { id: a, manifest: tpl.yaml, vars: { title: ep01 } }\n")
    with pytest.raises(CompileError) as ei:
        load_batch(_project(tmp_path, text, titles=("ep01",)))
    assert "M-420" in ei.value.codes()


def test_m421_collision_across_templates(tmp_path):
    text = ("loom_batch: 0\nmanifest: tpl.yaml\n"
            "defaults: { out_dir: out }\n"
            "jobs:\n"
            "  - { id: a, vars: { title: ep01 } }\n"
            "  - { id: b, manifest: tpl_norm.yaml, vars: { title: ep01 } }\n")
    spec = load_batch(_project(tmp_path, text, titles=("ep01",)))
    with pytest.raises(CompileError) as ei:
        run_batch(spec)
    assert "M-421" in ei.value.codes()
    assert "'a'" in str(ei.value) and "jobs[b]" in str(ei.value)


# ---- spec-hash (D-V3) -------------------------------------------------------

def test_legacy_spec_hash_byte_compat(tmp_path):
    """A batch with no per-job manifests hashes byte-identically to the
    0.6.0 formula — an upgraded Loom resumes a pre-upgrade state dir."""
    spec = load_batch(_project(tmp_path, LEGACY))
    tpl_bytes = (tmp_path / "tpl.yaml").read_bytes()
    expected = hashlib.sha256(
        (_canon({"manifest": "tpl.yaml",
                 "defaults": {"out_dir": "out/{title}"},
                 "jobs": [{"id": jb.id, "vars": jb.vars,
                           "out_dir": jb.out_dir} for jb in spec.jobs]})
         + tpl_bytes.hex()).encode()).hexdigest()
    assert spec.spec_hash == expected


def test_extended_spec_hash_deterministic_and_sensitive(tmp_path):
    bf = _project(tmp_path, MIXED)
    h1 = load_batch(bf).spec_hash
    assert load_batch(bf).spec_hash == h1            # deterministic
    # flips on any referenced template's BYTES (even a comment)
    (tmp_path / "tpl_norm.yaml").write_text(TPL_NORM + "# prose\n")
    h2 = load_batch(bf).spec_hash
    assert h2 != h1
    # flips on WHICH template a job names
    (tmp_path / "tpl_norm.yaml").write_text(TPL_NORM)
    assert load_batch(bf).spec_hash == h1
    bf.write_text(MIXED.replace("manifest: tpl_norm.yaml", "manifest: tpl.yaml"))
    assert load_batch(bf).spec_hash != h1


def test_journal_refused_after_template_edit(tmp_path):
    """Editing one template's bytes changes the spec-hash; an existing
    journal is then refused with the hard error naming both hashes."""
    bf = _project(tmp_path, MIXED)
    spec = load_batch(bf)
    state = tmp_path / "state"
    run_batch(spec, state_dir=state, no_cache=True)   # journal written
    (tmp_path / "tpl_norm.yaml").write_text(TPL_NORM + "# edited\n")
    spec2 = load_batch(bf)
    assert spec2.spec_hash != spec.spec_hash
    with pytest.raises(BatchError) as ei:
        run_batch(spec2, state_dir=state, no_cache=True)
    assert spec.spec_hash[:12] in str(ei.value)
    assert spec2.spec_hash[:12] in str(ei.value)


# ---- ledger contract (D-V4, additive) ---------------------------------------

def test_ledger_multitpl_contract(tmp_path):
    bf = _project(tmp_path, MIXED)
    spec = load_batch(bf)
    _rc, ledger_path = run_batch(spec, state_dir=tmp_path / "state",
                                 no_cache=True)
    led = json.loads(ledger_path.read_text())
    for k in REQUIRED_LEDGER_KEYS["batch"]:
        assert k in led, f"batch ledger missing {k}"
    for job in led["jobs"]:
        for k in REQUIRED_LEDGER_KEYS["job"]:
            assert k in job, f"job entry missing {k}"
    assert led["manifests"] == spec.manifests
    assert led["manifest_sha256"] == spec.manifests["tpl.yaml"]
    by_id = {j["job_id"]: j for j in led["jobs"]}
    assert by_id["plain"]["manifest"] == "tpl.yaml"
    assert by_id["norm"]["manifest"] == "tpl_norm.yaml"
    assert by_id["norm"]["manifest_sha256"] == spec.manifests["tpl_norm.yaml"]


def test_ledger_null_default_when_no_batch_manifest(tmp_path):
    text = ("loom_batch: 0\n"
            "defaults: { out_dir: \"out/{title}\" }\n"
            "jobs:\n"
            "  - { id: a, manifest: tpl.yaml, vars: { title: ep01 } }\n")
    bf = _project(tmp_path, text, titles=("ep01",))
    spec = load_batch(bf)
    _rc, ledger_path = run_batch(spec, state_dir=tmp_path / "state",
                                 no_cache=True)
    led = json.loads(ledger_path.read_text())
    assert led["manifest_sha256"] is None
    assert led["manifests"] == {"tpl.yaml": spec.manifests["tpl.yaml"]}


# ---- cache-key template-independence (E-V3) ---------------------------------

def _compile_from(tmp_path, tpl_name, text, title="ep01"):
    (tmp_path / tpl_name).write_text(text)
    m = load_manifest(tmp_path / tpl_name, variables={"title": title})
    return compile_manifest(m)


def test_cache_key_ignores_template_identity(tmp_path):
    write_wav(tmp_path / "wavs" / "ep01.wav", 2)
    tc = _fake_toolchain(tmp_path / "tc")
    p1 = _compile_from(tmp_path, "tpl_a.yaml", TPL)
    p2 = _compile_from(tmp_path, "tpl_b_other_name.yaml", TPL)
    k1 = target_key(p1, p1.targets[0], tmp_path, tc)
    k2 = target_key(p2, p2.targets[0], tmp_path, tc)
    assert k1 == k2      # the template's NAME never enters the key
    # a typed policy edit flips the key via the plan fragment
    p3 = _compile_from(tmp_path, "tpl_c.yaml", TPL_NORM)
    k3 = target_key(p3, p3.targets[0], tmp_path, tc)
    assert k3 != k1
