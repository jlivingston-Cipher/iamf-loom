"""iamf-tools backend — original textproto emitter + encoder_main invocation.

The emitter writes the `UserMetadata` textproto surface (schema:
iamf-tools iamf/cli/proto/user_metadata.proto, BSD-3-Clause-Clear) from
Loom's model. The schema is the public API surface; this file is original
code — no template instantiation in the product path (D-L6).

Loudness is measured natively by encoder_main per declared loudness layout
(WP1 G2b) — computed, never typed (ADR-5).
"""

from __future__ import annotations

from ..layouts import BEDS, MIX_LAYOUTS, ambisonics_labels, default_loudness_layouts
from ..model import Manifest, Presentation, Source
from ..plan import TOOLCHAIN
from ..toolchain import BINARIES

ENCODER = f"{TOOLCHAIN}/{BINARIES['encoder_main']}"   # B13: one path map

_PROFILE_ENUM = {
    "simple": "PROFILE_VERSION_SIMPLE",
    "base": "PROFILE_VERSION_BASE",
    "base_enhanced": "PROFILE_VERSION_BASE_ENHANCED",
}
_PROFILE_RANK = {"simple": 0, "base": 1, "base_enhanced": 2}

AUDIO_ELEMENT_ID = 300     # base id; element i is AUDIO_ELEMENT_ID + i (R8)
CODEC_CONFIG_ID = 200

# R8 (D-Z2): per-MIX-PRESENTATION caps, matching the pinned encoder's
# profile_filter.cc (probe P1: enforced by execution) and IAMF v1.1.0.
PROFILE_CAPS = {              # profile -> (max elements, max channels)
    "simple": (1, 16),
    "base": (2, 18),
    "base_enhanced": (28, 28),
}


def mix_requirement(n_elements: int, n_channels: int) -> str | None:
    """Smallest profile whose per-mix caps admit this mix, else None."""
    for prof in ("simple", "base", "base_enhanced"):
        max_e, max_c = PROFILE_CAPS[prof]
        if n_elements <= max_e and n_channels <= max_c:
            return prof
    return None


def derive_profile(policy_profile: str, preset: str | None,
                   m: Manifest | None = None) -> str:
    """profile: auto — computed from target needs, never typed (ADR-5).

    R8 arithmetic (probe-verified against the pinned encoder): the need is
    the max over mix presentations of the smallest profile admitting that
    mix's element/channel counts — caps are per mix presentation, so an
    N-language file (bed + one VO per mix) stays base regardless of N.
    The YouTube ingest shape floors at base (G9/G11); an explicit policy
    profile acts as a floor, never a cap-below-needs. Counts exceeding
    every profile are a compile error (M-416) raised by the compiler
    before this function is asked to derive.
    """
    need = "base" if preset == "youtube" else "simple"
    if m is not None:
        for pr in m.presentations:
            n_els = len(pr.elements)
            n_ch = sum(m.sources[m.elements[pe.ref].source].channels
                       for pe in pr.elements)
            req = mix_requirement(n_els, n_ch)
            if req is not None and _PROFILE_RANK[req] > _PROFILE_RANK[need]:
                need = req
    floor = policy_profile if policy_profile != "auto" else "simple"
    return need if _PROFILE_RANK[need] >= _PROFILE_RANK[floor] else floor


def element_allocation(m: Manifest) -> dict[str, tuple[int, int]]:
    """Deterministic per-element (audio_element_id, substream_base) in
    manifest declaration order. Element 0 keeps ids 300 / substreams 0..n-1,
    so single-element emission is byte-identical to Phase 1 (D-Z4)."""
    alloc: dict[str, tuple[int, int]] = {}
    sub_base = 0
    for i, (name, el) in enumerate(m.elements.items()):
        src = m.sources[el.source]
        n_sub = (len(BEDS[src.layout].substreams) if src.kind == "bed"
                 else src.channels)
        alloc[name] = (AUDIO_ELEMENT_ID + i, sub_base)
        sub_base += n_sub
    return alloc


