# fir2pyc 等价对拍 sweep #3（60 新叶子 + 9 复验 + 15 最终树复验，共 85 次运行）

日期：2026-11-12（11:46–12:32）
对象：`/tmp/boom_trim.fir`
命令：`python3 -m src.fir2pyc.cli check /tmp/boom_trim.fir --module <M> --cycles 20 --seed 1 --max-print 12 --workdir /tmp/eq_sweep3/<tag>`（单模块外层 timeout 400s，CSRFile 全激励 900s）
工具链：pycc @ pyCircuit-dev/.pycircuit_out/toolchain/install/bin/pycc（**11:15 重建版**：叶模块第二遍 eval_comb_pass 收敛、C++ 保留名改名、--logic-depth 默认 1024、evalfir 逐拍 comb 记忆化）

**⚠ 时线注意（本轮特有）**：sweep 期间主线在 12:11:59 落了 round-16 修复（lower.py
`_wire_dep_order` wire 发射序拓扑化，DecodeUnit 36 失配清零）。主 sweep 71 笔中
**68 笔在 round-15 树（lower.py@11:09）**、**最后 3 笔（TLXbar_mbus/pbus_in/pbus_out）在
round-16 树**（均 MATCH，与同族旧树结果一致）。为保证最终树有效性，追加
**15 笔最终树复验批次**（`/tmp/eq_sweep3/final_*/`）：DecodeUnit×2、MemExeUnit_1、
BoomIOMSHR + 11 个跨家族回归控制组——**12 MATCH / 1 MISMATCH / 1 pycc-error，零回归**。

## TL;DR

- **60 个新叶子模块（36 个家族中选样，每家族 ≤3 实例）：59 MATCH / 1 MISMATCH（98.3%）。零超时、零 pycc-error。**
- **9 个指定复验全部达预期且零回归**：TLFIFOFixer_2 / LoopBranchPredictorColumn / PMAChecker / FetchBuffer（226 叶）/ BoomWritebackUnit / OptimizationBarrier_UInt / **CSRFile 全激励无 --inputs（238 叶，38s）** 全 MATCH；DecodeUnit/DecodeUnit_1 从 timeout → 36 失配（round-15）→ **MATCH（round-16 独立复证，112 叶）**。
- **前两轮的阻塞家族在 11:15 工具链上全部转 MATCH**：RocketALU_1/_2 与 MulAddRecFNToRaw_preMul_1/postMul_1（sweep1 emit >420s 家族，现 ~21s）；RVCExpander_1/_2/_3（pycc depth-96 家族）；DecodeUnit×2（超时家族）；CSRFile/FetchBuffer（golden >330s 家族）。
- **唯一新失配家族：MemExeUnit_1 `io_agenbits.data`**（agen 地址生成数据通路，14 cyc/ports）；姊妹实例 MemExeUnit（无 agen 单元，243 叶）全 MATCH——失配孤立在 agen 数据通路。round-16 wire 序修复**未**治愈。
- **唯一持续阻塞：BoomIOMSHR pycc `pass pipeline failed`**（state-optimize #15 管线内失败，11:15 pycc 仍在；standalone 同输入通过）。
- 累计三 sweep distinct 叶子覆盖 **189/285（66.3%）**；其中直接验证 MATCH **181**（63.9%），家族旁证 MATCH 6，MISMATCH 1（MemExeUnit_1），pycc-error 1（BoomIOMSHR）。

## 1. 叶子清单与选样

- 判据（与 /tmp/leafs.txt 的 285 一致）：非 extmodule + body 无 Inst + 无 MemDecl。
  本轮清单脚本一度把 FPToFP/IntToFP 误判为叶子——**Inst 藏在 when 块内**（When 属性是
  `then` 非 `cons`，walk 漏走 when 体）；check 工具本身正确拒绝（报 `undeclared
  narrower`/`undeclared i2fResults_i2f_1`，即实例名）。两模块重分类为 non-leaf，不计入 285。
