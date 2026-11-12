# fir2pyc 等价对拍覆盖报告（BoomRAS 根因 + 41 模块 sweep）

日期：2026-11-12
对象：`/tmp/boom_trim.fir`（BOOM trim harness，571 modules：41 extmodule、245 non-leaf、285 leaf）
命令：`python3 -m src.fir2pyc.cli check /tmp/boom_trim.fir --module <M> --cycles 20 --seed 1`
工具链：pycc @ pyCircuit-dev/.pycircuit_out/toolchain/install/bin/pycc

---

## TL;DR

- **BoomRAS MISMATCH 根因已定位并实证**：`lower.py::_resolve_value` 用 `id(Expr)` 作 memo key 但不持有对象引用；`_resolve_subaccess_read` 为向量动态读逐元素新建临时 `eq(idx, ei)` Expr，被释放后 pymalloc 复用同一地址 → memo 全部命中首元素 → **32 层读 mux 树的条件全部退化为 `eq(io.read_idx, 0)`**，读树实际语义 `read_idx==0 ? ras[31] : ras[0]`。与初始化、hold 语义、写路径均无关。
- 静态扫描：285 个 leaf 模块中 **13 个含动态 subaccess 读**（本 bug 的直接受影响面）；51 个 non-leaf 也含动态 subaccess（multi-module 展开后同样受影响）。
- 42 次对拍（41 sweep + BoomRAS，--cycles 20 --seed 1）：**24 MATCH / 6 MISMATCH / 5 golden-error / 4 runner-infra / 2 emit-timeout / 1 emit-segfault**，明细见 §3。失配分四类签名：①动态读树坍缩（BoomRAS，已实证）；②eqcheck 驱动只打印 64 位低位字（MulDiv/PipelinedMultiplier 的 i128 输出，工具缺口非模型错）；③组合锥发散（BranchDecode/PMAChecker/DivSqrtRawFN_small，未定因）；④golden 评测器缺口挡住 5 个模块（其中 2 个恰是动态读家族的验证目标）。

---

## 1. BoomRAS 根因（已实证）

### 1.1 模块语义（boom_trim.fir L77799）

```
module BoomRAS :
  output io : { flip read_idx: UInt<5>, read_addr: UInt<40>, flip write_valid: UInt<1>,
                flip write_idx: UInt<5>, flip write_addr: UInt<40> }
  reg ras : UInt<40>[32], clock                ;; 无复位 32×40 reg 数组
  reg io_read_addr_REG   : UInt<1>             ;; bypass 标志（write_valid && write_idx==read_idx）
  reg io_read_addr_REG_1 : UInt                ;; 上拍 write_addr
  reg io_read_addr_REG_2 : UInt                ;; 上拍 ras[io.read_idx]
  io.read_addr = mux(REG, REG_1, REG_2)
  when io.write_valid: ras[io.write_idx] <- io.write_addr
```

### 1.2 发射的 MLIR（/tmp/eq_ras/model.mlir）哪里错了

- **写路径正确**：32 个元素各自 `en = OR(write_valid && write_idx==k)`（hold 修复生效）、`next = mux(cond, io.write_addr, 0)`、`init = const 0`。MLIR 中每个元素 k 都有正确的 `pyc.eq %arg4, %v<k>`。
- **读路径错误**：`ras[io.read_idx]` 展开成 32 层 mux 树，但 **全部 32 层用的是同一个条件 `%v332 = pyc.eq %arg2, %v106`（即 `eq(io.read_idx, 0)`）**：

```
%v332 = pyc.eq %arg2, %v106 : i5, i5 -> i1     ; = eq(io.read_idx, 0)
%v333 = pyc.mux %v332, %v6,  %v2               ; 第 1 层…第 31 层全部用 %v332
...
%v363 = pyc.mux %v332, %v96, %v362
```

  语义退化为：`read_idx==0 ? ras[31] : ras[0]`。

### 1.3 精确机制：id() 记忆化 + 临时对象地址复用

`lower.py::_resolve_value`：

```python
key = (id(e), w, signed, path)      # 只存 int，不持有 e 的引用
memo = self.val_memo.get(key)
...
self.val_memo[key] = out
```

`lower.py::_resolve_subaccess_read` 循环体：

```python
cond = self._resolve_cond(Expr(kind="prim", op="eq", args=[
            idx, Expr(kind="lit", value=ei, width=ib)]))
```

