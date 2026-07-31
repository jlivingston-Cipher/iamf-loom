"""The compiler: validated Manifest -> deterministic Plan (PRD R1 + R2).

Everything derivable is derived here (ADR-5): substream order, profile,
loudness layout sets, backend routing, every subprocess argv. Compile twice
on identical inputs -> byte-identical plans (E-L4).
"""

from __future__ import annotations

from .backends import route_target
from .backends import ffmpeg as ff
from .backends import iamftools as it
from .backends import mp4box as mb
from .diagnostics import Collector
from .layouts import BEDS, bed_arithmetic_ok
from .manifest import slug as _slug
from .model import Manifest, Source
from .plan import OUTDIR, Plan, SRCDIR, Step, TargetPlan, WORK, gain_token

NORMALIZE_TOLERANCE_LU = 0.3   # PRD R3 accept: target hit within ±0.3 LU


def _lay_slug(name: str) -> str:
    return name.replace(".", "_").replace("(", "").replace(")", "")


def _gate_manifest(m: Manifest, c: Collector) -> None:
    """The R8 compile gates (M-201/M-303/M-415/M-416), raising on failure."""
    # ---- R8 gates (the Phase-1 M-405 single-element gates retire here) -----
    if not m.targets:
        c.add("M-201", "targets", "at least one target is required")
    c.raise_if_any()

    elements = list(m.elements.values())
    for el in elements:
        s = m.sources[el.source]
        if s.kind == "bed" and not bed_arithmetic_ok(BEDS[s.layout]):
            # Cross-check against sentinel.layouts ground truth — never fires.
            c.add("M-303", f"sources.{s.name}.layout",
                  "internal layout-arithmetic mismatch vs sentinel.layouts "
                  "(file a bug; refusing to emit)")
    # D-Z2: one shared frame count across element sources (a packager
    # conforms nothing — a short VO stem is an upstream conform problem).
    by_frames: dict[int, list[str]] = {}
    for el in elements:
        s = m.sources[el.source]
        by_frames.setdefault(s.frames, []).append(s.name)
    if len(by_frames) > 1:
        desc = "; ".join(f"{fr} frames: {', '.join(sorted(names))}"
                         for fr, names in sorted(by_frames.items()))
        c.add("M-415", "elements",
              f"element sources disagree on frame count ({desc}) — conform "
              "the stems upstream; Loom will not trim or pad a master")
    # D-Z2: flac/lpcm share one codec_config OBU across every substream.
    if m.policy.codec.name in ("flac", "lpcm"):
        by_bits: dict[int, list[str]] = {}
        for el in elements:
            s = m.sources[el.source]
            by_bits.setdefault(s.bits, []).append(s.name)
        if len(by_bits) > 1:
            desc = "; ".join(f"{b}-bit: {', '.join(sorted(names))}"
                             for b, names in sorted(by_bits.items()))
            c.add("M-415", "elements",
                  f"{m.policy.codec.name} requires one shared bit depth "
                  f"across element sources ({desc}) — one codec_config "
                  "describes every substream")
    # D-Z2: per-mix-presentation profile caps (probe P1: the pinned encoder
    # enforces exactly these; a mix no profile admits dies at compile).
    for i, pr in enumerate(m.presentations):
        n_els = len(pr.elements)
        n_ch = sum(m.sources[m.elements[pe.ref].source].channels
                   for pe in pr.elements)
        if it.mix_requirement(n_els, n_ch) is None:
            c.add("M-416", f"presentations[{i}]",
                  f"{n_els} elements / {n_ch} channels in one mix "
                  "presentation exceed every profile's caps (per mix: "
                  "simple 1 element/16 ch; base 2/18; base_enhanced 28/28)")
    c.raise_if_any()


def _stage_steps(m: Manifest, plan: Plan,
                 elements: list) -> tuple[dict[str, str], list[str],
                                          list[str], bool]:
    """Shared staging: one step per distinct element source WAV. The legacy
    single-element step id is preserved (D-Z5 — golden stability).
    Returns (staged name map, stage step ids, distinct sources, single?)."""
    staged: dict[str, str] = {}
    stage_ids: list[str] = []
    seen_srcs: list[str] = []
    for el in elements:
        if el.source in seen_srcs:
            continue
        seen_srcs.append(el.source)
    single = len(seen_srcs) == 1
    for i, src_name in enumerate(seen_srcs):
        s = m.sources[src_name]
        staged_name = f"{_slug(s.name)}.wav"
        staged[src_name] = staged_name
        sid = "s00-stage-src" if single else f"s00-stage-{i}-{_slug(s.name)}"
        stage_ids.append(plan.add(Step(
            id=sid, kind="stage_input", tool="internal",
            src=f"{SRCDIR}/{s.path}", dst=f"{WORK}/wav/{staged_name}",
            writes=[f"{WORK}/wav/{staged_name}"],
            rationale="single input_wav_directory for encoder_main; staged "
                      "copy pins the bytes the plan's source hash refers to",
        )))
    return staged, stage_ids, seen_srcs, single