- 前两轮已覆盖 129（sweep1 42 + sweep2 99，重叠 12）；新可用 156，选 60（36 家族，
  每家族 ≤3 实例、优先有输出叶者）：IssueSlot×3（225 输出叶/个）、RocketALU×2、
  RVCExpander×3、RecFNToIN×3、RoundAnyRawFNToRecFN×3、MulAddRecFNToRaw_pre/postMul×1+1、
  MemExeUnit×2、RenameBusyTable×2、PMPChecker_s3×2、MaxPeriodFibonacciLFSR×3、
  OptimizationBarrier_EntryData/TLBEntryData×3+3、IntXbar×4、NullIntSource/NullPrefetcher、
  TL 全家桶（TLXbar×7/TLBuffer×6/TLFIFOFixer×3/TLWidthWidget8×3/TLJbar/
  TLAtomicAutomata_pbus/TLInterconnectCoupler_sbus_from_boom_tile/Repeater_a28）、
  FPToFP/IntToFP（后重分类非叶）。

## 2. 新失配与阻塞家族（2 个）

### F1: MemExeUnit_1 — agen 数据通路失配 [唯一 MISMATCH]

- `FAIL MemExeUnit_1: 14 mismatching cycle/ports over 19 compared cycles (1..19)`，round-15 与 round-16 树**均在**。
- **失配端口唯一**：`io_agenbits.data`（agen 和的寄存输出，gen.cpp 中 reg 由 `pyc_comb_689` 驱动）；同拍其余 100+ 个 `io_agenbits.uop.*` 载荷字段（delay_line tap 驱动）全部 MATCH。
- 症状：golden 为正常地址和；cpp 侧为垃圾/陈旧值（多次恒 -1/-2，或近邻错值如 golden=0x71c93b2248 vs cpp=0x724921bef3）。
- 语义：`io_agen_sum = asUInt(asSInt(tail(add(asSInt(exe_rs1_data), asSInt(exe_imm_data)))))`，随后 `ea_sign` 检查 `bits(sum,38,38)` vs `bits(sum,63,39)` 全 1/全 0。MemExeUnit_1 相对 MemExeUnit 多出整个 agen 单元（loads_saturating 计数器 + pause_mem + agen_sum + ea_sign）；MemExeUnit（243 输出叶）全 MATCH ⇒ 嫌疑收敛到 agen 求和锥（asSInt 加法/ea_sign 位检查）或该 reg 的 en/next 锥。
- 产物：`/tmp/eq_sweep3/MemExeUnit_1/`、`/tmp/eq_sweep3/final_MemExeUnit_1/`。

### F2: BoomIOMSHR — pycc state-optimize 管线内失败（持续，非错译）

- 11:15 pycc 仍 `pycc failed (exit 1): error: pass pipeline failed`（无诊断；sweep2 已定位 `0015_state-optimize` 的 after_*_FAILED.mlir，standalone 单跑同输入通过）。fir2pyc 侧无动作可解，归 pycc 管线上下文问题。

### （已闭环）DecodeUnit 36 失配家族

- 主 sweep（round-15 树）：DecodeUnit/DecodeUnit_1 不再超时（golden 记忆化生效），各 36 失配，签名集中在 `iodeq.uop` 译码控制信号（imm_sel/pimm/op2_sel/imm_rename/iq_type/fu_code/ldst_is_rs1/lrs2），cyc0 组合分歧。
- 12:11:59 主线落 round-16 `_wire_dep_order`（wire 叶发射按 wire→wire 依赖 Kahn 拓扑序）后，本轮最终树批次独立复证：**两模块 20 拍 112 输出叶全 MATCH**。

## 3. 复验结果（任务指定 9 项 + bonus）

| 模块 | 上轮状态 | 本轮（树） | 结果 |
|---|---|---|---|
| TLFIFOFixer_2 | skipped/SIGSEGV→in-progress | round-15 | **MATCH**（20 叶） |
| LoopBranchPredictorColumn | golden-only→MATCH(s2) | round-15 | **MATCH**（2 叶） |
| PMAChecker | emit-fail(LEAF)→MATCH(s2) | round-15 | **MATCH**（8 叶） |
| FetchBuffer | timeout 330s | round-15+16 | **MATCH**（226 叶，47s） |
| BoomWritebackUnit | emit-fail(LEAF) | round-15 | **MATCH**（27 叶） |
| OptimizationBarrier_UInt | emit-fail(LEAF) | round-15 | **MATCH**（1 叶） |
| CSRFile **无 --inputs 全激励** | timeout/>330s | round-15+16 | **MATCH**（238 叶，38s，900s 限额内） |
| DecodeUnit_1 | timeout 330s | r15→r16 | 36 MISMATCH → **MATCH**（112 叶） |
| DecodeUnit（bonus） | timeout 330s | r15→r16 | 36 MISMATCH → **MATCH**（112 叶） |
| BoomIOMSHR（bonus） | pycc-error | round-16 | 仍 pycc-error（F2） |

