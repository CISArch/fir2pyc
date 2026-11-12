# fir2pyc 仿真性能基线（DecodeUnit / CSRFile / FetchBuffer @ -O2）

日期：2026-11-12
对象：`/tmp/boom_trim.fir`（BOOM trim harness）3 个代表性叶模块（等价性已 285/285 全 PASS）
工具链：pycc @ `pyCircuit-dev/.pycircuit_out/toolchain/install/bin/pycc`（LLVM 19.1.7）
编译：`g++ (GCC) 11.5.0 -std=c++20 -O2 -ftemplate-depth=4096`
主机：INTEL(R) XEON(R) GOLD 6530，32 核，128GB RAM；计时均串行独占
产物：`/tmp/perf/`（`<M>_{sopt,nosopt}/` 各含 model.mlir / gen.cpp / perf_driver.cpp / perf_runner）

---

## TL;DR

- 三个叶模块 C++ 模型（-O2）吞吐：**DecodeUnit ≈ 84.1万 cycles/s，CSRFile ≈ 22.4万（state-opt）/ 20.4万（no-state-opt），FetchBuffer ≈ 7.85万 / 7.52万 cycles/s**。
- 对 evalfir 纯 Python golden 评测器的加速比：**DecodeUnit ≈ 3.2万×，CSRFile ≈ 1.5万×，FetchBuffer ≈ 8.9千×**（golden 仅 26.6 / 15.3 / 8.9 cycles/s）。
- state-opt（structural，greedy 修复后）在单模块上全部可用（pycc exit 0）：CSRFile regs 235→141（合并 120）带来 **+10.0%**；FetchBuffer regs 不变仍有 +4.4%；DecodeUnit 纯组合，两种模式 gen.cpp 逐字节相同。
- 3 次重复的波动均 <1%（最差 FetchBuffer 0.3%），数据稳定。

## 1. 数据表

C++：N=1,000,000 cycles，xorshift64 随机激励，3 次取中位；golden：evalfir 纯 Python 解释器同激励口径（400/250/150 cycles，2 次，中位）。

| 模块 | 类型 | out/in 叶数 | regs（sopt→nosopt） | C++ cycles/s（state-opt） | C++ cycles/s（--no-state-opt） | golden Python cycles/s | 加速比（vs sopt） |
|---|---|---|---|---|---|---|---|
| DecodeUnit | 纯组合译码器 | 112 / 163 | 0 → 0（gen.cpp 逐字节相同） | **840,774** | 840,734 | 26.6 | **≈31,600×** |
| CSRFile | CSR 文件（有状态，双时钟域） | 236 / 44 | 141 (2783b) → 235 (3191b)，merged=120 | **224,281** | 203,929 | 15.3 | **≈14,700×** |
| FetchBuffer | 队列（有状态） | 226 / 71 | 1779 (6793b) → 1779（不变） | **78,531** | 75,196 | 8.9 | **≈8,870×** |

state-opt 增益：CSRFile **+10.0%**（235→141 regs）、FetchBuffer **+4.4%**（regs 不变，差异来自 structural 管线的调度/发射差异，gen.cpp 2,088,453 vs 2,084,056 字节）、DecodeUnit ±0（无状态）。

原始 3 次数据（cycles/s）：

| 变体 | run1 | run2 | run3 | 中位 |
|---|---|---|---|---|
| DecodeUnit_sopt | 840,906.8 | 840,773.7 | 840,704.7 | 840,773.7 |
| DecodeUnit_nosopt | 840,786.2 | 840,733.9 | 840,706.2 | 840,733.9 |
| CSRFile_sopt | 223,841.3 | 224,281.2 | 224,359.8 | 224,281.2 |
| CSRFile_nosopt | 203,450.9 | 204,315.0 | 203,929.1 | 203,929.1 |
| FetchBuffer_sopt | 78,528.3 | 78,530.9 | 78,749.2 | 78,530.9 |
| FetchBuffer_nosopt | 74,648.5 | 75,214.3 | 75,196.2 | 75,196.2 |

100k cycles 校准跑（任务书默认 N）与 1M 数字一致（DecodeUnit 840,264 / CSRFile 224,227·203,920 / FetchBuffer 78,492·75,203）。

## 2. 方法学

- **MLIR 发射**：`python3 -m src.fir2pyc.cli emit /tmp/boom_trim.fir --module M -o /tmp/perf/M.mlir`（每个变体独立目录，`{M}_sopt` 默认管线 / `{M}_nosopt` 加 `--no-state-opt`）。
- **pycc**：`pycc model.mlir --emit=cpp --logic-depth 1024 [-o gen.cpp | --no-state-opt]`，6 个变体全部 exit 0（含 CSRFile state-opt——greedy maxIterations 修复后可用）。
- **性能 driver**：改编自 `src/fir2pyc/eqcheck.py` 的 DRIVER_TEMPLATE（生成器 `/tmp/perf/gen_driver.py`，未改 src/）——周期结构逐句相同：drive → `comb()` → 采样（XOR 进 sink，防死码删除）→ `clk=1; tick(); commit()` → `clk=0; tick(); commit()` → `comb()`；激励由 C++ 内联 xorshift64 生成（每输入叶每拍一个新随机字，reset 类端口仅 cycle 0 置 1），**无逐拍打印**；`steady_clock` 只计 N 拍主循环。
- **golden**：同一 `evalfir.ModuleEval`（等价验证的 golden 评测器），Python `random.Random(1)` 同分布激励，`reset_cycle` + `cycle` 计时；它解释执行整模块图，是参考语义实现而非优化实现。
- **口径注意**：CSRFile 的 MLIR 含两个 `!pyc.clock` 参数，driver 只翻转第一个（`clock`），与 eqcheck 验证 285/285 时的约定一致；宽叶（>64b）按 eqcheck 的逐 64 位字约定驱动。
- DecodeUnit 两种 state-opt 模式 gen.cpp 逐字节相同（`cmp` 验证），表中两列即同一二进制的重复测量。

