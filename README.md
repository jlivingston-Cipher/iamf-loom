# iamf-loom — manifest-driven IAMF batch packager

Loom turns ordinary WAV files into validated **IAMF** (Immersive Audio
Model and Formats) deliverables — the immersive-audio format used by Eclipsa
Audio devices and YouTube. You write a short YAML file describing the audio you
*have* and the outputs you *want*; Loom derives everything else, runs the
encoders, measures loudness, and validates every output with
[`iamf-sentinel`](https://github.com/jlivingston-Cipher/iamf-sentinel) before
accepting it. Its batch engine then scales that from one title to a catalog:
cached, resumable, and audit-logged.

**One manifest, correct output.** The two classic ways an immersive deliverable
silently goes wrong are a mistyped loudness value and a wrong channel order —
both produce files that encode cleanly, decode cleanly, and are still broken.
Loom's manifest closes those doors by design: there is *no field* for a
loudness value or a channel order. Loudness is always measured from the
rendered audio and embedded as the measurement; channel order is always derived
from the declared layout. What you cannot type, you cannot mistype — and you
never hand-edit encoder config files or wrangle multi-line muxer commands to
get there.

> **Free software, Apache-2.0** — part of the `iamf-sentinel` stack. The name
> `iamf-loom` is the project name; "Loom" is the short spoken form.

> **Scope.** Loom targets the **AOM IAMF v1.1.0** specification; every output
> is gated in-process by `iamf-sentinel` before it is admitted. Presets and
> profiles are **spec- and reference-validated, not platform-certified** —
> checked against the published specification and the reference tools, not
> against any platform's private ingest pipeline. The `youtube` preset applies
> the publicly-known IAMF shape for that target (base profile, H.264 pairing);
> a clean run is necessary, not by itself sufficient, for acceptance there. It
> is the only platform shape that has been verified end-to-end; Loom does not
> guess at other platforms' requirements.

This README and the code cite a numbered internal design docset (`doc NN`),
architecture decision records (`ADR-N`), and requirement labels (`R-N`). Those
documents are engineering provenance and are **not published** —
**[DESIGN-NOTES.md](DESIGN-NOTES.md)** explains the notation and carries a
one-line summary of every document this repo cites, so the references stay
meaningful on their own.

## What you need

**Python side** (installed automatically by pip): Python ≥ 3.11, PyYAML, and
`iamf-sentinel` (itself pure standard library). That is the complete Python
dependency list.

**External tools** are needed **only for `loom run`** — authoring and
inspecting plans with `compile` / `explain` needs none of them. See
[Installing the toolchain](#installing-the-toolchain-for-run) below for the
full setup walkthrough.

## Five-minute start

1. **Install:**

   ```bash
   pip install iamf-loom
   ```

2. **Have a WAV.** A bed source must match its declared layout exactly —
   channel count and order are probed and enforced. Channel order is the
   BS.2051 convention:

   | layout | channels, in WAV order |
   |---|---|
   | `stereo` | L R |
   | `5.1` | L R C LFE Ls Rs |
   | `7.1.4` | L R C LFE Lss Rss Lrs Rrs Ltf Rtf Ltb Rtb |

3. **Write `manifest.yaml`.** A complete one is 13 lines — this produces a
   lossless FLAC `.iamf` archive mezzanine from a 5.1 WAV:

   ```yaml
   loom: 0
   title: archive flac 5.1 mezzanine
   sources:
     main: { path: wavs/main.wav, kind: bed, layout: "5.1" }
   elements:
     bed: { from: main }
   presentations:
     - id: main
       annotations: { en-us: "5.1 Mix" }
       elements: [ { ref: bed } ]
   policy:
     codec: { name: flac }
   targets:
     - { format: iamf, out: dist/mezz51.iamf, preset: archive }
   ```

4. **Compile it** (no toolchain needed):

   ```bash
   loom compile manifest.yaml     # → the execution plan (JSON) on stdout, exit 0
   ```

   The plan is the whole story in advance: every source probed (channels,
   rate, bit depth, frames, sha256), every tool argv materialized, every
   routing decision with its rationale. A rejected manifest exits 2 and names
   its defect with a stable `M-` code and the remedy:

   ```
   error: M-305 at sources.main.layout: unsupported layout '9.9'; Phase 1 supports ['5.1', '7.1.4', 'stereo']
   ```

5. **Read it back as prose:**

   ```bash
   loom explain manifest.yaml     # sources as probed, policy as resolved, every step's "why"
   ```

6. **Execute it** (this is the step that needs the toolchain):

   ```bash
   loom run manifest.yaml --toolchain <root>
   ```

   Loom encodes, muxes, measures loudness on the rendered layouts, embeds the
   *measured* values, validates the output with `iamf-sentinel` (FAIL findings
   fail the run), and writes a machine-readable run ledger next to the
   outputs. Exit 0 means every target was produced, gated, and admitted.

## Manifest reference

Complete field vocabulary, matching the schema validator (`loom/manifest.py`);
anything else is rejected at compile with an `M-` code.

### `sources` — the audio you have

```yaml
sources:
  <name>: { path: <wav>, kind: bed, layout: "7.1.4" }          # bed
  <name>: { path: <wav>, kind: ambisonics }                    # scene (ACN/SN3D)
```

| field | values | notes |
|---|---|---|
| `path` | file path | probed at compile: channels/rate/bits/frames + sha256; must exist |
| `kind` | `bed` \| `ambisonics` \| `adm` | `adm` (ADM/BW64 master ingest) is **reserved for a future release**; using it today is rejected at compile with M-309 |
| `layout` | `stereo` \| `5.1` \| `7.1.4` | beds only; required; WAV must match it exactly |
| `order` | `bs2051` (beds) \| `acn` (ambisonics) | defaults shown; the only accepted values |
| `norm` | `sn3d` | ambisonics only; the only accepted value |

Ambisonics sources must carry (N+1)² channels for order N = 1…4 (so 4, 9, 16,
or 25 channels).

### `elements` and `presentations` — how it's organized

```yaml
elements:
  bed: { from: main }               # element <name> wraps source <from>
presentations:
  - id: main
    annotations: { en-us: "Theatrical Mix" }
    elements:
      - { ref: bed, gain_db: 0, headphones: stereo }
    loudness_layouts: [ stereo, "7.1.4" ]     # optional; layouts to measure
```

| field | values | notes |
|---|---|---|
| `elements[].ref` | an element name | at least one per presentation |
| `elements[].gain_db` | number, −60 … +20 | default 0 |
| `elements[].headphones` | `stereo` \| `binaural` | default `stereo`; `binaural` is required on the rendered presentation for preview targets |
| `loudness_layouts` | subset of `stereo` \| `5.1` \| `7.1.4` | optional: the speaker layouts this presentation's loudness is measured and declared for |
| `languages:` | list of `{ lang, label, <var>: <value> }` rows | expands the presentation to one per language (see the workflow below) |

### `policy` — global choices

| field | values | notes |
|---|---|---|
| `codec.name` | `opus` \| `flac` \| `lpcm` | default `opus` |
| `loudness.normalize` | LUFS, −36 … −5 | gain-ride then re-measure, ±0.3 LU verified; conflicts with `lpcm` (M-402 — passthrough stays bit-transparent); a ride that would clip fails loudly |
| `validate` | `fail_on_error` \| `off` | the Sentinel gate; on by default |

### `targets` — the outputs you want

```yaml
targets:
  - { format: iamf,    out: dist/master.iamf, preset: archive }
  - { format: mp4,     out: dist/av.mp4, video: v.mp4, preset: youtube }
  - { format: preview, out: review/check.opus, presentation: main-en-us }
```

| field | values | notes |
|---|---|---|
| `format` | `iamf` \| `mp4` \| `preview` | required |
| `out` | output path | required; `{title}` token allowed; duplicates rejected |
| `preset` | `youtube` \| `archive` | see Presets below |
| `video` | H.264 MP4 path | mp4 targets; required by `preset: youtube`; probed at compile (M-410/411/412) |
| `route` | `auto` \| `oneshot` \| `remux` | which encode/mux path produces the target; default `auto` picks per the routing rules (see "How it works") |
| `presentation` | a presentation id | preview targets only: which mix to render |

Preview targets: `out` must end `.wav` or `.opus`; no `video:`, no `preset:`;
the selected presentation must declare `headphones: binaural` (M-402 otherwise
— under STEREO headphone mode the decoder renders a plain stereo downmix, and
Loom refuses to mislabel that as binaural).

### Presets — the complete list

There are exactly **two**; no other platform shapes are invented:

- **`archive`** — the lossless FLAC raw-`.iamf` mezzanine (requires
  `policy.codec.name: flac`). A preset is a *shape* for the output — it never
  silently overrides your policy choices or sets a loudness; if a preset and
  your policy conflict, that is a compile error, not a quiet substitution.
- **`youtube`** — the validated A/V ingest MP4 (base profile, Opus, H.264
  pairing; requires `format: mp4` + `video:`). *"YouTube" is a trademark of
  Google LLC, used here nominatively to identify the delivery target; this
  project is independent and is not affiliated with or endorsed by Google or
  YouTube.*

## A real-world workflow: a localized series

A 12-episode series, each with a 7.1.4 music+effects bed and per-language VO
stems, delivered as one IAMF per episode carrying every language, plus a
binaural review copy — normalized to −14 LUFS. One template does an episode
(this manifest compiles as shown; verified):

```yaml
loom: 0
title: "{episode} localized"
sources:
  main:  { path: "wavs/{episode}/main714.wav", kind: bed, layout: "7.1.4" }
  vo_en: { path: "wavs/{episode}/vo_en.wav", kind: bed, layout: stereo }
  vo_de: { path: "wavs/{episode}/vo_de.wav", kind: bed, layout: stereo }
elements:
  bed:   { from: main }
  vo_en: { from: vo_en }
  vo_de: { from: vo_de }
presentations:
  - id: "main-{lang}"
    languages:
      - { lang: en-us, vo: vo_en, label: "English" }
      - { lang: de-de, vo: vo_de, label: "Deutsch" }
    elements:
      - { ref: bed }
      - { ref: "{vo}", gain_db: -3, headphones: binaural }
    loudness_layouts: [ stereo, "7.1.4" ]
policy:
  loudness: { normalize: -14 }
targets:
  - { format: iamf,    out: "dist/{episode}.iamf" }
  - { format: preview, out: "review/{episode}-en.opus", presentation: main-en-us }
```

Try one episode: `loom run episode.yaml --var episode=ep01 --toolchain <root>`.
The `languages:` block expands to one presentation per language inside one
IAMF, with loudness measured per presentation; `{episode}` is bound at the
command line or by a batch job. Then hand the season to the batch engine.

## Batch: industrializing a catalog

This is where Loom earns its keep. `loom batch` runs a whole catalog through
the same guaranteed pipeline — compiled up front, cached, resumable, and
audit-logged — so re-running a 200-title season costs only what actually
changed. The batch spec is small:

```yaml
loom_batch: 0
manifest: episode.yaml                  # the template every job runs
defaults: { out_dir: "out/{episode}" }
jobs:
  - { vars: { episode: ep01 } }
  - { vars: { episode: ep02 } }
  # ... one line per episode
```

```bash
loom batch season.yaml --workers 4
```

What the engine guarantees, stage by stage:

- **Every job compiles before anything runs.** The whole season is
  schema-checked and planned up front; two jobs resolving to the same output
  path abort the batch at compile — you find collisions in seconds, not after
  an hour of encoding.
- **A failing job costs only itself.** Encode failures don't stop the batch;
  the run finishes everything else and exits 2 at the end with the failures
  named in the ledger.
- **Finished work is never redone.** Every gate-passed output lands in a
  **content-addressed cache** keyed on the plan, the source-file hashes
  (video donors included), and the tool-binary hashes. Re-run the season
  untouched: every job replays from cache, byte-identical — in the shipped
  acceptance tests, 16 of 16 cache replays were byte-identical to the
  original outputs at roughly 17× the speed (doc 44). Re-master one episode's
  VO stem: only that episode's jobs re-run. Upgrade an encoder binary:
  everything correctly misses and rebuilds. Only validated outputs are ever
  admitted to the cache, and a hit re-hashes the stored file before it is
  trusted. `--no-cache` disables.
- **Interruptions are survivable.** The runner journals each finished job to
  disk as it completes; relaunch after a crash, a kill, or a lost machine and
  the batch picks up where it left off — a job is skipped only if its
  recorded outputs still hash clean on disk, so tampered or missing files
  re-run. Proven in the shipped tests by killing the runner mid-batch and
  resuming (doc 44). A journal from a *different* batch spec is a hard
  error, never silently mixed. `--fresh` starts over from nothing.
- **Worker count never changes the output.** `--workers 1` and `--workers 8`
  produce byte-identical deliverables (pinned by test) — parallelism is a
  speed choice, never a correctness variable.
- **Everything is on the record.** `batch-ledger.json` captures the spec
  hash, tool identities, and per-job vars, outputs, measured loudness, gate
  verdicts, cache status, and timings — the audit trail for the whole
  delivery. Its schema is pinned by a contract test.
- **Catalogs with different shapes stay one batch.** A job may name its own
  `manifest:` — so "the features get FLAC mezzanines, the trailers get
  YouTube MP4s" is two templates in one batch, not two pipelines. There are
  deliberately no per-job policy overrides: every job stays reproducible as
  a single `loom run` of its template (doc 49). Identical work still
  deduplicates across templates.

## Installing the toolchain (for `run`)

`loom run` drives four external tools. All of them are invoked strictly as
separate subprocesses — Loom links against nothing, so there are no shared
libraries to match and no license entanglement. Loom resolves the toolchain
root from
`--toolchain <root>`, else `$LOOM_TOOLCHAIN`, else `$SENTINEL_TOOLCHAIN`, and
expects this layout inside it:

| Tool | Used for | Expected location in the root |
|---|---|---|
| `encoder_main` / `decoder_main` (iamf-tools) | primary IAMF encoder + render oracle | `src/build-iamf/` |
| FFmpeg | the Opus A/V one-shot route | `bin/ffmpeg-install/bin/ffmpeg` |
| GPAC `MP4Box` | all MP4 remux | `bin/MP4Box`, else found on `PATH` |
| `sentinel-dsp` | BS.1770-4 loudness measurement kernel | `bin/sentinel-dsp`, else `$SENTINEL_DSP`, else `PATH` |

A missing tool is an actionable error naming the exact path Loom looked for —
run any manifest through `loom run` and it will tell you what's absent.

**Scripted build (recommended).** The
[`iamf-adm-corpus`](https://github.com/jlivingston-Cipher/iamf-adm-corpus)
repo ships `build_toolchain.sh` — a deterministic, resumable rebuild of the
reference toolchain (iamf-tools, libiamf, FFmpeg) into
`$SENTINEL_TOOLCHAIN` (~35 min cold on 2 cores):

```bash
export SENTINEL_TOOLCHAIN=$HOME/iamf-toolchain
./build_toolchain.sh
```

**MP4Box** is not built by that script — install GPAC from your distro (any
`MP4Box` on `PATH` is accepted) or build the static tool:

```bash
git clone https://github.com/gpac/gpac && cd gpac
./configure --static-mp4box && make            # ~10 min
# install the produced MP4Box binary at "$SENTINEL_TOOLCHAIN/bin/MP4Box"
```

**The `sentinel-dsp` kernel** builds from the
[`iamf-sentinel-pro`](https://github.com/jlivingston-Cipher/iamf-sentinel-pro)
repo with CMake and lands in the root (or anywhere on `PATH`):

```bash
cmake -S sentinel-dsp -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j2
cp build/sentinel-dsp "$SENTINEL_TOOLCHAIN/bin/sentinel-dsp"
```

Note: `SENTINEL_DSP=off` is `iamf-sentinel-pro`'s escape hatch to its numpy
reference path — it deliberately does **not** reroute Loom, which requires the
kernel for measurement (a present-but-broken kernel is an execution error,
never a silent fallback).

## Using Loom from an agent (MCP)

[`iamf-sentinel-mcp`](https://github.com/jlivingston-Cipher/iamf-sentinel-mcp)
exposes this packager and the validator to MCP clients (Claude Desktop, Claude
Code, and any runtime speaking the protocol): `loom_compile` and `loom_explain`
run toolchain-free and read-only; `loom_run` — which executes encoders and
writes files — is registered only when the server is launched with
`--enable-run`. The server also publishes `mcodes://catalog`, every `M-` code
diagnostic this packager can emit.

## How it works (the guarantees)

- **Deterministic plans (R1/R10; docs 42/45).** `compile` emits the complete
  plan — every command line that will run, in execution order, each with the
  reason it's there — and `explain` renders the same plan as prose. Both are
  locked by golden-file tests, so what a manifest compiles to cannot drift
  silently between versions.
- **Routing (ADR-1/2; docs 42/84).** Each target is produced by the tool
  that is *provably correct* for it: iamf-tools is the primary encoder;
  FFmpeg handles the Opus A/V one-shot; MP4Box handles all MP4 remux. The
  remux choice comes from a verified asymmetry in the two muxers' defects:
  FFmpeg's copy-remux silently destroys audio-trim data irrecoverably, while
  MP4Box writes a wrong-but-fixable timing table over intact audio — so Loom
  routes remux to MP4Box and then *repairs* its output (a byte-size-preserving
  `repair_stts` rewrite to the spec's timing model; a no-op on correct
  tables, so it retires itself when the upstream fix ships — gpac#3826).
- **Measured, never typed (R3; docs 43/70).** Loudness is measured by the
  `sentinel-dsp` kernel (an independent BS.1770-4 implementation) on the
  actually-rendered speaker layouts. `normalize:` applies a computed gain to
  the source and then *re-measures the result* — the values embedded in the
  file are always measurements of the audio that shipped, never arithmetic.
- **The Sentinel gate (R5; doc 43).** Every produced output is validated by
  `iamf-sentinel` in-process before Loom will admit it — including, when the
  decoder oracles are available, decoding the file and measuring what's
  actually in it. The checks that detect scrambled-channel corruption
  (S-320/S-321) are treated as run-failing on Loom's own outputs.
- **Multi-language and preview (R8/R9; docs 46/47).** One IAMF per title
  carries every language, with loudness measured per presentation. Which IAMF
  profile a mix needs is computed per presentation with the same element- and
  channel-count arithmetic the reference encoder itself enforces (a mix no
  profile admits is a compile error, M-416). Binaural review copies are
  rendered from an intermediate that has already passed the validation gate.

## Layout

- `loom/manifest.py` — loader + original stdlib schema validator (M- codes)
  + `{variable}` templating
- `loom/compiler.py` — manifest → plan; ADR-1/2 routing; ADR-5 derivations
- `loom/backends/` — per-tool argv builders: iamftools (original config
  emitter), ffmpeg (with guards for that tool's known IAMF pitfalls), mp4box
  (remux)
- `loom/executor.py` — subprocess runner; judges tool success by the outputs
  produced, never by exit code alone; cache lookup/replay/admission hooks
- `loom/repair.py` — the post-remux `repair_stts` §6.2.2 rewrite (F32)
- `loom/batch.py` — batch spec, N-worker runner, journal/resume, ledger
- `loom/cache.py` — content-addressed target cache
- `loom/explain.py` — R10: pure plan-to-text rendering (`loom explain`)
- `tests/` — schema negatives, golden plans, routing table, argv rules,
  vars/cache/batch units, toolchain-gated parity + batch accepts (skip
  cleanly without a toolchain)

## Related projects

- [`iamf-sentinel`](https://github.com/jlivingston-Cipher/iamf-sentinel) — the IAMF conformance
  validator that gates every Loom output
- [`iamf-sentinel-pro`](https://github.com/jlivingston-Cipher/iamf-sentinel-pro) — L3 rendered-QC
  plugin; its `sentinel-dsp` kernel is Loom's loudness measurement engine
- [`iamf-adm-corpus`](https://github.com/jlivingston-Cipher/iamf-adm-corpus) — synthetic ADM
  corpus + harness; ships the toolchain build script
- [`iamf-sentinel-mcp`](https://github.com/jlivingston-Cipher/iamf-sentinel-mcp) — MCP server
  exposing the validator and this packager to agent runtimes

## License & support

Apache-2.0 (see `LICENSE` / `NOTICE`) — the whole `iamf-sentinel` / `iamf-loom`
stack is free software under Apache-2.0. The manifest schema, compiler,
backends, and executor are original works; the encoder toolchain (iamf-tools,
FFmpeg, MP4Box) is invoked only as subprocesses across the ADR-4 boundary, and
outputs are validated by `iamf-sentinel`. Maintained best-effort; **commercial
support and consulting are available** — see the core repo's
[`SUPPORT.md`](https://github.com/jlivingston-Cipher/iamf-sentinel/blob/main/SUPPORT.md).