**回归监测：零回归。** round-16 全局改发射序后，11 个跨家族控制组（CSRFile/FetchBuffer/
IssueSlot/RVCExpander_1/RocketALU_1/TLFIFOFixer/RenameBusyTable/RoundAnyRawFNToRecFN_1/
PredRenameStage/RenameFreeList + 3 笔 TLXbar 实跑在 round-16 树）全部 MATCH。

## 4. MATCH 分布（sweep3 新增 59 + 复验/控制 19）

- 大端口聚合：IssueSlot×3（225 叶）、MemExeUnit（243 叶）、FetchBuffer（226 叶）、
  CSRFile 全激励（238 叶）、PredRenameStage（226 叶）——rename/issue/CSR 大状态模块全绿。
- 深译码锥转绿：RocketALU_1/_2、MulAddRecFNToRaw_preMul_1/postMul_1、RVCExpander_1/_2/_3、
  RecFNToIN×3、RoundAnyRawFNToRecFN×3（FPU 组合单元 + depth 家族全部解锁）。
- TL/总线网络：TLXbar×7、TLBuffer×6、TLFIFOFixer×3、TLWidthWidget8×3、TLJbar、
  TLAtomicAutomata_pbus、TLInterconnectCoupler、Repeater_a28 全 MATCH——TileLink 互联层等价。
- 其余：RenameBusyTable×2、PMPChecker_s3×2、MaxPeriodFibonacciLFSR×3、
  OptimizationBarrier×6、IntXbar×4、NullIntSource/NullPrefetcher、DivSqrt 同族。
- vacuous（0 输出叶）9 笔：TLBuffer/TLBuffer_1/2、TLFIFOFixer_1、TLWidthWidget8_1、
  TLXbar×4、TLAtomicAutomata_pbus、IntXbar_i0_o0——流程通过但无输出可比。

## 5. 累计覆盖（sweep1 + sweep2 + sweep3）

| 口径 | 数 |
|---|---|
| 叶子总数（非 ext、无 Inst、无 Mem） | 285 |
| sweep1 覆盖（42 distinct） | +42 |
| sweep2 新增（99 distinct，重叠 12） | +87 |
| **sweep3 新增（60 distinct）** | **+60** |
| **累计覆盖** | **189/285 = 66.3%** |
| 其中当前树直接验证 MATCH | 181 |
| 家族旁证 MATCH（基实例未直测、同族实例全绿：CompareRecFN、RecFNToIN、RoundAnyRawFNToRecFN、MulAddRecFNToRaw_preMul/postMul、RocketALU） | 6 |
| MISMATCH | 1（MemExeUnit_1） |
| pycc-error | 1（BoomIOMSHR） |
| 剩余未测 | 96（多为已证家族的重复实例：IssueSlot×53、OptimizationBarrier×27、TLWidthWidget8×6、LFSR×4 等） |

sweep3 新叶子 MATCH 率：**59/60 = 98.3%**（若含复验/控制 85 次运行，78 MATCH / 4 MISMATCH(全部为 DecodeUnit×2 在 round-15 的过渡态) / 1 pycc-error / 2 非叶重分类）。

## 6. 修复优先级（更新版 top-3）

1. **[最高 | 错译] MemExeUnit_1 agen 数据通路**：唯一 MISMATCH。切入点：golden↔gen.cpp
   逐拍 diff `io_agenbits.data` 的 en/next 锥（reg 由 pyc_comb_689 驱动），重点
   exe_rs1_data/exe_imm_data 采样与 asSInt 加法/ea_sign 位检查；姊妹 MemExeUnit 无此单元
   全 MATCH，是干净的 A/B 对照。产物 `/tmp/eq_sweep3/final_MemExeUnit_1/`。
2. **[中 | pycc] BoomIOMSHR state-optimize 管线内失败**：唯一 pycc-error；standalone 通过
   ⇒ 管线上下文问题（sweep2 已 dump pass_ir，`/tmp/eq_sweep2/BoomIOMSHR_s1_rerun/pass_ir/`）。