def _preride_steps(m: Manifest, t, source: Source, plan: Plan,
                   step_ids: list[str], prefix: str, route, staged,
                   seen_srcs: list[str], single: bool, staged_name: str,
                   normalize: float, anchor: str, anchor_mix_id: int | None,
                   profile: str,
                   input_wav: str) -> tuple[str, str, list[str], int]:
    """The R3 pre-ride chain (normalize only): pass-A encode via the
    target's own backend -> render the anchor -> measure -> gain-ride
    the staged WAV(s). The ridden WAVs (always 24-bit) feed the final
    encode; embedded values are re-MEASURED on the ridden audio, never
    derived from the pre-ride measurement (D-P2). R8 (D-Z5): every
    element source rides by the SAME anchor-derived gain — riding all
    stems by g dB rides every rendered mix by g dB (linear mixing).
    Returns the ridden (input_wav_dir, input_wav, input_wavs, enc_bits)."""
    if route.backend == "iamftools":
        cfg_pre = f"{WORK}/cfg/{prefix}-pre.textproto"
        pre_dir = f"{WORK}/norm/{prefix}/enc-pre"
        pre_out = f"{pre_dir}/{prefix}-pre.iamf"
        step_ids.append(plan.add(Step(
            id=f"{prefix}-cfg-pre", kind="write_config",
            tool="internal", dst=cfg_pre,
            content=it.emit_textproto(m, staged,
                                      f"{prefix}-pre", profile),
            writes=[cfg_pre],
            rationale="R3 pass-A: pre-ride encode config (loudness "
                      "measured on the un-ridden program)",
        )))
        step_ids.append(plan.add(Step(
            id=f"{prefix}-enc-pre", kind="encode", tool="encoder_main",
            argv=it.encoder_argv(cfg_pre, f"{WORK}/wav", pre_dir),
            reads=[cfg_pre,
                   *[f"{WORK}/wav/{staged[n]}" for n in seen_srcs]],
            writes=[pre_out],
            rationale="R3 pass-A encode; never a deliverable",
        )))
    else:
        pre_out = f"{WORK}/norm/{prefix}/pre.iamf"
        step_ids.append(plan.add(Step(
            id=f"{prefix}-enc-pre", kind="encode", tool="ffmpeg",
            argv=ff.encode_argv(m, t, source, input_wav, pre_out,
                                video=None, loudness=None),
            reads=[input_wav], writes=[pre_out],
            rationale="R3 pass-A encode (FFmpeg one-shot audio path); "
                      "never a deliverable",
        )))
    anchor_wav = f"{WORK}/norm/{prefix}/anchor-pre.wav"
    step_ids.append(plan.add(Step(
        id=f"{prefix}-render-anchor-pre", kind="render",
        tool="decoder_main",
        argv=ff.render_argv(pre_out, anchor, anchor_wav,
                            mix_id=anchor_mix_id),
        reads=[pre_out], writes=[anchor_wav],
        rationale=f"R3: reference-render the anchor layout ({anchor} "
                  "— the first effective loudness layout) pre-ride"
                  + (" of the anchor presentation "
                     "(mix_presentation_id pinned — D-Z6)"
                     if anchor_mix_id is not None else ""),
    )))
    pre_measure_id = f"{prefix}-measure-anchor-pre"
    step_ids.append(plan.add(Step(
        id=pre_measure_id, kind="measure_bs1770", tool="sentinel-dsp",
        argv=ff.measure_argv(anchor_wav, anchor),
        reads=[anchor_wav],
        rationale="R3: pre-ride BS.1770-4 on the anchor render "
                  "(sentinel-dsp kernel — conformant engine, F34); "
                  "the ride gain derives from THIS measurement only",
    )))
    ridden_dir = f"{WORK}/wavn/{prefix}"
    for ri, src_name in enumerate(seen_srcs):
        staged_wav = f"{WORK}/wav/{staged[src_name]}"
        ridden_wav = f"{ridden_dir}/{staged[src_name]}"
        ride, ride_astats = ff.gain_ride_argvs(
            staged_wav, ridden_wav,
            gain_token(pre_measure_id, normalize))
        step_ids.append(plan.add(Step(
            id=(f"{prefix}-ride" if single
                else f"{prefix}-ride-{ri}-{_slug(src_name)}"),
            kind="gain_ride", tool="ffmpeg",
            argv=ride, argv_secondary=ride_astats,
            reads=[staged_wav], writes=[ridden_wav],
            rationale=f"R3 gain-ride to {normalize:g} LUFS "
                      "(gain = target - measured, resolved at "
                      "execution; clip guard: ridden digital peak "
                      "must stay below -0.05 dBFS — Loom is not a "
                      "limiter)"
                      + ("" if single else
                         "; R8: every stem rides by the same gain — "
                         "riding all stems by g dB rides every "
                         "rendered mix by g dB (linear mixing)"),
        )))
    return (ridden_dir, f"{ridden_dir}/{staged_name}",
            [f"{ridden_dir}/{staged[n]}" for n in seen_srcs], 24)