def q78(gain_db: float) -> int:
    """dB -> Q7.8 fixed point (proto: 256 * dB)."""
    return round(gain_db * 256)


def _esc(s: str) -> str:
    """Escape a string for a double-quoted textproto literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _codec_block(policy, source: Source, bits_override: int | None = None) -> list[str]:
    if policy.codec.name == "opus":
        per_channel = policy.codec.bitrate_uncoupled
        return [
            "codec_config_metadata {",
            f"  codec_config_id: {CODEC_CONFIG_ID}",
            "  codec_config {",
            "    codec_id: CODEC_ID_OPUS",
            "    num_samples_per_frame: 960",
            "    decoder_config_opus {",
            "      version: 1",
            "      input_sample_rate: 48000",
            "      opus_encoder_metadata {",
            f"        target_bitrate_per_channel: {per_channel}",
            "        application: APPLICATION_AUDIO",
            "        use_float_api: false",
            "      }",
            "    }",
            "  }",
            "}",
        ]
    if policy.codec.name == "flac":
        # Phase 2 (R4, preset: archive): lossless mezzanine. Proto quirks
        # (pinned iamf-tools 93597f1 testdata): STREAMINFO bits_per_sample is
        # stored MINUS ONE ("15 # Flac interprets this as 16 bits");
        # min/max block size must equal num_samples_per_frame.
        bps = (bits_override or source.bits) - 1
        return [
            "codec_config_metadata {",
            f"  codec_config_id: {CODEC_CONFIG_ID}",
            "  codec_config {",
            "    codec_id: CODEC_ID_FLAC",
            "    num_samples_per_frame: 1024",
            "    decoder_config_flac {",
            "      metadata_blocks {",
            "        header {",
            "          block_type: FLAC_BLOCK_TYPE_STREAMINFO",
            "        }",
            "        stream_info {",
            "          minimum_block_size: 1024",
            "          maximum_block_size: 1024",
            f"          sample_rate: {source.sample_rate}",
            f"          bits_per_sample: {bps}",
            f"          total_samples_in_stream: {source.frames}",
            "        }",
            "      }",
            "      flac_encoder_metadata {",
            "        compression_level: 8",
            "      }",
            "    }",
            "  }",
            "}",
        ]
    # lpcm
    return [
        "codec_config_metadata {",
        f"  codec_config_id: {CODEC_CONFIG_ID}",
        "  codec_config {",
        "    codec_id: CODEC_ID_LPCM",
        "    num_samples_per_frame: 1024",
        "    decoder_config_lpcm {",
        "      sample_format_flags: LPCM_LITTLE_ENDIAN",
        f"      sample_size: {bits_override or source.bits}",
        f"      sample_rate: {source.sample_rate}",
        "    }",
        "  }",
        "}",
    ]


def _element_block(source: Source, element_id: int, sub_base: int) -> list[str]:
    if source.kind == "bed":
        bed = BEDS[source.layout]
        n_sub = len(bed.substreams)
        n_coupled = sum(1 for s in bed.substreams if s.coupled)
        ids = ", ".join(str(sub_base + i) for i in range(n_sub))
        return [
            "audio_element_metadata {",
            f"  audio_element_id: {element_id}",
            "  audio_element_type: AUDIO_ELEMENT_CHANNEL_BASED",
            "  reserved: 0",
            f"  codec_config_id: {CODEC_CONFIG_ID}",
            f"  audio_substream_ids: [{ids}]",
            "  scalable_channel_layout_config {",
            "    reserved: 0",
            "    channel_audio_layer_configs: [",
            "      {",
            f"        loudspeaker_layout: {bed.textproto_layout_enum}",
            "        output_gain_is_present_flag: 0",
            "        recon_gain_is_present_flag: 0",
            "        reserved_a: 0",
            f"        substream_count: {n_sub}",
            f"        coupled_substream_count: {n_coupled}",
            "      }",
            "    ]",
            "  }",
            "}",
        ]
    n = source.channels
    ids = ", ".join(str(sub_base + i) for i in range(n))
    mapping = ", ".join(str(i) for i in range(n))
    return [
        "audio_element_metadata {",
        f"  audio_element_id: {element_id}",
        "  audio_element_type: AUDIO_ELEMENT_SCENE_BASED",
        "  reserved: 0",
        f"  codec_config_id: {CODEC_CONFIG_ID}",
        f"  audio_substream_ids: [{ids}]",
        "  ambisonics_config {",
        "    ambisonics_mode: AMBISONICS_MODE_MONO",
        "    ambisonics_mono_config {",
        f"      output_channel_count: {n}",
        f"      substream_count: {n}",
        f"      channel_mapping: [{mapping}]",
        "    }",
        "  }",
        "}",
    ]


def _frame_block(source: Source, staged_wav: str, element_id: int) -> list[str]:
    if source.kind == "bed":
        labels = BEDS[source.layout].textproto_labels
    else:
        labels = ambisonics_labels(source.ambisonics_order or 0)
    chans = ",\n".join(
        f"    {{ channel_id: {i} channel_label: {lab} }}"
        for i, lab in enumerate(labels)
    )
    return [
        "audio_frame_metadata {",
        f'  wav_filename: "{staged_wav}"',
        "  samples_to_trim_at_end_includes_padding: false",
        "  samples_to_trim_at_start_includes_codec_delay: false",
        "  samples_to_trim_at_end: 0",
        "  samples_to_trim_at_start: 0",
        f"  audio_element_id: {element_id}",
        "  channel_metadatas: [",
        chans,
        "  ]",
        "}",
    ]


def presentation_native_layout(m: Manifest, pr: Presentation) -> str | None:
    """Default-loudness-layout native: the widest bed among the
    presentation's referenced elements (single-element manifests reduce to
    the Phase-1 value — that element's bed layout, or None)."""
    native: str | None = None
    width = 0
    for pe in pr.elements:
        src = m.sources[m.elements[pe.ref].source]
        if src.kind == "bed" and src.channels > width:
            native, width = src.layout, src.channels
    return native