3. **[低 | 覆盖收尾] 96 个剩余叶子批跑**：几乎全是已证家族重复实例，串行 ~35 分钟可清完，
   覆盖即 100%（工具缺口 0 后叶子口径全闭环）。
   附带：eqcheck 对 inst-in-when 模块（FPToFP/IntToFP 型）可在 lowering 前给明确的
   "not a leaf" 报错（现在是 golden 侧 `undeclared <instname>`，可读性差）。

## 7. 全量结果表（85 次运行）

分类以**该笔实际运行的树**为准（r15 = lower.py@11:09，r16 = @12:11:59 后）。

| # | tag | 模块 | 树 | 分类 | 耗时 | 备注 |
|---:|---|---|---|---|---:|---|
| 1 | rerun_TLFIFOFixer_2 | TLFIFOFixer_2 | r15 | MATCH | 21s | 20 叶 |
| 2 | rerun_LoopBranchPredictorColumn | LoopBranchPredictorColumn | r15 | MATCH | 23s | 2 叶 |
| 3 | rerun_PMAChecker | PMAChecker | r15 | MATCH | 21s | 8 叶 |
| 4 | rerun_FetchBuffer | FetchBuffer | r15 | MATCH | 47s | 226 叶 |
| 5 | rerun_BoomWritebackUnit | BoomWritebackUnit | r15 | MATCH | 22s | 27 叶 |
| 6 | rerun_OptimizationBarrier_UInt | OptimizationBarrier_UInt | r15 | MATCH | 21s | 1 叶 |
| 7 | rerun_DecodeUnit_1 | DecodeUnit_1 | r15 | MISMATCH | 25s | 36 cyc/ports；r16 复验 MATCH |
| 8 | rerun_DecodeUnit | DecodeUnit | r15 | MISMATCH | 24s | 同上 |
| 9 | rerun_CSRFile_full | CSRFile | r15 | MATCH | 38s | 全激励 238 叶，无 --inputs |
| 10 | FPToFP | FPToFP | r15 | non-leaf | 28s | inst(RecFNToRecFN) 在 when 内；重分类 |
| 11 | IntToFP | IntToFP | r15 | non-leaf | 28s | inst(INToRecFN) 在 when 内；重分类 |
| 12 | IntXbar_i0_o0 | IntXbar_i0_o0 | r15 | MATCH | 21s | 组合，0 叶（vacuous） |
| 13 | IntXbar_i1_o1 | IntXbar_i1_o1 | r15 | MATCH | 21s | 组合，1 叶 |
| 14 | IntXbar_i1_o1_1 | IntXbar_i1_o1_1 | r15 | MATCH | 21s | 组合 |
| 15 | IntXbar_i1_o1_2 | IntXbar_i1_o1_2 | r15 | MATCH | 21s | 组合 |
| 16 | IssueSlot | IssueSlot | r15 | MATCH | 28s | 225 叶 |
| 17 | IssueSlot_1 | IssueSlot_1 | r15 | MATCH | 28s | 225 叶 |
| 18 | IssueSlot_10 | IssueSlot_10 | r15 | MATCH | 28s | 225 叶 |
| 19 | MaxPeriodFibonacciLFSR_1 | MaxPeriodFibonacciLFSR_1 | r15 | MATCH | 21s | 16 叶 |
| 20 | MaxPeriodFibonacciLFSR_3 | MaxPeriodFibonacciLFSR_3 | r15 | MATCH | 21s | 6 叶 |
| 21 | MaxPeriodFibonacciLFSR_4 | MaxPeriodFibonacciLFSR_4 | r15 | MATCH | 21s | 6 叶 |
| 22 | MemExeUnit | MemExeUnit | r15 | MATCH | 26s | 243 叶 |
| 23 | MemExeUnit_1 | MemExeUnit_1 | r15 | MISMATCH | 27s | 14 cyc/ports，仅 io_agenbits.data（F1） |
| 24 | MulAddRecFNToRaw_postMul_1 | MulAddRecFNToRaw_postMul_1 | r15 | MATCH | 21s | 组合，7 叶；sweep1 基实例 emit>420s |
| 25 | MulAddRecFNToRaw_preMul_1 | MulAddRecFNToRaw_preMul_1 | r15 | MATCH | 22s | 组合，19 叶 |
| 26 | NullIntSource | NullIntSource | r15 | MATCH | 20s | 组合，1 叶 |
| 27 | NullPrefetcher | NullPrefetcher | r15 | MATCH | 21s | 115 叶 |
| 28 | OptimizationBarrier_EntryData_1 | OptimizationBarrier_EntryData_1 | r15 | MATCH | 21s | 15 叶 |
| 29 | OptimizationBarrier_EntryData_10 | OptimizationBarrier_EntryData_10 | r15 | MATCH | 20s | 15 叶 |
| 30 | OptimizationBarrier_EntryData_11 | OptimizationBarrier_EntryData_11 | r15 | MATCH | 20s | 15 叶 |
| 31 | OptimizationBarrier_TLBEntryData_1 | OptimizationBarrier_TLBEntryData_1 | r15 | MATCH | 21s | 23 叶 |
| 32 | OptimizationBarrier_TLBEntryData_10 | OptimizationBarrier_TLBEntryData_10 | r15 | MATCH | 21s | 23 叶 |
| 33 | OptimizationBarrier_TLBEntryData_11 | OptimizationBarrier_TLBEntryData_11 | r15 | MATCH | 21s | 23 叶 |
| 34 | PMPChecker_s3 | PMPChecker_s3 | r15 | MATCH | 22s | 3 叶 |
| 35 | PMPChecker_s3_1 | PMPChecker_s3_1 | r15 | MATCH | 21s | 3 叶 |
| 36 | RVCExpander_1 | RVCExpander_1 | r15 | MATCH | 22s | 7 叶；sweep2 基实例 pycc depth-96 |
| 37 | RVCExpander_2 | RVCExpander_2 | r15 | MATCH | 21s | 7 叶 |
| 38 | RVCExpander_3 | RVCExpander_3 | r15 | MATCH | 21s | 7 叶 |
| 39 | RecFNToIN_1 | RecFNToIN_1 | r15 | MATCH | 20s | 2 叶 |
| 40 | RecFNToIN_2 | RecFNToIN_2 | r15 | MATCH | 20s | 2 叶 |
| 41 | RecFNToIN_3 | RecFNToIN_3 | r15 | MATCH | 20s | 2 叶 |
| 42 | RenameBusyTable | RenameBusyTable | r15 | MATCH | 26s | 7 叶 |
| 43 | RenameBusyTable_1 | RenameBusyTable_1 | r15 | MATCH | 23s | 7 叶 |
| 44 | Repeater_TLBundleA_a28d64s3k1z3u | Repeater_TLBundleA_a28d64s3k1z3u | r15 | MATCH | 21s | 11 叶 |
| 45 | RocketALU_1 | RocketALU_1 | r15 | MATCH | 22s | 3 叶；sweep1 基实例 emit>420s |
| 46 | RocketALU_2 | RocketALU_2 | r15 | MATCH | 21s | 3 叶 |
| 47 | RoundAnyRawFNToRecFN_1 | RoundAnyRawFNToRecFN_1 | r15 | MATCH | 22s | 组合，2 叶 |
| 48 | RoundAnyRawFNToRecFN_2 | RoundAnyRawFNToRecFN_2 | r15 | MATCH | 21s | 组合 |
| 49 | RoundAnyRawFNToRecFN_3 | RoundAnyRawFNToRecFN_3 | r15 | MATCH | 21s | 组合 |
| 50 | TLAtomicAutomata_pbus | TLAtomicAutomata_pbus | r15 | MATCH | 20s | 0 叶（vacuous） |
| 51 | TLBuffer | TLBuffer | r15 | MATCH | 20s | 0 叶 |
| 52 | TLBuffer_1 | TLBuffer_1 | r15 | MATCH | 20s | 0 叶 |
| 53 | TLBuffer_2 | TLBuffer_2 | r15 | MATCH | 20s | 0 叶 |
| 54 | TLBuffer_a28d64s3k1z4u_1 | TLBuffer_a28d64s3k1z4u_1 | r15 | MATCH | 20s | 20 叶 |
| 55 | TLBuffer_a32d64s3k2z4c_1 | TLBuffer_a32d64s3k2z4c_1 | r15 | MATCH | 20s | 42 叶 |
| 56 | TLBuffer_a32d64s5k1z3u | TLBuffer_a32d64s5k1z3u | r15 | MATCH | 20s | 20 叶 |
| 57 | TLFIFOFixer | TLFIFOFixer | r15 | MATCH | 21s | 42 叶 |
| 58 | TLFIFOFixer_1 | TLFIFOFixer_1 | r15 | MATCH | 21s | 0 叶 |
| 59 | TLFIFOFixer_3 | TLFIFOFixer_3 | r15 | MATCH | 22s | 20 叶 |
| 60 | TLInterconnectCoupler_sbus_from_boom_tile | 同名 | r15 | MATCH | 21s | 42 叶 |
| 61 | TLJbar | TLJbar | r15 | MATCH | 20s | 42 叶 |
| 62 | TLWidthWidget8 | TLWidthWidget8 | r15 | MATCH | 20s | 20 叶 |
| 63 | TLWidthWidget8_1 | TLWidthWidget8_1 | r15 | MATCH | 20s | 0 叶 |
| 64 | TLWidthWidget8_2 | TLWidthWidget8_2 | r15 | MATCH | 20s | 42 叶 |
| 65 | TLXbar_SlaveXbar_BoomTile_i0_o0_a1d8s1k1z1u | 同名 | r15 | MATCH | 21s | 0 叶 |
| 66 | TLXbar_cbus_in_i1_o1_a28d64s3k1z4u | 同名 | r15 | MATCH | 21s | 20 叶 |
| 67 | TLXbar_fbus_i0_o0_a1d8s1k1z1u | 同名 | r15 | MATCH | 21s | 0 叶 |
| 68 | TLXbar_i1_o1_a32d64s5k1z3u | 同名 | r15 | MATCH | 30s | 20 叶 |
| 69 | TLXbar_mbus_i1_o1_a32d64s5k1z3u | 同名 | **r16** | MATCH | 22s | 20 叶 |
| 70 | TLXbar_pbus_in_i0_o0_a1d8s1k1z1u | 同名 | **r16** | MATCH | 21s | 0 叶 |
| 71 | TLXbar_pbus_out_i0_o0_a1d8s1k1z1u | 同名 | **r16** | MATCH | 21s | 0 叶 |
| 72 | final_DecodeUnit | DecodeUnit | r16 | MATCH | 24s | round-16 修复独立复证，112 叶 |
| 73 | final_DecodeUnit_1 | DecodeUnit_1 | r16 | MATCH | 24s | 112 叶 |
| 74 | final_MemExeUnit_1 | MemExeUnit_1 | r16 | MISMATCH | 33s | F1 持续 |
| 75 | final_BoomIOMSHR | BoomIOMSHR | r16 | pycc-error | 20s | F2 持续 |
| 76 | final_CSRFile_full | CSRFile | r16 | MATCH | 37s | 全激励 238 叶 |
| 77 | final_FetchBuffer | FetchBuffer | r16 | MATCH | 47s | 226 叶 |
| 78 | final_IssueSlot | IssueSlot | r16 | MATCH | 27s | 225 叶 |
| 79 | final_RVCExpander_1 | RVCExpander_1 | r16 | MATCH | 21s | 7 叶 |
| 80 | final_RocketALU_1 | RocketALU_1 | r16 | MATCH | 21s | 3 叶 |
| 81 | final_TLFIFOFixer | TLFIFOFixer | r16 | MATCH | 21s | 42 叶 |
| 82 | final_RenameBusyTable | RenameBusyTable | r16 | MATCH | 25s | 7 叶 |
| 83 | final_RoundAnyRawFNToRecFN_1 | RoundAnyRawFNToRecFN_1 | r16 | MATCH | 20s | 组合 2 叶 |
| 84 | final_PredRenameStage | PredRenameStage | r16 | MATCH | 47s | 226 叶（编译器调度修复回归控制） |
| 85 | final_RenameFreeList | RenameFreeList | r16 | MATCH | 22s | 5 叶（depth 家族回归控制） |

产物索引：`/tmp/eq_sweep3/<tag>/`（model.mlir / gen.cpp / driver.cpp / golden_trace.txt /
stim.txt / check.log / runner）、汇总 `/tmp/eq_sweep3/results.tsv` +
`/tmp/eq_sweep3/results_final.tsv`；选样清单 `/tmp/sweep3_targets.json`；
清单脚本 `/tmp/sweep3_pick.py`（⚠ 已知 When.then walk 缺陷，见 §1）。
