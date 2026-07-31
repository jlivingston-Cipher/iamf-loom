"""F32 in-product container repair (item 13, doc 84): `stts` -> §6.2.2.

MP4Box writes IAMF IA-sample durations that exclude the start-trim from the
first sample's duration (F32, gpac/gpac#3826, filed with patch offer),
violating IAMF v1.1.0 §6.2.2's normative duration model — "the duration of
an IA Sample includes audio samples trimmed at the beginning but excludes
audio samples trimmed at the end" — while writing a *correct* edit list.
The file contradicts itself, and a 14496-12-conformant consumer that trusts
`stts` (measured: FFmpeg n8.1.2, doc 60) discards the first temporal unit
whole: 648 real samples lost on the doc-60 matrix. The `conform` row of
that matrix proved the repair: rewrite only the timing tables to the
§6.2.2 model and both reference ecosystems decode sample-exact.

This module is that proven surgery, generalized from doc 60's
`patch_tables.py` (audio-only fixtures) to Loom deliverables (which may
carry a video track): byte-size-preserving in-place rewrites only — no
offset shifts, `mdat`/essence bytes untouched — with the dependent
duration fields (mdhd, tkhd, elst segment_duration, mvhd) re-cohered.
Essence facts (nspf, per-substream trim sums, TU count) are read from the
file's own reconstructed IA stream via the sentinel clean-room parser
(doc 60 E-E: extraction is byte-identical to source; importing our own
library is not an ADR-4 boundary crossing).

Contract (doc-35 discipline — assert loudly, never guess):
- Already-conformant tables (or trim-free essence) -> no-op. This is what
  makes the repair a natural no-op the day gpac's fix ships.
- Any structural surprise (no iacb track, non-v0 boxes, timescale !=
  sample rate, table shape outside the adjudicated F32 form) ->
  RepairError, which the executor converts into a failed step. A silent
  pass-through would ship the defect; a silent guess could corrupt.

Removal trigger (mirrors ADR-2's): the gpac fix merged upstream AND a
released gpac carries it — the repair is already a no-op then; delete the
step when the toolchain pin moves past that release.

Box layout knowledge is from ISO/IEC 14496-12 (clean-room, same boundary
as sentinel's walker).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator


class RepairError(Exception):
    """Structural surprise during container repair — never repair by guess."""


# ---- box walking (ISO/IEC 14496-12 §4.2) -----------------------------------

def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[str, int, int, int]]:
    """Yield (fourcc, box_start, payload_start, box_end) for each child box."""
    p = start
    while p + 8 <= end:
        size = int.from_bytes(data[p:p + 4], "big")
        fourcc = data[p + 4:p + 8].decode("latin-1")
        header = 8
        if size == 1:
            size = int.from_bytes(data[p + 8:p + 16], "big")
            header = 16
        elif size == 0:
            size = end - p
        if size < header or p + size > end:
            raise RepairError(f"malformed box {fourcc!r} at {p}")
        yield fourcc, p, p + header, p + size
        p += size


def _find(data: bytes, start: int, end: int,
          path: list[str]) -> tuple[int, int, int] | None:
    """First box matching path, as (box_start, payload_start, box_end)."""
    for fc, bs, ps, be in _boxes(data, start, end):
        if fc == path[0]:
            if len(path) == 1:
                return bs, ps, be
            return _find(data, ps, be, path[1:])
    return None


def _stsd_has_iacb(data: bytes, trak_ps: int, trak_be: int) -> bool:
    """True when the trak's stsd carries an `iamf` AudioSampleEntry with an
    `iacb` IAConfigurationBox child (sample-entry layout per 14496-12
    §12.2.3: SampleEntry 8 + audio fixed part 20 = 28 bytes, then children —
    the same walk sentinel's container layer performs)."""
    stsd = _find(data, trak_ps, trak_be, ["mdia", "minf", "stbl", "stsd"])
    if stsd is None:
        return False
    _, ps, be = stsd
    for fc, _bs, es, ee in _boxes(data, ps + 8, be):
        if fc != "iamf":
            continue
        for cfc, _cbs, _cps, _cbe in _boxes(data, es + 28, ee):
            if cfc == "iacb":
                return True
    return False


def _u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "big")


def _w32(data: bytearray, off: int, value: int, what: str) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise RepairError(f"{what} {value} does not fit in 32 bits")
    data[off:off + 4] = value.to_bytes(4, "big")


