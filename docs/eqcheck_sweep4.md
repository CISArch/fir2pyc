# fir2pyc 等价对拍 sweep #4（96 剩余叶子收尾 → 覆盖 285/285 闭环）

日期：2026-11-12（13:49–14:20）
对象：`/tmp/boom_trim.fir`
命令：`python3 -m src.fir2pyc.cli check /tmp/boom_trim.fir --module <M> --cycles 20 --seed 1 --max-print 12 --workdir /tmp/eq_sweep4/wd_<M>`（单模块外层 timeout 400s，串行）
树版本：lower.py @ 13:36（md5 `3fa1d7293b`）——**全程 51 笔 + 8 笔控制组 md5 一致，sweep 期间树未变化**（13:36 的改动在 sweep 开始前已落地），故无需按进程时刻对账、无补跑。

## TL;DR

- **96 个未覆盖叶子全部归类，叶子覆盖 285/285 = 100%（sweep1 42 + sweep2 新增 87 + sweep3 新增 60 + sweep4 新增 96）。**
- 本轮 **51 笔直接运行：51 MATCH / 0 MISMATCH / 0 timeout / 0 pycc-error**；另加 **8 笔跨家族控制组（当前树）全 MATCH，零回归**。
- 直接运行 = 43 笔全量家族实例（OptimizationBarrier×27、TLWidthWidget8×6、LFSR×4、RoundAnyRawFNToRecFN×3、TLBuffer×2、RVCExpander_4）+ **IssueSlot×53 按"前 3 + 每 10 个取 1"采样 8 笔**（`IssueSlot_2/_3/_4/_12/_22/_32/_42/_52`），全部 MATCH，未触发全家族补跑条件。
- **有效 MATCH 累计 283/285（99.3%）**：直测 MATCH 232 + 家族旁证 MATCH 51（sweep3 遗留 6 + 本轮 IssueSlot 未采样 45）。
- 例外 2 个（均为既往已知、非本轮新增）：**MISMATCH 1（MemExeUnit_1 `io_agenbits.data`，sweep3 F1）**、**pycc-error 1（BoomIOMSHR state-optimize 管线内失败，sweep2 起持续）**。
- **新发现失配家族：无。** 96 个叶子确实全部是已证家族的重复实例（IssueSlot×53、OptimizationBarrier×27、TLWidthWidget8×6、LFSR×4、RoundAnyRawFNToRecFN×3、TLBuffer×2、RVCExpander×1）。

## 1. 清单与选样方法

- 清单脚本 `/tmp/eq_sweep4/inventory.py`：重新解析 `/tmp/boom_trim.fir`，**body 递归遍历含 `When.then`/`When.els`**（吸取 sweep3 教训），leaf = 非 extmodule + 全 body 无 `Inst` + 无 `MemDecl/MemPort` ⇒ **285 个叶子（断言通过，与 /tmp/leafs.txt 一致）**。
- covered = 三报告并集：sweep1 42（eqcheck_coverage.md）+ sweep2 99（含与 s1 重叠 12）+ sweep3 60 新叶子（eqcheck_sweep3.md §7 第 12–71 笔）= **189 distinct**。
- uncovered = 285 − 189 = **96**，家族分布：IssueSlot×53、OptimizationBarrier_EntryData×17、OptimizationBarrier_TLBEntryData×10、TLWidthWidget8×6、MaxPeriodFibonacciLFSR×4、RoundAnyRawFNToRecFN×3、TLBuffer×2、RVCExpander×1。
- 清单/选样产物：`/tmp/eq_sweep4/targets.json`、runner `/tmp/eq_sweep4/runner.py`、结果 `/tmp/eq_sweep4/results.tsv`。

## 2. IssueSlot×53 采样协议（family-sampled）

- 已证基础：sweep3 直测 `IssueSlot`/`IssueSlot_1`/`IssueSlot_10` 全 MATCH（各 225 输出叶，r15+r16 双树）。
- 本轮协议：剩余 53 个实例按数字序取**前 3（_2/_3/_4）+ 每 10 个取 1（_12/_22/_32/_42/_52）共 8 笔**；触发条件 = 任一采样实例非 MATCH ⇒ 全家族补跑（`issueslot_full.flag` 机制）。
- 结果：8/8 MATCH（各 225 输出叶、27–29s），**未触发补跑**；其余 45 个实例记 **family-sampled MATCH**（同家族直测累计 3+8=11/56 实例，含基实例）。

## 3. 家族结果汇总（51 笔直测）