每次迭代新建的 `eq` Expr 是临时对象：解析完成后即释放，CPython pymalloc 把**同一块地址**分配给下一次迭代的同尺寸 Expr。于是 `id(e)` 与首元素（ei=0）的 memo key 相同 → `_resolve_value` 命中旧条目 → 返回 ei=0 的 SSA `%v332`，并且后续 31 次迭代次次命中。写侧条件来自长寿命 When AST 节点（不经过这个临时对象路径），所以未被污染。

Monkeypatch 探针（/tmp/probe_ras.py，包装 `ModuleLower._resolve_value` 打印 id 冲突）实测：

```
!! ID COLLISION id=0x7f7d9e31ec70: memo holds 'eq(io.read_idx, l5=0x0)',
   now resolving 'eq(io.read_idx, l5=0x1)'    ← 0x2 … 0x1f 共 31 次，同一地址
```

### 1.4 逐拍复核（golden 解释器 vs 按错误读树语义仿真的 C++）

激励（seed=1）关键点：cycle 10 `ras[0] <- 0x388712b8bc`（写生效于 cycle 11 的 Q）。

| cyc | read_idx | golden out | 按错误读树预测 | 判定 | check 实报 |
|----:|----:|----:|----:|----|----|
| 0–11 | — | 0 | 0 | 一致 | 一致 |
| 12 | 7  | 0 | 0x388712b8bc（读 ras[0]） | MISMATCH | golden=0 cpp=0x388712b8bc ✓ |
| 13 | 24 | 0xf19755d4c1（ras[7]，cycle 6 写入） | 0x388712b8bc | MISMATCH | ✓ |
| 14 | 0  | 0（ras[24]） | 0x388712b8bc | MISMATCH | ✓ |
| 15 | 29 | 0x388712b8bc（ras[0]，golden 读对） | 0（读 ras[31]） | MISMATCH | golden=0x388712b8bc cpp=0 ✓ |
| 16 | 31 | 0 | 0x388712b8bc | MISMATCH | ✓ |
| 17 | 23 | 0 | 0x388712b8bc | MISMATCH | （打印上限未显示） |
| 18 | 22 | 0 | 0x388712b8bc | MISMATCH | （同上） |
| 19 | 16 | 0 | 0x388712b8bc | MISMATCH | （同上） |

- **首分歧 cycle 12**：`io_read_addr_REG_2` 这一拍应锁存 ras[read_idx@11]=ras[24]=0，C++ 锁存的是 ras[0]=0x388712b8bc。
- **失配总数 8（cycles 12–19）**，与 `FAIL BoomRAS: 8 mismatching cycle/ports` 精确吻合；只打印 5 条是 eqcheck.py:322 `if fails <= 5` 的打印上限，不是 3 拍未报。
- **首分歧对象是 `io_read_addr_REG_2`（读值锁存 reg），不是栈元素本身**；ras[0] 的写与 hold 全程正确。cycle 15 的互换（golden=C++他拍值、cpp=0）是“golden 读对 ras[0]、C++ 读错 ras[31]”的直接证据。

### 1.5 对任务三项假设的回答

(a) 首先发散的信号：输出 `io.read_addr`（cycle 12），物理上是 `io_read_addr_REG_2` 的 Q 经 bypass mux；栈元素 ras[k] 的 Q 本身无分歧。
(b) 既不是“条件求值不同”也不是“Q 保持不同”：两侧寄存器状态机完全一致，分歧发生在**组合读树选错元素**（C++ 结构性读 ras[0]/ras[31]）。
(c) 初始化无问题：32 个 ras 元素与 3 个锁存 reg 的 init 全部正确（ras 元素 init=const 0，与 golden 的“无复位 reg 按 0”一致）；`en=OR(driver conds)` hold 修复在本模块工作正常。**“regreset 非零 init / reg bank 未初始化”假设被否定**——cpp 里反复出现的 0x388712b8bc 不是未初始化垃圾，而是 cycle 10 写入的合法激励值。

---

## 2. 爆炸半径（静态扫描）

对 285 个 leaf 模块做 AST 级动态 subaccess 扫描（/tmp/blast.py）：