def _require_v0(data: bytes, ps: int, box: str) -> None:
    if data[ps] != 0:
        raise RepairError(f"{box} version {data[ps]} unsupported "
                          "(doc-60 evidence covers v0 only)")


# ---- essence facts ---------------------------------------------------------

def _essence_facts(data: bytes) -> tuple[int, int, int, int, int]:
    """(nspf, sample_rate, start_trim, end_trim, tus) from the MP4's own
    reconstructed IA stream — per-substream sums, consensus by max
    (sentinel S-407/S-408's bookkeeping; a disagreement is the gate's job,
    not ours)."""
    from sentinel.container.mp4 import extract_iamf_stream, parse_mp4
    from sentinel.parser import parse_bytes

    info = parse_mp4(bytes(data))
    stream = extract_iamf_stream(bytes(data), info)
    if stream is None:
        raise RepairError("cannot reconstruct the IA stream (no iacb/mdat?)")
    mod = parse_bytes(stream, source="<extracted>", container="raw")
    cc = next(iter(mod.codec_configs.values()), None)
    if cc is None or not cc.num_samples_per_frame:
        raise RepairError("no codec config / num_samples_per_frame in essence")
    if not cc.sample_rate:
        raise RepairError("no sample rate in essence codec config")
    per: dict[int, list[int]] = {}
    for fr in mod.audio_frames:
        st = per.setdefault(fr.substream_id, [0, 0, 0])
        st[0] += fr.trim_start
        st[1] += fr.trim_end
        st[2] += 1
    if not per:
        raise RepairError("essence carries no audio frames")
    start_trim = max(v[0] for v in per.values())
    end_trim = max(v[1] for v in per.values())
    tus = max(v[2] for v in per.values())
    return cc.num_samples_per_frame, cc.sample_rate, start_trim, end_trim, tus


# ---- the repair ------------------------------------------------------------

