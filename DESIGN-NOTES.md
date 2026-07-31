# Design notes — reading the provenance references in this codebase

This project was developed against an internal, numbered design docset: every work
cycle produced a numbered document recording what was decided, what was measured, and
the evidence behind both. Code comments cite those records rather than restating them
(`doc 44`, `ADR-2`, `D-Q4`, `R8`, …). The docset itself is not published, but the
notation is simple and the index below preserves the context each citation carries —
a comment like “`-for-test` (doc 44 — required for byte-determinism)” reads as “this
flag exists because of a recorded, tested decision.”

## Notation

| Form | Meaning |
|---|---|
| `doc NN` (§ optional) | A numbered internal design/evidence document; see the index below. |
| `ADR-1` … `ADR-6b` | Architecture Decision Records (doc 14, amendments noted where cited). The ones that shape Loom: **ADR-1** (backend routing: iamf-tools primary, FFmpeg for one-shot A/V encode), **ADR-2** (all remux via MP4Box; grounded — after two context revisions — in the F31/F32 repairability asymmetry), **ADR-4** (external tools are subprocess-only), **ADR-5** (job-level typed overrides rejected; multi-template batches instead). |
| `PRD`, `R1` … `R10` | The packager PRD (doc 13) and its numbered requirements (R3 normalize, R6 batch, R8 multi-language, R9 preview, R10 explain…). |
| `F1` … `F34` | Failure-mode register entries — real defects and pitfalls in the ecosystem's toolchains, mapped to checks in `iamf-sentinel`'s `F_TO_CHECK.md`. Ones cited here: F7 (log-parsing trap, retired), F9 (an MP4Box warning once thought benign — actually the visible surface of F32), F10 (the binaural render is single-oracle), F29 (ffmpeg ebur128's `BACK_MASK` over-weights 7.1.x rears/top-backs vs BS.1770-4 Tables 4/5 — the reason Loom never measures with ebur128; see the register), F31 (FFmpeg stream-copy loses IAMF start-trim), F32 (MP4Box `stts` duration defect, filed as gpac/gpac#3826), F34 (our own measure-path bug, fixed by routing all measurement through the `sentinel-dsp` kernel). |
| `G1` … `G11` | WP1 toolchain-validation gates/findings (doc 10) — measured facts about the reference toolchain that the routing rationales cite (e.g. G9: the YouTube A/V ingest shape; G11: capabilities only the iamf-tools route exposes). |
| `M-NNN` | Loom's stable compile-diagnostic codes (`loom.diagnostics.CODES`). |
| `S-NNN` | Sentinel check IDs (the gate wired into `loom run`). |
| `D-…` | A pre-registered per-cycle design decision label (e.g. `D-Q4`: a gate-failing output is never admitted to the cache; `D-V2`: cross-template collision detection). |
| `E-…` | A pre-registered expectation — written before the experiment ran. |
| `WP1` | The toolchain-validation work package; `wp1-samples` is its corpus (see test-skip messages for staging). |

## Index of cited documents

| Doc | What it established |
|---|---|
| 04 | Fact base: Dolby Atmos delivery formats (background for preset design). |
| 11 | WP1 failure-mode register (first F-numbers). |
| 13 | The Loom PRD — R1–R10. |
| 14 | The architecture ADRs (ADR-1…6). |
| 35 | The `sentinel-dsp` kernel contract in the validator: present-but-broken kernel = error, never a silent fallback (Loom inherits the same posture for its measure steps). |
| 42 | Loom Phase 1: manifest compiler, plan DAG, dual encode backends. |
| 43 | Loom Phase 2: `normalize:`, presets, the Sentinel gate wired into `loom run` (on by default), gate-severity escalation. |
| 44 | Loom Phase 3: `loom batch`, the content-addressed cache, fsync'd journal + hash-verified resume, MP4Box `-for-test` for byte-determinism, the ledger key contract. |
| 45 | R10 `loom explain`: the compiled plan rendered as its own justification (golden-pinned). |
| 46 | R9 `format: preview`: binaural review copies; the gate runs on the intermediate; the silent-stereo-downmix trap that requires `headphones: binaural` (M-402). |
| 47 | R8 multi-language presentation expansion: per-mix profile arithmetic (M-415/M-416), presentation-scoped row bindings. |
| 48 | The decision to publish the whole stack as free software, Apache-2.0. |
| 49 | Per-job policy variance: multi-template batches adopted; ADR-5 (typed overrides rejected); the cache key excludes template identity so cross-template dedup works; the spec-hash formula pinned for pre-upgrade resume. |
| 50 | The repository/publication regime (public repos start with fresh history). |
| 51 | Release prep: the re-licensing pass (Apache-2.0 across the stack). |
| 69 | F33 fix in the validator's DSP (conformant 7.1.x weights) — the measurement engine Loom's gate and measure steps rely on. |
| 70 | F34 fix: every Loom `measure_bs1770` step routed to the `sentinel-dsp` kernel; measured-vs-declared ≤0.005 LU on both reference decoders. |
| 76 | Pre-publication quality audit: the M-code error-path test table, `util.py` consolidation, the unconfigured-toolchain contract. |

The golden snapshots (`tests/fixtures/golden*`), the ledger contract
(`REQUIRED_LEDGER_KEYS`), and the M-code registry are the living, shipped form of most
of this history — the index exists so the remaining pointers stay meaningful.
