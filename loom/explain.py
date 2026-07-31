"""`loom explain` (PRD R10) -- render the compiled plan as a human-readable
account of what will run and WHY each backend/policy was chosen.

Pure rendering (doc 45, D-X3): `render_explain(manifest, plan)` is a pure
function of the validated Manifest and the compiled Plan -- no filesystem
access, no toolchain discovery, no wall-clock, no environment reads, and no
version string anywhere in the output (the doc-43 deviation-3 lesson: prose
surfaces must not churn with version bumps). The rationale fields the
compiler has carried since Phase 1 (doc 42, D-L4) are printed verbatim —
explain is a consumer of the plan, never a mutation of it.
"""

from __future__ import annotations

import re
import textwrap

from .model import Manifest
from .plan import Plan, Step

WIDTH = 78
_TOKEN_RE = re.compile(r"\$\{(?:measure|gain):[^}]+\}")


def _wrap(text: str, indent: str, first_indent: str | None = None) -> str:
    return textwrap.fill(
        text, width=WIDTH,
        initial_indent=first_indent if first_indent is not None else indent,
        subsequent_indent=indent,
        break_long_words=False, break_on_hyphens=False)


def _step_tokens(step: Step) -> list[str]:
    """Distinct ${measure:…}/${gain:…} tokens in argv order (dedup, stable)."""
    seen: list[str] = []
    for a in list(step.argv) + list(step.argv_secondary):
        for tok in _TOKEN_RE.findall(a):
            if tok not in seen:
                seen.append(tok)
    return seen


def _sources_section(m: Manifest, plan: Plan, out: list[str]) -> None:
    out.append("SOURCES  (file facts are probed, never typed -- ADR-5)")
    for name in sorted(plan.sources):
        s = plan.sources[name]
        declared = [f"kind {s['kind']}"]
        if s["layout"]:
            declared.append(f"layout {s['layout']}")
        declared.append(f"order {s['order']}")
        if s["norm"]:
            declared.append(f"norm {s['norm']}")
        out.append(f"  {name}: {s['path']}")
        out.append("    declared: " + ", ".join(declared))
        probed = (f"{s['channels']} ch, {s['sample_rate']} Hz, "
                  f"{s['bits']}-bit, {s['frames']} frames, "
                  f"sha256 {s['sha256'][:12]}")
        if s.get("ambisonics_order") is not None:
            probed += f", ambisonics order {s['ambisonics_order']}"
        out.append("    probed:   " + probed)
    out.append("")


def _policy_section(m: Manifest, out: list[str]) -> None:
    p = m.policy
    out.append("POLICY  (as resolved)")
    codec = f"  codec: {p.codec.name}"
    if p.codec.name == "opus":
        codec += (f"  (coupled {p.codec.bitrate_coupled} bps, "
                  f"uncoupled {p.codec.bitrate_uncoupled} bps)")
    out.append(codec)
    out.append(f"  loudness: {p.loudness_mode} -- embedded loudness values are "
               "measured at")
    out.append("    execution on rendered layouts (BS.1770), never typed")
    if p.normalize is not None:
        out.append(_wrap(
            f"normalize: {p.normalize:g} LUFS -- gain-ride the source, then "
            "re-measure the ridden audio; accept within +/-0.3 LU; clip "
            "guard: ridden digital peak must stay below -0.05 dBFS (Loom is "
            "not a limiter)", "    ", "  "))
    prof = f"  profile: {p.profile}"
    if p.profile == "auto":
        prof += " -- derived per target from element/channel arithmetic"
    out.append(prof)
    if p.validate == "off":
        out.append("  validate: off -- the Sentinel gate is SKIPPED "
                   "(ledger-noted, auditable)")
    else:
        out.append(f"  validate: {p.validate} -- every output passes through "
                   "the Sentinel gate at")
        out.append("    run time (L3 rendered QC when oracles are present); "
                   "findings S-320/S-321")
        out.append("    escalate to run-failing on Loom-packaged outputs")
    out.append("")


def _target_section(idx: int, total: int, tp, steps_by_id: dict[str, Step],
                    out: list[str]) -> None:
    out.append(f"TARGET {idx}/{total}: {tp.out}")
    head = f"  format: {tp.format}"
    if tp.preset:
        head += f"   preset: {tp.preset}"
    head += f"   profile: {tp.profile}   backend: {tp.backend}"
    if tp.muxer:
        head += f" + {tp.muxer} mux"
    out.append(head)
    out.append("  why this route:")
    out.append(_wrap(tp.rationale, "    "))
    out.append("  steps (execution order):")
    for n, sid in enumerate(tp.step_ids, 1):
        step = steps_by_id[sid]
        out.append(f"    {n:2d}. {step.id}  [{step.kind}, {step.tool}]")
        io_bits = []
        if step.src is not None:
            io_bits.append(f"from {step.src}")
        if step.dst is not None:
            io_bits.append(f"to {step.dst}")
        if step.writes and step.dst is None:
            io_bits.append("writes " + ", ".join(step.writes))
        if io_bits:
            out.append(_wrap("; ".join(io_bits), "          ", "        "))
        toks = _step_tokens(step)
        if toks:
            out.append(_wrap("resolves at execution: " + ", ".join(toks),
                             "          ", "        "))
        if step.rationale:
            out.append(_wrap("why: " + step.rationale, "          ",
                             "        "))
    out.append("")


def render_explain(m: Manifest, plan: Plan) -> str:
    out: list[str] = []
    out.append(f"LOOM EXPLAIN -- {plan.title}")
    out.append("=" * min(WIDTH, max(24, 14 + len(plan.title))))
    out.append(f"manifest sha256: {plan.manifest_sha256}")
    out.append(f"{len(plan.sources)} source(s) | {len(plan.targets)} "
               f"target(s) | {len(plan.steps)} plan step(s)")
    out.append("")
    _sources_section(m, plan, out)
    _policy_section(m, out)
    steps_by_id = {s.id: s for s in plan.steps}
    for i, tp in enumerate(plan.targets, 1):
        _target_section(i, len(plan.targets), tp, steps_by_id, out)
    out.append("NOTES")
    out.append(_wrap(
        "$TOOLCHAIN / $WORK / $OUTDIR / $SRCDIR are execution-time roots; "
        "plans are environment-portable and contain no absolute paths.",
        "    ", "  "))
    if any(_step_tokens(s) for s in plan.steps):
        out.append(_wrap(
            "${measure:...} / ${gain:...} tokens are data-dependent plan "
            "edges: their values come from BS.1770-4 measurements made "
            "during execution (computed, never typed -- ADR-5).",
            "    ", "  "))
    return "\n".join(out) + "\n"
