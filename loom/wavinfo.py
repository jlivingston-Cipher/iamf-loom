"""Minimal stdlib RIFF/WAVE header reader.

Python's `wave` module rejects WAVE_FORMAT_EXTENSIBLE, which professional
multichannel masters routinely use — so Loom carries its own ~60-line reader.
Reads the fmt chunk (format tag, channels, sample rate, bits) and the data
chunk size; never loads sample data.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


class WavError(Exception):
    pass


@dataclass(frozen=True)
class WavInfo:
    channels: int
    sample_rate: int
    bits_per_sample: int
    frames: int
    format_tag: int


def read_wav_info(path: str | Path) -> WavInfo:
    p = Path(path)
    try:
        with open(p, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                raise WavError(f"{p.name}: not a RIFF/WAVE file")
            fmt = None
            data_size = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, csize = hdr[:4], struct.unpack("<I", hdr[4:8])[0]
                if cid == b"fmt ":
                    body = f.read(csize)
                    if len(body) < 16:
                        raise WavError(f"{p.name}: truncated fmt chunk")
                    (tag, ch, rate, _br, _ba, bits) = struct.unpack(
                        "<HHIIHH", body[:16]
                    )
                    if tag == WAVE_FORMAT_EXTENSIBLE and len(body) >= 40:
                        # SubFormat GUID: first 2 bytes are the real format tag
                        tag = struct.unpack("<H", body[24:26])[0]
                        bits_valid = struct.unpack("<H", body[18:20])[0]
                        if bits_valid:
                            bits = bits_valid
                    fmt = (tag, ch, rate, bits)
                elif cid == b"data":
                    data_size = csize
                    f.seek(csize + (csize & 1), 1)
                    continue
                else:
                    f.seek(csize + (csize & 1), 1)
                    continue
                if csize & 1:
                    f.seek(1, 1)
            if fmt is None:
                raise WavError(f"{p.name}: no fmt chunk")
            if data_size is None:
                raise WavError(f"{p.name}: no data chunk")
            tag, ch, rate, bits = fmt
            if ch == 0 or bits == 0:
                raise WavError(f"{p.name}: nonsensical fmt (ch={ch}, bits={bits})")
            frames = data_size // (ch * (bits // 8)) if bits >= 8 else 0
            return WavInfo(ch, rate, bits, frames, tag)
    except OSError as e:
        raise WavError(f"{p}: {e.strerror or e}") from e
