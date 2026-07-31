"""Plan executor — runs the compiled DAG as subprocesses (ADR-4 boundary).

Success is judged by output-file existence AND size, never exit code alone
(F8: iamfdec-class tools can exit 0 having produced nothing). Every run
writes a machine-readable ledger (the R6 run-ledger seed): step results,
measured loudness, output hashes, tool identities.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .gate import run_gate
from .plan import Plan, Step
from .toolchain import ToolchainError, binary, resolve_root
from .util import sha256_file

_MEASURE_RE = re.compile(r"\$\{measure:([^:}]+):([^:}]+)\}")
_GAIN_RE = re.compile(r"\$\{gain:([^:}]+):([^:}]+)\}")

CLIP_GUARD_DBFS = -0.05    # R3: a ride may never reach full scale

# anchor layout name -> IAMF sound_system value (bitstream verify sanity)
_ANCHOR_SS = {"stereo": 0, "5.1": 1, "7.1.4": 9}


class ExecutionError(Exception):
    pass


@dataclass
class RunResult:
    ok: bool
    ledger_path: Path | None
    failures: list[str] = field(default_factory=list)


def _parse_kernel_json(stdout: str) -> dict[str, float]:
    """sentinel-dsp JSON -> {il, dp, tp} (F34: one conformant engine for all
    three injected figures; replaces the ebur128/astats stderr scraping and
    retires the F7 LAST-`I:`-line parsing trap with it)."""
    try:
        doc = json.loads(stdout)
    except ValueError as e:
        raise ExecutionError(f"sentinel-dsp emitted unparsable JSON: {e}")
    try:
        return {"il": float(doc["integrated_lufs"]),
                "dp": float(doc["digital_peak_dbfs"]),
                "tp": float(doc["true_peak_dbtp"])}
    except (KeyError, TypeError) as e:
        raise ExecutionError(f"sentinel-dsp JSON missing measure field: {e}")


class Executor:
    def __init__(self, plan: Plan, manifest_dir: Path, out_dir: Path,
                 work_dir: Path, toolchain: str | None = None,
                 validate_policy: str = "fail_on_error",
                 cache=None, ledger_extra: dict | None = None):
        self.plan = plan
        self.manifest_dir = Path(manifest_dir)
        self.out_dir = Path(out_dir)
        self.work_dir = Path(work_dir)
        self.toolchain = resolve_root(toolchain)
        self.validate_policy = validate_policy
        self.cache = cache          # loom.cache.Cache | None (R6, D-Q4)
        self.measured: dict[str, dict[str, float]] = {}
        self.ledger: dict = {
            "loom_version": __version__,
            "title": plan.title,
            "manifest_sha256": plan.manifest_sha256,
            "toolchain": str(self.toolchain) if self.toolchain is not None else None,
            "steps": [],
            "measured_loudness": {},
            "normalize": {},
            "gate": {},
            "stts_repair": {},
            "outputs": {},
        }
        if cache is not None:
            self.ledger["cache"] = {}   # out path -> hit | miss | off
        if ledger_extra:
            self.ledger.update(ledger_extra)

    # ---- token resolution ----------------------------------------------------
    def _resolve(self, s: str) -> str:
        if self.toolchain is not None:
            s = s.replace("$TOOLCHAIN", str(self.toolchain))
        s = (s.replace("$WORK", str(self.work_dir))
              .replace("$OUTDIR", str(self.out_dir))
              .replace("$SRCDIR", str(self.manifest_dir)))

        def sub(mm: re.Match) -> str:
            sid, key = mm.group(1), mm.group(2)
            if sid not in self.measured:
                raise ExecutionError(
                    f"token references measure step {sid!r} which has not run")
            return f"{self.measured[sid][key]:.2f}"

        def sub_gain(mm: re.Match) -> str:
            sid, target = mm.group(1), float(mm.group(2))
            if sid not in self.measured:
                raise ExecutionError(
                    f"gain token references measure step {sid!r} "
                    "which has not run")
            gain = target - self.measured[sid]["il"]
            rec = self.ledger["normalize"].setdefault(sid, {})
            rec.update({"target_lufs": target,
                        "pre_ride_il": self.measured[sid]["il"],
                        "applied_gain_db": round(gain, 2)})
            return f"{gain:.2f}"

        return _GAIN_RE.sub(sub_gain, _MEASURE_RE.sub(sub, s))

    def _resolve_argv(self, argv: list[str]) -> list[str]:
        out = []
        for a in argv:
            out.append(self._resolve(a))
        return out

    def _tool_argv0(self, step: Step, argv: list[str]) -> list[str]:
        if step.tool in ("encoder_main", "decoder_main", "ffmpeg", "mp4box",
                         "sentinel-dsp"):
            argv = [str(binary(self.toolchain, step.tool))] + argv[1:]
        return argv

    # ---- steps ---------------------------------------------------------------
    # X-76a dispatch: one handler per step kind (encode/render/remux share the
    # plain-subprocess default). Each handler mutates `rec` in place and may
    # raise ExecutionError — the caller's contract is unchanged.
    def _run_step(self, step: Step) -> dict:
        t0 = time.monotonic()
        rec: dict = {"id": step.id, "kind": step.kind, "tool": step.tool}
        handler = self._STEP_HANDLERS.get(step.kind, Executor._step_subprocess)
        handler(self, step, rec)
        rec["seconds"] = round(time.monotonic() - t0, 3)
        return rec

    def _step_stage_or_copy(self, step: Step, rec: dict) -> None:
        """stage_input and copy: one file, staged verbatim (F8 size check)."""
        src = Path(self._resolve(step.src or ""))
        dst = Path(self._resolve(step.dst or ""))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        rec["ok"] = dst.is_file() and dst.stat().st_size > 0

    def _step_write_config(self, step: Step, rec: dict) -> None:
        dst = Path(self._resolve(step.dst or ""))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(step.content or "")
        rec["ok"] = dst.is_file() and dst.stat().st_size > 0

    def _step_gain_ride(self, step: Step, rec: dict) -> None:
        # R3: apply the ride, then guard against clipping (astats on the
        # RIDDEN file; Loom is not a limiter).
        a1 = self._tool_argv0(step, self._resolve_argv(step.argv))
        for w in step.writes:
            Path(self._resolve(w)).parent.mkdir(parents=True, exist_ok=True)
        r1 = subprocess.run(a1, capture_output=True, text=True)
        outputs_ok = all(
            Path(self._resolve(w)).is_file()
            and Path(self._resolve(w)).stat().st_size > 0
            for w in step.writes)
        rec["rc"] = r1.returncode
        rec["ok"] = r1.returncode == 0 and outputs_ok
        if rec["ok"]:
            a2 = self._tool_argv0(step, self._resolve_argv(step.argv_secondary))
            r2 = subprocess.run(a2, capture_output=True, text=True)
            dpm = re.search(r"Peak level dB:\s*(-?[\d.]+)", r2.stderr)
            if r2.returncode != 0 or dpm is None:
                raise ExecutionError(
                    "post-ride astats produced no peak level")
            ridden_peak = float(dpm.group(1))
            rec["ridden_peak_dbfs"] = ridden_peak
            for nrec in self.ledger["normalize"].values():
                if "applied_gain_db" in nrec and "ridden_peak_dbfs" not in nrec:
                    nrec["ridden_peak_dbfs"] = ridden_peak
            if ridden_peak > CLIP_GUARD_DBFS:
                raise ExecutionError(
                    f"clip guard: gain-ride would reach {ridden_peak:.2f} "
                    f"dBFS (> {CLIP_GUARD_DBFS} dBFS) — the normalize "
                    "target needs limiting, which Loom refuses to do "
                    "silently (choose a lower target or limit upstream)")
        else:
            rec["stderr_tail"] = (r1.stderr or r1.stdout).strip().splitlines()[-8:]

    def _step_verify_loudness(self, step: Step, rec: dict) -> None:
        p = step.params or {}
        target = float(p["target"])
        tol = float(p["tolerance"])
        anchor = p["anchor"]
        if p["method"] == "measured":
            sid = p["measure_step"]
            if sid not in self.measured:
                raise ExecutionError(
                    f"verify_loudness: measure step {sid!r} has not run")
            got = self.measured[sid]["il"]
            how = "re-measured (injected) values"
        else:  # bitstream — read back what encoder_main embedded
            from sentinel.parser import parse_bytes
            fpath = Path(self._resolve(p["path"]))
            model = parse_bytes(fpath.read_bytes(), source=str(fpath))
            want_ss = _ANCHOR_SS.get(anchor)
            got = None
            for mp in model.mix_presentations:
                for sm in mp.sub_mixes:
                    for lay in sm.layouts:
                        if lay.sound_system == want_ss:
                            got = lay.integrated_loudness
                            break
                    if got is not None:
                        break
                if got is not None:
                    break
            if got is None:
                raise ExecutionError(
                    f"verify_loudness: no {anchor} (sound_system "
                    f"{want_ss}) loudness layout found in {fpath.name}")
            how = "bitstream read-back (sentinel parser)"
        delta = got - target
        verdict = abs(delta) <= tol
        rec.update({"anchor": anchor, "target_lufs": target,
                    "post_ride_il": got, "delta_lu": round(delta, 3),
                    "method": how, "ok": verdict})
        self.ledger["normalize"][step.id] = {
            "anchor": anchor, "target_lufs": target, "post_ride_il": got,
            "delta_lu": round(delta, 3), "tolerance_lu": tol,
            "method": how, "within_tolerance": verdict,
        }
        if not verdict:
            rec["error"] = (
                f"normalize missed: {anchor} landed {got:.2f} LUFS vs "
                f"target {target:.2f} (Δ {delta:+.2f} LU > ±{tol})")

    def _step_verify_preview(self, step: Step, rec: dict) -> None:
        # R9 accept (doc 46, D-Y6): structural + spectral floor, on the
        # pre-encode binaural WAV, F8-discipline throughout.
        p = step.params or {}
        argv = self._tool_argv0(step, self._resolve_argv(step.argv))
        r = subprocess.run(argv, capture_output=True, text=True)
        peaks = [float(x) for x in re.findall(
            r"Peak level dB:\s*([-+]?(?:[\d.]+|inf))", r.stderr)]
        per_channel = peaks[:-1] if len(peaks) > 1 else []
        overall = peaks[-1] if peaks else float("-inf")
        from .wavinfo import read_wav_info
        wi = read_wav_info(Path(self._resolve(p["wav"])))
        sid = p["measure_step"]
        if sid not in self.measured:
            raise ExecutionError(
                f"verify_preview: measure step {sid!r} has not run")
        il = self.measured[sid]["il"]
        problems: list[str] = []
        if r.returncode != 0:
            problems.append("per-channel astats failed "
                            f"(rc {r.returncode})")
        if wi.channels != int(p["expect_channels"]):
            problems.append(f"channels {wi.channels} != "
                            f"{p['expect_channels']} (binaural stereo)")
        if wi.sample_rate != int(p["expect_sample_rate"]):
            problems.append(f"sample rate {wi.sample_rate} != source "
                            f"{p['expect_sample_rate']}")
        if wi.frames != int(p["expect_frames"]):
            problems.append(f"frame count {wi.frames} != source "
                            f"{p['expect_frames']} (render must be "
                            "sample-exact — WP1 round-trip precedent)")
        if il <= float(p["silence_floor_lufs"]):
            problems.append(f"measured IL {il:.2f} LUFS at/below the "
                            f"{p['silence_floor_lufs']:g} gating floor — "
                            "a silent review copy reviews nothing")
        rec.update({"channels": wi.channels, "sample_rate": wi.sample_rate,
                    "bits": wi.bits_per_sample, "frames": wi.frames,
                    "il_lufs": il, "overall_peak_dbfs": overall,
                    "per_channel_peak_dbfs": per_channel,
                    "container": p["container"],
                    "ok": not problems})
        self.ledger.setdefault("preview", {})[step.id] = {
            "container": p["container"], "channels": wi.channels,
            "sample_rate": wi.sample_rate, "bits": wi.bits_per_sample,
            "frames": wi.frames, "il_lufs": il,
            "overall_peak_dbfs": overall,
            "per_channel_peak_dbfs": per_channel,
            "loudness_note": "recorded only — wav/ogg carry no IAMF "
                             "loudness structures (R9)",
            "oracle_note": "single-oracle render: iamfdec binauralizer "
                           "disabled toolchain-wide (F10)",
            "ok": not problems,
        }
        if problems:
            rec["error"] = "preview verify failed: " + "; ".join(problems)

    def _step_measure_bs1770(self, step: Step, rec: dict) -> None:
        argv = self._tool_argv0(step, self._resolve_argv(step.argv))
        r = subprocess.run(argv, capture_output=True, text=True)
        rec["rc"] = r.returncode
        if r.returncode != 0:
            rec["ok"] = False
            rec["stderr_tail"] = (r.stderr or r.stdout).strip().splitlines()[-8:]
        else:
            vals = _parse_kernel_json(r.stdout)
            self.measured[step.id] = vals
            self.ledger["measured_loudness"][step.id] = vals
            rec["measured"] = vals
            rec["ok"] = True

    def _step_repair_stts(self, step: Step, rec: dict) -> None:
        """repair_stts: F32 in-product container repair (item 13, doc 84).

        A RepairError is an execution error, never a silent pass-through
        (the doc-35 discipline): a guess could corrupt the deliverable and
        a skip would ship the defect this step exists to remove."""
        from .repair import RepairError, repair_stts
        mp4 = self._resolve((step.params or {})["path"])
        try:
            result = repair_stts(mp4)
        except RepairError as e:
            raise ExecutionError(f"stts repair on {mp4}: {e}") from None
        rec["repair"] = result
        out_key = (step.params or {}).get("out", mp4)
        self.ledger["stts_repair"][out_key] = result
        rec["ok"] = True

    def _step_subprocess(self, step: Step, rec: dict) -> None:
        """encode | render | remux — plain subprocess, judged by outputs (F8)."""
        argv = self._tool_argv0(step, self._resolve_argv(step.argv))
        for w in step.writes:
            Path(self._resolve(w)).parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(argv, capture_output=True, text=True)
        rec["rc"] = r.returncode
        outputs_ok = True
        for w in step.writes:
            wp = Path(self._resolve(w))
            if not (wp.is_file() and wp.stat().st_size > 0):
                outputs_ok = False  # F8: judge by outputs, not rc
        rec["ok"] = (r.returncode == 0) and outputs_ok
        if not rec["ok"]:
            rec["stderr_tail"] = (r.stderr or r.stdout).strip().splitlines()[-8:]

    _STEP_HANDLERS = {
        "stage_input": _step_stage_or_copy,
        "write_config": _step_write_config,
        "copy": _step_stage_or_copy,
        "gain_ride": _step_gain_ride,
        "verify_loudness": _step_verify_loudness,
        "verify_preview": _step_verify_preview,
        "measure_bs1770": _step_measure_bs1770,
        "repair_stts": _step_repair_stts,
    }

    # ---- R6 cache paths (D-Q4) ----------------------------------------------
    def _cache_hit(self, tp) -> bool:
        """Try the content-addressed cache for this target; replay on hit."""
        from .cache import target_key
        key = target_key(self.plan, tp, self.manifest_dir, self.toolchain,
                         self.cache.tool_memo)
        self._cache_keys[tp.out] = key
        meta = self.cache.lookup(key)
        if meta is None:
            self.ledger["cache"][tp.out] = "miss"
            return False
        outp = self.out_dir / tp.out
        outp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.cache.artifact(key, meta), outp)
        entry = dict(meta["output_entry"])
        entry["cache"] = "hit"
        self.ledger["outputs"][tp.out] = entry
        gate = dict(meta["gate"])
        gate["cached"] = True   # verdict replayed: the bytes are proven
        self.ledger["gate"][tp.out] = gate  # identical to bytes that passed
        if meta.get("stts_repair"):
            rep = dict(meta["stts_repair"])
            rep["cached"] = True   # same bytes -> same repair evidence
            self.ledger["stts_repair"][tp.out] = rep
        self.ledger["measured_loudness"].update(
            meta.get("measured_loudness", {}))
        self.ledger["normalize"].update(meta.get("normalize", {}))
        if meta.get("preview"):
            self.ledger.setdefault("preview", {}).update(meta["preview"])
        self.ledger["cache"][tp.out] = "hit"
        return True

    def _cache_admit(self, tp, outp: Path) -> None:
        """Admit a gate-passed output (the only admissible kind, D-Q4)."""
        ids = set(tp.step_ids)
        meta = {
            "sha256": self.ledger["outputs"][tp.out]["sha256"],
            "output_entry": self.ledger["outputs"][tp.out],
            "gate": self.ledger["gate"][tp.out],
            "stts_repair": self.ledger["stts_repair"].get(tp.out),
            "measured_loudness": {
                k: v for k, v in self.ledger["measured_loudness"].items()
                if k in ids},
            "normalize": {
                k: v for k, v in self.ledger["normalize"].items()
                if k in ids},
            "preview": {
                k: v for k, v in self.ledger.get("preview", {}).items()
                if k in ids},
        }
        self.cache.admit(self._cache_keys[tp.out], outp, meta)

    def run(self) -> RunResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []
        done: set[str] = set()
        skip_targets: set[int] = set()
        self._cache_keys: dict[str, str] = {}
        use_cache = self.cache is not None and self.cache.enabled
        if self.cache is not None and not self.cache.enabled:
            self.ledger["cache_policy"] = "off (--no-cache)"
        for ti, tp in enumerate(self.plan.targets):
            if use_cache and self._cache_hit(tp):
                continue
            for sid in tp.step_ids:
                if sid in done or ti in skip_targets:
                    continue
                step = next(s for s in self.plan.steps if s.id == sid)
                try:
                    rec = self._run_step(step)
                except (ExecutionError, ToolchainError, OSError) as e:
                    rec = {"id": step.id, "kind": step.kind, "tool": step.tool,
                           "ok": False, "error": str(e)}
                self.ledger["steps"].append(rec)
                done.add(sid)
                if not rec.get("ok"):
                    failures.append(f"{step.id}: "
                                    f"{rec.get('error', 'step failed')}")
                    skip_targets.add(ti)
                    break
            if ti not in skip_targets:
                outp = self.out_dir / tp.out
                if outp.is_file():
                    self.ledger["outputs"][tp.out] = {
                        "sha256": sha256_file(outp),
                        "bytes": outp.stat().st_size,
                        "backend": tp.backend,
                        "profile": tp.profile,
                    }
                    # ---- R5: the Sentinel gate, on by default -------------
                    if self.validate_policy == "off":
                        self.ledger["gate"][tp.out] = {
                            "tier": "skipped",
                            "note": "validate: off — gate skipped by policy",
                        }
                    else:
                        # R9 (D-Y5): a preview target gates its INTERMEDIATE
                        # .iamf — the deliverable is a render of validated
                        # bytes (wav/ogg are not IAMF; Sentinel judges the
                        # stream the render came from).
                        gate_path = getattr(tp, "gate_path", None)
                        gate_on = (Path(self._resolve(gate_path))
                                   if gate_path else outp)
                        gr = run_gate(gate_on, self.toolchain)
                        gj = gr.to_json()
                        if gate_path:
                            gj["gated_path"] = gate_path
                            gj["note"] = ("preview: the deliverable is a "
                                          "render of gate-validated bytes "
                                          "(R9, D-Y5)")
                        self.ledger["gate"][tp.out] = gj
                        if not gr.passed:
                            what = (gr.execution_error
                                    or f"FAIL findings: {gr.fail_ids}")
                            failures.append(
                                f"gate[{tp.out}]: {what} — a Loom run that "
                                "emits a nonconformant file is a Loom bug "
                                "(PRD goal 4)")
                        elif use_cache:
                            # only gate-PASSED outputs are admitted (D-Q4)
                            self._cache_admit(tp, outp)
        self.ledger["ok"] = not failures
        ledger_path = self.out_dir / "loom-run.json"
        ledger_path.write_text(json.dumps(self.ledger, indent=2) + "\n")
        return RunResult(ok=not failures, ledger_path=ledger_path,
                         failures=failures)
