"""FFmpeg one-shot backend (ADR-1's designated FFmpeg use).

Every WP1 sharp edge is enforced here, by construction:

  F4  — substream order derives from the layout table (coupled pairs first,
        C/LFE last); the manifest has no field that could reorder it.
  F1  — every distinct parameter_id carries its own parameter_rate.
  F2/F3 — scene elements: ambisonics_mode lives INSIDE a layer= entry and the
        layer carries an explicit `ambisonic N` ch_layout.
  F6  — with a video stream mapped, every stream gets an explicit unique
        -streamid.
  F7  — closed by the two-pass measure->inject flow (WP1 loudness_inject.py
        is the proven v0): pass 1 raw .iamf, reference-render each loudness
        layout (decoder_main), measure BS.1770-4 via the sentinel-dsp kernel
        (F34 — one argv, one JSON, conformant weights; ebur128's BACK_MASK
        over-weights 7.1.x rears/top-backs, F29), pass 2 injects the values.
"""

from __future__ import annotations

from ..layouts import BEDS, MIX_LAYOUTS, default_loudness_layouts
from ..model import Manifest, Source, Target
from ..plan import TOOLCHAIN, measure_token
from ..toolchain import BINARIES

# B13 (doc 76): tool argv0 tokens derive from toolchain.BINARIES — the ONE
# root-relative path map (the executor's binary() resolves the same entries).
FFMPEG = f"{TOOLCHAIN}/{BINARIES['ffmpeg']}"
DECODER = f"{TOOLCHAIN}/{BINARIES['decoder_main']}"


def substream_split(source: Source) -> list[tuple[str, str, bool]]:
    """(label, pan_expr, coupled) per substream, in IAMF order (F4)."""
    if source.kind == "bed":
        bed = BEDS[source.layout]
        out = []
        for ss in bed.substreams:
            if ss.coupled:
                a, b = ss.wav_channels
                out.append((ss.label, f"stereo|c0=c{a}|c1=c{b}", True))
            else:
                (a,) = ss.wav_channels
                out.append((ss.label, f"mono|c0=c{a}", False))
        return out
    return [(f"acn{i}", f"mono|c0=c{i}", False) for i in range(source.channels)]


def loudness_layout_names(m: Manifest, source: Source) -> list[str]:
    """Effective loudness layouts of the anchor presentation
    (presentations[0]). R8: the default-list native is the widest bed among
    the presentation's referenced elements (reduces to the single element's
    layout on Phase-1 manifests); `source` remains the routing source for
    single-element callers."""
    from . import iamftools as it
    pr = m.presentations[0]
    if len(m.elements) > 1:
        native = it.presentation_native_layout(m, pr)
    else:
        native = source.layout if source.kind == "bed" else None
    return list(pr.loudness_layouts) or default_loudness_layouts(native)


def encode_argv(m: Manifest, t: Target, source: Source, staged_wav: str,
                out_path: str, *, video: str | None,
                loudness: dict[str, dict[str, str]] | None) -> list[str]:
    """One FFmpeg invocation: pan-split, stream groups, streamids, mux.

    loudness: layout name -> {il,dp,tp} strings (values or measure tokens);
    None = pass-1 (no loudness fields; FFmpeg would write 0.0 — F7 — which is
    why a pass-1 artifact is never a deliverable).
    """
    pol = m.policy.codec
    subs = substream_split(source)
    fc, maps = [], []
    for label, pan, _c in subs:
        fc.append(f"[0:a]pan={pan}[{label}]")
        maps += ["-map", f"[{label}]"]
    argv = [FFMPEG, "-y", "-hide_banner", "-i", staged_wav]
    if video is not None:
        argv += ["-i", video]
    argv += ["-filter_complex", ";".join(fc)] + maps
    if video is not None:
        argv += ["-map", "1:v", "-c:v", "copy"]
    for i, (_l, _p, coupled) in enumerate(subs):
        br = pol.bitrate_coupled if coupled else pol.bitrate_uncoupled
        argv += [f"-c:a:{i}", "libopus", f"-b:a:{i}", str(br)]

    st = "".join(f":st={i}" for i in range(len(subs)))
    if source.kind == "bed":
        ae = (f"type=iamf_audio_element:id=1{st}:audio_element_type=channel"
              f",layer=ch_layout={BEDS[source.layout].ffmpeg_ch_layout}")
    else:
        # F2/F3: ambisonics_mode is a LAYER key and the layer needs an
        # explicit `ambisonic N` ch_layout.
        ae = (f"type=iamf_audio_element:id=1{st}:audio_element_type=scene"
              f",layer=ch_layout=ambisonic {source.ambisonics_order}"
              f":ambisonics_mode=mono")

    pr = m.presentations[0]
    pe = pr.elements[0]
    lang = sorted(pr.annotations)[0] if pr.annotations else "en-us"
    ann = pr.annotations.get(lang, pr.id)
    lay_entries = []
    for name in loudness_layout_names(m, source):
        ml = MIX_LAYOUTS[name]
        entry = f"layout=sound_system={ml.ffmpeg_sound_system}"
        if loudness is not None:
            v = loudness[name]
            entry += (f":integrated_loudness={v['il']}"
                      f":digital_peak={v['dp']}:true_peak={v['tp']}")
        lay_entries.append(entry)
    # F1: parameter_rate REQUIRED per distinct parameter_id (100 and 101).
    mp = (f"type=iamf_mix_presentation:id=2:stg=0:annotations={lang}={ann},"
          f"submix=parameter_id=100:parameter_rate=48000:default_mix_gain=0.0"
          f"|element=stg=0:parameter_id=101:parameter_rate=48000"
          f":default_mix_gain={pe.gain_db:.1f}"
          f":annotations={lang}={pe.ref}"
          f"|" + "|".join(lay_entries))
    argv += ["-stream_group", ae, "-stream_group", mp]
    # F6: explicit unique streamids; audio 0..n-1 (= substream ids), video n.
    for i in range(len(subs)):
        argv += ["-streamid", f"{i}:{i}"]
    if video is not None:
        argv += ["-streamid", f"{len(subs)}:{len(subs)}"]
    if out_path.endswith(".mp4"):
        argv += ["-movflags", "+faststart"]
    argv.append(out_path)
    return argv