- **含动态 subaccess 读的 leaf：13 个**（读树 bug 直接受影响面）
  `BoomRAS`、`BoomWritebackUnit`、`LoopBranchPredictorColumn`(+_1/_2/_3)、`RenameMapTable`(+_1)、`RenameFreeList`(+_1/_2)、`PredRenameStage`、`CSRFile`
- 仅动态写（写路径，当前实现正确）：3 个
- 含动态 subaccess 的 non-leaf：51 个（层次化展开后走同一条 `_resolve_subaccess_read`，同理受影响）

注意：动态读只有当**向量各元素在观测窗口内可区分**时才暴露为 MISMATCH；内容全 0/全相等时静默错误。这是“对拍通过≠无此 bug”的原因，13 个静态命中模块无论 sweep 结果如何都应视为**待修复后重验**。

---

## 3. Sweep 结果（42 次对拍，--cycles 20 --seed 1）

选样：控制组 3（此前 MATCH）+ ALU/FPU 14 + 分支预测/RAS 6 + 仲裁器 10 + 同步原语 4 + 队列/重发/屏障/过滤 6 + 杂项 3。
排除说明：`IntToFP`、`BoomMSHRFile` 为 non-leaf（体含 inst/mem），leaf 模式 check 不适用；`BoomPTW`、`BoomBTB`、`BoomTage*` 在 trim harness 中已不存在。leaf 总量 285，其中 IO 叶子 ≤64 的约 210 个。总耗时 28 分钟（04:04:46–04:32:51），串行，单模块 420s 外超时。

### 3.1 MATCH（24）

| 模块 | IO叶 | 时间 | 类别 |
|---|---:|---:|---|
| OptimizationBarrier_TLBEntryData | 48 | 21s | 控制组/屏障 |
| NonSyncResetSynchronizerPrimitiveShiftReg_d3 | 4 | 21s | 控制组/同步 |
| MaxPeriodFibonacciLFSR_2 | 16 | 22s | 控制组/LFSR |
| AMOALU | 8 | 21s | ALU |
| ForwardingAgeLogic | 6 | 21s | 前递年龄 |
| BreakpointUnit | 49 | 21s | 调试 |
| NullLocalBranchPredictorBank | 13 | 21s | 分支预测 |
| Arbiter1_Bool | 9 | 22s | 仲裁器 |
| Arbiter3_UInt5 | 15 | 21s | 仲裁器 |
| Arbiter2_L1MetaReadReq | 18 | 22s | 仲裁器 |
| Arbiter2_L1DataWriteReq | 21 | 22s | 仲裁器 |
| Arbiter3_BoomL1DataReadReq | 23 | 21s | 仲裁器 |
| Arbiter2_WritebackReq | 27 | 21s | 仲裁器 |
| Arbiter2_TLBundleC_a32d64s2k2z4c | 30 | 21s | 仲裁器 |
| Arbiter3_Valid_PTWReq | 31 | 22s | 仲裁器/PTW |
| Arbiter6_BoomL1MetaReadReq | 38 | 21s | 仲裁器 |
| AsyncResetRegVec_w1_i0 | 5 | 22s | 同步/异步复位 |
| AsyncResetRegVec_w2_i0 | 5 | 22s | 同步/异步复位 |
| MaxPeriodFibonacciLFSR | 36 | 22s | LFSR |
| OptimizationBarrier_EntryData | 32 | 21s | 屏障 |
| OptimizationBarrier_PTE | 24 | 22s | 屏障 |
| OptimizationBarrier_UInt | 4 | 21s | 屏障 |
| BroadcastFilter | 27 | 21s | 广播过滤 |
| IntXbar_i4_o1 | 10 | 21s | 中断交叉 |

### 3.2 MISMATCH（6）

| 模块 | 首分歧 | 失配总数 | 签名 | 判定 |
|---|---|---:|---|---|
| BoomRAS (7叶) | cyc12 `io.read_addr` | 8 (12–19) | 动态读树坍缩（§1 已实证） | lowering bug |
| MulDiv (15叶) | cyc15 `ioresp.bits.full_data` | 1 | cpp=golden 低 64 位（i128 输出，`Bits::value()` 只回 words_[0]，driver 只打印 `.value()`） | eqcheck 工具缺口，模型疑似正确 |
| PipelinedMultiplier (12叶) | cyc6 `ioresp.bits.full_data` | 16 | 同上（0xd20b52d7db03dd08 = golden 低 64 位） | 同上 |
| DivSqrtRawFN_small (29叶) | cyc4 `iorawOut.sExp` | 11 | golden=549 (0x225) vs cpp=37 (0x25)：差恰为 bit9+bit8 中的 512，算术位丢失 | 模型侧位宽/进位差异，未定因 |
| BranchDecode (11叶) | cyc0 `ioout.target` | 20 (每拍) | golden=0xd891bd0a70，cpp=0：组合锥 C++ 恒 0 | 组合发散，未定因（非读树签名） |
| PMAChecker (11叶) | cyc0 `ioresp.r` | 120 | golden=0，cpp=1：PMP 默认命中判定相反 | 组合发散，未定因 |