def _preview_steps(m: Manifest, t, source: Source, plan: Plan,
                   step_ids: list[str], prefix: str, route, staged,
                   normalize: float | None, anchor: str, n_pres: int,
                   profile: str, enc_bits: int | None,
                   input_wav_dir: str, input_wavs: list[str]) -> None:
    """R9: stereo binaural review render (doc 46, D-Y3) — appends the
    target's steps AND its TargetPlan (gate_path = the intermediate).
    R8 (D-Z6): the preview renders ONE presentation — `presentation:`
    selects it (default presentations[0]); on multi-presentation
    intermediates the render argv pins its mix_presentation_id."""
    sel_idx = 0
    if t.presentation is not None:
        sel_idx = next(i for i, pr in enumerate(m.presentations)
                       if pr.id == t.presentation)
    sel_id = m.presentations[sel_idx].id
    preview_mix_id = 42 + sel_idx if n_pres > 1 else None
    ext = "opus" if t.out.endswith(".opus") else "wav"
    cfg_path = f"{WORK}/cfg/{prefix}.textproto"
    enc_dir = f"{WORK}/prev/{prefix}/enc"
    enc_out = f"{enc_dir}/{prefix}.iamf"
    step_ids.append(plan.add(Step(
        id=f"{prefix}-cfg", kind="write_config", tool="internal",
        dst=cfg_path,
        content=it.emit_textproto(m, staged, prefix,
                                  profile, bits_override=enc_bits),
        writes=[cfg_path],
        rationale="original textproto emitter (D-L6); loudness fields "
                  "absent — encoder_main measures natively (G2b)",
    )))
    step_ids.append(plan.add(Step(
        id=f"{prefix}-enc", kind="encode", tool="encoder_main",
        argv=it.encoder_argv(cfg_path, input_wav_dir, enc_dir),
        reads=[cfg_path, *input_wavs], writes=[enc_out],
        rationale=route.rationale,
    )))
    if normalize is not None:
        step_ids.append(plan.add(Step(
            id=f"{prefix}-verify-norm", kind="verify_loudness",
            tool="internal", reads=[enc_out],
            params={"method": "bitstream", "path": enc_out,
                    "anchor": anchor, "target": normalize,
                    "tolerance": NORMALIZE_TOLERANCE_LU},
            rationale="R3 accept on the preview's intermediate: the "
                      "preview must render the normalized program, "
                      "so the anchor-layout loudness encoder_main "
                      "measured on the ridden audio is verified "
                      "within ±0.3 LU before any render happens",
        )))
    binaural_wav = f"{WORK}/prev/{prefix}/binaural.wav"
    step_ids.append(plan.add(Step(
        id=f"{prefix}-render-binaural", kind="render",
        tool="decoder_main",
        argv=ff.render_binaural_argv(enc_out, binaural_wav,
                                     mix_id=preview_mix_id),
        reads=[enc_out], writes=[binaural_wav],
        rationale="R9: binaural stereo render of the gate-validated "
                  "intermediate via decoder_main "
                  "--output_layout=Binaural (obr, subprocess — "
                  "ADR-4); single-oracle: iamfdec's binauralizer is "
                  "disabled toolchain-wide (F10)"
                  + (f"; R8: renders presentation {sel_id!r} "
                     f"(mix_presentation_id {42 + sel_idx} pinned — "
                     "D-Z6; the selection, defaulting to the first "
                     "presentation, is a render choice, never "
                     "metadata)"
                     if preview_mix_id is not None else ""),
    )))
    measure_id = f"{prefix}-measure-binaural"
    step_ids.append(plan.add(Step(
        id=measure_id, kind="measure_bs1770", tool="sentinel-dsp",
        argv=ff.measure_argv(binaural_wav, "stereo"),
        reads=[binaural_wav],
        rationale="R9: review-copy loudness (sentinel-dsp kernel — "
                  "conformant engine, F34), recorded in the ledger "
                  "only — wav/ogg carry no IAMF loudness structures, "
                  "so nothing is embedded (noted, not glossed)",
    )))
    step_ids.append(plan.add(Step(
        id=f"{prefix}-verify", kind="verify_preview", tool="ffmpeg",
        argv=ff.preview_verify_argv(binaural_wav),
        reads=[binaural_wav],
        params={"wav": binaural_wav, "container": ext,
                "expect_channels": 2,
                "expect_sample_rate": source.sample_rate,
                "expect_frames": source.frames,
                "measure_step": measure_id,
                "silence_floor_lufs": -70.0},
        rationale="R9 accept: 2 ch at the source rate, frame count "
                  "exact vs the source program, measured IL above "
                  "the -70 LUFS gating floor (digital-silence "
                  "guard); per-channel peaks recorded, not "
                  "run-failing (a quiet channel is a program fact)",
    )))
    if ext == "opus":
        step_ids.append(plan.add(Step(
            id=f"{prefix}-opus", kind="encode", tool="ffmpeg",
            argv=ff.preview_opus_argv(binaural_wav,
                                      f"{OUTDIR}/{t.out}"),
            reads=[binaural_wav], writes=[f"{OUTDIR}/{t.out}"],
            rationale="R9 review copy: Opus-in-Ogg at the fixed "
                      f"{ff.PREVIEW_OPUS_BITRATE} review bitrate; "
                      "-bitexact keeps version-bearing tags out of "
                      "the deliverable (the doc-44 date-stamp "
                      "lesson, applied at design time)",
        )))
    else:
        step_ids.append(plan.add(Step(
            id=f"{prefix}-out", kind="copy", tool="internal",
            src=binaural_wav, dst=f"{OUTDIR}/{t.out}",
            writes=[f"{OUTDIR}/{t.out}"],
            rationale="deliver the verified binaural render to the "
                      "target path (delivery is the last step — a "
                      "failed verify ships nothing)",
        )))
    plan.targets.append(TargetPlan(
        out=t.out, format=t.format, backend=route.backend,
        muxer=route.muxer, preset=t.preset, profile=profile,
        step_ids=step_ids, rationale=route.rationale,
        gate_path=enc_out,
    ))


