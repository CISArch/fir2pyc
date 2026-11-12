# fir2pyc 等价对拍 sweep #2（99 叶子模块 + 控制组/回归，共 100+11 次运行）

日期：2026-11-12（07:0x 收官）  
对象：`/tmp/boom_trim.fir`  
命令：`python3 -m src.fir2pyc.cli check /tmp/boom_trim.fir --module <M> --cycles 20 --seed <S>`（单模块 330s 外超时；产物 `/tmp/eq_sweep2/<M>/`）  
工具链：pycc @ pyCircuit-dev/.pycircuit_out/toolchain/install/bin/pycc

**⚠ 时线注意**：本轮 sweep 期间主线 agent 在同时落修复（src mtime：eqcheck.py 06:13 / cli.py 06:19 / lower.py 06:36 / evalfir.py 06:42）。
06:42 之前跑的模块可能带旧语义。因此**全部非 MATCH 项都在终态树上重跑过**（`/tmp/eq_sweep2/*_rerun/`，重跑窗口内 src mtime 不变），下表"分类"以终态树为准；「主 sweep → 重跑」不同者单独标注。

## TL;DR

- **99 个叶子模块（100 次主运行 + 11 次重跑）终态：88 MATCH / 2 MISMATCH / 4 pycc-error / 4 timeout / 1 eqcheck-contract**。
- **上轮六类缺陷全部验证修复**（控制组 5/5 MATCH；MulDiv seeds 1/2/3/5、DivSqrtRawFN_small seeds 1/2 MATCH；上轮 5 个 golden-error 模块 4 个 MATCH、1 个转为 pycc 深度限制；无时钟组合模块已支持，23 个组合模块 MATCH，其中 `CompareRecFN_1` 即上轮 infra 失败家族成员）。
- **新失配家族 2 个（真模型错译）**：①`PredRenameStage` br_mask/ppred kill-保持类爆发失配（890 cycle/ports）；②`RenameFreeList_2` 空闲链表分配读 off-by-one（cyc16+，10 cycle/ports）。
- **新阻塞家族（非语义错译）**：①pycc `logic depth exceeds limit`（depth>96，3 模块）；②`state-optimize`(#15) 管线内失败无诊断（BoomIOMSHR，standalone 单跑同输入通过）；③重模块 emit/lowering >330s（CSRFile/FetchBuffer/DecodeUnit×2）；④eqcheck 扁平化成员名契约缺口（BasicDispatcher）。

上轮 24 MATCH 基础上，两轮累计 distinct 叶子覆盖 **129/285（45%）**；当前树累计 MATCH 模块 **122**。

## 1. 上轮已知项处置

| 上轮项 | 本轮处置 |
|---|---|
| 控制组 LFSR_2 / BoomRAS / PipelinedMultiplier / BranchDecode / PMAChecker | 5/5 MATCH（BoomRAS 读树修复、BranchDecode/PMAChecker 组合锥修复生效） |
| MulDiv seeds 2/3/5 pipeline-latch | **已修**：seeds 1/2/3/5 全 MATCH（重跑验证） |
| DivSqrtRawFN_small sig bit26 残留 | **已修**：seed 1 主 sweep 尚 MISMATCH(cyc12 iorawOut.sig golden=0x4000001 cpp=0x1) → 终态树重跑 seeds 1/2 MATCH；DivSqrtRawFN_small_1（新实例）MATCH |
| TLFIFOFixer_2 SIGSEGV | 按指令 SKIP（另一 agent 在修 evalfir；round-13 记录：golden 阶段已修，现阻塞点移到 pycc comb-cycle） |
| 上轮 4 个无时钟 infra 失败 | **已修**：eqcheck 支持无时钟模块（combinational 模式），23 个组合模块 MATCH |
| 上轮 5 个 golden-error | RVCExpander→pycc 深度限制（golden 侧已能跑）；LoopBranchPredictorColumn/BoomProbeUnit/BoomWritebackUnit/Repeater_→MATCH（evalfir 聚合 mux 缺口已补） |
| RocketALU / MulAddRecFNToRaw_postMul（emit >420s） | 未重测（同族深 mux 链，本轮 DecodeUnit×2 timeout 同签名） |
| BoomWritebackUnit / FetchBuffer / OptimizationBarrier_UInt（masstest LEAF 败） | BoomWritebackUnit **check MATCH**；FetchBuffer **timeout**（330s）；OptimizationBarrier_UInt 上轮已 MATCH，未重测 |

## 2. 新失配家族（真错译，2 个）

### F1: PredRenameStage — br_mask/ppred kill-保持类爆发失配 [最严重]

- 终态树复跑仍在：`FAIL PredRenameStage: 890 mismatching cycle/ports over 19 compared cycles`。
- 签名：**爆发式**（cycle 5:155 / 9:135 / 12:154 / 13:130，其余 0~3），**按端口聚合的头部**：`ioren2_uops[0/1].br_mask`（11/11 拍）、`ioren2_uops[0].ppred`（9 拍）、其余 uop 载荷字段（rob_idx/prs2/prs3/stq_idx/stale_pdst 等）在爆发拍跟进。
- 首分歧（cyc5）：`ioren2_uops[0].inst golden=0x62827e2b cpp=0`、`debug_pc golden=0x3b32737af7 cpp=0`——C++ 侧整批 uop 载荷归零而 golden 保有值，符合「kill/hold 条件错译 → 该拍寄存器 en/next 语义错 → 后级级联」的 reg 保持类家族（与已修的 hold 语义同族但新触发模式：**按 br_mask 掩码驱动的 kill**）。
- 完整 890 项明细：`/tmp/eq_sweep2/PredRenameStage_full/check.log`（--max-print 2500）；终态重跑：`/tmp/eq_sweep2/PredRenameStage_s1_rerun/`。

### F2: RenameFreeList_2 — 空闲链表分配读 off-by-one 【已修复 2026-11-12】

- ~~终态树复跑仍在：`FAIL RenameFreeList_2: 10 mismatching cycle/ports`，首分歧 cyc16。~~ **已定位并修复**：`check --cycles 20` 现在 PASS。
- 真因（非「分配读滞后一格」）：`br_deallocs = br_alloc_lists[io.brupdate.b2.uop.br_tag] & mask(mispredict)`，`br_tag` 4bit（0..15）但 `br_alloc_lists` 只有 12 项——**越界动态 subaccess 读**。lowering 的 `_resolve_subaccess_read` eq 级联以**元素 0 作缺省基**（越界索引静默返回 `br_alloc_lists[0]`），evalfir golden 对越界读返回 **0**（无 driver 的 x-0 缺省）。随机激励 brtag≥12（1/4 概率）时 C++ 侧假「释放」preg0，`free_list` bit0 自 cyc13 起与 golden 分叉（debug OR 输出掩蔽到 cyc16 才可见）。
- 修复：lower.py `_resolve_subaccess_read` 级联基改为**常量 0**（越界 → 0，与 evalfir x-0 约定一致）；界内值不变（仅元素 0 多一层 mux）。验证：RenameFreeList_2 PASS + 控制组 LFSR_2/BoomRAS/PipelinedMultiplier/BoomRAS-seed3 全 PASS + masstest 281/285 持平。
- 注意：同模块 RenameFreeList/RenameFreeList_1 仍被 pycc 深度限制挡住（见 F3），三实例两族症状并存。失配工作目录：`/tmp/eq_rfl/`（修前）/ `/tmp/eq_rfl_fix/`（修后）。

## 3. 新阻塞家族（非错译）

### F3: pycc logic-depth 限制（3 模块）

| 模块 | pycc 报错 |
|---|---|
| RVCExpander | `depth=101 limit=96`（model.mlir:2143） |
| RenameFreeList | `depth=103/175/168/186 limit=96`（:225-230） |
| RenameFreeList_1 | `depth=143/136/150 limit=96`（:192-195） |
- lowering 产出的线性 mux/译码链超过 SLPPackWiresPass 深度上限 96。上轮 RocketALU/MulAddRecFNToRaw_postMul 的 >420s emit 与本轮 DecodeUnit×2 timeout 疑似同根（深译码锥）。修法候选：lowering 端平衡 mux 树（成对折叠），或 pycc 端把上限做成可配/自动分块。

### F4: BoomIOMSHR — state-optimize(#15) 管线内失败

- `pycc failed (exit 1)`，唯一输出 `error: pass pipeline failed`（无位置/原因诊断）。`--dump-pass-ir` 定位到 `0015_state-optimize` 的 `after_*_FAILED.mlir`。
- **同输入 standalone `pyc-opt --pass-pipeline='builtin.module(func.func(pyc-state-optimize))'` 通过**（emitted IR 与 before-dump 都试过）——失败依赖管线上下文/嵌套（与 2026-11-11 白板「pycc 全顶层阻塞」记录同族）。pycc 侧问题，非 fir2pyc 错译。

### F5: BasicDispatcher — eqcheck 扁平化成员名契约缺口

- rc=2：`member iodis_uops_3_0__ready (from port iodis_uops.3[0].ready) not found in generated model`；模型实际成员名形如 `iodis_uops_f0_0__bits_bp_debug_if`——vector-of-bundle 经 bulk-connect 展开后的命名与 eqcheck 期望不一致。golden 已能跑（evalfir OK），只差名字对齐。

### F6: 重模块超时（4）

- CSRFile（284 IO 叶）、FetchBuffer、DecodeUnit、DecodeUnit_1 均 >330s 未完成（卡在 lowering 或 pycc 段）。CSRFile 可用既有建议（`--outputs` 白名单）缩面；DecodeUnit 与 F3 同根嫌疑。

## 4. MATCH 分布（88）

- 值得注意的增强证据：Arbiter2_Wakeup/Arbiter3_BoomDCacheResp/Arbiter4_ExeUnitResp/HellaCacheArbiter（63~121 输出叶，大端口聚合 MATCH）；RenameMapTable(+_1)（动态读家族 MATCH——读树修复的正面证据）；BranchDecode_1/_2、BreakpointUnit_2~4、DivSqrtRawFN_small_1（同构实例）。
- **33/88 为 0 输出叶的 vacuous MATCH**（ClockGroup×8、FixedClockBroadcast×8、BundleBridgeNexus_NoOutput×9、ClockGroupAggregator×4 等——模块无输出可比，仅流程通过）；**23/88 为无时钟组合模块**（上轮不可测类）。扣除 vacuous 后有效 MATCH 55。

## 5. 修复优先级（top-3 + 次级）

1. **[最高 | 错译] PredRenameStage br_mask/ppred kill-保持**：890 cycle/ports 的最大失配量，br_mask 每拍失配，kill 后载荷载荷错——reg-hold/kill 条件家族新触发模式；建议从 br_mask 寄存器的 en/next 锥做 golden↔gen.cpp 内部态逐拍 diff（round-13 的 state_dump 方法可直接复用）。
2. ~~**[高 | 错译] RenameFreeList_2 分配读 off-by-one**~~ **已修复（2026-11-12）**：真因是越界动态 subaccess 读（brtag 4bit vs 12 项列表）级联缺省基不一致——lowering 缺省取元素 0、golden 取 0；已改 `_resolve_subaccess_read` 基为零，RenameFreeList_2 PASS、控制组 4/4、masstest 281/285 持平。详见 §2 F2。
3. **[高 | 阻塞] pycc 深度限制 96 + 深译码锥超时**：一石三鸟（RVCExpander/RenameFreeList(_1) 直接挡、DecodeUnit×2 timeout 疑同根、RocketALU/postMul 上轮现象同族）；两个方向任选：lowering 端 mux 树平衡化（不动 pycc），或 pycc 端可配上限/自动深链分块。
4. [中] BoomIOMSHR state-optimize 管线内失败：standalone 同输入通过 ⇒ pycc 侧管线上下文问题，接白板已有「pycc 全顶层阻塞」线查。
5. [中] eqcheck 扁平化成员名契约（BasicDispatcher）：对齐 bulk-connect 展开命名即可解锁。
6. [低] CSRFile --outputs 白名单；eqcheck 0 输出叶模块快速跳过（33 个 vacuous 运行各耗 ~21s ≈ 11 分钟，可省）。

## 6. 全量结果表

（分类为终态树判定；「主 sweep → 重跑」列为主 sweep 与终态重跑结果不一致的标注；耗时为主 sweep 单模块墙钟）

| # | 模块 | seed | 分类 | 耗时 | 备注/证据 |
|---:|---|---:|---|---:|---|
| 1 | MaxPeriodFibonacciLFSR_2 | 1 | MATCH | 21s | MATCH over 19 compared cycles (cycle 1..19, 6 output leaves, seed=1) |
| 2 | BoomRAS | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 1 output leaves, seed=1) |
| 3 | PipelinedMultiplier | 1 | MATCH | 21s | MATCH over 19 compared cycles (cycle 1..19, 4 output leaves, seed=1) |
| 4 | BranchDecode | 1 | MATCH | 24s | MATCH over 20 compared cycles (cycle 0..19, 7 output leaves, seed=1) |
| 5 | PMAChecker | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 8 output leaves, seed=1) |
| 6 | DivSqrtRawFN_small | 1 | MISMATCH -> MATCH (re-run on post-fix tree) | 22s | 06:11 主跑 MISMATCH(cyc12 sig bit26)；终态树重跑 seeds 1/2 MATCH |
| 7 | MulDiv | 1 | MATCH | 22s | MATCH over 19 compared cycles (cycle 1..19, 5 output leaves, seed=1) |
| 8 | MulDiv | 2 | MATCH | 23s | 上轮 seeds 2/3/5 pipeline-latch；本轮 1/2/3/5 全 MATCH |
| 9 | RVCExpander | 1 | pycc-error | 20s |  |
| 10 | LoopBranchPredictorColumn | 1 | MATCH | 29s | MATCH over 19 compared cycles (cycle 1..19, 2 output leaves, seed=1) |
| 11 | BoomProbeUnit | 1 | MATCH | 21s | MATCH over 19 compared cycles (cycle 1..19, 37 output leaves, seed=1) |
| 12 | BoomWritebackUnit | 1 | MATCH | 22s | MATCH over 19 compared cycles (cycle 1..19, 27 output leaves, seed=1) |
| 13 | Repeater_TLBundleA_a26d64s3k1z3u | 1 | MATCH | 23s | MATCH over 19 compared cycles (cycle 1..19, 11 output leaves, seed=1) |
| 14 | RenameMapTable | 1 | MATCH | 39s | MATCH over 19 compared cycles (cycle 1..19, 8 output leaves, seed=1) |
| 15 | RenameMapTable_1 | 1 | MATCH | 40s | MATCH over 19 compared cycles (cycle 1..19, 8 output leaves, seed=1) |
| 16 | RenameFreeList | 1 | pycc-error | 80s |  |
| 17 | RenameFreeList_1 | 1 | pycc-error | 40s |  |
| 18 | RenameFreeList_2 | 1 | MISMATCH | 26s | FAIL RenameFreeList_2: 10 mismatching cycle/ports over 19 compared cycles (1..19) |
| 19 | PredRenameStage | 1 | MISMATCH | 74s | FAIL PredRenameStage: 890 mismatching cycle/ports over 19 compared cycles (1..19) |
| 20 | CSRFile | 1 | timeout | 330s | >330s |
| 21 | LoopBranchPredictorColumn_1 | 1 | MATCH | 28s | MATCH over 19 compared cycles (cycle 1..19, 2 output leaves, seed=1) |
| 22 | LoopBranchPredictorColumn_2 | 1 | MATCH | 29s | MATCH over 19 compared cycles (cycle 1..19, 2 output leaves, seed=1) |
| 23 | LoopBranchPredictorColumn_3 | 1 | MATCH | 28s | MATCH over 19 compared cycles (cycle 1..19, 2 output leaves, seed=1) |
| 24 | FetchBuffer | 1 | timeout | 330s | >330s |
| 25 | AXI4IdIndexer | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 47 output leaves, seed=1) |
| 26 | Arbiter2_BoomDCacheReqInternal | 1 | MATCH | 23s | MATCH over 20 compared cycles (cycle 0..19, 123 output leaves, seed=1) |
| 27 | Arbiter2_BranchPredictionUpdate | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 24 output leaves, seed=1) |
| 28 | Arbiter2_L1DataWriteReq_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 8 output leaves, seed=1) |
| 29 | Arbiter2_L1MetaWriteReq | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 9 output leaves, seed=1) |
| 30 | Arbiter2_L1MetaWriteReq_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 9 output leaves, seed=1) |
| 31 | Arbiter2_Wakeup | 1 | MATCH | 24s | MATCH over 20 compared cycles (cycle 0..19, 118 output leaves, seed=1) |
| 32 | Arbiter2_WritebackReq_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 10 output leaves, seed=1) |
| 33 | Arbiter3_BoomDCacheResp | 1 | MATCH | 26s | MATCH over 20 compared cycles (cycle 0..19, 118 output leaves, seed=1) |
| 34 | Arbiter3_ExeUnitResp | 1 | MATCH | 26s | MATCH over 20 compared cycles (cycle 0..19, 120 output leaves, seed=1) |
| 35 | Arbiter4_ExeUnitResp | 1 | MATCH | 29s | MATCH over 20 compared cycles (cycle 0..19, 121 output leaves, seed=1) |
| 36 | AsyncResetRegVec_w1_i0_1 | 1 | MATCH | 22s | MATCH over 19 compared cycles (cycle 1..19, 1 output leaves, seed=1) |
| 37 | AsyncResetRegVec_w1_i0_2 | 1 | MATCH | 21s | MATCH over 19 compared cycles (cycle 1..19, 1 output leaves, seed=1) |
| 38 | AsyncResetRegVec_w1_i0_3 | 1 | MATCH | 22s | MATCH over 19 compared cycles (cycle 1..19, 1 output leaves, seed=1) |
| 39 | AsyncResetRegVec_w1_i0_4 | 1 | MATCH | 22s | MATCH over 19 compared cycles (cycle 1..19, 1 output leaves, seed=1) |
| 40 | BasicDispatcher | 1 | other-rc2 | 40s | check BasicDispatcher: cycles=20 seed=1 workdir=/tmp/eq_sweep2/BasicDispatcher golden: 20  |
| 41 | BoomIOMSHR | 1 | pycc-error | 22s |  |
| 42 | BranchDecode_1 | 1 | MATCH | 25s | MATCH over 20 compared cycles (cycle 0..19, 7 output leaves, seed=1) |
| 43 | BranchDecode_2 | 1 | MATCH | 25s | MATCH over 20 compared cycles (cycle 0..19, 7 output leaves, seed=1) |
| 44 | BranchDecode_3 | 1 | MATCH | 24s | MATCH over 20 compared cycles (cycle 0..19, 7 output leaves, seed=1) |
| 45 | BranchKillablePipeline | 1 | MATCH | 30s | MATCH over 20 compared cycles (cycle 0..19, 465 output leaves, seed=1) |
| 46 | BranchKillablePipeline_1 | 1 | MATCH | 27s | MATCH over 20 compared cycles (cycle 0..19, 310 output leaves, seed=1) |
| 47 | BranchKillablePipeline_2 | 1 | MATCH | 35s | MATCH over 20 compared cycles (cycle 0..19, 620 output leaves, seed=1) |
| 48 | BranchMaskGenerationLogic | 1 | MATCH | 22s | MATCH over 19 compared cycles (cycle 1..19, 7 output leaves, seed=1) |
| 49 | BreakpointUnit_1 | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 6 output leaves, seed=1) |
| 50 | BreakpointUnit_2 | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 6 output leaves, seed=1) |
| 51 | BreakpointUnit_3 | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 6 output leaves, seed=1) |
| 52 | BreakpointUnit_4 | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 6 output leaves, seed=1) |
| 53 | BundleBridgeNexus_NoOutput | 1 | MATCH | 22s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 54 | BundleBridgeNexus_NoOutput_1 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 55 | BundleBridgeNexus_NoOutput_2 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 56 | BundleBridgeNexus_NoOutput_3 | 1 | MATCH | 22s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 57 | BundleBridgeNexus_NoOutput_4 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 58 | BundleBridgeNexus_NoOutput_5 | 1 | MATCH | 22s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 59 | BundleBridgeNexus_NoOutput_6 | 1 | MATCH | 20s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 60 | BundleBridgeNexus_NoOutput_7 | 1 | MATCH | 22s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 61 | BundleBridgeNexus_NoOutput_8 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 0 output leaves, combinational, seed=1 |
| 62 | BundleBridgeNexus_TraceAux | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 2 output leaves, combinational, seed=1 |
| 63 | BundleBridgeNexus_TraceBundle | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 18 output leaves, combinational, seed= |
| 64 | BundleBridgeNexus_TraceCoreInterface | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 9 output leaves, combinational, seed=1 |
| 65 | BundleBridgeNexus_UInt1 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 66 | BundleBridgeNexus_UInt1_1 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 67 | BundleBridgeNexus_UInt32 | 1 | MATCH | 22s | MATCH over 1 compared cycles (cycle 0..0, 2 output leaves, combinational, seed=1 |
| 68 | ClockGroup | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 69 | ClockGroupAggregator_aggregator | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 70 | ClockGroupAggregator_cbus | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 71 | ClockGroupAggregator_coh | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 72 | ClockGroupAggregator_fbus | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 73 | ClockGroupAggregator_mbus | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 74 | ClockGroupAggregator_pbus | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 75 | ClockGroupAggregator_sbus | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 76 | ClockGroup_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 77 | ClockGroup_2 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 78 | ClockGroup_3 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 79 | ClockGroup_4 | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 80 | ClockGroup_5 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 81 | CompareRecFN_1 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 4 output leaves, combinational, seed=1 |
| 82 | DecodeUnit | 1 | timeout | 330s | >330s |
| 83 | DecodeUnit_1 | 1 | timeout | 331s | >330s |
| 84 | DivSqrtRawFN_small_1 | 1 | MATCH | 21s | MATCH over 19 compared cycles (cycle 1..19, 12 output leaves, seed=1) |
| 85 | FixedClockBroadcast_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 86 | FixedClockBroadcast_1_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 87 | FixedClockBroadcast_1_2 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 88 | FixedClockBroadcast_1_3 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 89 | FixedClockBroadcast_1_4 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 90 | FixedClockBroadcast_3 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 91 | FixedClockBroadcast_3_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 0 output leaves, seed=1) |
| 92 | ForwardingAgeLogic_1 | 1 | MATCH | 21s | MATCH over 20 compared cycles (cycle 0..19, 2 output leaves, seed=1) |
| 93 | HellaCacheArbiter | 1 | MATCH | 22s | MATCH over 20 compared cycles (cycle 0..19, 63 output leaves, seed=1) |
| 94 | IntSyncCrossingSource_n1x1_Registered | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 95 | IntSyncSyncCrossingSink_n1x1 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 96 | IntSyncSyncCrossingSink_n1x1_1 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 97 | IntSyncSyncCrossingSink_n1x1_2 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 98 | IntSyncSyncCrossingSink_n1x1_3 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 99 | IntSyncSyncCrossingSink_n1x1_4 | 1 | MATCH | 21s | MATCH over 1 compared cycles (cycle 0..0, 1 output leaves, combinational, seed=1 |
| 100 | IntSyncSyncCrossingSink_n1x2 | 1 | MATCH | 20s | MATCH over 1 compared cycles (cycle 0..0, 2 output leaves, combinational, seed=1 |

产物索引：`/tmp/eq_sweep2/<M>/`（每模块 model.mlir / gen.cpp / driver.cpp / golden_trace.txt / stim.txt / check.log）、`rerun_results.tsv`、`PredRenameStage_full/`、`BoomIOMSHR_s1_rerun/pass_ir/`。