def render_argv(iamf_path: str, layout_name: str, rendered_wav: str,
                mix_id: int | None = None) -> list[str]:
    """mix_id (R8, D-Z6): pins the rendered mix presentation when the
    stream carries more than one (probe P2: omitted -> the decoder picks;
    a requested-but-absent id FALLS BACK rc 0 with only a warning, so Loom
    emits ids it wrote itself and the toolchain suite discriminates by
    rendered loudness). None -> argv byte-identical to Phase 1."""
    ml = MIX_LAYOUTS[layout_name]
    argv = [
        DECODER,
        f"--input_filename={iamf_path}",
        f"--output_filename={rendered_wav}",
        f"--output_layout={ml.decoder_output_layout}",
    ]
    if mix_id is not None:
        argv.append(f"--mix_id={mix_id}")
    return argv


SENTINEL_DSP = f"{TOOLCHAIN}/{BINARIES['sentinel-dsp']}"


def measure_argv(rendered_wav: str, layout_name: str) -> list[str]:
    """BS.1770-4 measurement via the sentinel-dsp kernel (F34, doc 69 §4).

    One argv, one JSON on stdout: integrated_lufs / digital_peak_dbfs /
    true_peak_dbtp — all three injected figures from one conformant,
    deterministic engine. Replaces the ffmpeg ebur128+astats pair, whose
    BACK_MASK weighting is F29-non-conformant on 7.1.x rears/top-backs;
    the values Loom DECLARES must be conformant (PRD goal 4).
    `--layout` takes the kernel's own vocabulary — exactly
    MIX_LAYOUTS[...].decoder_output_layout ("2.0"/"5.1"/"7.1.4"), which is
    also the channel order decoder_main renders (the d69 pin's path).
    """
    ml = MIX_LAYOUTS[layout_name]
    return [SENTINEL_DSP, rendered_wav, "--layout",
            ml.decoder_output_layout]


def gain_ride_argvs(src_wav: str, ridden_wav: str,
                    gain_expr: str) -> tuple[list[str], list[str]]:
    """R3 gain-ride: (volume argv, post-ride astats argv for the clip guard).

    The ride always writes 24-bit integer PCM (a 16-bit source upgrades
    losslessly; quantization error stays at the 24-bit floor). gain_expr is a
    `${gain:...}` token — the applied dB lands in the ledger, auditable
    (ADR-5). The astats argv runs on the RIDDEN file; the executor fails the
    step if its digital peak exceeds -0.05 dBFS (Loom is not a limiter).
    """
    ride = [FFMPEG, "-y", "-hide_banner", "-i", src_wav,
            "-af", f"volume={gain_expr}dB", "-c:a", "pcm_s24le", ridden_wav]
    astats = [FFMPEG, "-hide_banner", "-nostats", "-i", ridden_wav,
              "-af",
              "astats=metadata=1:measure_overall=Peak_level:measure_perchannel=none",
              "-f", "null", "-"]
    return ride, astats


def loudness_tokens(measure_step_ids: dict[str, str]) -> dict[str, dict[str, str]]:
    """layout name -> {il,dp,tp} measure tokens for the pass-2 argv."""
    return {
        name: {
            "il": measure_token(sid, "il"),
            "dp": measure_token(sid, "dp"),
            "tp": measure_token(sid, "tp"),
        }
        for name, sid in measure_step_ids.items()
    }


# ---- R9 preview (doc 46) ----------------------------------------------------

PREVIEW_OPUS_BITRATE = "128k"   # review copy; a named constant, not a manifest
                                # surface (ADR-5 — no new numeric field)


def render_binaural_argv(iamf_path: str, rendered_wav: str,
                         mix_id: int | None = None) -> list[str]:
    """decoder_main binaural render (R9). --output_sample_type omitted (the
    decoder picks per input; actual bits are recorded, never assumed).
    mix_id (R8, D-Z6): pins the selected presentation on multi-presentation
    intermediates; None on single-presentation streams (unambiguous —
    argv byte-identical to the R9 shape)."""
    argv = [
        DECODER,
        f"--input_filename={iamf_path}",
        f"--output_filename={rendered_wav}",
        "--output_layout=Binaural",
    ]
    if mix_id is not None:
        argv.append(f"--mix_id={mix_id}")
    return argv


def preview_opus_argv(wav: str, out_path: str) -> list[str]:
    """Opus review copy. -bitexact: no version-bearing muxer/encoder tags in
    the deliverable (the doc-44 date-stamp lesson, applied at design time)."""
    return [FFMPEG, "-y", "-hide_banner", "-i", wav,
            "-c:a", "libopus", "-b:a", PREVIEW_OPUS_BITRATE,
            "-bitexact", out_path]


def preview_verify_argv(wav: str) -> list[str]:
    """Per-channel + overall peak for the R9 verify step (recorded in the
    ledger; per-channel silence is a program fact, not a run failure)."""
    return [FFMPEG, "-hide_banner", "-nostats", "-i", wav,
            "-af",
            "astats=metadata=1:measure_overall=Peak_level:"
            "measure_perchannel=Peak_level",
            "-f", "null", "-"]
