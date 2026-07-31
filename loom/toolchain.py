"""Toolchain root resolution (mirrors the Sentinel Pro pattern).

--toolchain > $LOOM_TOOLCHAIN > $SENTINEL_TOOLCHAIN; unset means "not
configured". A missing binary is an actionable error naming the path —
never a traceback (the Sentinel error-contract style, doc 35).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

class ToolchainError(Exception):
    pass


def resolve_root(cli_value: str | None = None) -> Path | None:
    """The configured toolchain root, or None when nothing names one."""
    root = (cli_value or os.environ.get("LOOM_TOOLCHAIN")
            or os.environ.get("SENTINEL_TOOLCHAIN"))
    return Path(root) if root else None


BINARIES = {
    "encoder_main": "src/build-iamf/encoder_main",
    "decoder_main": "src/build-iamf/decoder_main",
    "ffmpeg": "bin/ffmpeg-install/bin/ffmpeg",
    "mp4box": "bin/MP4Box",
    "sentinel-dsp": "bin/sentinel-dsp",
}


def binary(root: Path | None, tool: str) -> Path:
    if tool == "sentinel-dsp":
        # F34: the BS.1770-4-conformant measure engine (doc 69 §4). Honour
        # sentinel-pro's $SENTINEL_DSP when it names a binary; the token
        # `off` is sentinel-pro's numpy escape hatch and must NOT silently
        # reroute Loom's measure steps — Loom requires the kernel.
        env = os.environ.get("SENTINEL_DSP")
        if env and env != "off":
            p = Path(env)
            if p.is_file() and os.access(p, os.X_OK):
                return p
    rel = BINARIES[tool]
    p = (root / rel) if root is not None else None
    if p is not None and p.is_file() and os.access(p, os.X_OK):
        return p
    if tool == "mp4box":
        # MP4Box commonly lives on PATH (system GPAC); accept that too.
        found = shutil.which("MP4Box")
        if found:
            return Path(found)
    if tool == "sentinel-dsp":
        # The kernel commonly lives on PATH (the doc-35 backend contract).
        found = shutil.which("sentinel-dsp")
        if found:
            return Path(found)
    raise ToolchainError(
        (f"toolchain binary `{tool}` not found at {p}" if p is not None
         else f"no toolchain configured for `{tool}`")
        + " — set --toolchain, "
        "$LOOM_TOOLCHAIN or $SENTINEL_TOOLCHAIN to a root built per "
        "wp3-scripts/build_toolchain.sh (+ toolchain-addendum.sh)"
        + (" — the sentinel-dsp kernel builds via `cmake -S "
           "sentinel-pro/sentinel-dsp` and may also be named by "
           "$SENTINEL_DSP or found on PATH" if tool == "sentinel-dsp" else "")
    )


def available(root: Path | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for tool in BINARIES:
        try:
            binary(root, tool)
            out[tool] = True
        except ToolchainError:
            out[tool] = False
    return out
