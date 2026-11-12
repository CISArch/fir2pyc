"""fir2pyc command-line entry points."""

from __future__ import annotations

import argparse
import sys
import threading


def _note(kind, detail):
    print(f"[{kind}] {detail}", file=sys.stderr)


def run_with_big_stack(fn, stack_bytes=512 * 1024 * 1024):
    """Run fn() on a worker thread with a large C stack and a raised
    recursion limit, so deep recursion (e.g. lowering deep mux chains via
    _resolve_value/_fold_driver) does not hit RecursionError.

    Returns fn()'s return value; any exception is re-raised on the caller
    thread with its original traceback.
    """
    box = {}

    def _target():
        # Must run inside the worker: setrecursionlimit is per-thread on
        # Python >= 3.12 (global on older versions, which also works).
        sys.setrecursionlimit(300_000)
        try:
            box["ret"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["exc"] = exc

    prev = threading.stack_size(stack_bytes)
    try:
        th = threading.Thread(target=_target, name="fir2pyc-big-stack")
        th.start()
    finally:
        threading.stack_size(prev)
    th.join()
    if "exc" in box:
        raise box["exc"]
    return box.get("ret")


def cmd_parse(args: argparse.Namespace) -> int:
    from .fir_parser import parse_fir_file

    circuit = parse_fir_file(args.fir)
    n_mod = len(circuit.modules)
    n_stmt = sum(len(m.body) + len(m.ports) for m in circuit.modules.values())
    print(f"OK circuit {circuit.name}: {n_mod} modules, {n_stmt} statements")
    if args.verbose:
        for m in circuit.modules.values():
            print(f"  module {m.name}: {len(m.ports)} ports, {len(m.body)} stmts")
    return 0


def cmd_emit(args: argparse.Namespace) -> int:
    from .fir_parser import parse_fir_file
    from .lower import lower_circuit, reachable_modules

    class _TopNotFound(Exception):
        pass

    def _work() -> str:
        # Whole emit flow (parse + lowering) runs on the big-stack thread.
        circuit = parse_fir_file(args.fir)
        if args.module:
            return lower_circuit(circuit, args.module, note=_note,
                                 orphan_width_fallback=args.orphan_fallback,
                                 stub_oversized_mems=args.stub_oversized_mems,
                                 mem_reg_bank_cap=args.mem_reg_bank_cap)
        top = args.top or circuit.name
        if top not in circuit.modules:
            raise _TopNotFound(top)
        return lower_circuit(circuit, top, note=_note,
                             orphan_width_fallback=args.orphan_fallback,
                             stub_oversized_mems=args.stub_oversized_mems,
                             mem_reg_bank_cap=args.mem_reg_bank_cap)

    try:
        text = run_with_big_stack(_work)
    except _TopNotFound as exc:
        print(f"top module {exc.args[0]!r} not found", file=sys.stderr)
        return 1
    with open(args.out, "w") as f:
        f.write(text)
    n_lines = text.count("\n")
    print(f"OK wrote {args.out} ({n_lines} lines)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .eqcheck import run as eqcheck_run

    return eqcheck_run(args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fir2pyc",
                                 description="FIRRTL -> pyc MLIR importer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse a .fir file and report statistics")
    p.add_argument("fir", help="input .fir file")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("emit", help="lower to pyc textual MLIR")
    p.add_argument("fir", help="input .fir file")
    p.add_argument("-o", "--out", required=True, help="output .mlir path")
    p.add_argument("--top", help="top module name (default: circuit name)")
    p.add_argument("--module", help="lower a single module standalone")
    p.add_argument("--orphan-fallback", action="store_true",
                   help="default unresolved bare-UInt widths to 1 (unsound)")
    p.add_argument("--stub-oversized-mems", action="store_true",
                   help="stub memories deeper than the bank limit (reads 0)")
    p.add_argument("--mem-reg-bank-cap", type=int, default=1024,
                   help="depth (entries) below which memories become "
                        "register banks; deeper ones become pyc.byte_mem")
    p.set_defaults(func=cmd_emit)

    p = sub.add_parser("check", help="cycle-accurate equivalence check: "
                       "evalfir golden vs pycc C++ model")
    p.add_argument("fir", help="input .fir file")
    p.add_argument("--module", required=True,
                   help="module to check (leaf modules: no inst/mem)")
    p.add_argument("--cycles", type=int, default=20,
                   help="simulation cycles (default 20; cycle 0 is a reset "
                        "cycle and is not compared for regreset modules)")
    p.add_argument("--seed", type=int, default=1,
                   help="random stimulus seed (default 1)")
    p.add_argument("--logic-depth", type=int, default=1024,
                   help="pycc --logic-depth passthrough (default 1024; "
                        "flattened mux/decode chains can exceed the old "
                        "hardcoded 96)")
    p.add_argument("--inputs",
                   help="comma-separated substrings: only input leaves "
                        "whose MLIR name matches one get random stimulus, "
                        "others are driven 0 (large-IO modules)")
    p.add_argument("--golden-only", action="store_true",
                   help="run only the evalfir golden evaluator and dump the "
                        "cycle-by-cycle value trace (no pycc/C++ comparison)")
    p.add_argument("--max-print", type=int, default=5,
                   help="max mismatching cycle/ports to print (default 5)")
    p.add_argument("--workdir",
                   help="work directory for MLIR/C++ artifacts "
                        "(default: fresh dir under /tmp)")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
