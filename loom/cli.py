"""Loom CLI — compile / explain / run / batch / version (P1 surface).

`loom explain` (R10, doc 45) renders the compiled plan with the per-target/
per-step rationales the compiler has carried since Phase 1 (D-L4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .compiler import compile_manifest
from .diagnostics import CompileError
from .executor import Executor
from .manifest import load_manifest


def _parse_vars(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        k, sep, v = pair.partition("=")
        if not sep or not k:
            raise SystemExit(f"error: --var expects name=value, got {pair!r}")
        out[k] = v
    return out


def _cmd_compile(args: argparse.Namespace) -> int:
    try:
        m = load_manifest(args.manifest, variables=_parse_vars(args.var))
        plan = compile_manifest(m)
    except CompileError as e:
        for d in e.diagnostics:
            print(f"error: {d}", file=sys.stderr)
        return 2
    text = plan.dumps()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
        print(f"plan written: {args.output} "
              f"({len(plan.steps)} steps, {len(plan.targets)} targets)")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    from .explain import render_explain
    try:
        m = load_manifest(args.manifest, variables=_parse_vars(args.var))
        plan = compile_manifest(m)
    except CompileError as e:
        for d in e.diagnostics:
            print(f"error: {d}", file=sys.stderr)
        return 2
    text = render_explain(m, plan)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
        print(f"explain written: {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        variables = _parse_vars(args.var)
        m = load_manifest(args.manifest, variables=variables)
        plan = compile_manifest(m)
    except CompileError as e:
        for d in e.diagnostics:
            print(f"error: {d}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir or m.manifest_dir)
    work_dir = Path(args.workdir or (out_dir / ".loom-work"))
    ex = Executor(plan, m.manifest_dir, out_dir, work_dir,
                  toolchain=args.toolchain,
                  validate_policy=m.policy.validate,
                  ledger_extra={"vars": variables} if variables else None)
    res = ex.run()
    for tp in plan.targets:
        mark = "OK " if res.ok else "?? "
        gate = ex.ledger["gate"].get(tp.out)
        if gate is None:
            gs = ""
        elif gate.get("tier") == "skipped":
            gs = "  gate=off"
        else:
            n_fail = len(gate.get("fail_ids", []))
            n_warn = sum(1 for f in gate.get("findings", [])
                         if f.get("severity") == "WARN")
            gs = (f"  gate[{gate.get('tier')}]="
                  + ("pass" if gate.get("passed") else "FAIL")
                  + (f" ({n_fail} fail, {n_warn} warn)" if n_fail or n_warn
                     else ""))
        print(f"{mark}{tp.out}  [{tp.backend}"
              + (f"+{tp.muxer}" if tp.muxer and tp.muxer != "ffmpeg" else "")
              + f", profile={tp.profile}]{gs}")
    print(f"ledger: {res.ledger_path}")
    if not res.ok:
        for f in res.failures:
            print(f"error: {f}", file=sys.stderr)
        return 2
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    from .batch import BatchError, load_batch, run_batch
    try:
        spec = load_batch(args.batch)
    except CompileError as e:
        for d in e.diagnostics:
            print(f"error: {d}", file=sys.stderr)
        return 2
    try:
        rc, ledger_path = run_batch(
            spec, workers=args.workers, state_dir=args.state_dir,
            fresh=args.fresh, no_cache=args.no_cache,
            toolchain=args.toolchain, log=print)
    except (BatchError, CompileError) as e:
        if isinstance(e, CompileError):
            for d in e.diagnostics:
                print(f"error: {d}", file=sys.stderr)
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"batch ledger: {ledger_path}")
    return rc


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"loom {__version__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="loom",
        description="Loom — manifest-driven IAMF batch packager (Phase 3)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile", help="compile a manifest to an execution plan")
    c.add_argument("manifest")
    c.add_argument("-o", "--output", help="write the plan JSON here")
    c.add_argument("--var", action="append", metavar="NAME=VALUE",
                   help="bind a {variable} in the manifest (repeatable)")
    c.set_defaults(func=_cmd_compile)

    e = sub.add_parser("explain", help="render the compiled plan and why "
                                       "each backend/policy was chosen (R10)")
    e.add_argument("manifest")
    e.add_argument("-o", "--output", help="write the explain text here")
    e.add_argument("--var", action="append", metavar="NAME=VALUE",
                   help="bind a {variable} in the manifest (repeatable)")
    e.set_defaults(func=_cmd_explain)

    r = sub.add_parser("run", help="compile and execute a manifest")
    r.add_argument("manifest")
    r.add_argument("--toolchain", help="toolchain root (else $LOOM_TOOLCHAIN / "
                                       "$SENTINEL_TOOLCHAIN)")
    r.add_argument("--out-dir", help="root for target `out:` paths "
                                     "(default: the manifest's directory)")
    r.add_argument("--workdir", help="scratch dir (default: <out>/.loom-work)")
    r.add_argument("--var", action="append", metavar="NAME=VALUE",
                   help="bind a {variable} in the manifest (repeatable)")
    r.set_defaults(func=_cmd_run)

    b = sub.add_parser("batch", help="run a batch of jobs over one or more "
                                     "template manifests (R6; per-job "
                                     "`manifest:` for policy variance)")
    b.add_argument("batch", help="batch file (loom_batch: 0)")
    b.add_argument("--workers", type=int,
                   help="parallel jobs (default: min(4, cpus))")
    b.add_argument("--state-dir", help="journal/cache/work root (default: "
                                       "<batch-dir>/.loom-batch/<spec-hash>)")
    b.add_argument("--fresh", action="store_true",
                   help="ignore the journal and cache reads (writes still "
                        "admitted)")
    b.add_argument("--no-cache", action="store_true",
                   help="disable the content-addressed cache entirely")
    b.add_argument("--toolchain", help="toolchain root (else $LOOM_TOOLCHAIN / "
                                       "$SENTINEL_TOOLCHAIN)")
    b.set_defaults(func=_cmd_batch)

    v = sub.add_parser("version", help="print version")
    v.set_defaults(func=_cmd_version)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
