"""Validated manifest model (post-schema, pre-compile).

Nothing in this model stores a derivable value (ADR-5): no loudness numbers,
no substream ids/order, no profile strings beyond the policy floor. The
compiler derives all of that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Source:
    name: str
    path: str                # manifest-relative as written
    kind: str                # bed | ambisonics  (adm rejected in Phase 1)
    layout: str | None       # beds only
    order: str               # bs2051 (bed) | acn (ambisonics)
    norm: str | None         # sn3d (ambisonics)
    # filled by validation:
    channels: int = 0
    sample_rate: int = 0
    bits: int = 0
    frames: int = 0
    sha256: str = ""
    ambisonics_order: int | None = None


@dataclass(frozen=True)
class Element:
    name: str
    source: str


@dataclass(frozen=True)
class PresentationElement:
    ref: str
    gain_db: float = 0.0
    headphones: str = "stereo"   # stereo | binaural


@dataclass(frozen=True)
class Presentation:
    id: str
    annotations: dict[str, str] = field(default_factory=dict)
    elements: tuple[PresentationElement, ...] = ()
    loudness_layouts: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodecPolicy:
    name: str = "opus"           # opus | lpcm | flac
    bitrate_coupled: int = 128_000
    bitrate_uncoupled: int = 64_000


@dataclass(frozen=True)
class Policy:
    codec: CodecPolicy = field(default_factory=CodecPolicy)
    loudness_mode: str = "measure"   # forward-compat knob; "measure" is the only mode (M-203 rejects others)
    normalize: float | None = None   # R3: gain-ride target (LUFS), else None
    profile: str = "auto"        # auto | simple | base | base_enhanced
    validate: str = "fail_on_error"


@dataclass(frozen=True)
class Target:
    format: str                  # iamf | mp4 | preview
    out: str
    video: str | None = None
    preset: str | None = None    # youtube | archive
    route: str = "auto"          # auto | oneshot | remux
    presentation: str | None = None  # R8: preview mix selection (id), else
                                     # presentations[0]; a selection, never
                                     # metadata (ADR-5)


@dataclass(frozen=True)
class Manifest:
    title: str
    manifest_dir: Path
    manifest_sha256: str
    sources: dict[str, Source]
    elements: dict[str, Element]
    presentations: tuple[Presentation, ...]
    policy: Policy
    targets: tuple[Target, ...]