def _presentation_block(pr: Presentation, idx: int, m: Manifest,
                        alloc: dict[str, tuple[int, int]],
                        pid_start: int) -> tuple[list[str], int]:
    langs = sorted(pr.annotations)
    ann_langs = ", ".join(f'"{lang}"' for lang in langs)
    ann_texts = ", ".join(f'"{_esc(pr.annotations[lang])}"' for lang in langs)
    layouts = (list(pr.loudness_layouts)
               or default_loudness_layouts(presentation_native_layout(m, pr)))
    lay_lines: list[str] = []
    for name in layouts:
        ml = MIX_LAYOUTS[name]
        lay_lines += [
            "    layouts {",
            "      loudness_layout {",
            "        layout_type: LAYOUT_TYPE_LOUDSPEAKERS_SS_CONVENTION",
            "        ss_layout {",
            f"          sound_system: {ml.textproto_sound_system}",
            "          reserved: 0",
            "        }",
            "      }",
            "      loudness {",
            "        info_type_bit_masks: []",
            "      }",
            "    }",
        ]
    # parameter ids: unique placeholders from a running counter (mode 1 =
    # defaults used; ids must simply not collide). One id per element entry
    # plus one for the output mix — reduces exactly to the Phase-1
    # 1000+idx*2 / 1001+idx*2 formula on single-element manifests (D-Z4).
    pid = pid_start
    el_lines: list[str] = []
    for pe in pr.elements:
        hp = ("HEADPHONES_RENDERING_MODE_BINAURAL_WORLD_LOCKED"
              if pe.headphones == "binaural"
              else "HEADPHONES_RENDERING_MODE_STEREO")
        element_id = alloc[pe.ref][0]
        el_lines += [
            "    audio_elements {",
            f"      audio_element_id: {element_id}",
            f'      localized_element_annotations: ["{pe.ref}"]',
            "      rendering_config {",
            f"        headphones_rendering_mode: {hp}",
            "      }",
            "      element_mix_gain {",
            "        param_definition {",
            f"          parameter_id: {pid}  # placeholder; default used",
            "          parameter_rate: 48000",
            "          param_definition_mode: 1",
            "          reserved: 0",
            "        }",
            f"        default_mix_gain: {q78(pe.gain_db)}",
            "      }",
            "    }",
        ]
        pid += 1
    pid_out = pid
    pid += 1
    lines = [
        "mix_presentation_metadata {",
        f"  mix_presentation_id: {42 + idx}",
        f"  annotations_language: [{ann_langs}]",
        f"  localized_presentation_annotations: [{ann_texts}]",
        "  sub_mixes {",
        *el_lines,
        "    output_mix_gain {",
        "      param_definition {",
        f"        parameter_id: {pid_out}  # placeholder; default used",
        "        parameter_rate: 48000",
        "        param_definition_mode: 1",
        "        reserved: 0",
        "      }",
        "      default_mix_gain: 0",
        "    }",
        *lay_lines,
        "  }",
        "}",
    ]
    return lines, pid


