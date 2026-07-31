"""The Sentinel gate (PRD R5) — validation wired into `loom run`.

"A Loom run that emits a nonconformant file is a Loom bug, definitionally"
(PRD goal 4). The gate calls our own validator in-process:

- `sentinel.engine.validate()` (iamf-sentinel, Apache-2.0, ours) always runs
  L1 structural + L2 channel-semantics checks.
- When `sentinel_pro` is importable AND the toolchain root carries oracle
  binaries, the gate passes a `sentinel_pro.oracle.Toolchain` so L3 rendered
  QC runs too (S-31x/S-32x — the duplication/silence signatures that make the
  F4 class detectable content-agnostically). The reference decoders stay
  subprocess-only *inside* Sentinel (ADR-4 unchanged); importing our own
  libraries is not a boundary crossing.

Gate policy (manifest `policy.validate`):
  fail_on_error — any FAIL-severity finding fails the run (rc 2).
  off           — gate skipped, noted in the ledger (the I-know-what-I-am-
                  doing hatch; the skip itself is auditable).

Escalation (doc 43 deviation, dissected): Sentinel's generic profile keeps
the F4-signature checks S-320 (silent channel) / S-321 (duplicate channels)
at WARN — correct for arbitrary third-party content, where silence or
duplication can be a legitimate master. On a file LOOM ITSELF packaged from
labeled sources, those signatures mean the derived channel mapping is wrong —
a Loom bug by definition (PRD goal 4) — so the gate escalates them to
run-failing. A genuinely degenerate master is what `validate: off` is for;
the escalation is marked in the ledger, never silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# WARN-severity Sentinel findings that fail a Loom run anyway (F4 signatures
# on our own output = packaging bug, not content quirk).
ESCALATE_IDS = {"S-320", "S-321"}


@dataclass
class GateResult:
    path: str
    tier: str                    # "l1l2+l3" | "l1l2" | "skipped" | "error"
    findings: list[dict] = field(default_factory=list)
    fail_ids: list[str] = field(default_factory=list)
    execution_error: str | None = None

    @property
    def passed(self) -> bool:
        return self.execution_error is None and not self.fail_ids

    def to_json(self) -> dict:
        return {
            "tier": self.tier,
            "passed": self.passed,
            "fail_ids": self.fail_ids,
            "findings": self.findings,
            **({"execution_error": self.execution_error}
               if self.execution_error else {}),
        }


def _l3_toolchain(toolchain_root: Path):
    """A sentinel_pro Toolchain when pro + oracles are available, else None."""
    try:
        from sentinel_pro.oracle import Toolchain
    except ImportError:
        return None
    tc = Toolchain.discover(str(toolchain_root))
    return tc if tc.available else None


def run_gate(out_path: str | Path, toolchain_root: Path) -> GateResult:
    """Validate one produced output. Never raises."""
    p = str(out_path)
    try:
        from sentinel.engine import validate
        from sentinel.findings import Severity
    except ImportError as e:  # sentinel is a declared dependency
        return GateResult(path=p, tier="error",
                          execution_error=f"iamf-sentinel unavailable: {e}")
    tc = _l3_toolchain(Path(toolchain_root))
    tier = "l1l2+l3" if tc is not None else "l1l2"
    report = validate(p, profile="generic", toolchain=tc)
    if report.execution_error is not None:
        return GateResult(path=p, tier=tier,
                          execution_error=report.execution_error)
    findings = [
        {"id": f.check_id, "severity": f.severity.label, "message": f.message,
         **({"escalated": True} if (f.check_id in ESCALATE_IDS
                                    and f.severity < Severity.FAIL) else {})}
        for f in report.sorted_findings()
    ]
    fail_ids = [f.check_id for f in report.findings
                if f.severity >= Severity.FAIL or f.check_id in ESCALATE_IDS]
    return GateResult(path=p, tier=tier, findings=findings, fail_ids=fail_ids)
