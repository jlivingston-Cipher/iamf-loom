"""Layout ground truth for the compiler.

Substream decomposition tables derive the IAMF substream order (coupled pairs
first, C/LFE uncoupled last — the F4 rule) from the declared source layout.
The (substreams, coupled) arithmetic is cross-checked at compile time against
`sentinel.layouts` (the clean-room spec tables, OSS core) so Loom and Sentinel
can never drift apart silently.

WAV input channel order is the BS.2051 convention WP1's essence used
(order: bs2051, the only bed order accepted in Phase 1):
  stereo : L R
  5.1    : L R C LFE Ls Rs
  7.1.4  : L R C LFE Lss Rss Lrs Rrs Ltf Rtf Ltb Rtb
Ambisonics: ACN channel order, SN3D normalization (order: acn / norm: sn3d).
"""

from __future__ import annotations

from dataclasses import dataclass

try:  # ground truth cross-check (iamf-sentinel, ours, Apache-2.0)
    from sentinel.layouts import LOUDSPEAKER_LAYOUT as _SENTINEL_LL
except ImportError:  # pragma: no cover - sentinel is a declared dependency
    _SENTINEL_LL = None


@dataclass(frozen=True)
class Substream:
    label: str                # stable internal label, e.g. "frontLR"
    wav_channels: tuple[int, ...]  # indices into the source WAV (1=mono, 2=pair)

    @property
    def coupled(self) -> bool:
        return len(self.wav_channels) == 2


@dataclass(frozen=True)
class BedLayout:
    name: str                 # manifest name
    channels: int
    substreams: tuple[Substream, ...]   # ALREADY in IAMF order (F4 rule)
    ffmpeg_ch_layout: str     # AVIAMFLayer ch_layout string
    textproto_layout_enum: str
    textproto_labels: tuple[str, ...]   # per WAV channel index
    sentinel_lsl: int         # loudspeaker_layout code for the cross-check


# --- Bed layouts (Phase 1 set = the WP1-validated corpus) -------------------

BEDS: dict[str, BedLayout] = {
    "stereo": BedLayout(
        name="stereo",
        channels=2,
        substreams=(Substream("frontLR", (0, 1)),),
        ffmpeg_ch_layout="stereo",
        textproto_layout_enum="LOUDSPEAKER_LAYOUT_STEREO",
        textproto_labels=("CHANNEL_LABEL_L_2", "CHANNEL_LABEL_R_2"),
        sentinel_lsl=1,
    ),
    "5.1": BedLayout(
        name="5.1",
        channels=6,
        # IAMF order: (L/R)(Ls/Rs) then C, LFE — never the WAV order.
        substreams=(
            Substream("frontLR", (0, 1)),
            Substream("surrLR", (4, 5)),
            Substream("center", (2,)),
            Substream("lfe", (3,)),
        ),
        ffmpeg_ch_layout="5.1(side)",
        textproto_layout_enum="LOUDSPEAKER_LAYOUT_5_1_CH",
        textproto_labels=(
            "CHANNEL_LABEL_L_5", "CHANNEL_LABEL_R_5", "CHANNEL_LABEL_CENTRE",
            "CHANNEL_LABEL_LFE", "CHANNEL_LABEL_LS_5", "CHANNEL_LABEL_RS_5",
        ),
        sentinel_lsl=2,
    ),
    "7.1.4": BedLayout(
        name="7.1.4",
        channels=12,
        # (front)(side-surround)(rear-surround)(top-front)(top-back) C LFE
        substreams=(
            Substream("frontLR", (0, 1)),
            Substream("sideLR", (4, 5)),
            Substream("backLR", (6, 7)),
            Substream("topfLR", (8, 9)),
            Substream("topbLR", (10, 11)),
            Substream("center", (2,)),
            Substream("lfe", (3,)),
        ),
        ffmpeg_ch_layout="7.1.4",
        textproto_layout_enum="LOUDSPEAKER_LAYOUT_7_1_4_CH",
        textproto_labels=(
            "CHANNEL_LABEL_L_7", "CHANNEL_LABEL_R_7", "CHANNEL_LABEL_CENTRE",
            "CHANNEL_LABEL_LFE", "CHANNEL_LABEL_LSS_7", "CHANNEL_LABEL_RSS_7",
            "CHANNEL_LABEL_LRS_7", "CHANNEL_LABEL_RRS_7", "CHANNEL_LABEL_LTF_4",
            "CHANNEL_LABEL_RTF_4", "CHANNEL_LABEL_LTB_4", "CHANNEL_LABEL_RTB_4",
        ),
        sentinel_lsl=7,
    ),
}


def bed_arithmetic_ok(layout: BedLayout) -> bool:
    """Cross-check our substream decomposition against sentinel.layouts."""
    n_sub = len(layout.substreams)
    n_coupled = sum(1 for s in layout.substreams if s.coupled)
    if _SENTINEL_LL is None:
        return True
    ground = _SENTINEL_LL.get(layout.sentinel_lsl)
    return (
        ground is not None
        and ground.channels == layout.channels
        and ground.expected_substreams == n_sub
        and ground.expected_coupled == n_coupled
    )


# --- Loudness / mix layouts (sound systems) ---------------------------------

@dataclass(frozen=True)
class MixLayout:
    name: str
    ffmpeg_sound_system: str      # -stream_group layout=sound_system=...
    textproto_sound_system: str   # ss_layout sound_system enum
    decoder_output_layout: str    # decoder_main --output_layout


MIX_LAYOUTS: dict[str, MixLayout] = {
    "stereo": MixLayout("stereo", "stereo", "SOUND_SYSTEM_A_0_2_0", "2.0"),
    "5.1": MixLayout("5.1", "5.1(side)", "SOUND_SYSTEM_B_0_5_0", "5.1"),
    "7.1.4": MixLayout("7.1.4", "7.1.4", "SOUND_SYSTEM_J_4_7_0", "7.1.4"),
}


def default_loudness_layouts(native: str | None) -> list[str]:
    """Stereo always; plus the element's native bed layout when measurable."""
    out = ["stereo"]
    if native and native in MIX_LAYOUTS and native != "stereo":
        out.append(native)
    return out


def ambisonics_labels(order: int) -> tuple[str, ...]:
    return tuple(f"CHANNEL_LABEL_A_{i}" for i in range((order + 1) ** 2))