def repair_stts(mp4_path: str | Path) -> dict:
    """Repair the IAMF track's timing tables to the §6.2.2 model, in place.

    Returns an evidence record; raises RepairError on any structural
    surprise. No-op (repaired: False) when the tables already conform.
    """
    path = Path(mp4_path)
    data = bytearray(path.read_bytes())
    nspf, sr, start_trim, end_trim, tus = _essence_facts(data)
    if not 0 <= end_trim < nspf:
        raise RepairError(f"end-trim {end_trim} outside [0, nspf {nspf})")

    moov = _find(data, 0, len(data), ["moov"])
    if moov is None:
        raise RepairError("no moov box")
    _, mps, mpe = moov

    # The IAMF track: exactly one trak whose stsd carries an iacb entry.
    iamf_traks = [(bs, ps, be) for fc, bs, ps, be in _boxes(data, mps, mpe)
                  if fc == "trak" and _stsd_has_iacb(data, ps, be)]
    if len(iamf_traks) != 1:
        raise RepairError(f"expected exactly one iacb trak, found "
                          f"{len(iamf_traks)}")
    _, tps_payload, tbe = iamf_traks[0]

    mdhd = _find(data, tps_payload, tbe, ["mdia", "mdhd"])
    if mdhd is None:
        raise RepairError("iacb trak has no mdhd")
    _, hps, _ = mdhd
    _require_v0(data, hps, "mdhd")
    media_ts = _u32(data, hps + 12)
    mdhd_before = _u32(data, hps + 16)
    if media_ts != sr:
        raise RepairError(f"media timescale {media_ts} != essence sample "
                          f"rate {sr} (scaling unproven — doc 60 is 1:1)")

    stts = _find(data, tps_payload, tbe, ["mdia", "minf", "stbl", "stts"])
    if stts is None:
        raise RepairError("iacb trak has no stts")
    _, sps, sbe = stts
    _require_v0(data, sps, "stts")
    n_entries = _u32(data, sps + 4)
    if sps + 8 + 8 * n_entries > sbe:
        raise RepairError("stts entry count overruns the box")
    entries = [(_u32(data, sps + 8 + 8 * i), _u32(data, sps + 12 + 8 * i))
               for i in range(n_entries)]
    if not entries:
        raise RepairError("stts has no entries")
    total_count = sum(c for c, _ in entries)
    if total_count != tus:
        raise RepairError(f"stts covers {total_count} samples but essence "
                          f"carries {tus} temporal units")

    # §6.2.2 model: every IA sample lasts nspf, except the final one, whose
    # duration excludes the end-trim.
    final_delta = nspf - end_trim
    sum_before = sum(c * d for c, d in entries)
    model_sum = tus * nspf - end_trim

    def conformant() -> bool:
        if end_trim == 0:
            return all(d == nspf for _, d in entries)
        return (all(d == nspf for _, d in entries[:-1])
                and entries[-1] == (1, final_delta))

    record = {
        "nspf": nspf, "sample_rate": sr, "start_trim": start_trim,
        "end_trim": end_trim, "temporal_units": tus,
        "stts_entries": n_entries,
        "sum_before": sum_before, "model_sum": model_sum,
    }

    if conformant():
        record.update(repaired=False,
                      note="stts already matches the §6.2.2 model "
                           "(gpac fixed, or trim-free content) — no-op")
        return record

    # Rewrite, keeping the entry count (byte-size-preserving). The final
    # short sample must be its own single-count entry when end-trim > 0.
    if end_trim > 0:
        if tus > 1 and n_entries < 2:
            raise RepairError("cannot encode the end-trim remainder in a "
                              "single stts entry")
        if entries[-1][0] != 1 and tus > 1:
            raise RepairError(f"final stts entry count {entries[-1][0]} != 1 "
                              "— shape outside the adjudicated F32 form")
    for i in range(n_entries):
        delta = final_delta if (i == n_entries - 1 and end_trim > 0) else nspf
        _w32(data, sps + 12 + 8 * i, delta, "stts delta")

    new_mdur = model_sum   # (tus-1)*nspf + final_delta, by construction
    _w32(data, hps + 16, new_mdur, "mdhd duration")

    mvhd = _find(data, mps, mpe, ["mvhd"])
    if mvhd is None:
        raise RepairError("no mvhd box")
    _, vps, _ = mvhd
    _require_v0(data, vps, "mvhd")
    movie_ts = _u32(data, vps + 12)
    mvhd_before = _u32(data, vps + 16)

    # elst: SHALL be present when the essence is trimmed (§6.2.2). MP4Box
    # writes it correctly (media_time = start-trim); keep it, re-cohere the
    # segment duration. Loud error if the start-trim's carrier is missing.
    elst = _find(data, tps_payload, tbe, ["edts", "elst"])
    media_time = 0
    if elst is not None:
        _, eps, _ = elst
        _require_v0(data, eps, "elst")
        if _u32(data, eps + 4) != 1:
            raise RepairError("elst entry count != 1")
        media_time = int.from_bytes(data[eps + 12:eps + 16], "big",
                                    signed=True)
    if start_trim > 0:
        if elst is None:
            raise RepairError("essence carries start-trim but the file has "
                              "no elst (S-407 territory, not repairable "
                              "from tables alone)")
        if media_time != start_trim:
            raise RepairError(f"elst media_time {media_time} != summed "
                              f"start-trim {start_trim} — contradicts the "
                              "adjudicated F32 shape")

    pres_media = new_mdur - max(media_time, 0)
    pres_movie = round(pres_media * movie_ts / media_ts)
    if elst is not None:
        _, eps, _ = elst
        _w32(data, eps + 8, pres_movie, "elst segment_duration")

    tkhd = _find(data, tps_payload, tbe, ["tkhd"])
    if tkhd is None:
        raise RepairError("iacb trak has no tkhd")
    _, kps, _ = tkhd
    _require_v0(data, kps, "tkhd")
    tkhd_before = _u32(data, kps + 20)
    _w32(data, kps + 20, pres_movie, "tkhd duration")

    # mvhd: the longest track's presentation. With a video track present its
    # tkhd is untouched, so max(old, ours) is exact; audio-only, old < ours.
    _w32(data, vps + 16, max(mvhd_before, pres_movie), "mvhd duration")

    path.write_bytes(data)
    record.update(
        repaired=True, sum_after=new_mdur,
        mdhd_duration=[mdhd_before, new_mdur],
        tkhd_duration=[tkhd_before, pres_movie],
        mvhd_duration=[mvhd_before, max(mvhd_before, pres_movie)],
        elst_segment_duration=pres_movie if elst is not None else None,
        note="stts rewritten to the §6.2.2 duration model "
             "(size-preserving; essence bytes untouched)")
    return record