def emit_textproto(m: Manifest, staged: dict[str, str],
                   file_name_prefix: str, profile: str,
                   bits_override: int | None = None) -> str:
    """The complete UserMetadata textproto for one target.

    staged: source name -> staged WAV filename (all in one wav directory).
    bits_override: R3 — a gain-ridden input WAV is always 24-bit PCM; the
    codec block must describe the WAV the encoder actually reads.
    R8: one frame block + one element block per declared element, in
    manifest order; ids/substreams per `element_allocation` (single-element
    manifests emit byte-identically to Phase 1 by construction, D-Z4).
    """
    prof = _PROFILE_ENUM[profile]
    alloc = element_allocation(m)
    first_source = m.sources[next(iter(m.elements.values())).source]
    frame_lines: list[str] = []
    element_lines: list[str] = []
    for name, el in m.elements.items():
        src = m.sources[el.source]
        element_id, sub_base = alloc[name]
        if frame_lines:
            frame_lines.append("")
        frame_lines += _frame_block(src, staged[el.source], element_id)
        if element_lines:
            element_lines.append("")
        element_lines += _element_block(src, element_id, sub_base)
    lines: list[str] = [
        # D-Q8 (doc 44): version-independent tag — the header lives inside
        # golden-plan bytes, so a version bump must never touch goldens again.
        "# Generated by Loom — do not edit (computed-never-typed, ADR-5).",
        "# proto-file: iamf/cli/proto/user_metadata.proto",
        "# proto-message: UserMetadata",
        "",
        "test_vector_metadata {",
        f'  human_readable_description: "Loom: {_esc(m.title)}"',
        f'  file_name_prefix: "{file_name_prefix}"',
        "  is_valid: true",
        "}",
        "",
        *frame_lines,
        "",
        "ia_sequence_header_metadata {",
        f"  primary_profile: {prof}",
        f"  additional_profile: {prof}",
        "}",
        "",
        *_codec_block(m.policy, first_source, bits_override),
        "",
        *element_lines,
    ]
    pid = 1000
    for idx, pr in enumerate(m.presentations):
        block, pid = _presentation_block(pr, idx, m, alloc, pid)
        lines += ["", *block]
    lines += [
        "",
        "temporal_delimiter_metadata {",
        "  enable_temporal_delimiters: false",
        "}",
        "",
    ]
    return "\n".join(lines)


def encoder_argv(config_path: str, wav_dir: str, out_dir: str) -> list[str]:
    return [
        ENCODER,
        f"--user_metadata_filename={config_path}",
        f"--input_wav_directory={wav_dir}",
        f"--output_iamf_directory={out_dir}",
    ]