| 家族 | 本轮实例 | 直测结果 | 用时 | 备注 |
|---|---:|---|---:|---|
| OptimizationBarrier_EntryData | 17 | MATCH×17 | 491s | 各 15 输出叶 |
| OptimizationBarrier_TLBEntryData | 10 | MATCH×10 | 205s | 各 23 输出叶 |
| IssueSlot（采样 8/53） | 8 | MATCH×8 | 226s | 各 225 输出叶；余 45 个 family-sampled |
| TLWidthWidget8 | 6 | MATCH×6 | 124s | 20/42/0 叶混布（_3/_4 为 0 叶 vacuous） |
| MaxPeriodFibonacciLFSR | 4 | MATCH×4 | 84s | 6/7/16 叶 |
| RoundAnyRawFNToRecFN | 4→3 | MATCH×3 | 78s | 纯组合（cycle 0..0） |
| TLBuffer | 2 | MATCH×2 | 41s | 0 叶（vacuous，流程通过） |
| RVCExpander | 1 | MATCH×1 | 21s | 7 叶 |
| **合计** | **51** | **MATCH×51** | **1270s** | 零超时零报错 |

vacuous（0 输出叶）3 笔：TLBuffer_3/_4、TLWidthWidget8_3（_4 亦 0 叶）——golden↔cpp 流程一致通过但无输出可比，与 sweep3 同口径。

## 4. 最终树控制组（8 笔，零回归）

13:36 树相对 sweep3 终验树（12:11 r16）为新版本，为把**既往覆盖结果桥接到当前树**，抽 8 个跨家族代表复验（`/tmp/eq_sweep4/ctrl_*/`）：

| 模块 | 结果 | 耗时 | 输出叶 |
|---|---|---:|---:|
| DecodeUnit | MATCH | 35s | 112 |
| CSRFile | MATCH | 41s | 238 |
| FetchBuffer | MATCH | 63s | 226 |
| BoomRAS | MATCH | 25s | 1 |
| MulDiv | MATCH | 26s | 5 |
| RenameMapTable | MATCH | 45s | 8 |
| BreakpointUnit | MATCH | 21s | 6 |
| HellaCacheArbiter | MATCH | 21s | 63 |

加上本轮 51 笔本身就在 13:36 树上运行（覆盖全部 8 个新家族），当前树有效覆盖无缺口。

## 5. 最终覆盖口径（四 sweep 累计，definitive）

| 口径 | 数 |
|---|---|
| 叶子总数（非 ext、无 Inst、无 Mem，含 when 体遍历） | **285** |
| sweep1 直测覆盖 | +42 |
| sweep2 新增 distinct | +87 |
| sweep3 新增 distinct | +60 |
| **sweep4 新增（96 全部归类）** | **+96** |
| **叶子分类覆盖率** | **285/285 = 100%** |
| 直测 MATCH（含各树直测与终树复验） | 232 |
| 家族旁证 MATCH（sweep3 6 + sweep4 IssueSlot 采样外 45） | 51 |
| **有效 MATCH** | **283/285 = 99.3%** |
| MISMATCH | 1（MemExeUnit_1，io_agenbits.data；`/tmp/eq_sweep3/final_MemExeUnit_1/`） |
| pycc-error | 1（BoomIOMSHR state-optimize；`/tmp/eq_sweep2/BoomIOMSHR_s1_rerun/`） |

**结论（覆盖收尾）**：boom_trim.fir 的 285 个叶子模块在 fir2pyc 语义等价对拍口径下已全部归类，工具缺口 0；剩余两枚已知例外均非覆盖缺口——MemExeUnit_1 是唯一疑似错译（agen 数据通路，修复优先级见 sweep3 §6），BoomIOMSHR 是 pycc 管线上下文问题（standalone 通过）。叶子级对拍战役到此闭环，后续力量应转向这两点的修复与 inst-in-when 非 leaf 模块的分层对拍（FPToFP/IntToFP 型，golden 侧报 `undeclared <instname>` 可读性差，建议 lowering 前给明确 "not a leaf" 报错）。

## 6. 全量结果表（51 笔直测 + 8 笔控制，树均为 lower.py@13:36 md5 3fa1d7293b）

