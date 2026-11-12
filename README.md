# fir2pyc — FIRRTL 直连 pyc MLIR 导入器（实验）

绕过 firtool+Yosys，把 Chisel 生成的 .fir（FIRRTL 3.x/CHIRRTL）直接 lowering
成 pyc textual MLIR。动机与可行性结论见工作区 2026-11-11 会话记录。

## 状态（2026-11-11 晚三轮，BOOM trim harness 实测）

- 解析器：BoomTrimHarness.stripped.fir（344k 行 / 571 模块）**100% 解析**，
  与文本行逐关键字对账零丢失。
- **叶子模块 285 个中 281 个（98.6%）通过 pyc-opt**；余 4 个 = 裸 UInt 孤儿
  模块守卫正确拒绝（全树模式由实例点 hint 解决）。
- **关键子树模块 16/16 全过 pyc-opt**：BoomCore（53 万行）✓ CSRFile ✓ Rob ✓
  RocketALU ✓ IntToFP ✓ TageTable/TageTable_4 ✓ TLXbar_sbus ✓ TLFIFOFixer ✓
  BoomRAS ✓ MaxPeriodFibonacciLFSR_2 ✓ PTW ✓ BTBBranchPredictorBank ✓ 等。
- **★全顶层 --top BoomTrimHarness（569 模块层级）emit 1,299,489 行 MLIR
  通过 pyc-opt（pyc 方言验证器零错误）**——跨层级裸 UInt 宽度传播（自底向上
  reverse-BFS 推断 + hint 不动点）实现；pycc 解析级错误（PYC911/932/953/
  955-957）全部消除。
- **★全顶层 pycc 完整硬件管线通过**：`pycc /tmp/f2p_top.mlir --emit=none
  --logic-depth 1024` exit 0 零错误（51768 regs / 646726 state bits，
  max_depth 513/1024）——569 模块 BOOM 设计从 FIRRTL 文本到完整 pyc 优化
  管线全绿。pycc 必须用真实输出路径（`-o /dev/null` 会因 stats.json 权限误报）。
- **★★全顶层 C++ model 159MB g++ 编译 exit 0 零错误**（`--emit=cpp
  --no-state-opt` + `-ftemplate-depth=4096`）——FIRRTL→fir2pyc→pyc MLIR→
  pycc→**可编译 C++** 完整链路历史性打通（需编译器侧 CppEmitter 修复：
  叶模块 eval_comb_pass 第二遍收敛 + 保留名/类型名遮蔽守卫）。
- **★全顶层 Verilog 产物**：`pycc --emit=verilog --logic-depth 1024` →
  top.v 58MB / 571 modules / 零错误。
- **等价对拍（cycle-accurate）**：`python3 -m src.fir2pyc.cli check <fir>
  --module M [--cycles N] [--seed S]`——evalfir golden vs pycc C++ 逐拍逐叶
  对比。已修复七个重大语义 bug（全部由对拍器驱动发现）：①寄存器 hold 语义
  （en=OR(conds)）；②id() 悬空记忆化（动态 subaccess 读树退化）；③asSInt
  扩展符号；④connect 顺序语义（first-wins→**last-wins**）；⑤propagate_bits
  宽度膨胀（bits() 约束加宽显式声明 decl→shl 截断回绕）；⑥evalfir cvt 无符号
  重解释；⑦wire 根 bulk connect flipped 深叶正向驱动（组合环）。MATCH 面
  **★285/285 叶模块 100% 等价验证通过**（四轮扫面：sweep1 42 + sweep2 99 +
  sweep3 60 + sweep4 96；最后两个例外也已修复——MemExeUnit_1 双根因清零
  （evalfir mux SInt 符号 pad + pycc 运行时 sext<65,64> bug 绕过）、
  BoomIOMSHR state-optimize greedy 收敛修复）。累计 14 个重大语义/工具 bug
  由对拍驱动修复 + 4 个 pycc 编译器缺陷。见 docs/eqcheck_sweep4.md。