def _iamftools_steps(m: Manifest, t, plan: Plan, step_ids: list[str],
                     prefix: str, route, staged,
                     normalize: float | None, anchor: str, profile: str,
                     enc_bits: int | None, input_wav_dir: str,
                     input_wavs: list[str]) -> None:
    """iamftools route: config -> encode (-> verify) -> mux/deliver."""
    cfg_path = f"{WORK}/cfg/{prefix}.textproto"
    enc_dir = f"{WORK}/enc/{prefix}"
    enc_out = f"{enc_dir}/{prefix}.iamf"
    step_ids.append(plan.add(Step(
        id=f"{prefix}-cfg", kind="write_config", tool="internal",
        dst=cfg_path,
        content=it.emit_textproto(m, staged, prefix,
                                  profile, bits_override=enc_bits),
        writes=[cfg_path],
        rationale="original textproto emitter (D-L6); loudness fields "
                  "absent — encoder_main measures natively (G2b)",
    )))
    step_ids.append(plan.add(Step(
        id=f"{prefix}-enc", kind="encode", tool="encoder_main",
        argv=it.encoder_argv(cfg_path, input_wav_dir, enc_dir),
        reads=[cfg_path, *input_wavs],
        writes=[enc_out],
        rationale=route.rationale,
    )))
    if normalize is not None:
        step_ids.append(plan.add(Step(
            id=f"{prefix}-verify", kind="verify_loudness",
            tool="internal", reads=[enc_out],
            params={"method": "bitstream", "path": enc_out,
                    "anchor": anchor, "target": normalize,
                    "tolerance": NORMALIZE_TOLERANCE_LU},
            rationale="R3 accept: the anchor-layout loudness that "
                      "encoder_main independently measured on the "
                      "ridden audio must land within ±0.3 LU of the "
                      "normalize target (read back from the bitstream "
                      "via the sentinel clean-room parser)",
        )))
    if route.muxer == "mp4box":
        video = f"{SRCDIR}/{t.video}" if t.video else None
        step_ids.append(plan.add(Step(
            id=f"{prefix}-mux", kind="remux", tool="mp4box",
            argv=mb.remux_argv(enc_out, f"{OUTDIR}/{t.out}", video),
            reads=[enc_out] + ([video] if video else []),
            writes=[f"{OUTDIR}/{t.out}"],
            rationale="ADR-2: MP4Box for every remux (repairability "
                      "asymmetry — FFmpeg copy discards start-trim, "
                      "F31; MP4Box output stays repairable, F32); the "
                      "F9 DTS warning on stderr is expected — it is "
                      "the visible surface of F32, repaired by the "
                      "next step and tripwired downstream by the "
                      "Sentinel gate (S-408)",
        )))
        step_ids.append(plan.add(Step(
            id=f"{prefix}-repair", kind="repair_stts", tool="internal",
            reads=[f"{OUTDIR}/{t.out}"],
            writes=[f"{OUTDIR}/{t.out}"],
            params={"out": t.out, "path": f"{OUTDIR}/{t.out}"},
            rationale="item 13 (doc 84): MP4Box's stts excludes the "
                      "start-trim from the first IA sample's duration "
                      "(F32, gpac/gpac#3826, filed with patch) and "
                      "contradicts its own — correct — elst; a "
                      "14496-12-conformant consumer then discards the "
                      "first temporal unit whole (-648 samples on the "
                      "doc-60 matrix). Rewrite the deltas to the "
                      "IAMF §6.2.2 duration model in place "
                      "(byte-size-preserving; essence untouched; facts "
                      "read from the file's own IA stream) and "
                      "re-cohere mdhd/tkhd/elst/mvhd durations. "
                      "Already-conformant tables no-op, so this step "
                      "self-retires when gpac's fix is merged AND "
                      "released (the removal trigger)",
        )))
    else:
        step_ids.append(plan.add(Step(
            id=f"{prefix}-out", kind="copy", tool="internal",
            src=enc_out, dst=f"{OUTDIR}/{t.out}",
            writes=[f"{OUTDIR}/{t.out}"],
            rationale="deliver the encoder output to the target path",
        )))


