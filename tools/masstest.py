#!/usr/bin/env python3
"""Batch leaf-module mass test: emit + pyc-opt for every leaf module, fast.

Parses the .fir once, emits each module, verifies each with pyc-opt.
Usage: python3 tools/masstest.py <fir> [--out DIR] [--filter SUBSTR]
       [--check N]  semantic gate: cycle-accurate equivalence check
                    (evalfir golden vs pycc C++) on N cycles per leaf.
                    Slow (pycc+g++ per module); use --filter for subsets.
"""

import subprocess
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from fir2pyc.fir_parser import (  # noqa: E402
    Inst, MemDecl, MemPort, When, parse_fir_file,
)


def _has_inst_or_mem(stmts) -> bool:
    for st in stmts:
        if isinstance(st, (Inst, MemDecl, MemPort)):
            return True
        if isinstance(st, When):
            if _has_inst_or_mem(st.then):
                return True
            if st.els and _has_inst_or_mem(st.els):
                return True
    return False
from fir2pyc.lower import ModuleLower  # noqa: E402
from fir2pyc.cli import run_with_big_stack  # noqa: E402

PYC = os.environ.get(
    "PYC",
    "/home/lidongzhe/pycircuit-workspace/pyCircuit-dev"
    "/.pycircuit_out/toolchain/install/bin")


def main() -> int:
    fir = sys.argv[1]
    outdir = Path("/tmp/f2p_out")
    flt = None
    check_cycles = 0
    for i, a in enumerate(sys.argv[2:]):
        if a == "--out":
            outdir = Path(sys.argv[i + 3])
        if a == "--filter":
            flt = sys.argv[i + 3]
        if a == "--check":
            check_cycles = int(sys.argv[i + 3])
    outdir.mkdir(parents=True, exist_ok=True)
    circuit = run_with_big_stack(lambda: parse_fir_file(fir))
    names = []
    for n, m in circuit.modules.items():
        if m.ext:
            continue
        if _has_inst_or_mem(m.body):
            continue
        if flt and flt not in n:
            continue
        names.append(n)
    npass = nfail = 0
    fails = {}
    for idx, n in enumerate(names):
        mod = circuit.modules[n]

        def _emit_one(n=n, mod=mod):
            # Deep mux chains recurse hard in lowering; run each module's
            # emit on a big-stack thread instead of crashing on Python's
            # default recursion limit / 8 MiB C stack.
            ml = ModuleLower(circuit, mod, n)
            return ("module attributes {pyc.top = @" + n +
                    ", pyc.frontend.contract = \"pycircuit\"} {\n" +
                    ml.emit().rstrip("\n") + "\n}")

        try:
            text = run_with_big_stack(_emit_one)
        except Exception as exc:  # noqa: BLE001
            nfail += 1
            kind = type(exc).__name__
            fails.setdefault(("emit", kind, str(exc)[:90]), []).append(n)
            continue
        p = outdir / f"{n}.mlir"
        p.write_text(text)
        r = subprocess.run([f"{PYC}/pyc-opt", str(p), "-o", "/dev/null"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            npass += 1
        else:
            nfail += 1
            err = (r.stderr.strip().splitlines() or ["?"])[-1][:110]
            fails.setdefault(("pyc-opt", err[:90]), []).append(n)
        if (idx + 1) % 25 == 0:
            print(f"[{idx + 1}/{len(names)}] pass={npass} fail={nfail}",
                  flush=True)
    print(f"TOTAL {len(names)}: pass={npass} fail={nfail}")
    if check_cycles:
        # semantic gate: cycle-accurate equivalence per leaf (slow)
        import time
        nchk = nok = 0
        for n in names:
            t0 = time.time()
            r = subprocess.run(
                [sys.executable, "-m", "src.fir2pyc.cli", "check", fir,
                 "--module", n, "--cycles", str(check_cycles)],
                capture_output=True, text=True, timeout=900, cwd=str(
                    Path(__file__).resolve().parent.parent))
            nchk += 1
            ok = r.returncode == 0
            nok += ok
            tag = "MATCH" if ok else "FAIL"
            print(f"  [check {nchk}/{len(names)}] {n}: {tag} "
                  f"({time.time() - t0:.0f}s)", flush=True)
        print(f"CHECK TOTAL {nchk}: match={nok} fail={nchk - nok}")
    for key, mods in sorted(fails.items(), key=lambda kv: -len(kv[1])):
        msg = key[2] if len(key) > 2 else key[1]
        print(f"  x{len(mods)} {key[0]} {msg}: e.g. {mods[0]}"
              + (f" (+{len(mods) - 1} more)" if len(mods) > 1 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
