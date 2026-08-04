"""iamf-loom — manifest-driven IAMF batch packager (Apache-2.0).

Phase 1 (doc 42): manifest compiler (PRD R1) + dual encode backends behind one
interface (PRD R2). Phase 2 (doc 43): loudness normalize (R3), presets +
FLAC mezzanine (R4), the Sentinel gate in-process by default (R5), the
compile-time video probe. Phase 3 (doc 44): batch industrialization (R6) —
N-worker runner, content-addressed target cache, hash-verified resume,
the machine-readable batch ledger; `{variable}` manifest templating.
P1 (doc 45): `loom explain` (R10) -- the compiled plan rendered with its
routing/policy rationales.
Design bound by ADR-1/2/4/5 (doc 14); the F1-F13 sharp edges (doc 11) are
encoded in the command builders so the failure classes are unrepresentable
in the manifest grammar (ADR-5).

Original work. All third-party tools (encoder_main, decoder_main, FFmpeg,
MP4Box) are invoked strictly as subprocesses across CLI boundaries (ADR-4);
no code is linked against or derived from GPL/LGPL/reference sources.
"""

__version__ = "0.8.2"