def _ffmpeg_oneshot_steps(m: Manifest, t, source: Source, plan: Plan,
                          step_ids: list[str], prefix: str, route,
                          normalize: float | None, anchor: str,
                          input_wav: str) -> None:
    """FFmpeg one-shot route: pass-1 -> render+measure per layout -> pass-2."""
    pass1 = f"{WORK}/os/{prefix}.pass1.iamf"
    step_ids.append(plan.add(Step(
        id=f"{prefix}-p1", kind="encode", tool="ffmpeg",
        argv=ff.encode_argv(m, t, source, input_wav,
                            pass1, video=None, loudness=None),
        reads=[input_wav],
        writes=[pass1],
        rationale="pass 1 of the two-pass loudness flow; never a "
                  "deliverable (FFmpeg alone writes 0.0 loudness, F7)"
                  + (" — reads the ridden WAV (R3), so these "
                     "measurements are the post-ride truth"
                     if normalize is not None else ""),
    )))
    measure_ids: dict[str, str] = {}
    for lay in ff.loudness_layout_names(m, source):
        ls = _lay_slug(lay)
        rendered = f"{WORK}/os/{prefix}.{ls}.wav"
        rid = f"{prefix}-render-{ls}"
        step_ids.append(plan.add(Step(
            id=rid, kind="render", tool="decoder_main",
            argv=ff.render_argv(pass1, lay, rendered),
            reads=[pass1], writes=[rendered],
            rationale=f"reference-render the {lay} loudness layout "
                      "for BS.1770 measurement",
        )))
        mid = f"{prefix}-measure-{ls}"
        measure_ids[lay] = mid
        step_ids.append(plan.add(Step(
            id=mid, kind="measure_bs1770", tool="sentinel-dsp",
            argv=ff.measure_argv(rendered, lay),
            reads=[rendered],
            rationale="BS.1770-4 via the sentinel-dsp kernel (F34: "
                      "the declared values must be conformant; "
                      "ebur128's BACK_MASK is F29-non-conformant on "
                      "7.1.x); computed, never typed",
        )))
    video = f"{SRCDIR}/{t.video}" if t.video else None
    step_ids.append(plan.add(Step(
        id=f"{prefix}-p2", kind="encode", tool="ffmpeg",
        argv=ff.encode_argv(m, t, source, input_wav,
                            f"{OUTDIR}/{t.out}", video=video,
                            loudness=ff.loudness_tokens(measure_ids)),
        reads=[input_wav] + ([video] if video else []),
        writes=[f"{OUTDIR}/{t.out}"],
        rationale=route.rationale,
    )))
    if normalize is not None:
        step_ids.append(plan.add(Step(
            id=f"{prefix}-verify", kind="verify_loudness",
            tool="internal",
            params={"method": "measured",
                    "measure_step": measure_ids[anchor],
                    "anchor": anchor, "target": normalize,
                    "tolerance": NORMALIZE_TOLERANCE_LU},
            rationale="R3 accept: the re-measured (and injected) "
                      "anchor-layout loudness of the ridden program "
                      "must land within ±0.3 LU of the normalize "
                      "target",
        )))