### 3.3 golden-error（5，evalfir 评测器缺口，C++ 侧未及验证）

| 模块 | 报错 | 缺口 |
|---|---|---|
| RVCExpander (10叶) | `EvalError: path .bits of prim mux` | mux 分支为聚合类型的字段路径求值 |
| LoopBranchPredictorColumn (+_1/_2/_3 同构，抽 1 测) (13叶) | `EvalError: lhs root subaccess`（setup 阶段） | 动态 subaccess 作 connect LHS |
| BoomProbeUnit (57叶) | `EvalError: path .state of prim mux` | 同 RVCExpander |
| BoomWritebackUnit (42叶) | `EvalError: path .opcode of prim mux` | 同 RVCExpander |
| Repeater_TLBundleA_a26d64s3k1z3u (24叶) | `EvalError: path .opcode of prim mux` | 同 RVCExpander |

注意：LoopBranchPredictorColumn 与 BoomWritebackUnit 是 §2 静态命中动态读的两员——golden 侧缺口恰好挡住了对读树 bug 最有佐证价值的两个模块。

### 3.4 infra-fail（4，runner 不支持无时钟组合模块）

| 模块 | IO叶 | 报错 |
|---|---:|---|
| CompareRecFN (7叶) | 7 | `module has no clock port; not toggling a clock` + `runner failed: stim short at cycle 0` |
| RecFNToIN (7叶) | 7 | 同上 |
| RoundAnyRawFNToRecFN (12叶) | 12 | 同上 |
| MulAddRecFNToRaw_preMul (23叶) | 23 | 同上 |

四者 FIRRTL 里都没有 `input clock`（纯组合 FPU 单元）；lowerer 给 MLIR 合成了 `pyc_clk/pyc_rst` 形参，但 stim/runner 协议按"每拍驱动输入+翻转时钟"设计，无时钟模块直接 fail。pycc/g++/runner 构建本身全部成功。

### 3.5 emit 超时与崩溃（2+1）

| 模块 | 现象 |
|---|---|
| RocketALU (9叶) | lowering 阶段 >420s 未产出 model.mlir（外层 420s 超时；ALU 译码深 mux 链） |
| MulAddRecFNToRaw_postMul (25叶) | 同上 >420s |
| TLFIFOFixer_2 (42叶) | **lowering 段错误（SIGSEGV/rc=139）**：打印 `[stats] dyn_lhs=8` 后即崩，workdir 为空（model.mlir 未落盘）——疑似 dyn-LHS 展开路径的崩溃，需单查 |

补充：RocketALU 同样无 `input clock`（纯组合 ALU），即使 lowering 完成也会撞上 §3.4 的 runner 缺口。

---

## 4. 失败模式归纳