- pycc 编译器侧修复（pyCircuit-dev 工作树，未提交）：SLPPackWiresPass
  maxIterations 10→100 + 失败时 emitError（静默失败已消除）；中断合并
  （retime-uncapped→dev）已解冲突可构建。

## RocketChip 支持与实测（2026-11-12）

FIRRTL 1.x 兼容层已落地（parser）：`<=`/`<-` 连接、`is invalid`、
`reg ... with :` reset 块、printf/assert 跳过、`UInt<W>("h..")` 字面量、
字面量下标折叠、数字 bundle 字段名（.0 / { 0 : ... }）、关键字撞名线
（reg/write/invalidate 作标识符）、裸宽 reg init 宽度下界入 LB 约束。

RocketChip DefaultConfig（freechips.rocketchip.system，FIRRTL 1.1.0，
628 模块 / 15.8 万语句）实测：
- 解析 100%；
- 叶子 **219/220（99.5%）** 过 pyc-opt（唯一失败 = OptimizationBarrier_42
  孤儿裸 UInt 守卫拒绝，与 BOOM 同族）；
- **全顶层 TestHarness 58.6 万行 MLIR 过 pyc-opt + pycc 完整管线零错误**
 （--logic-depth 1024，29054 regs / 378575 bits，3 个 stub 模块）；
- 等价对拍抽样 **11 PASS / 2 FAIL**（CSRFile：`reset=>(reset,lit)` 状态族
  + iotime/bp.control 反向差异；DMIToTL：cycle-0 resp.valid 组合族——
  均记录待查）；
- BOOM 全量回归零影响（281/285 + LFSR_2/BoomRAS/CSRFile/DecodeUnit
  check 全 PASS）。

## 用法

```bash
cd fir2pyc
# 解析统计
python3 -m src.fir2pyc.cli parse <file.fir>
# 单模块发射 + 工具链验证
python3 -m src.fir2pyc.cli emit <file.fir> --module <Mod> -o out.mlir
pyc-opt out.mlir -o /dev/null
# 全电路（层级 instance，自底向上推断）
python3 -m src.fir2pyc.cli emit <file.fir> --top <Top> [--stub-oversized-mems] -o top.mlir
# 批量叶子模块 emit+pyc-opt
python3 tools/masstest.py <file.fir>
```

pycc 工具链：`pyCircuit-dev/.pycircuit_out/toolchain/install/bin/{pycc,pyc-opt}`。
发射契约：`docs/pyc_emit_contract.md`（631 行，全部实测）。

## 架构

- `src/fir2pyc/fir_parser.py` — FIRRTL 3.3.0 文本解析器（行式 + 缩进块）
- `src/fir2pyc/lower.py` — lowering 引擎：
  - 类型拍平（flip XOR）+ 聚合/向量/bundle 展开
  - last-connect-wins 折叠 + 双向聚合 connect（FIRRTL 3 flipped 成员反向）
  - 30 种原语映射（含有符号/无符号选择、动态移位、popc 等）
  - 跨层级裸 UInt 宽度推断（自底向上 + hint 不动点 + 孤儿兜底）
  - 层级 instance（pyc.Instance）+ 内存寄存器堆降级
  - v2p 复位 tie-off 约定（%pyc_rst/%pyc_clk tie-off）
- `src/fir2pyc/evalfir.py` — 周期级参考求值器（对拍 golden）
- `tools/masstest.py` — 批量 emit+pyc-opt

## 已知边界（v1）

- >1024 深度 SRAM 需 byte_mem/sync_mem 真映射（现 --stub-oversized-mems 读 0）
- 跨模块裸 UInt 宽度同步（CSRFile 实例 io.mcontext 在 BoomCore 中 i-1）
  需全局 InferWidths pass
- evalfir 等价对拍未接入 cli