## 3. 全顶层 BoomTrimHarness（-O0）：**未获得有效数据点——产物为空壳，见下**

- 历史 `/tmp/f2p_out/top_gen3.o`（2,688 字节）只含静态初始化符号，本就不可链接；本次用 `main_top.cpp`（`#include "top_gen3.cpp"`，空激励周期，eqcheck 周期结构）重新编译：`g++ -std=c++20 -O0 -ftemplate-depth=4096`，450s / 峰值 8.25GB RAM，exit 0。
- **但产物二进制只含 64KB .text**（ BoomTrimHarness + ExampleRocketSystem 两级 ~29 个符号），跑出 300k cycles/s——不是全设计数字。逐层核查结论：
  1. `.fir` 侧 `BoomTrimHarness` 实例化 `ldut of ExampleRocketSystem`，`ExampleRocketSystem` 实例化 **34 个子模块**（SystemBus/TilePRCIDomain/MemoryBus/FrontBus…，全 .fir 共 561 条 inst）；
  2. 顶层 MLIR（`/tmp/f2p_top_opt.mlir`，571 个 func.func）**正常**——`@ExampleRocketSystem` 函数体内含 527 个 `pyc.instance`（含 `callee = @SystemBus` 等）；
  3. C++ 产物（159MB `top_gen3.cpp`，571 个 struct）里 **`ExampleRocketSystem` 的 struct（646749–647183 行，仅 435 行）没有任何 Sub-modules / unique_ptr**：全文件 `make_unique<SystemBus|MemoryBus|FrontBus|PeripheryBus_pbus|ClockSinkDomain>` = 0 处；其余 569 个 struct 逻辑完整（全文件 264 处 make_unique），但没有任何从 BoomTrimHarness 可达的路径——**运行期整棵设计树悬空，模型实际是空壳**。
- 归因：实例树在 **pycc `--emit=cpp` 后端**被静默丢弃（MLIR 有、C++ 无；其他中间层 struct 的子模块正常物化，唯独 `@ExampleRocketSystem` 这一层全丢）。这是 fir2pyc 全顶层 C++ 产物的**实质缺口**：三产物"齐备"仅指文件生成成功，顶层 C++ 并不包含设计本体，全顶层性能数据点在修复前不可得。pycc stats（regs=51768/646726 bits）统计的是整个 MLIR 文件而非可达树，此前易被误读为"模型完整"。
- 附带事实：`top_gen3.cpp` 全量 g++ -O0 编译需 450s / 8.25GB 峰值内存（124GB 机器可承受）；修好后全顶层数据点可直接用 `/tmp/perf/main_top.cpp` 复测。

## 4. 与工作区已知数字的对比（定性）

- **verilog2pyc 路径**：pe_cluster 级设计约 **74万 inst/simcycle**——与本报告的单叶模块 cycles/s 不是同一设计、同一指标，只能定性对照：fir2pyc 单叶模块（-O2）落在 7.5万～84万 cycles/s 区间，量级相当。
- **ESSENT r18 Rocket（C++）**：dhrystone 实测 **12.6k cycles/s**——那是整核设计 vs 本报告叶模块，不可直接比；BOOM 全顶层 C++ 数据点当前不可得（§3：产物为空壳，待 pycc 修复）。
- **auto_perf（pyCircuit 主线，34 trace geomean）**：dev 分支 5783 cycles/s、dirty-consume 12,029 cycles/s——同为整设计口径。叶模块吞吐高 1～2 个数量级属正常（实例层级、脏位唤醒在空激励/随机激励下开销占比不同）。
- **对 evalfir golden 的 0.9万～3.2万× 加速**是「编译 C++ vs 纯 Python 解释器」的headline数字，反映的是把 FIRRTL 落到 pyc MLIR → pycc C++ 这条路径相对参考解释器的收益，不代表对其他 C++ 路径的优势。

## 5. 复现

```bash
cd fir2pyc
for M in DecodeUnit CSRFile FetchBuffer; do
  python3 -m src.fir2pyc.cli emit /tmp/boom_trim.fir --module $M -o /tmp/perf/$M.mlir
done
# pycc（sopt / nosopt 两变体，见 /tmp/perf/*/{model.mlir,gen.cpp}）
PYCC=pyCircuit-dev/.pycircuit_out/toolchain/install/bin/pycc
$PYCC /tmp/perf/CSRFile_sopt/model.mlir --emit=cpp --logic-depth 1024 -o /tmp/perf/CSRFile_sopt/gen.cpp
$PYCC /tmp/perf/CSRFile_nosopt/model.mlir --emit=cpp --logic-depth 1024 --no-state-opt -o /tmp/perf/CSRFile_nosopt/gen.cpp
# driver 生成 + 编译 + 运行
python3 /tmp/perf/gen_driver.py --module CSRFile --gen-dir /tmp/perf/CSRFile_sopt
g++ -std=c++20 -O2 -ftemplate-depth=4096 -I<install>/include -I/tmp/perf/CSRFile_sopt \
    /tmp/perf/CSRFile_sopt/perf_driver.cpp -o /tmp/perf/CSRFile_sopt/perf_runner
/tmp/perf/CSRFile_sopt/perf_runner 1000000    # ×3 取中位
# golden Python
python3 /tmp/perf/gen_driver.py --module CSRFile --golden 250
```