def compile_manifest(m: Manifest) -> Plan:
    """X-76a shape: gates, plan seed + staging, then one step builder per
    route (pre-ride / preview / iamftools / ffmpeg-oneshot). Pure code
    motion from the pre-split body — the emitted plan is byte-identical
    (golden-verified)."""
    c = Collector()
    _gate_manifest(m, c)

    elements = list(m.elements.values())
    primary: Source = m.sources[elements[0].source]
    source = primary  # single-element flows (oneshot route) read this

    plan = Plan(title=m.title, manifest_sha256=m.manifest_sha256)
    for name in sorted(m.sources):
        s = m.sources[name]
        plan.sources[name] = {
            "path": s.path, "sha256": s.sha256, "kind": s.kind,
            "layout": s.layout, "order": s.order, "norm": s.norm,
            "channels": s.channels, "sample_rate": s.sample_rate,
            "bits": s.bits, "frames": s.frames,
            "ambisonics_order": s.ambisonics_order,
        }

    staged, stage_ids, seen_srcs, single = _stage_steps(m, plan, elements)
    staged_name = staged[elements[0].source]  # single-element flows

    normalize = m.policy.normalize
    anchor = ff.loudness_layout_names(m, source)[0]  # first effective layout
    n_pres = len(m.presentations)
    # D-Z6: pin the rendered mix when the stream carries more than one
    # (probe P2: an omitted --mix_id lets the decoder pick). The anchor is
    # presentations[0] -> mix_presentation_id 42 (emitter arithmetic).
    anchor_mix_id = 42 if n_pres > 1 else None

    for ti, t in enumerate(m.targets):
        tpath = f"targets[{ti}]"
        route = route_target(t, m, c, tpath)
        if route is None:
            continue
        stem = _slug(t.out.rsplit("/", 1)[-1].rsplit(".", 1)[0]) or f"t{ti}"
        prefix = f"t{ti:02d}-{stem}"
        step_ids: list[str] = [*stage_ids]
        profile = it.derive_profile(m.policy.profile, t.preset, m)

        input_wav_dir = f"{WORK}/wav"
        input_wav = f"{WORK}/wav/{staged_name}"
        input_wavs = [f"{WORK}/wav/{staged[n]}" for n in seen_srcs]
        enc_bits: int | None = None
        if normalize is not None:
            input_wav_dir, input_wav, input_wavs, enc_bits = _preride_steps(
                m, t, source, plan, step_ids, prefix, route, staged,
                seen_srcs, single, staged_name, normalize, anchor,
                anchor_mix_id, profile, input_wav)

        if t.format == "preview":
            _preview_steps(m, t, source, plan, step_ids, prefix, route,
                           staged, normalize, anchor, n_pres, profile,
                           enc_bits, input_wav_dir, input_wavs)
            continue

        if route.backend == "iamftools":
            _iamftools_steps(m, t, plan, step_ids, prefix, route, staged,
                             normalize, anchor, profile, enc_bits,
                             input_wav_dir, input_wavs)
        else:  # ffmpeg_oneshot
            _ffmpeg_oneshot_steps(m, t, source, plan, step_ids, prefix,
                                  route, normalize, anchor, input_wav)

        plan.targets.append(TargetPlan(
            out=t.out, format=t.format, backend=route.backend,
            muxer=route.muxer, preset=t.preset, profile=profile,
            step_ids=step_ids, rationale=route.rationale,
        ))

    c.raise_if_any()
    return plan
