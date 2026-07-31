"""Compile-time diagnostics — stable M- codes (the Loom analogue of Sentinel's
S- IDs). Every user-facing compile failure carries (code, path, message);
messages name both sides of any mismatch (PRD edge cases: never a guess).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str  # dotted manifest path, e.g. "sources.main_714.layout"
    message: str

    def __str__(self) -> str:  # noqa: D105
        return f"{self.code} at {self.path}: {self.message}"


class CompileError(Exception):
    """Raised with the full list of diagnostics collected before abort."""

    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = list(diagnostics)
        super().__init__(
            "; ".join(str(d) for d in self.diagnostics) or "compile failed"
        )

    def codes(self) -> list[str]:
        return [d.code for d in self.diagnostics]


@dataclass
class Collector:
    """Accumulates diagnostics so one compile reports as many as possible."""

    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, code: str, path: str, message: str) -> None:
        self.diagnostics.append(Diagnostic(code, path, message))

    def raise_if_any(self) -> None:
        if self.diagnostics:
            raise CompileError(self.diagnostics)


# Registry (code -> summary) — a stable contract, mirrored in tests.
CODES: dict[str, str] = {
    # M-1xx: manifest file / parse level
    "M-101": "manifest file not found or unreadable",
    "M-102": "manifest is not valid YAML/JSON",
    "M-103": "missing/unsupported `loom` schema version (expected `loom: 0`)",
    # M-2xx: schema / structural
    "M-201": "missing required field",
    "M-202": "wrong type for field",
    "M-203": "unknown enum value",
    "M-204": "duplicate id",
    "M-205": "unknown reference",
    "M-206": "`unsafe_overrides` is reserved and not available in Phase 1",
    "M-207": "invalid identifier (names/ids: letters, digits, _ and - only)",
    # M-3xx: source / asset level
    "M-301": "source file missing",
    "M-302": "source is not a readable RIFF/WAVE file",
    "M-303": "declared layout does not match the WAV channel count",
    "M-304": "ambisonics channel count is not (N+1)^2",
    "M-305": "unsupported layout name (Phase 1 set: stereo, 5.1, 7.1.4)",
    "M-306": "bed source missing required `layout`",
    "M-307": "unsupported order/norm (beds: bs2051; ambisonics: acn/sn3d)",
    "M-308": "unsupported WAV format (Phase 1: 48 kHz, 16/24-bit integer PCM)",
    "M-309": "`kind: adm` lands with R7 (post-WP3 policy defaults); not in Phase 1",
    # M-4xx: policy / routing
    "M-401": "(resolved in Phase 2) `loudness.normalize` was Phase-1-rejected; "
             "kept for the stable-contract record, no longer emitted",
    "M-402": "policy conflict",
    "M-403": "target not routable under the requested route (ADR-1/ADR-2)",
    "M-404": "`preset: youtube` requires a `video:` input (G9: A/V MP4 ingest shape)",
    "M-405": "(resolved in R8) the Phase-1 single-element gates retired with "
             "multi-element mixes; kept for the stable-contract record, "
             "no longer emitted",
    "M-406": "(resolved in Phase 2) flac emitter was Phase-1-absent; "
             "kept for the stable-contract record, no longer emitted",
    "M-407": "unsupported loudness layout (Phase 1 set: stereo, 5.1, 7.1.4)",
    "M-408": "gain_db out of sane range (-60.0 .. +20.0)",
    # M-4xx additions (Phase 2)
    "M-409": "`loudness.normalize` target out of sane range (-36.0 .. -5.0 LUFS)",
    # M-5xx: video probe (Phase 2, doc-42 deviation 5 — the S-406 class at compile)
    "M-410": "`video:` file has no readable video track (ISO-BMFF probe)",
    "M-411": "audio/video duration mismatch beyond tolerance (> 1.0 s)",
    "M-412": "`preset: youtube` requires an H.264 (avc1/avc3) video track (G9 shape)",
    # M-41x/M-42x additions (Phase 3, R6 batch)
    "M-413": "manifest references an unbound {variable} (supply --var or a "
             "batch `vars:` binding; escape literal braces as {{ }})",
    # M-41x additions (R8, multi-language presentation expansion)
    "M-414": "`languages:` expansion block malformed (expected a non-empty "
             "list of mappings with string values and a unique `lang` key "
             "per row)",
    "M-415": "element sources are not mutually consistent (multi-element "
             "mixes require one shared frame count; flac/lpcm additionally "
             "require one shared bit depth)",
    "M-416": "element/channel counts exceed every IAMF profile's limits "
             "(per mix presentation: simple 1 element/16 ch; base 2/18; "
             "base_enhanced 28/28)",
    "M-420": "batch file missing, unparsable, or schema-invalid "
             "(expected `loom_batch: 0` with `manifest:` and `jobs:`)",
    "M-421": "duplicate job id or two jobs resolving to the same output path",
}
