"""Content-addressed target cache (R6, D-Q4).

The doc-13 key tuple — (source hash, manifest fragment, tool versions) —
realized from the plan itself. Plans are environment-independent by
construction ($TOOLCHAIN/$WORK/$OUTDIR/$SRCDIR stay unresolved until
execution; golden plans are byte-identical by test), so a target's cache key
is the sha256 of a canonical JSON of:

  (a) the target's plan fragment — TargetPlan + its Steps, with `rationale`
      fields STRIPPED (rationale is explanation, not semantics; prose edits
      must never invalidate a cache);
  (b) the sha256 of every distinct $SRCDIR-referenced file in the fragment
      (covers the audio sources AND the video donor, which plan.sources does
      not hash);
  (c) tool identities — the sha256 of each resolved toolchain binary the
      fragment's steps name (`internal` steps are keyed by the loom version);
  (d) loom_version + PLAN_VERSION.

Only gate-passed outputs are ever admitted. The hit path re-hashes the
stored artifact against its meta (paranoid, cheap); a mismatch or an
unparsable meta is treated as a miss and rebuilt — the store is
self-healing, never trusted blindly. Admission is atomic (tmp +
os.replace); a same-key double-build race is benign (identical bytes,
last writer wins).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .plan import PLAN_VERSION, Plan, TargetPlan
from .toolchain import BINARIES, ToolchainError, binary
from .util import canon, sha256_file

SRCDIR_PREFIX = "$SRCDIR/"


def _strip_rationale(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "rationale"}


def fragment_srcdir_paths(plan: Plan, tp: TargetPlan) -> list[str]:
    """Every distinct $SRCDIR-relative path the fragment's steps touch."""
    rels: set[str] = set()
    ids = set(tp.step_ids)
    for step in plan.steps:
        if step.id not in ids:
            continue
        cands = list(step.reads) + list(step.argv) + list(step.argv_secondary)
        if step.src is not None:
            cands.append(step.src)
        for s in cands:
            if isinstance(s, str) and s.startswith(SRCDIR_PREFIX):
                rels.add(s[len(SRCDIR_PREFIX):])
    return sorted(rels)


def tool_identities(plan: Plan, tp: TargetPlan, toolchain_root: Path,
                    memo: dict[str, str] | None = None) -> dict[str, str]:
    """tool name -> sha256 of the resolved binary (or a named absence)."""
    out: dict[str, str] = {}
    ids = set(tp.step_ids)
    for step in plan.steps:
        if step.id not in ids:
            continue
        tool = step.tool
        if tool in out:
            continue
        if tool not in BINARIES:          # `internal` — loom itself is the tool
            out[tool] = f"loom:{__version__}"
            continue
        if memo is not None and tool in memo:
            out[tool] = memo[tool]
            continue
        try:
            ident = sha256_file(binary(toolchain_root, tool))
        except ToolchainError:
            ident = f"missing:{tool}"
        if memo is not None:
            memo[tool] = ident
        out[tool] = ident
    return out


def target_key(plan: Plan, tp: TargetPlan, manifest_dir: Path,
               toolchain_root: Path,
               tool_memo: dict[str, str] | None = None) -> str:
    ids = set(tp.step_ids)
    steps = [_strip_rationale(s.to_json())
             for s in plan.steps if s.id in ids]
    src_hashes: dict[str, str] = {}
    for rel in fragment_srcdir_paths(plan, tp):
        fp = Path(manifest_dir) / rel
        src_hashes[rel] = sha256_file(fp) if fp.is_file() else "missing"
    payload = {
        "plan_version": PLAN_VERSION,
        "loom_version": __version__,
        "target": _strip_rationale(tp.to_json()),
        "steps": steps,
        "srcdir_files": src_hashes,
        "tools": tool_identities(plan, tp, toolchain_root, tool_memo),
    }
    return hashlib.sha256(canon(payload).encode()).hexdigest()


@dataclass
class Cache:
    root: Path
    enabled: bool = True
    tool_memo: dict[str, str] = field(default_factory=dict)

    def _dir(self, key: str) -> Path:
        return Path(self.root) / key[:2] / key

    def lookup(self, key: str) -> dict | None:
        """Verified meta for `key`, or None. Verification re-hashes the
        stored artifact — a corrupt entry reads as a miss (self-healing)."""
        if not self.enabled:
            return None
        d = self._dir(key)
        meta_p = d / "meta.json"
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            art = d / meta["artifact"]
            if not art.is_file():
                return None
            if sha256_file(art) != meta["sha256"]:
                return None
            return meta
        except (OSError, ValueError, KeyError):
            return None

    def artifact(self, key: str, meta: dict) -> Path:
        return self._dir(key) / meta["artifact"]

    def admit(self, key: str, out_file: Path, meta: dict) -> None:
        """Atomically store `out_file` + meta under `key` (gate-passed
        outputs only — the caller enforces that contract)."""
        if not self.enabled:
            return
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        name = Path(out_file).name
        meta = dict(meta)
        meta["artifact"] = name
        tmp_a = d / (name + ".tmp")
        shutil.copyfile(out_file, tmp_a)
        os.replace(tmp_a, d / name)
        tmp_m = d / "meta.json.tmp"
        tmp_m.write_text(json.dumps(meta, indent=1) + "\n",
                         encoding="utf-8", newline="\n")
        os.replace(tmp_m, d / "meta.json")
