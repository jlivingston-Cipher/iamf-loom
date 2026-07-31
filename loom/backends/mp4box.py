"""MP4Box mux routing (ADR-2).

All remux/repackage flows route through MP4Box; FFmpeg muxes only when it
is also the encoder in the same invocation. Grounding (ADR-2, amended):
the repairability asymmetry — FFmpeg's IAMF copy path discards
`num_samples_to_trim_at_start` irrecoverably, zeroing both carriers (F31),
while MP4Box keeps the bitstream byte-identical and writes a correct
`elst`; its `stts` non-conformance (F32, gpac/gpac#3826) is repaired
in-product by the `repair_stts` step that follows every remux (item 13,
doc 84 — byte-size-preserving §6.2.2 rewrite, no-op once gpac's fix is
merged and released) and tripwired downstream by Sentinel S-408. The
WP1-validated
YouTube candidate shape (G9) came from exactly this command shape:
video first, then the raw .iamf, `-new` output — producing fast-start
`ftyp/moov/mdat` with brands isom/mp42/iso6/iamf and RFC6381
`iamf.001.001.opus` (GPAC lowercases the codec fourcc — F12, cosmetic,
recorded not "fixed"). The F9 DTS-patch warning on raw-IAMF import is
expected on stderr — it is the visible surface of F32, not benign.
"""

from __future__ import annotations

from ..plan import TOOLCHAIN
from ..toolchain import BINARIES

MP4BOX = f"{TOOLCHAIN}/{BINARIES['mp4box']}"   # B13: one path map


def remux_argv(iamf_path: str, out_path: str, video: str | None) -> list[str]:
    # `-for-test` (doc 44, R6): MP4Box otherwise stamps wall-clock
    # creation/modification dates + the GPAC version into mvhd/tkhd/mdhd/udta,
    # making byte-identical outputs impossible (PRD goal 3: deterministic
    # outputs for dedup/forensics). Verified to preserve the G9 shape exactly
    # (brands, fast-start, RFC6381 string) — only dates/version strings drop.
    argv = [MP4BOX, "-for-test"]
    if video is not None:
        argv += ["-add", video]
    argv += ["-add", iamf_path, "-new", out_path]
    return argv
