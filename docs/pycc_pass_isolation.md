# pycc pass-pipeline failure isolation — /tmp/f2p_top.mlir (BOOM full top, 569 modules)

Date: 2026-11-12 · Toolchain: `pyCircuit-dev/.pycircuit_out/toolchain/install/bin` (pycc, pyc-opt) · Diagnostic only, no source edited.

## TL;DR

| Question | Answer |
|---|---|
| Failing pass (first failure of `pycc --emit=none`) | **`pyc-slp-pack-wires`** (`SLPPackWiresPass`, pass #15 of the pycc pipeline) |
| Failing module | **`@BoomIOMSHR`** (intrinsic: its 1-func closure cut alone fails) |
| Failure mode | greedy rewrite driver **does not converge within `GreedyRewriteConfig::maxIterations` (default 10)** → `applyPatternsAndFoldGreedily` returns failure **silently** → pass calls `signalPassFailure()` **without any diagnostic** → pycc prints only `error: pass pipeline failed` |
| IR pattern | not a single bad op: a **fold mountain** — `@BoomIOMSHR` post-`pyc-eliminate-wires` is 1119 ops of dense constant concat/compare trees (MSHR hit/way logic, masked constants like `pyc.constant 72057594037927935 : i56`); eliminate-wires inlined 194 wire ops (1313→1119) exposing hundreds of foldable constant ops, and the driver's fold+SLP work doesn't reach fixpoint in 10 rounds (at bail-out it had folded ~87 ops: 1119→1032) |
| Secondary blocker (tooling) | **`pyc.constant` printer asserts (SIGABRT) on any constant with >64 active bits** (`PYCOps.cpp:49` `ConstantOp::print` → `APInt::getZExtValue()`); canonicalize legitimately folds such constants (e.g. `concat(const 1:i1, const 0:i64) -> i65` = 2^64; real cases up to `i111` in `MulAddRecFNToRaw_preMul`). This kills `--dump-pass-ir` beyond the first post-canonicalize print and is why pass isolation was hard |
| Next gate after fixing slp | `pyc-check-logic-depth`: `@BoomIOMSHR` needs depth 43 > default 32 (emits a proper diagnostic; ~88/571 funcs fail it on isolated cuts) |

## Pipeline (`--emit=none`, from `compiler/mlir/tools/pycc.cpp`)

```
 1 check-frontend-contract        9  func(eliminate-dead-instances)  17 func(pack-i1-regs)
 2 inline-functions              10  symbol-dce                      18 func(fuse-comb)
 3 check-hierarchy-discipline    11  func(lower-scf-static)          19 canonicalize
 4 symbol-dce                    12  func(eliminate-wires)           20 cse … 26 symbol-dce
 5 canonicalize                  13  func(eliminate-dead-state)      27 check-comb-cycles
 6 cse                           14  func(slp-pack-wires)  ← FAILS   28 change-driven-schedule
 7 sccp                          15  func(comb-canonicalize)         29 func(check-flat-types)
 8 remove-dead-values            16  func(shl-chain-fold)            30 func(check-no-dynamic)
                                                                     31 check-logic-depth  ← next gate
                                                                     32 func(collect-compile-stats)
```

No inliner/state-opt/seq-bank at `--emit=none`. The pass that fails is identified by the per-pass IR dumper's `_FAILED` marker (`…_after_0015_slp-pack-wires…_FAILED.mlir`).

## Evidence chain

1. **Full file, plain**: `pycc /tmp/f2p_top.mlir --emit=none -o <real-path>` → exit 1, stderr exactly `error: pass pipeline failed` (one line, zero diagnostics).
2. **Full file, `--dump-pass-ir=auto`** (requires `--out-dir`!): aborts (SIGABRT, rc 134) at the *first full-IR print after canonicalize* — stack symbolizes to `pyc::ConstantOp::print` → `llvm::APInt::getZExtValue` assertion `getActiveBits() <= 64`. Note: the dumper buffers IR in memory, so **no partial dump files survive an abort**; `--dump-pass-ir-max-lines` does not help (truncation happens after printing).
3. **Single-func closure cuts** (header + func + transitive instance callees + `}`): exhaustive scan of all 571 funcs →
   - 475 OK, 88 fail at `pyc-check-logic-depth` **with diagnostics**, **8 fail silently — exactly the `@BoomIOMSHR` instantiation chain** (`BoomIOMSHR` ← `BoomMSHRFile` ← `BoomNonBlockingDCache` ← `BoomTile` ← `HierarchicalElementResetDomain` ← `TilePRCIDomain` ← `ExampleRocketSystem` ← `BoomTrimHarness`).
   - `@BoomIOMSHR` cut alone (closure = 1 func) reproduces the silent failure → the trigger is intrinsic to that func, not cross-module.
4. **BoomIOMSHR cut + dumps**: pycc's per-pass dump shows `…_after_0015_slp-pack-wires…_FAILED.mlir`; everything before passes. Op counts across the boundary: eliminate-wires 1313→1119 (inlines 194 wires), eliminate-dead-state 1119→1119 (no-op), slp at failure 1119→1032 (87 ops folded, then bail).
5. **Non-convergence proof (wrapped experiment)**: re-wrap the slp before-dump into a fresh module and run pycc again — the *second* pipeline pass pre-folds the func (slp input shrinks **1119 → 980 ops**) and slp then **converges (4/4 runs)**; the pipeline proceeds to `pyc-check-logic-depth` and fails there with `depth=43 limit=32`. ⇒ original failure = driver round limit, deterministic per input (5/5, with/without `--mlir-disable-threading`, with/without ASLR).
6. **Printer assert minimal repro** (`/tmp/f2p_out/isol/bad_const.mlir`, 9 lines):

```mlir
module attributes {pyc.top = @T, pyc.frontend.contract = "pycircuit"} {
  func.func @T(%arg0: !pyc.clock, %arg1: !pyc.reset) -> i65 attributes {…full contract attrs…} {
    %c64 = pyc.constant 0 : i64
    %c1  = pyc.constant 1 : i1
    %v   = pyc.concat(%c1, %c64) : (i1, i64) -> i65     // fold → constant 2^64 : i65
    return %v : i65
  }
}
```
   - `pycc bad_const.mlir --emit=none --dump-pass-ir=auto …` → rc 134, `Assertion 'getActiveBits() <= 64 && "Too many bits for uint64_t"' failed` (printer dies on the fold-created `pyc.constant 18446744073709551616 : i65`).
   - `pycc bad_const.mlir --emit=none` (no dump) → **exit 0**: the wide constant is *legal IR*; only printing crashes. Matches the scan: funcs that create wide constants but pass the pipeline (e.g. `RenameBusyTable`), vs 15 funcs that both create one and are candidates for the emit-time `getZExtValue` calls in `CppEmitter.cpp` (11) / `VerilogEmitter.cpp` (8).

## Recommended fixes (in priority order)

1. **`SLPPackWiresPass`** (`compiler/mlir/lib/Transforms/SLPPackWiresPass.cpp:127-134`):
   - raise the driver budget: `GreedyRewriteConfig cfg; cfg.maxIterations = 100;` (field exists in MLIR 19 `GreedyPatternRewriteDriver.h`; default 10), and/or
   - **emit a diagnostic before `signalPassFailure()`** (e.g. `f.emitError("pyc-slp-pack-wires: greedy rewrite did not converge in N iterations")`) — silent pass failure is what turned a fixable convergence issue into an undebuggable bare `pass pipeline failed`. Audit the same silent pattern in other greedy-based passes.
2. **`ConstantOp::print`** (`compiler/mlir/lib/Dialect/PYC/PYCOps.cpp:49`): stop calling `getZExtValue()`; print the APInt width-safely (`APInt::toString(SmallStringImpl&, unsigned, bool, IntegerType::SignednessSemantics)` or `toStringUnsigned/toStringSigned`) so folded >64-active-bit constants can be dumped/diagnosed. This alone would have made `--dump-pass-ir` usable on the full file.
3. **Emitter audit**: `CppEmitter.cpp` / `VerilogEmitter.cpp` call `getZExtValue` on constants (11 + 8 sites) — the same i65/i80/i111 folded constants (RocketALU, FPToFP, MulAddRecFNToRaw_preMul, FPU family, RenameFreeList, …) will crash emission once the pipeline passes. Handle >64-bit values as multi-word.
4. **`pyc-check-logic-depth`**: after (1), the next failure is loud and by design (BoomIOMSHR depth 43 > 32; many FP/predictor funcs deeper) — raise `--logic-depth` for this design or accept the gate.
5. **Latent silent path**: `ChangeDrivenSchedulePass` signals failure with no diagnostic when `FunctionCombDepGraph::build` fails (only some paths emit); worth adding diagnostics while touching this area.

## Reproduction recipes

```bash
PY=/home/lidongzhe/pycircuit-workspace/pyCircuit-dev/.pycircuit_out/toolchain/install/bin

# 1) silent failing pass (BoomIOMSHR cut; build by cutting header + '  func.func @BoomIOMSHR' block + '}')
$PY/pycc /tmp/f2p_out/isol/cut_BoomIOMSHR.mlir --emit=none -o /tmp/f2p_out/isol/out
#   → exit 1, stderr = "error: pass pipeline failed" only
$PY/pycc /tmp/f2p_out/isol/cut_BoomIOMSHR.mlir --emit=none --out-dir=/tmp/f2p_out/isol/d \
    --dump-pass-ir=auto --dump-pass-ir-filter='slp-pack-wires' --dump-pass-ir-phase=both -o /tmp/f2p_out/isol/out2
#   → pass_ir/*_after_0015_slp-pack-wires…_FAILED.mlir

# 2) printer assert (9-line file above)
$PY/pycc /tmp/f2p_out/isol/bad_const.mlir --emit=none --out-dir=/tmp/f2p_out/isol/badout \
    --dump-pass-ir=auto --dump-pass-ir-filter='cse' --dump-pass-ir-phase=before -o /tmp/f2p_out/isol/out3
#   → rc 134, APInt assertion in ConstantOp::print

# pyc-opt can reproduce slp alone on the exact pass input (post eliminate-wires state):
$PY/pyc-opt <slp-before-dump> --pass-pipeline="builtin.module(func.func(pyc-slp-pack-wires))" -o /tmp/x
#   → exit 1, silent (same non-convergence)
```

## Isolation methodology notes (for next time)

- `--dump-pass-ir=auto` needs `--out-dir`; dumps are buffered and lost on abort; `pycc -o /dev/null` breaks on `.stats.json` (use a real path).
- pyc-opt registers only `pyc-*` + `cse` — **no** `canonicalize`/`symbol-dce`/`sccp` — so pycc pipeline prefixes can't be replicated exactly with pyc-opt; per-func closure cuts + the dumper's `_FAILED` marker are the working isolation recipe.
- Callers come first in fir2pyc output (not topological for prefix cuts): prefix slicing breaks instance refs; cut closures instead (transitive `callee = @X`).
- `/tmp` scratch artifacts kept: `/tmp/f2p_out/isol/` (cuts, dumps, `bad_const.mlir`, scan results `scan_all2.log`).
