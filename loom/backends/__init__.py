"""Backend routing — the ADR-1/ADR-2 decision table as a pure function.

Users never choose a backend; policy and target shape do (ADR-1). The only
user-visible routing surface is `route:` (auto | oneshot | remux), which can
*narrow* the table — request a route shape and fail loudly if the manifest's
features don't fit — never widen it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..diagnostics import Collector
from ..model import Manifest, Target


@dataclass(frozen=True)
class Route:
    backend: str          # iamftools | ffmpeg_oneshot
    muxer: str | None     # mp4box | ffmpeg | None
    rationale: str


def _oneshot_blockers(t: Target, m: Manifest) -> list[str]:
    """Why this target can NOT take the FFmpeg A/V one-shot route (ADR-1)."""
    blockers: list[str] = []
    if t.format != "mp4":
        blockers.append("one-shot is an A/V MP4 shape (target format is "
                        f"{t.format})")
    if t.video is None:
        blockers.append("one-shot means encode+mux in one FFmpeg invocation; "
                        "no `video:` given (ADR-2: FFmpeg muxes only when it "
                        "is also the encoder)")
    if m.policy.codec.name != "opus":
        blockers.append(f"ADR-1 restricts the FFmpeg one-shot to Opus; policy "
                        f"codec is {m.policy.codec.name} (FLAC/LPCM route to "
                        "iamf-tools)")
    if t.preset == "youtube":
        blockers.append("preset: youtube needs base-profile signaling, which "
                        "the FFmpeg CLI does not expose (G11) — routed via "
                        "iamf-tools + MP4Box")
    if len(m.presentations) != 1:
        blockers.append(f"{len(m.presentations)} presentations declared; the "
                        "WP1-validated one-shot shape carries exactly one")
    if len(m.elements) != 1:
        blockers.append(f"{len(m.elements)} elements declared; the "
                        "WP1-validated one-shot shape carries exactly one "
                        "element (multi-element mixes are an iamf-tools "
                        "textproto surface, R8)")
    for pr in m.presentations:
        for pe in pr.elements:
            if pe.headphones == "binaural":
                blockers.append("headphones: binaural is expressed via "
                                "iamf-tools rendering_config; not exposed on "
                                "the FFmpeg one-shot")
                break
        for text in pr.annotations.values():
            if any(ch in text for ch in ":|,="):
                blockers.append(
                    f"annotation text {text!r} contains stream-group syntax "
                    "characters (:|,=) — expressible only via the iamf-tools "
                    "textproto route")
                break
    return blockers


PREVIEW_RATIONALE = (
    "preview: stereo binaural review render (R9). iamf-tools primary encode "
    "(ADR-1) to an intermediate .iamf that is never a deliverable, rendered "
    "by decoder_main --output_layout=Binaural (the obr renderer, invoked "
    "subprocess-only — ADR-4). Single-oracle render: iamfdec's binauralizer "
    "is disabled toolchain-wide (F10), so verification is structural + "
    "spectral, never cross-decoder."
)


def route_target(t: Target, m: Manifest, c: Collector, tpath: str) -> Route | None:
    """Return the Route, or add an M-403 diagnostic and return None."""
    if t.format == "preview":
        # R9: a preview has exactly one route; `route:` may not redirect it.
        if t.route != "auto":
            c.add("M-403", tpath,
                  f"route: {t.route} requested but a preview target has "
                  "exactly one route — iamf-tools encode + decoder_main "
                  "binaural render (R9); the FFmpeg one-shot has no binaural "
                  "render and there is nothing to remux")
            return None
        return Route(backend="iamftools", muxer=None,
                     rationale=PREVIEW_RATIONALE)

    blockers = _oneshot_blockers(t, m)
    oneshot_ok = not blockers

    if t.route == "oneshot":
        if oneshot_ok:
            return Route(
                backend="ffmpeg_oneshot", muxer="ffmpeg",
                rationale="requested route honored: Opus A/V one-shot, the "
                          "ADR-1 designated FFmpeg use; two-pass BS.1770 "
                          "measure->inject applied (F7 closed); F4 order and "
                          "F6 streamids compiler-enforced",
            )
        c.add("M-403", tpath,
              "route: oneshot requested but " + "; ".join(blockers))
        return None

    if t.route == "remux" and t.format == "iamf":
        # remux of a raw target is just the iamf-tools encode; accept.
        return Route(backend="iamftools", muxer=None,
                     rationale="raw .iamf: iamf-tools primary (ADR-1 — native "
                               "BS.1770 measured loudness, profile control)")

    if t.format == "iamf":
        return Route(backend="iamftools", muxer=None,
                     rationale="raw .iamf: iamf-tools primary (ADR-1 — native "
                               "BS.1770 measured loudness, profile control)")

    # mp4 targets
    if t.route == "auto" and oneshot_ok:
        return Route(backend="ffmpeg_oneshot", muxer="ffmpeg",
                     rationale="Opus A/V one-shot from WAV: the ADR-1 "
                               "designated FFmpeg use (encode+mux in one "
                               "invocation, ADR-2); two-pass BS.1770 "
                               "measure->inject applied (F7 closed); F4 order "
                               "and F6 streamids compiler-enforced")
    reason = ("requested route" if t.route == "remux"
              else "; ".join(blockers) if blockers else "policy")
    yt = (" YouTube shape needs base profile — iamf-tools is the only CLI "
          "route (G11)." if t.preset == "youtube" else "")
    return Route(backend="iamftools", muxer="mp4box",
                 rationale=f"iamf-tools encode + MP4Box mux (ADR-2: FFmpeg "
                           f"muxes only when it is also the encoder — its "
                           f"IAMF copy path discards start-trim irrecoverably "
                           f"(F31), while MP4Box keeps the essence intact and "
                           f"repairable (F32); {reason})." + yt)
