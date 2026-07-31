"""Compile-time `video:` input probe (Phase 2 — the doc-42 deviation-5 note).

Original, stdlib-only ISO-BMFF reader written from ISO/IEC 14496-12 (clean-room
discipline; independent of Sentinel's container walker — the sentinel trees are
never modified by Loom work). Extracts only what the compile-time checks need:
whether a real video track exists, its duration, and its sample-entry fourcc —
so the S-406 class (A/V duration mismatch discovered only after encode) is
caught at compile, and `preset: youtube` can insist on the G9-validated H.264
shape before any tool runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VideoProbe:
    ok: bool = False                 # a readable video track was found
    duration_s: float | None = None  # from the video track's mdhd
    fourcc: str | None = None        # first stsd sample-entry type, e.g. avc1
    error: str | None = None
    all_handlers: list[str] = field(default_factory=list)


def _boxes(data: bytes, start: int, end: int):
    """Yield (fourcc, payload_start, box_end); tolerates 64-bit largesize."""
    p = start
    while p + 8 <= end:
        size = int.from_bytes(data[p:p + 4], "big")
        fourcc = data[p + 4:p + 8].decode("latin-1", "replace")
        header = 8
        if size == 1:
            if p + 16 > end:
                break
            size = int.from_bytes(data[p + 8:p + 16], "big")
            header = 16
        elif size == 0:
            size = end - p
        if size < header or p + size > end:
            break
        yield fourcc, p + header, p + size
        p += size


def _child(data: bytes, start: int, end: int, fourcc: str):
    for fc, ps, be in _boxes(data, start, end):
        if fc == fourcc:
            return ps, be
    return None


def _track_probe(data: bytes, ps: int, be: int) -> tuple[str, float | None, str | None]:
    """(handler, duration_s, stsd_fourcc) for one trak box."""
    handler, dur, fourcc = "", None, None
    mdia = _child(data, ps, be, "mdia")
    if mdia is None:
        return handler, dur, fourcc
    mps, mbe = mdia
    hd = _child(data, mps, mbe, "hdlr")
    if hd is not None:
        hps, hbe = hd
        if hbe - hps >= 12:
            handler = data[hps + 8:hps + 12].decode("latin-1", "replace")
    mh = _child(data, mps, mbe, "mdhd")
    if mh is not None:
        hps, hbe = mh
        version = data[hps]
        try:
            if version == 1 and hbe - hps >= 32:
                timescale = int.from_bytes(data[hps + 20:hps + 24], "big")
                duration = int.from_bytes(data[hps + 24:hps + 32], "big")
            elif hbe - hps >= 24:
                timescale = int.from_bytes(data[hps + 12:hps + 16], "big")
                duration = int.from_bytes(data[hps + 16:hps + 20], "big")
            else:
                timescale, duration = 0, 0
            if timescale > 0:
                dur = duration / timescale
        except (IndexError, ValueError):
            pass
    minf = _child(data, mps, mbe, "minf")
    if minf is not None:
        nps, nbe = minf
        stbl = _child(data, nps, nbe, "stbl")
        if stbl is not None:
            sps, sbe = stbl
            sd = _child(data, sps, sbe, "stsd")
            if sd is not None:
                dps, dbe = sd
                # stsd: version/flags (4) + entry_count (4) + first entry box
                if dbe - dps >= 16:
                    fourcc = data[dps + 12:dps + 16].decode("latin-1", "replace")
    return handler, dur, fourcc


def probe_video(path: str | Path) -> VideoProbe:
    """Probe an MP4 for its (first) video track. Never raises."""
    pr = VideoProbe()
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        pr.error = f"cannot read file: {e}"
        return pr
    moov = None
    for fc, ps, be in _boxes(data, 0, len(data)):
        if fc == "moov":
            moov = (ps, be)
            break
    if moov is None:
        pr.error = "no moov box found (not an ISO-BMFF/MP4 file?)"
        return pr
    for fc, ps, be in _boxes(data, moov[0], moov[1]):
        if fc != "trak":
            continue
        handler, dur, fourcc = _track_probe(data, ps, be)
        pr.all_handlers.append(handler or "?")
        if handler == "vide" and not pr.ok:
            pr.ok = True
            pr.duration_s = dur
            pr.fourcc = fourcc
    if not pr.ok:
        pr.error = (f"no video (`vide`) track found; track handlers: "
                    f"{pr.all_handlers or 'none'}")
    return pr


H264_FOURCCS = {"avc1", "avc3"}
DURATION_TOLERANCE_S = 1.0