| # | 模块 | 秒 | 结果摘要 |
|---:|---|---:|---|
| 1 | MaxPeriodFibonacciLFSR_5 | 21 | MATCH 19cyc/6叶 |
| 2 | MaxPeriodFibonacciLFSR_6 | 21 | MATCH 19cyc/16叶 |
| 3 | MaxPeriodFibonacciLFSR_7 | 21 | MATCH 19cyc/7叶 |
| 4 | MaxPeriodFibonacciLFSR_8 | 21 | MATCH 19cyc/7叶 |
| 5 | OptimizationBarrier_EntryData_12 | 29 | MATCH 20cyc/15叶 |
| 6 | OptimizationBarrier_EntryData_13 | 29 | MATCH 20cyc/15叶 |
| 7 | OptimizationBarrier_EntryData_14 | 28 | MATCH 20cyc/15叶 |
| 8 | OptimizationBarrier_EntryData_15 | 29 | MATCH 20cyc/15叶 |
| 9 | OptimizationBarrier_EntryData_16 | 29 | MATCH 20cyc/15叶 |
| 10 | OptimizationBarrier_EntryData_17 | 29 | MATCH 20cyc/15叶 |
| 11 | OptimizationBarrier_EntryData_18 | 29 | MATCH 20cyc/15叶 |
| 12 | OptimizationBarrier_EntryData_19 | 29 | MATCH 20cyc/15叶 |
| 13 | OptimizationBarrier_EntryData_2 | 29 | MATCH 20cyc/15叶 |
| 14 | OptimizationBarrier_EntryData_20 | 28 | MATCH 20cyc/15叶 |
| 15 | OptimizationBarrier_EntryData_3 | 29 | MATCH 20cyc/15叶 |
| 16 | OptimizationBarrier_EntryData_4 | 29 | MATCH 20cyc/15叶 |
| 17 | OptimizationBarrier_EntryData_5 | 29 | MATCH 20cyc/15叶 |
| 18 | OptimizationBarrier_EntryData_6 | 29 | MATCH 20cyc/15叶 |
| 19 | OptimizationBarrier_EntryData_7 | 29 | MATCH 20cyc/15叶 |
| 20 | OptimizationBarrier_EntryData_8 | 29 | MATCH 20cyc/15叶 |
| 21 | OptimizationBarrier_EntryData_9 | 29 | MATCH 20cyc/15叶 |
| 22 | OptimizationBarrier_TLBEntryData_12 | 21 | MATCH 20cyc/23叶 |
| 23 | OptimizationBarrier_TLBEntryData_13 | 21 | MATCH 20cyc/23叶 |
| 24 | OptimizationBarrier_TLBEntryData_2 | 21 | MATCH 20cyc/23叶 |
| 25 | OptimizationBarrier_TLBEntryData_3 | 21 | MATCH 20cyc/23叶 |
| 26 | OptimizationBarrier_TLBEntryData_4 | 21 | MATCH 20cyc/23叶 |
| 27 | OptimizationBarrier_TLBEntryData_5 | 20 | MATCH 20cyc/23叶 |
| 28 | OptimizationBarrier_TLBEntryData_6 | 20 | MATCH 20cyc/23叶 |
| 29 | OptimizationBarrier_TLBEntryData_7 | 20 | MATCH 20cyc/23叶 |
| 30 | OptimizationBarrier_TLBEntryData_8 | 20 | MATCH 20cyc/23叶 |
| 31 | OptimizationBarrier_TLBEntryData_9 | 20 | MATCH 20cyc/23叶 |
| 32 | RVCExpander_4 | 21 | MATCH 20cyc/7叶 |
| 33 | RoundAnyRawFNToRecFN_4 | 21 | MATCH 组合/2叶 |
| 34 | RoundAnyRawFNToRecFN_5 | 28 | MATCH 组合/2叶 |
| 35 | RoundAnyRawFNToRecFN_6 | 29 | MATCH 组合/2叶 |
| 36 | TLBuffer_3 | 21 | MATCH 20cyc/0叶（vacuous） |
| 37 | TLBuffer_4 | 20 | MATCH 20cyc/0叶（vacuous） |
| 38 | TLWidthWidget8_3 | 21 | MATCH 20cyc/0叶（vacuous） |
| 39 | TLWidthWidget8_4 | 21 | MATCH 20cyc/0叶（vacuous） |
| 40 | TLWidthWidget8_5 | 21 | MATCH 20cyc/20叶 |
| 41 | TLWidthWidget8_6 | 20 | MATCH 20cyc/20叶 |
| 42 | TLWidthWidget8_7 | 21 | MATCH 20cyc/42叶 |
| 43 | TLWidthWidget8_8 | 20 | MATCH 20cyc/20叶 |
| 44 | IssueSlot_2（采样） | 27 | MATCH 19cyc/225叶 |
| 45 | IssueSlot_3（采样） | 27 | MATCH 19cyc/225叶 |
| 46 | IssueSlot_4（采样） | 27 | MATCH 19cyc/225叶 |
| 47 | IssueSlot_12（采样） | 29 | MATCH 19cyc/225叶 |
| 48 | IssueSlot_22（采样） | 29 | MATCH 19cyc/225叶 |
| 49 | IssueSlot_32（采样） | 29 | MATCH 19cyc/225叶 |
| 50 | IssueSlot_42（采样） | 29 | MATCH 19cyc/225叶 |
| 51 | IssueSlot_52（采样） | 29 | MATCH 19cyc/225叶 |
| 52–59 | 控制组×8（§4 表） | 277s | 全 MATCH |

主批墙钟 13:52:38–14:14:14 ≈ **21.6 min**（run-sum 1270s）；控制组 ≈ 5.5 min；总墙钟 ≈ **27 min**（较预估 ~35min 略快，采样省下 ~20min）。

产物索引：`/tmp/eq_sweep4/wd_<模块>/`（model.mlir/gen.cpp/driver.cpp/golden_trace.txt/stim.txt/check.log）、控制组 `/tmp/eq_sweep4/ctrl_<模块>/`、汇总 `/tmp/eq_sweep4/results.tsv`、清单 `/tmp/eq_sweep4/targets.json`、脚本 `inventory.py`/`runner.py`/`summarize.py`。
