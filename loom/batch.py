"""Batch runner (R6, D-Q2/D-Q5/D-Q6/D-Q7; multi-template per doc 49 D-V2).

A batch is jobs = template manifests × per-job variable bindings. The
batch-level `manifest:` is the default template; a job may name its own
(per-job POLICY variance is expressed by templates, never by batch-file
policy overrides — ADR-5, doc 49):

    loom_batch: 0
    manifest: tpl.yaml          # default template, relative to this file
    defaults: { out_dir: "dist/{title}" }   # optional, vars-substituted
    jobs:
      - { id: ep01, vars: { title: ep01 } }
      - { vars: { title: ep02 }, out_dir: "alt/ep02" }
      - { id: mezz01, manifest: tpl_archive.yaml, vars: { title: ep01 },
          out_dir: "mezz/ep01" }

Contracts, in the order they bite:

* compile before running: every job's manifest compiles up front; a job that
  fails compile is a `failed` job with its diagnostics in the ledger — and
  never aborts the others (the PRD title-#713 story). Two jobs resolving to
  the same output path abort the whole batch at compile (M-421) — the
  parallel-write hazard dies before any encoder runs.
* journal (`<state>/journal.jsonl`): one fsync'd line per finished job.
  Resume is the DEFAULT when a journal exists: a job is skipped iff its line
  says ok AND every recorded output re-hashes to the recorded sha256 —
  the skip is earned by hash, never by journal say-so. A journal from a
  different spec-hash is a hard error naming both hashes.
* workers: ThreadPoolExecutor at job granularity (executor work is
  subprocess-dominated; each job owns its workdir and out entries). Shared
  state is exactly the journal (one lock) and cache admission (atomic
  rename).
* the batch ledger (`<state>/batch-ledger.json`) is the R6 contract —
  REQUIRED_LEDGER_KEYS below is pinned by test, not by promise.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .cache import Cache
from .util import canon, sha256_file
from .compiler import compile_manifest
from .diagnostics import Collector, CompileError, Diagnostic
from .executor import Executor
from .manifest import load_manifest, substitute_str
from .plan import Plan
from .toolchain import BINARIES, ToolchainError, binary, resolve_root

SUPPORTED_BATCH_VERSION = 0

# The R6 machine-readable-ledger contract (D-Q7; extended additively by
# doc 49 D-V4 — `manifests` at batch level, `manifest`/`manifest_sha256`
# per job), mirrored in tests.
REQUIRED_LEDGER_KEYS = {
    "batch": ["loom_version", "spec_hash", "manifest_sha256", "manifests",
              "batch_file",
              "tool_identities", "workers", "cache_policy", "jobs", "totals"],
    "job": ["job_id", "vars", "status", "seconds", "worker",
            "manifest", "manifest_sha256",
            "outputs", "measured_loudness", "normalize", "gate", "cache",
            "failures"],
    "totals": ["jobs", "ok", "failed", "skipped_resume",
               "cache_hits", "cache_misses", "seconds"],
}


@dataclass
class Job:
    id: str
    vars: dict[str, str]
    out_dir: str            # resolved relative to the batch file's directory
    manifest_rel: str = ""  # template path as written (job-level or default)
    manifest_path: Path | None = None   # resolved template path
    manifest_sha256: str = ""


@dataclass
class BatchSpec:
    path: Path              # the batch file
    dir: Path               # its directory (out_dir/manifest anchors)
    manifest_path: Path | None      # the batch-level DEFAULT template (D-V2)
    manifest_sha256: str | None
    manifests: dict[str, str]       # rel-path (as written) -> sha256 (D-V4)
    jobs: list[Job]
    spec_hash: str          # sha256(canonical spec + template bytes) — D-V3


def load_batch(path: str | Path) -> BatchSpec:
    """Load and validate a batch file. Raises CompileError (M-420/M-421).

    Per-job policy variance (doc 49, D-V2): a job may name its own
    `manifest:`; the batch-level `manifest:` is the default and becomes
    optional. Policy variance across a batch is expressed by templates,
    never by batch-file policy overrides (ADR-5).
    """
    p = Path(path)
    c = Collector()
    if not p.is_file():
        raise CompileError([Diagnostic("M-420", "batch",
                                       f"file not found: {p}")])
    try:
        import yaml
        data = yaml.safe_load(p.read_bytes())
    except Exception as e:  # yaml.YAMLError, OSError
        raise CompileError([Diagnostic("M-420", "batch",
                                       f"parse error: {e}")]) from e
    if not isinstance(data, dict):
        raise CompileError([Diagnostic("M-420", "batch",
                                       "top level must be a mapping")])
    if data.get("loom_batch") != SUPPORTED_BATCH_VERSION:
        c.add("M-420", "loom_batch",
              f"expected `loom_batch: {SUPPORTED_BATCH_VERSION}`, "
              f"got {data.get('loom_batch')!r}")
    man_rel = data.get("manifest")
    if man_rel is not None and (not isinstance(man_rel, str) or not man_rel):
        c.add("M-420", "manifest",
              "batch-level `manifest:` must be a non-empty string when given")
        man_rel = None
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        c.add("M-420", "defaults", "expected a mapping")
        defaults = {}
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        c.add("M-420", "jobs", "a non-empty `jobs:` list is required")
        jobs_raw = []
    c.raise_if_any()

    jobs: list[Job] = []
    seen: dict[str, int] = {}
    any_job_manifest = False
    for i, j in enumerate(jobs_raw):
        jpath = f"jobs[{i}]"
        if not isinstance(j, dict):
            c.add("M-420", jpath, f"expected mapping, got {type(j).__name__}")
            continue
        jvars = j.get("vars") or {}
        if not isinstance(jvars, dict):
            c.add("M-420", f"{jpath}.vars", "expected a mapping")
            jvars = {}
        jvars = {str(k): str(v) for k, v in jvars.items()}
        jid = j.get("id")
        if jid is None:
            vh = hashlib.sha256(canon(jvars).encode()).hexdigest()[:8]
            jid = f"job{i:02d}-{vh}"
        elif not isinstance(jid, str) or not jid:
            c.add("M-420", f"{jpath}.id", "expected a non-empty string")
            continue
        if jid in seen:
            c.add("M-421", f"{jpath}.id",
                  f"duplicate job id {jid!r} (also jobs[{seen[jid]}])")
            continue
        seen[jid] = i
        jman = j.get("manifest")
        if jman is not None and (not isinstance(jman, str) or not jman):
            c.add("M-420", f"{jpath}.manifest",
                  "job `manifest:` must be a non-empty string when given")
            continue
        if jman is not None:
            any_job_manifest = True
        jman_rel = jman if jman is not None else man_rel
        if jman_rel is None:
            c.add("M-420", f"{jpath}.manifest",
                  f"job {jid!r} has no template (no job-level `manifest:` "
                  "and no batch-level default)")
            continue
        out_dir = j.get("out_dir", defaults.get("out_dir"))
        if not isinstance(out_dir, str) or not out_dir:
            c.add("M-420", f"{jpath}.out_dir",
                  "no `out_dir` (job-level or defaults.out_dir)")
            continue
        out_dir = substitute_str(out_dir, jvars, f"{jpath}.out_dir", c)
        jobs.append(Job(id=jid, vars=jvars, out_dir=out_dir,
                        manifest_rel=jman_rel))
    c.raise_if_any()

    # resolve + hash every referenced template (job-scoped M-420 on a miss)
    manifests: dict[str, str] = {}
    bytes_by_rel: dict[str, bytes] = {}
    for jb in jobs:
        rel = jb.manifest_rel
        if rel not in bytes_by_rel:
            mp = (p.parent / rel).resolve()
            if not mp.is_file():
                c.add("M-420", f"jobs[{jb.id}].manifest",
                      f"template manifest not found: {mp}")
                continue
            bytes_by_rel[rel] = mp.read_bytes()
            manifests[rel] = hashlib.sha256(bytes_by_rel[rel]).hexdigest()
        jb.manifest_path = (p.parent / rel).resolve()
        jb.manifest_sha256 = manifests[rel]
    if man_rel is not None and man_rel not in bytes_by_rel:
        # default template declared but referenced by no job — still must
        # exist and still enters the batch identity (it IS spec text).
        mp = (p.parent / man_rel).resolve()
        if not mp.is_file():
            c.add("M-420", "manifest",
                  f"template manifest not found: {mp}")
        else:
            bytes_by_rel[man_rel] = mp.read_bytes()
            manifests[man_rel] = hashlib.sha256(
                bytes_by_rel[man_rel]).hexdigest()
    c.raise_if_any()

    if not any_job_manifest:
        # Legacy spec-hash, byte-identical to the 0.6.0 formula (D-V3):
        # an upgraded Loom resumes a pre-upgrade state dir.
        spec_hash = hashlib.sha256(
            (canon({"manifest": man_rel, "defaults": defaults,
                     "jobs": [{"id": jb.id, "vars": jb.vars,
                               "out_dir": jb.out_dir} for jb in jobs]})
             + bytes_by_rel[man_rel].hex()).encode()).hexdigest()
    else:
        spec_hash = hashlib.sha256(canon(
            {"manifest": man_rel, "defaults": defaults,
             "jobs": [{"id": jb.id, "vars": jb.vars, "out_dir": jb.out_dir,
                       "manifest": jb.manifest_rel} for jb in jobs],
             "templates": manifests}).encode()).hexdigest()

    default_path = (p.parent / man_rel).resolve() if man_rel else None
    return BatchSpec(path=p, dir=p.parent.resolve(),
                     manifest_path=default_path,
                     manifest_sha256=manifests.get(man_rel) if man_rel
                     else None,
                     manifests=manifests,
                     jobs=jobs, spec_hash=spec_hash)


@dataclass
class JobResult:
    job: Job
    status: str                       # ok | failed | skipped-resume
    seconds: float = 0.0
    worker: str = ""
    outputs: dict[str, str] = field(default_factory=dict)   # rel out -> sha256
    failures: list[str] = field(default_factory=list)
    ledger: dict = field(default_factory=dict)              # per-job run ledger


class BatchError(Exception):
    """Runtime batch-level refusal (journal/spec mismatch etc.)."""


class _Journal:
    def __init__(self, path: Path, spec_hash: str):
        self.path = path
        self.spec_hash = spec_hash
        self.lock = threading.Lock()

    def read(self) -> dict[str, dict]:
        """job_id -> last journal record. Hard error on spec mismatch."""
        if not self.path.is_file():
            return {}
        out: dict[str, dict] = {}
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue   # torn tail line from a kill — ignore, re-run
            if rec.get("spec") != self.spec_hash:
                raise BatchError(
                    f"journal {self.path} belongs to batch spec "
                    f"{rec.get('spec', '?')[:12]}… but this batch is "
                    f"{self.spec_hash[:12]}… — two different batches never "
                    "share a state dir (use --state-dir or --fresh)")
            if rec.get("job_id"):
                out[rec["job_id"]] = rec
        return out

    def append(self, rec: dict) -> None:
        rec = {"spec": self.spec_hash, **rec}
        with self.lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())


def _tool_identity_map(root: Path) -> dict[str, str]:
    out = {}
    for tool in sorted(BINARIES):
        try:
            out[tool] = sha256_file(binary(root, tool))
        except ToolchainError:
            out[tool] = f"missing:{tool}"
    return out


def run_batch(spec: BatchSpec, workers: int | None = None,
              state_dir: str | Path | None = None, fresh: bool = False,
              no_cache: bool = False, toolchain: str | None = None,
              log: Callable[[str], None] = lambda s: None) -> tuple[int, Path]:
    """Execute a batch. Returns (rc, batch_ledger_path); rc 2 iff any job
    failed — a failed job never aborts the others (PRD title-#713)."""
    t0 = time.monotonic()
    workers = workers or min(4, os.cpu_count() or 2)
    state = Path(state_dir) if state_dir else (
        spec.dir / ".loom-batch" / spec.spec_hash[:8])
    state.mkdir(parents=True, exist_ok=True)
    tc_root = resolve_root(toolchain)
    journal = _Journal(state / "journal.jsonl", spec.spec_hash)
    prior = {} if fresh else journal.read()
    cache = Cache(root=state / "cache", enabled=not no_cache)

    # ---- compile every job up front (collisions die here, M-421) -----------
    plans: dict[str, Plan] = {}
    results: dict[str, JobResult] = {}
    out_paths: dict[Path, str] = {}
    coll = Collector()
    for job in spec.jobs:
        try:
            m = load_manifest(job.manifest_path, variables=job.vars)
            plan = compile_manifest(m)
        except CompileError as e:
            results[job.id] = JobResult(
                job=job, status="failed",
                failures=[str(d) for d in e.diagnostics])
            continue
        plans[job.id] = plan
        for tp in plan.targets:
            ap = (spec.dir / job.out_dir / tp.out).resolve()
            if ap in out_paths:
                coll.add("M-421", f"jobs[{job.id}]",
                         f"output path {ap} collides with job "
                         f"{out_paths[ap]!r} — two jobs must never write "
                         "the same file")
            out_paths[ap] = job.id
    coll.raise_if_any()

    # ---- resume: skips are earned by hash, never by journal say-so ---------
    to_run: list[Job] = []
    for job in spec.jobs:
        if job.id in results:      # failed compile above
            continue
        rec = prior.get(job.id)
        if rec and rec.get("ok"):
            verified = True
            for rel, sha in rec.get("outputs", {}).items():
                fp = spec.dir / job.out_dir / rel
                if not fp.is_file() or sha256_file(fp) != sha:
                    verified = False
                    break
            if verified:
                results[job.id] = JobResult(
                    job=job, status="skipped-resume",
                    outputs=dict(rec.get("outputs", {})),
                    worker=rec.get("worker", ""),
                    seconds=rec.get("seconds", 0.0))
                log(f"resume: {job.id} verified, skipped")
                continue
            log(f"resume: {job.id} journal says ok but outputs do not "
                "verify — re-running")
        to_run.append(job)

    # ---- execute (N workers, job granularity) ------------------------------
    def run_one(job: Job) -> JobResult:
        jt0 = time.monotonic()
        worker = threading.current_thread().name
        try:
            out_dir = spec.dir / job.out_dir
            work_dir = state / "work" / job.id
            m = load_manifest(job.manifest_path, variables=job.vars)
            plan = plans[job.id]
            ex = Executor(plan, job.manifest_path.parent, out_dir, work_dir,
                          toolchain=toolchain,
                          validate_policy=m.policy.validate,
                          cache=cache,
                          ledger_extra={"job_id": job.id, "vars": job.vars})
            res = ex.run()
            outputs = {out: entry["sha256"]
                       for out, entry in ex.ledger["outputs"].items()}
            ok, failures, ledger = res.ok, list(res.failures), ex.ledger
        except Exception as e:   # a crashed job is a failed job, never a
            outputs, ok = {}, False           # crashed batch (PRD #713)
            failures, ledger = [f"job crashed: {e!r}"], {}
        jr = JobResult(job=job, status="ok" if ok else "failed",
                       seconds=round(time.monotonic() - jt0, 3),
                       worker=worker, outputs=outputs,
                       failures=failures, ledger=ledger)
        journal.append({
            "job_id": job.id, "ok": ok, "outputs": outputs,
            "seconds": jr.seconds, "worker": worker,
            "cache_hits": sum(1 for v in ledger.get("cache", {}).values()
                              if v == "hit"),
            "cache_misses": sum(1 for v in ledger.get("cache", {}).values()
                                if v == "miss"),
        })
        log(f"{'ok    ' if ok else 'FAILED'} {job.id} "
            f"({jr.seconds:.1f}s, {worker})")
        return jr

    if to_run:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for jr in pool.map(run_one, to_run):
                results[jr.job.id] = jr

    # journal failed-compile jobs too (resume re-runs them after a fix)
    for job in spec.jobs:
        r = results[job.id]
        if r.status == "failed" and not r.ledger and job.id not in prior:
            journal.append({"job_id": job.id, "ok": False, "outputs": {},
                            "seconds": 0.0, "worker": "",
                            "compile_diagnostics": r.failures})

    # ---- the batch ledger: the R6 contract (D-Q7) --------------------------
    jobs_out = []
    for job in spec.jobs:
        r = results[job.id]
        led = r.ledger
        jobs_out.append({
            "job_id": job.id, "vars": job.vars, "status": r.status,
            "seconds": r.seconds, "worker": r.worker,
            "out_dir": job.out_dir,
            "manifest": job.manifest_rel,
            "manifest_sha256": job.manifest_sha256,
            "outputs": led.get("outputs",
                               {o: {"sha256": s}
                                for o, s in r.outputs.items()}),
            "measured_loudness": led.get("measured_loudness", {}),
            "normalize": led.get("normalize", {}),
            "gate": led.get("gate", {}),
            "cache": led.get("cache", {}),
            "failures": r.failures,
        })
    n_ok = sum(1 for j in jobs_out if j["status"] == "ok")
    n_failed = sum(1 for j in jobs_out if j["status"] == "failed")
    n_resumed = sum(1 for j in jobs_out if j["status"] == "skipped-resume")
    hits = sum(1 for j in jobs_out for v in j["cache"].values() if v == "hit")
    misses = sum(1 for j in jobs_out
                 for v in j["cache"].values() if v == "miss")
    ledger = {
        "loom_version": __version__,
        "spec_hash": spec.spec_hash,
        "manifest_sha256": spec.manifest_sha256,
        "manifests": spec.manifests,
        "batch_file": str(spec.path),
        "tool_identities": _tool_identity_map(tc_root),
        "workers": workers,
        "cache_policy": "off (--no-cache)" if no_cache else "readwrite",
        "resume": not fresh,
        "jobs": jobs_out,
        "totals": {
            "jobs": len(jobs_out), "ok": n_ok, "failed": n_failed,
            "skipped_resume": n_resumed,
            "cache_hits": hits, "cache_misses": misses,
            "seconds": round(time.monotonic() - t0, 3),
        },
    }
    ledger_path = state / "batch-ledger.json"
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n")
    return (0 if n_failed == 0 else 2), ledger_path
