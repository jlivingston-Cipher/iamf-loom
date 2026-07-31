"""Execution plan — the deterministic product of compilation (D-L4).

A plan is a serializable DAG of subprocess steps. Every argv is fully
materialized at compile time up to two token classes:

  $TOOLCHAIN / $WORK / $OUTDIR   — environment roots, resolved at execution
  ${measure:<step_id>:<key>}     — values produced by an earlier measure step
                                   in the same plan (il / dp / tp), the
                                   data-dependent edges of the DAG
  ${gain:<step_id>:<target>}     — R3: `target − measured il` of an earlier
                                   measure step, in dB (%.2f) — the gain-ride
                                   amount; target is a compile-time constant
                                   baked into the token, the measurement is
                                   execution-time (computed, never typed)

Plans embed generated config files (textprotos) inline, so a plan is fully
self-contained and golden-snapshot-testable byte for byte.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PLAN_VERSION = 1
TOOLCHAIN = "$TOOLCHAIN"
WORK = "$WORK"
OUTDIR = "$OUTDIR"
SRCDIR = "$SRCDIR"   # the manifest's directory (source-relative paths)


@dataclass
class Step:
    id: str
    kind: str            # write_config | stage_input | encode | render |
                         # measure_bs1770 | remux | copy | gain_ride |
                         # verify_loudness | verify_preview
    tool: str            # encoder_main | decoder_main | ffmpeg | mp4box | internal
    argv: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    content: str | None = None      # write_config: file body (inline)
    src: str | None = None          # stage_input / copy
    dst: str | None = None
    argv_secondary: list[str] = field(default_factory=list)  # measure: astats
    params: dict[str, Any] | None = None   # verify_loudness: method/target/…
    rationale: str = ""

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "tool": self.tool,
        }
        if self.argv:
            d["argv"] = self.argv
        if self.argv_secondary:
            d["argv_secondary"] = self.argv_secondary
        if self.src is not None:
            d["src"] = self.src
        if self.dst is not None:
            d["dst"] = self.dst
        if self.content is not None:
            d["content"] = self.content
        if self.params is not None:
            d["params"] = self.params
        if self.reads:
            d["reads"] = self.reads
        if self.writes:
            d["writes"] = self.writes
        if self.rationale:
            d["rationale"] = self.rationale
        return d


@dataclass
class TargetPlan:
    out: str
    format: str
    backend: str         # iamftools | ffmpeg_oneshot
    muxer: str | None    # mp4box | ffmpeg | None
    preset: str | None
    profile: str         # derived profile actually encoded
    step_ids: list[str] = field(default_factory=list)
    rationale: str = ""
    gate_path: str | None = None   # R9: gate the intermediate (preview only);
                                   # emitted only when set — existing targets'
                                   # bytes untouched by construction (D-Y4)

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "out": self.out, "format": self.format, "backend": self.backend,
            "muxer": self.muxer, "preset": self.preset, "profile": self.profile,
            "steps": self.step_ids, "rationale": self.rationale,
        }
        if self.gate_path is not None:
            d["gate_path"] = self.gate_path
        return d


@dataclass
class Plan:
    title: str
    manifest_sha256: str
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    targets: list[TargetPlan] = field(default_factory=list)

    def add(self, step: Step) -> str:
        self.steps.append(step)
        return step.id

    def to_json(self) -> dict[str, Any]:
        return {
            "loom_plan": PLAN_VERSION,
            "title": self.title,
            "manifest_sha256": self.manifest_sha256,
            "tokens": {"toolchain": TOOLCHAIN, "work": WORK,
                       "outdir": OUTDIR, "srcdir": SRCDIR},
            "sources": self.sources,
            "steps": [s.to_json() for s in self.steps],
            "targets": [t.to_json() for t in self.targets],
        }

    def dumps(self) -> str:
        # sort_keys=False: dict construction order is deterministic by design;
        # key order carries the reading order (steps before targets, etc.).
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n"


def measure_token(step_id: str, key: str) -> str:
    return "${measure:" + step_id + ":" + key + "}"


def gain_token(step_id: str, target_lufs: float) -> str:
    """R3 gain-ride token: resolves to `target − measured il` dB (%.2f)."""
    return "${gain:" + step_id + ":" + f"{target_lufs:.2f}" + "}"