1. **动态 subaccess 读树坍缩（§1 实证）**：id() memo + 临时对象地址复用 → 32 层读 mux 条件全部退化为 eq(idx,0)。BoomRAS 实证；13 个静态命中 leaf 同模式风险（其中 LoopBranchPredictorColumn、BoomWritebackUnit 因 golden 缺口暂无法佐证）。
2. **eqcheck >64 位比较缺口**：`Bits<W>::value()` 只返回低 64 位字（words_[0]），driver.cpp 逐叶打印 `.value()` → 所有宽于 64 位的输出叶实际只比对低 64 位（MulDiv/PipelinedMultiplier 的 i128 full_data 失配全部由此而来）。模型侧 gen.cpp 是全宽 Wire<128> 运算，疑似正确。
3. **组合锥发散（BranchDecode/PMAChecker/DivSqrtRawFN_small）**：cycle-0 或算术位级分歧，MLIR 中无读树坍缩签名（sigscan：PMAChecker 0 复用、BranchDecode 仅 1 处复用），是**新的失配家族**，需 IR 级 diff 定因（下一步：pyc trace_port 定位首错 wire，或 golden 逐 wire 对拍）。
4. **golden 评测器缺口偏科**：5/42 的失败在 golden 侧（聚合 mux 字段路径、动态 subaccess LHS），其中两个恰是动态读家族成员——evalfir 表达力落后于 lowerer，把 lowering bug 的复现面藏起来了。
5. **runner 假设有时钟**：无时钟组合模块（CompareRecFN/RecFNToIN/RoundAnyRawFNToRecFN/MulAddRecFNToRaw_preMul，另 RocketALU 也无时钟）stim 协议直接 fail——纯组合 FPU 单元目前整体不可测。
6. **lowering 性能/健壮性**：深 mux 链模块 emit >420s（RocketALU、MulAddRecFNToRaw_postMul）；TLFIFOFixer_2 在 `[stats] dyn_lhs=8` 后 SIGSEGV。

**覆盖结论**：MATCH 集中在仲裁器（10/10）、同步原语/LFSR（4/4）、OptimizationBarrier 族（5/5）——mux-of-reg、优先仲裁、hold 语义等常见模式 lowering 是稳的。失配集中在三个前沿：向量动态索引读、宽位宽输出、纯组合大锥。

---

## 5. 修复建议（按爆炸半径排序，未动代码）

1. **[最高 | 一行级 diff | 全动态读受益]** `lower.py::_resolve_value` 的 memo：`key` 改为内容寻址（如 `(str(e), w, signed, path)`，Expr 已有 `__str__`），或在 `val_memo` 里同时保存 `(e, out)` 让对象保活、id 不复用。该 bug 是**静默错译**（不报错、IR 合法、仅语义错），13 leaf + 51 non-leaf 个含动态读模块全部在射程内。
2. **[高 | eqcheck 工具]** driver.cpp 对 `kWords>=2` 的输出叶打印全部字（`word(0),word(1),…` 或全宽十六进制），eqcheck 侧按位宽重组比较——立即解锁宽输出模块的真对拍。
3. **[高 | evalfir 表达力]** 补两个缺口：mux 分支为聚合类型的字段路径（5 个模块被挡）与动态 subaccess LHS——补齐后即可验证 LoopBranchPredictorColumn/BoomWritebackUnit 的动态读路径。
4. **[中 | runner]** 无时钟模块支持：stim 协议检测到模块无时钟端口（或 pyc_clk 为合成形参）时改走纯组合拍：每拍只驱动输入 + comb settle 后采样，不做 tick。
5. **[中 | lowering 健壮性]** 排查 TLFIFOFixer_2 SIGSEGV（dyn_lhs=8 后崩溃，疑 dyn-LHS 展开路径）；RocketALU 类深 mux 链 emit 提速。
6. **[低 | 回归护栏]** 发射后结构指纹断言：仅在"同一条读链的 mux 层级"内检查 cond SSA 互异（普通 mux 复用合法，须限定链内判定），可在 pycc 之前拦截坍缩类错译。
7. **[低 | 工具]** eqcheck 失配打印上限 5 条改可配（`--max-print`）；BranchDecode/PMAChecker/DivSqrtRawFN_small 三个未定因失配立 IR-diff 专项；13 个静态命中 leaf 中超 64 IO 叶子的（CSRFile 284、RenameMapTable 156、RenameFreeList 144、PredRenameStage 917）建议给 eqcheck 加 `--outputs` 白名单后纳入。

---

## 附：产物与复现

- BoomRAS 工作目录：`/tmp/eq_ras/`（golden_trace.txt / stim.txt / model.mlir / gen.cpp / driver.cpp）
- id 冲突探针：`/tmp/probe_ras.py`（monkeypatch `_resolve_value`，不改源文件）
- 逐拍复核脚本：`/tmp/ras_table.py`
- 爆炸半径扫描：`/tmp/blast.py`
- mux 条件复用签名扫描：`/tmp/sigscan.py <model.mlir>...`
- sweep 结果：`/tmp/eq_sweep/<M>/`（每模块独立工作目录），原文日志在 sweep 作业输出（42 行 `模块|分类|耗时|明细`）
- leaf 清单：`/tmp/leafs.txt`（285 个 leaf，name/nports/nio_leaves）
