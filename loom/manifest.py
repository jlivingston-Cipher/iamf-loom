"""Manifest loading + schema validation (PRD R1, ADR-5).

Original stdlib validator (no jsonschema): every failure is a stable M- code
with a dotted path and a message that names both sides of any mismatch.
YAML manifests need PyYAML; `.json` manifests parse with the stdlib.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .diagnostics import Collector, CompileError, Diagnostic
from .util import sha256_file
from .layouts import BEDS, MIX_LAYOUTS
from .model import (
    CodecPolicy, Element, Manifest, Policy, Presentation,
    PresentationElement, Source, Target,
)
from .wavinfo import WavError, read_wav_info

SUPPORTED_VERSION = 0
SOURCE_KINDS = {"bed", "ambisonics", "adm"}
CODECS_KNOWN = {"opus", "lpcm", "flac"}   # all supported since Phase 2
PROFILES = {"auto", "simple", "base", "base_enhanced"}
FORMATS = {"iamf", "mp4", "preview"}
PREVIEW_EXTENSIONS = (".wav", ".opus")   # R9 review-copy containers
PRESETS = {"youtube", "archive"}
ROUTES = {"auto", "oneshot", "remux"}
HEADPHONES = {"stereo", "binaural"}
GAIN_RANGE = (-60.0, 20.0)
NORMALIZE_RANGE = (-36.0, -5.0)           # R3: covers -14/-23/-24/-27 practice
IDENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_ident(name: Any, path: str, c: Collector) -> bool:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        c.add("M-207", path,
              f"{name!r} is not a valid identifier (letters, digits, _ , -)")
        return False
    return True


def _parse_bitrate(v: Any, path: str, c: Collector, default: int) -> int:
    if v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        try:
            if s.endswith("k"):
                return int(float(s[:-1]) * 1000)
            return int(s)
        except ValueError:
            pass
    c.add("M-202", path, f"expected a bitrate like '128k' or 128000, got {v!r}")
    return default


def _req(d: dict, key: str, typ, path: str, c: Collector):
    if key not in d:
        c.add("M-201", f"{path}.{key}", f"required field `{key}` is missing")
        return None
    v = d[key]
    if typ is float and isinstance(v, int):
        v = float(v)
    if not isinstance(v, typ):
        c.add("M-202", f"{path}.{key}",
              f"expected {getattr(typ, '__name__', typ)}, got {type(v).__name__}")
        return None
    return v


# ---- {variable} templating (R6, D-Q3) --------------------------------------
# `{name}` in string scalars; `{{`/`}}` escape a literal brace. Bindings are
# strings, period — no expressions, no defaults, no nesting (a template is
# data, not a program). An unbound reference is M-413 naming variable + path.
_VAR_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_str(s: str, variables: dict[str, str],
                   path: str, c: Collector) -> str:
    if "{" not in s and "}" not in s:
        return s

    def sub(m: re.Match) -> str:
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        name = m.group(1)
        if name not in variables:
            c.add("M-413", path,
                  f"unbound variable {{{name}}} (no binding supplied; "
                  "escape a literal brace as {{ }})")
            return tok
        return variables[name]

    return _VAR_RE.sub(sub, s)


def _substitute_tree(node: Any, variables: dict[str, str],
                     path: str, c: Collector) -> Any:
    if isinstance(node, str):
        return substitute_str(node, variables, path, c)
    if isinstance(node, dict):
        return {k: _substitute_tree(v, variables,
                                    f"{path}.{k}" if path else str(k), c)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_tree(v, variables, f"{path}[{i}]", c)
                for i, v in enumerate(node)]
    return node


# ---- `languages:` presentation expansion (R8, D-Z3) -------------------------
# A presentation carrying `languages:` expands to one presentation per row
# BEFORE any schema validation and BEFORE the global --var/batch pass. Row
# keys are presentation-scoped string bindings applied by PARTIAL
# substitution: unknown {refs} and the {{ }} escapes are left intact for the
# global pass (which owns M-413 and resolves escapes exactly once). Row
# bindings shadow global bindings inside their presentation.

def _substitute_partial(node: Any, variables: dict[str, str]) -> Any:
    """Row-binding substitution: known names only; no diagnostics; escapes
    and unknown references pass through untouched for the global pass."""
    if isinstance(node, str):
        if "{" not in node:
            return node

        def sub(mt: re.Match) -> str:
            tok = mt.group(0)
            if tok in ("{{", "}}"):
                return tok           # escapes belong to the global pass
            name = mt.group(1)
            return variables.get(name, tok)

        return _VAR_RE.sub(sub, node)
    if isinstance(node, dict):
        return {k: _substitute_partial(v, variables) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_partial(v, variables) for v in node]
    return node


def _expand_languages(data: Any, c: Collector) -> Any:
    """Expand `languages:` blocks on presentations (pure preprocessing)."""
    pr_raw = data.get("presentations") if isinstance(data, dict) else None
    if not isinstance(pr_raw, list):
        return data
    out: list[Any] = []
    changed = False
    for i, pr in enumerate(pr_raw):
        if not isinstance(pr, dict) or "languages" not in pr:
            out.append(pr)
            continue
        changed = True
        ppath = f"presentations[{i}].languages"
        rows = pr.get("languages")
        if not isinstance(rows, list) or not rows:
            c.add("M-414", ppath, "expected a non-empty list of language rows")
            continue
        seen_langs: set[str] = set()
        template = {k: v for k, v in pr.items() if k != "languages"}
        for j, row in enumerate(rows):
            rpath = f"{ppath}[{j}]"
            if not isinstance(row, dict):
                c.add("M-414", rpath,
                      f"expected a mapping, got {type(row).__name__}")
                continue
            bad = [k for k, v in row.items() if not isinstance(v, str)]
            if bad:
                c.add("M-414", rpath,
                      f"row values must be strings (bindings are data, not "
                      f"programs); non-string keys: {sorted(bad)}")
                continue
            lang = row.get("lang")
            if not lang:
                c.add("M-414", rpath, "row is missing the required `lang` key")
                continue
            if lang in seen_langs:
                c.add("M-414", rpath, f"duplicate lang {lang!r} in block")
                continue
            seen_langs.add(lang)
            expanded = _substitute_partial(template, dict(row))
            if "annotations" not in template:
                # Derived, never typed: <lang> -> label, else the expanded id.
                label = row.get("label") or expanded.get("id") or lang
                expanded["annotations"] = {lang: label}
            out.append(expanded)
    if not changed:
        return data
    new_data = dict(data)
    new_data["presentations"] = out
    return new_data



def _read_manifest_data(p: Path) -> tuple[dict, bytes]:
    """Read + parse the manifest file (M-101/M-102) and require a mapping at
    the top level (M-103). Raises CompileError; schema validation is the
    section parsers' job. Returns (data, raw_bytes) — the raw bytes feed the
    manifest_sha256 identity."""
    if not p.is_file():
        raise CompileError([Diagnostic("M-101", "manifest",
                                       f"file not found: {p}")])
    raw_bytes = p.read_bytes()
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(raw_bytes)
        except json.JSONDecodeError as e:
            raise CompileError([Diagnostic("M-102", "manifest",
                                           f"JSON parse error: {e}")]) from e
    else:
        try:
            import yaml  # PyYAML — declared dependency
        except ImportError as e:  # pragma: no cover
            raise CompileError([Diagnostic("M-102", "manifest",
                                           "PyYAML is required for YAML manifests "
                                           "(pip install iamf-loom)")]) from e
        try:
            data = yaml.safe_load(raw_bytes)
        except yaml.YAMLError as e:
            raise CompileError([Diagnostic("M-102", "manifest",
                                           f"YAML parse error: {e}")]) from e

    if not isinstance(data, dict):
        raise CompileError([Diagnostic("M-103", "manifest",
                                       "top level must be a mapping")])
    return data, raw_bytes


# ---- section parsers (X-76a decomposition) ----------------------------------
# Diagnostic strings and check ORDER are contract surface (the M-code table
# pins them); each parser body is the pre-split block, moved verbatim.

def _parse_sources(src_raw: dict, p: Path, c: Collector) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    for name in src_raw:
        spath = f"sources.{name}"
        if not _check_ident(name, spath, c):
            continue
        s = src_raw[name]
        if not isinstance(s, dict):
            c.add("M-202", spath, f"expected mapping, got {type(s).__name__}")
            continue
        kind = s.get("kind")
        if kind not in SOURCE_KINDS:
            c.add("M-203", f"{spath}.kind",
                  f"unknown kind {kind!r} (bed | ambisonics | adm)")
            continue
        if kind == "adm":
            c.add("M-309", f"{spath}.kind",
                  "`kind: adm` routes to iamf-tools ADM ingest with R7, "
                  "gated on the WP3 fidelity-corpus policy defaults")
            continue
        fpath = _req(s, "path", str, spath, c)
        if fpath is None:
            continue
        layout = s.get("layout")
        order = s.get("order") or ("bs2051" if kind == "bed" else "acn")
        norm = s.get("norm") or ("sn3d" if kind == "ambisonics" else None)
        if kind == "bed":
            if layout is None:
                c.add("M-306", f"{spath}.layout",
                      "beds must declare `layout` (stereo | 5.1 | 7.1.4)")
                continue
            layout = str(layout)
            if layout not in BEDS:
                c.add("M-305", f"{spath}.layout",
                      f"unsupported layout {layout!r}; Phase 1 supports "
                      f"{sorted(BEDS)} (matching the WP1-validated corpus)")
                continue
            if order != "bs2051":
                c.add("M-307", f"{spath}.order",
                      f"bed order {order!r} unsupported; Phase 1 accepts bs2051")
                continue
        else:  # ambisonics
            if order != "acn":
                c.add("M-307", f"{spath}.order",
                      f"ambisonics order {order!r} unsupported; Phase 1 accepts acn")
                continue
            if norm != "sn3d":
                c.add("M-307", f"{spath}.norm",
                      f"ambisonics norm {norm!r} unsupported; Phase 1 accepts sn3d "
                      "(an N3D master would encode silently wrong — F2/F3 class)")
                continue

        fs = (p.parent / fpath).resolve()
        if not fs.is_file():
            c.add("M-301", f"{spath}.path", f"source file not found: {fpath}")
            continue
        try:
            wi = read_wav_info(fs)
        except WavError as e:
            c.add("M-302", f"{spath}.path", str(e))
            continue
        if wi.sample_rate != 48_000 or wi.bits_per_sample not in (16, 24):
            c.add("M-308", f"{spath}.path",
                  f"{Path(fpath).name} is {wi.sample_rate} Hz / "
                  f"{wi.bits_per_sample}-bit; Phase 1 requires 48000 Hz, 16/24-bit "
                  "integer PCM")
            continue
        amb_order: int | None = None
        if kind == "bed":
            expect = BEDS[layout].channels
            if wi.channels != expect:
                c.add("M-303", f"{spath}.layout",
                      f"manifest says {layout} ({expect} ch), WAV has "
                      f"{wi.channels} ch — refusing to guess")
                continue
        else:
            n = math.isqrt(wi.channels)
            if n * n != wi.channels or not (1 <= n - 1 <= 4):
                c.add("M-304", f"{spath}.path",
                      f"ambisonics WAV has {wi.channels} ch, which is not "
                      f"(N+1)^2 for a supported order N (1..4)")
                continue
            amb_order = n - 1
        sources[name] = Source(
            name=name, path=fpath, kind=kind, layout=layout, order=order,
            norm=norm, channels=wi.channels, sample_rate=wi.sample_rate,
            bits=wi.bits_per_sample, frames=wi.frames,
            sha256=sha256_file(fs), ambisonics_order=amb_order,
        )
    return sources


def _parse_elements(el_raw: dict, src_raw: dict,
                    c: Collector) -> dict[str, Element]:
    elements: dict[str, Element] = {}
    for name in el_raw:
        epath = f"elements.{name}"
        if not _check_ident(name, epath, c):
            continue
        e = el_raw[name]
        if not isinstance(e, dict):
            c.add("M-202", epath, f"expected mapping, got {type(e).__name__}")
            continue
        src = _req(e, "from", str, epath, c)
        if src is None:
            continue
        if src not in src_raw:
            c.add("M-205", f"{epath}.from",
                  f"unknown source {src!r} (declared: {sorted(src_raw)})")
            continue
        elements[name] = Element(name=name, source=src)
    return elements


def _parse_presentations(data: dict, title: str, el_raw: dict,
                         elements: dict[str, Element],
                         c: Collector) -> list[Presentation]:
    presentations: list[Presentation] = []
    pr_raw = data.get("presentations")
    if pr_raw is None:
        # Default presentation over the single element, if there is one.
        if len(elements) == 1:
            only = next(iter(elements))
            presentations.append(Presentation(
                id="main", annotations={"en-us": title},
                elements=(PresentationElement(ref=only),),
                loudness_layouts=(),
            ))
        else:
            c.add("M-201", "presentations",
                  "required when the manifest declares != 1 element")
    elif not isinstance(pr_raw, list):
        c.add("M-202", "presentations",
              f"expected list, got {type(pr_raw).__name__}")
    else:
        seen_ids: set[str] = set()
        for i, pr in enumerate(pr_raw):
            ppath = f"presentations[{i}]"
            if not isinstance(pr, dict):
                c.add("M-202", ppath, "expected mapping")
                continue
            pid = _req(pr, "id", str, ppath, c)
            if pid is None:
                continue
            if not _check_ident(pid, f"{ppath}.id", c):
                continue
            if pid in seen_ids:
                c.add("M-204", f"{ppath}.id", f"duplicate presentation id {pid!r}")
                continue
            seen_ids.add(pid)
            ann = pr.get("annotations") or {"en-us": pid}
            if not isinstance(ann, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in ann.items()
            ):
                c.add("M-202", f"{ppath}.annotations",
                      "expected mapping of language tag -> text")
                continue
            pels: list[PresentationElement] = []
            for j, pe in enumerate(pr.get("elements") or []):
                jpath = f"{ppath}.elements[{j}]"
                if not isinstance(pe, dict):
                    c.add("M-202", jpath, "expected mapping")
                    continue
                ref = _req(pe, "ref", str, jpath, c)
                if ref is None:
                    continue
                if ref not in el_raw:
                    c.add("M-205", f"{jpath}.ref",
                          f"unknown element {ref!r} (declared: {sorted(el_raw)})")
                    continue
                gain = pe.get("gain_db", 0.0)
                if isinstance(gain, int):
                    gain = float(gain)
                if not isinstance(gain, float):
                    c.add("M-202", f"{jpath}.gain_db",
                          f"expected number, got {type(gain).__name__}")
                    continue
                if not (GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]):
                    c.add("M-408", f"{jpath}.gain_db",
                          f"{gain} dB outside {GAIN_RANGE[0]}..{GAIN_RANGE[1]}")
                    continue
                hp = pe.get("headphones", "stereo")
                if hp not in HEADPHONES:
                    c.add("M-203", f"{jpath}.headphones",
                          f"unknown mode {hp!r} (stereo | binaural)")
                    continue
                pels.append(PresentationElement(ref=ref, gain_db=gain,
                                                headphones=hp))
            if not pels:
                # R8: with the M-405 single-element gates retired, an empty
                # element list is no longer masked by the Phase-1 gate.
                c.add("M-201", f"{ppath}.elements",
                      "a presentation must reference at least one element")
                continue
            lls = pr.get("loudness_layouts") or []
            if not isinstance(lls, list):
                c.add("M-202", f"{ppath}.loudness_layouts", "expected list")
                lls = []
            for ll in lls:
                if str(ll) not in MIX_LAYOUTS:
                    c.add("M-407", f"{ppath}.loudness_layouts",
                          f"unsupported loudness layout {ll!r}; Phase 1 set: "
                          f"{sorted(MIX_LAYOUTS)}")
            presentations.append(Presentation(
                id=pid, annotations=dict(ann), elements=tuple(pels),
                loudness_layouts=tuple(str(x) for x in lls
                                       if str(x) in MIX_LAYOUTS),
            ))
    return presentations


def _parse_policy(data: dict, c: Collector) -> Policy:
    pol_raw = data.get("policy") or {}
    if not isinstance(pol_raw, dict):
        c.add("M-202", "policy", "expected mapping")
        pol_raw = {}
    cod_raw = pol_raw.get("codec") or {}
    if not isinstance(cod_raw, dict):
        c.add("M-202", "policy.codec", "expected mapping")
        cod_raw = {}
    codec_name = cod_raw.get("name", "opus")
    if codec_name not in CODECS_KNOWN:
        c.add("M-203", "policy.codec.name",
              f"unknown codec {codec_name!r} (opus | lpcm | flac)")
        codec_name = "opus"
    codec = CodecPolicy(
        name=codec_name,
        bitrate_coupled=_parse_bitrate(cod_raw.get("bitrate_coupled"),
                                       "policy.codec.bitrate_coupled", c, 128_000),
        bitrate_uncoupled=_parse_bitrate(cod_raw.get("bitrate_uncoupled"),
                                         "policy.codec.bitrate_uncoupled", c, 64_000),
    )
    loud_raw = pol_raw.get("loudness") or {}
    if not isinstance(loud_raw, dict):
        c.add("M-202", "policy.loudness", "expected mapping")
        loud_raw = {}
    normalize: float | None = None
    norm_raw = loud_raw.get("normalize")
    if norm_raw is not None:
        if isinstance(norm_raw, bool) or not isinstance(norm_raw, (int, float)):
            c.add("M-202", "policy.loudness.normalize",
                  f"expected a LUFS number (e.g. -16), got {norm_raw!r}")
        elif not (NORMALIZE_RANGE[0] <= float(norm_raw) <= NORMALIZE_RANGE[1]):
            c.add("M-409", "policy.loudness.normalize",
                  f"{norm_raw} LUFS outside the sane range "
                  f"{NORMALIZE_RANGE[0]} .. {NORMALIZE_RANGE[1]} "
                  "(platform practice spans YouTube -14 to Netflix -27)")
        elif codec_name == "lpcm":
            c.add("M-402", "policy.loudness.normalize",
                  "normalize requested with codec: lpcm — lpcm passthrough "
                  "must stay bit-transparent; a gain-ridden archive is a "
                  "different deliverable (drop normalize or use opus/flac)")
        else:
            normalize = float(norm_raw)
    mode = loud_raw.get("mode", "measure")
    if mode != "measure":
        c.add("M-203", "policy.loudness.mode",
              f"unknown mode {mode!r}; Loom supports `measure` "
              "(plus the optional `normalize:` ride, R3)")
    profile = pol_raw.get("profile", "auto")
    if profile not in PROFILES:
        c.add("M-203", "policy.profile",
              f"unknown profile {profile!r} ({' | '.join(sorted(PROFILES))})")
        profile = "auto"
    validate = pol_raw.get("validate", "fail_on_error")
    if validate is False:   # YAML 1.1 reads a bare `off` as boolean False
        validate = "off"
    elif validate is True:
        validate = "fail_on_error"
    if validate not in {"fail_on_error", "off"}:
        c.add("M-203", "policy.validate",
              f"unknown value {validate!r} (fail_on_error | off)")
        validate = "fail_on_error"
    return Policy(codec=codec, loudness_mode="measure", normalize=normalize,
                  profile=profile, validate=validate)


def _parse_targets(data: dict, p: Path, title: str,
                   sources: dict[str, Source], elements: dict[str, Element],
                   presentations: list[Presentation], policy: Policy,
                   c: Collector) -> list[Target]:
    codec_name = policy.codec.name
    targets: list[Target] = []
    tg_raw = _req(data, "targets", list, "", c)
    if tg_raw is not None:
        seen_out: set[str] = set()
        for i, tg in enumerate(tg_raw):
            tpath = f"targets[{i}]"
            if not isinstance(tg, dict):
                c.add("M-202", tpath, "expected mapping")
                continue
            fmt = _req(tg, "format", str, tpath, c)
            out = _req(tg, "out", str, tpath, c)
            if fmt is None or out is None:
                continue
            if fmt not in FORMATS:
                c.add("M-203", f"{tpath}.format",
                      f"unknown format {fmt!r} (iamf | mp4 | preview)")
                continue
            out = out.replace("{title}", slug(title))
            if out in seen_out:
                c.add("M-204", f"{tpath}.out", f"duplicate output path {out!r}")
                continue
            seen_out.add(out)
            preset = tg.get("preset")
            if preset is not None and preset not in PRESETS:
                c.add("M-203", f"{tpath}.preset",
                      f"unknown preset {preset!r} (youtube | archive)")
                continue
            route = tg.get("route", "auto")
            if route not in ROUTES:
                c.add("M-203", f"{tpath}.route",
                      f"unknown route {route!r} (auto | oneshot | remux)")
                continue
            sel = tg.get("presentation")
            if sel is not None:
                # R8 (D-Z6): a render-time mix selection, never metadata.
                if not isinstance(sel, str):
                    c.add("M-202", f"{tpath}.presentation",
                          f"expected a presentation id string, got "
                          f"{type(sel).__name__}")
                    continue
                if fmt != "preview":
                    c.add("M-402", f"{tpath}.presentation",
                          "presentation selection applies to preview render "
                          "targets only — iamf/mp4 deliverables carry every "
                          "presentation (that is the point of them)")
                    continue
                if sel not in {pr.id for pr in presentations}:
                    c.add("M-205", f"{tpath}.presentation",
                          f"unknown presentation {sel!r} (declared: "
                          f"{sorted(pr.id for pr in presentations)})")
                    continue
            if fmt == "preview":
                # R9: a preview is a stereo binaural review render — the
                # container/codec derives from the out extension, and the
                # platform-delivery surface (video, presets) does not apply.
                if not out.endswith(PREVIEW_EXTENSIONS):
                    c.add("M-203", f"{tpath}.out",
                          f"preview target out {out!r} must end in .wav or "
                          ".opus (the R9 review-copy containers)")
                    continue
                if tg.get("video") is not None:
                    c.add("M-402", f"{tpath}.video",
                          "preview is an audio-only review render (R9); "
                          "`video:` applies to mp4 targets")
                    continue
                if preset is not None:
                    c.add("M-402", f"{tpath}.preset",
                          f"preset: {preset} is a platform delivery shape; a "
                          "preview render takes no preset (R9)")
                    continue
                # Probe-forced (doc 46 deviation 1): with
                # headphones_rendering_mode STEREO the reference decoder
                # renders the plain stereo downmix under a Binaural output
                # layout (renderer_factory "fakes" stereo — spec behavior,
                # verified byte-identical at probe). A "binaural preview"
                # of such a program would be mislabeled; fail loudly.
                # R8 (D-Z7): the requirement follows the SELECTED
                # presentation — the one the preview actually renders —
                # not any presentation in the file.
                selected = None
                if presentations:
                    selected = presentations[0]
                    if sel is not None:
                        selected = next(pr for pr in presentations
                                        if pr.id == sel)
                if selected is not None and not any(
                        pe.headphones == "binaural"
                        for pe in selected.elements):
                    c.add("M-402", f"{tpath}.format",
                          f"preview renders presentation "
                          f"{selected.id!r}, but none of its elements "
                          "declares `headphones: binaural` — under "
                          "headphones_rendering_mode STEREO the decoder "
                          "renders the stereo downmix even for a Binaural "
                          "output layout (IAMF rendering semantics), so the "
                          "review copy would not be binaural; declare "
                          "`headphones: binaural` on the selected "
                          "presentation's element(s)")
                    continue
            video = tg.get("video")
            if video is not None:
                if not isinstance(video, str):
                    c.add("M-202", f"{tpath}.video", "expected path string")
                    continue
                if not (p.parent / video).is_file():
                    c.add("M-301", f"{tpath}.video",
                          f"video file not found: {video}")
                    continue
            if preset == "youtube" and video is None:
                c.add("M-404", f"{tpath}.video",
                      "preset: youtube produces the validated A/V ingest MP4 "
                      "(G9); provide `video:` with an H.264 MP4 to stream-copy")
                continue
            if preset == "youtube" and fmt != "mp4":
                c.add("M-402", f"{tpath}.preset",
                      "preset: youtube applies to mp4 targets only")
                continue
            if preset == "archive":
                if fmt != "iamf":
                    c.add("M-402", f"{tpath}.preset",
                          "preset: archive is the lossless raw-.iamf mezzanine "
                          "shape; it applies to iamf targets only")
                    continue
                if codec_name != "flac":
                    c.add("M-402", f"{tpath}.preset",
                          "preset: archive is lossless; declare "
                          f"`policy.codec.name: flac` (currently {codec_name!r} "
                          "— presets are shape, they never silently override "
                          "policy)")
                    continue
            if video is not None:
                # Compile-time video probe (Phase 2, doc-42 deviation 5):
                # catch the S-406 class before any tool runs.
                from .videoprobe import (DURATION_TOLERANCE_S, H264_FOURCCS,
                                         probe_video)
                vp = probe_video(p.parent / video)
                if not vp.ok:
                    c.add("M-410", f"{tpath}.video",
                          f"{video}: {vp.error}")
                    continue
                if preset == "youtube" and vp.fourcc not in H264_FOURCCS:
                    c.add("M-412", f"{tpath}.video",
                          f"{video}: video sample entry is {vp.fourcc!r}; the "
                          "G9-validated YouTube ingest shape stream-copies "
                          "H.264 (avc1/avc3)")
                    continue
                if elements:
                    # R8: any element's source works — frame counts are
                    # uniform across elements by M-415 (compiler-enforced).
                    src = sources.get(next(iter(elements.values())).source)
                    if (src is not None and src.sample_rate
                            and vp.duration_s is not None):
                        adur = src.frames / src.sample_rate
                        if abs(vp.duration_s - adur) > DURATION_TOLERANCE_S:
                            c.add("M-411", f"{tpath}.video",
                                  f"video {video} is {vp.duration_s:.2f} s but "
                                  f"the audio program is {adur:.2f} s — a "
                                  "stream-copied A/V mux would carry the "
                                  "mismatch into the deliverable (the S-406 "
                                  "class, caught at compile)")
                            continue
            targets.append(Target(format=fmt, out=out, video=video,
                                  preset=preset, route=route,
                                  presentation=sel))
    return targets


def load_manifest(path: str | Path,
                  variables: dict[str, str] | None = None) -> Manifest:
    """Load, schema-validate, and asset-validate a manifest. Raises CompileError.

    X-76a shape: raw read/parse, then the R8 preprocessing passes, then one
    parser per manifest section (sources / elements / presentations / policy /
    targets) sharing a single Collector — diagnostics accumulate across
    sections exactly as before the split, and raise once at the end."""
    p = Path(path)
    data, raw_bytes = _read_manifest_data(p)
    c = Collector()

    # ---- `languages:` expansion, then {variable} substitution (R8 D-Z3;
    # row bindings are applied partially first, so they shadow global
    # bindings inside their presentation; the global pass owns M-413) -------
    data = _expand_languages(data, c)
    c.raise_if_any()
    variables = {k: str(v) for k, v in (variables or {}).items()}
    data = _substitute_tree(data, variables, "", c)
    c.raise_if_any()

    if data.get("loom") != SUPPORTED_VERSION:
        c.add("M-103", "loom",
              f"expected `loom: {SUPPORTED_VERSION}`, got {data.get('loom')!r}")
    if "unsafe_overrides" in data:
        c.add("M-206", "unsafe_overrides",
              "escape hatches are not available in Phase 1 (ADR-5)")

    title = _req(data, "title", str, "", c) or "untitled"

    src_raw = _req(data, "sources", dict, "", c) or {}
    sources = _parse_sources(src_raw, p, c)
    el_raw = _req(data, "elements", dict, "", c) or {}
    elements = _parse_elements(el_raw, src_raw, c)
    presentations = _parse_presentations(data, title, el_raw, elements, c)
    policy = _parse_policy(data, c)
    targets = _parse_targets(data, p, title, sources, elements,
                             presentations, policy, c)

    c.raise_if_any()

    return Manifest(
        title=title,
        manifest_dir=p.parent.resolve(),
        manifest_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        sources=sources,
        elements=elements,
        presentations=tuple(presentations),
        policy=policy,
        targets=tuple(targets),
    )


def slug(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s).strip("_")
