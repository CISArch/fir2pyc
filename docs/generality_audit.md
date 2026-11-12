# fir2pyc 通用性审计（design-specific 硬编码 vs. 通用性）

日期：2026-11-12 · 范围：`src/fir2pyc/{fir_parser,lower,evalfir,eqcheck,cli}.py`、`tools/masstest.py`
方法：全量通读 + 名称/常量 grep 逐条分类 + 第三设计（riscv-mini Tile.fir）实测。
性质：只读审计，未改动任何源文件（验证补丁在 /tmp 副本上完成，见 §5）。

## 结论（TL;DR）

1. **无任何按模块名的行为特判**（category iii 按名称 = 0 条）。所有按名称的命中都是
   env 门控的调试钩子或注释。核心语义按结构（宽度/条件）分流。
2. 但存在 **3 处按结构的设计调参默认值**，在第三设计上行为不保守：
   `lower_circuit` 无条件 `ORPHAN_FB=True`（CLI 旗标失效）、`_wire_dep_order`
   的 4000 叶阈值（大模块跳过正确性排序）、byte_mem 单写口限制（深内存多写口
   静默 stub 化）。
3. **实测第三设计（riscv-mini）发现 1 个真实语义 bug**：SInt mux 结果扩宽时
   零扩展（`lower.py:2352`），BOOM 285/285 与 Rocket 抽样均未覆盖到该模式；
   单行修复已在对拍中验证（ImmGenWire FAIL→PASS，Rocket 6 叶 + riscv-mini
   CSR/Control 无回归）。
4. 解析/emit/pyc-opt 对 riscv-mini **12/12 模块全过**（含全顶层 93,596 行）；
   对拍 5 个可检叶中 4 PASS、1 FAIL（即上述 bug）。

---

## 1. 命中清单（hit inventory）

分类：**(i)** env 门控调试钩子 · **(ii)** 注释/docstring · **(iii)** 行为特判（BUG）·
**(h)** 通用启发式（结构键控）· **(w)** 针对上游缺陷的结构键控 workaround ·
**(p)** 机器路径/环境默认值 · **(c)** 代码整洁度问题。

| 位置 | 内容 | 分类 | 说明 |
|---|---|---|---|
| lower.py:588 | `F2P_TRACE and mod.name == 'TageTable'` | (i) | PBITS 宽度传播 trace；**按 BOOM 模块名过滤** |
| lower.py:598 | `F2P_DEBUG and mod.name == 'TageTable'` | (i) | propagate 栈 trace；按名过滤 |
| lower.py:705 | `F2P_DEBUG and mod.name == 'TageTable'` | (i) | 约束 dump，内部再按信号名 `'_tag_T_3'` 过滤（BOOM 信号名） |
| lower.py:759-766 | `F2P_DEBUG and mod.name == 'TageTable'` | (i) | LB dump；过滤 `'tag'/'ghist'/'update'` 并硬编码 `'_tag_T','_tag_T_1','_tag_T_2','_tag_T_3','tag_history','tag_history_1','update_tag'`（全部 BOOM TageTable 信号名） |
| lower.py:1883 | `F2P_TRACE`（无过滤） | (i) | CLKV 时钟类解析 trace，通用 |
| lower.py:1967 | `F2P_TRACE`（无过滤） | (i) | 零宽/孤儿叶 fold trace，通用 |
| lower.py:2019 | `F2P_TRACE`（无过滤） | (i) | resolve 宽度≤0 trace，通用 |
| lower.py:1404 | `# CSRFile reg_misa losing its reset value...` | (ii) | 注释：解释 en=0 空驱动寄存器 hold 语义的动机 |
| lower.py:1524 | `# (DecodeUnit: uop.* imm cone read cs.imm_sel ...)` | (ii) | 注释：解释 wire 依赖排序的正确性动机 |
| lower.py:652-653, evalfir.py:600-603 | `DivSqrtRawFN_small ... bug` | (ii) | 注释：cvt 无符号零扩展、bits 约束不回写显式宽度的回归出处 |
| fir_parser.py:4 | docstring "verified against the BOOM trim harness" | (ii) | 文档性 |
| lower.py:56 | `MAX_WIDTH = 16384` | (h) | dshl 宽度 sanity 上限；超出显式报错（不静默） |
| lower.py:58 + cli.py:118 | `MAX_BANK_DEPTH = 1024` / `--mem-reg-bank-cap 1024` | (h→真问题) | ≤cap 深度走 reg-bank（多写口正确）；>cap 走 byte_mem（**单写口限制**，见 §2.3） |
| lower.py:267, 2632 | `dshl` 移位量宽度 `min(bwidth, 12)` 截断 | (h) | 宽度计算钳制；evalfir 不钳 → 极端设计两侧口径不一致（见 §4） |
| lower.py:1609 | `if len(wire_keys) <= 4000:` 才做拓扑排序 | (h→真问题) | 大模块跳过 wire 依赖排序（见 §2.2） |
| lower.py:2779-2780 | `global ORPHAN_FB; ORPHAN_FB = True` | (h→真问题) | 全电路 emit 无条件孤儿宽度=1（见 §2.1） |
| lower.py:1330-1331 | `_emit_wires` 中 width<1 硬置 1 | (h) | "no i-1/i0 emission"；与 ORPHAN_FB 叠加后孤儿叶永不报错 |
| lower.py:1469 | `params.get("DEFAULT", "0")` extmodule 输出置常量 | (h) | 按**参数名** `DEFAULT`（rocket-chip plusarg 约定）；无该参则输出恒 0，`extmod_inst` 计数+note |
| lower.py:2680-2702 | `sext` 且 `from_w % 64 == 0` 时改走 extract+concat | (w) | **结构键控**（宽度%64），绕过 pycc C++ `sext<Out,In>` 低字 clobber 缺陷；非名称键控 ✓ |
| lower.py:1943-1955 | 常量 >64 位拆 concat | (w) | 结构键控（值宽度），pyc.constant 解析器崩溃规避 ✓ |
| lower.py:2822-2889 | emit 失败 → 常量 0 stub（note 记录） | (w) | 结构键控（任意 LowerError）；见 §2.4 |
| lower.py:2077-2079 | `_chain_has_dyn` return 后不可达死代码 | (c) | 无害残留 |
| lower.py:2890+2893-2895 | `stats_total` 两轮累加重复计数 ×2 | (c) | 仅统计上报翻倍 |
| cli.py:14,27 | 512 MiB 大栈线程 + recursionlimit 300000 | (h) | 通用深递归防护 ✓ |
| cli.py:133-136 | `--logic-depth 1024`（pycc 透传） | (h) | 通用（扁平 mux/decode 链深度），文档写明动机 ✓ |
| eqcheck.py:41-42 | `DEFAULT_PYCC = /home/lidongzhe/.../pyCircuit-dev/...` | (p) | 机器特定默认路径，`PYCC` 环境变量可覆盖 |
| masstest.py:35-38 | `PYC` 同上默认 | (p) | 同上，`PYC` 可覆盖 |

grep 覆盖的名称（`Boom/TageTable/CSRFile/Rocket/Rename/PredRename/DecodeUnit/TLXbar/CLINT/AXI4/TilePRCI/ExampleRocket/TestHarness/DefaultConfig/LFSR/BoomRAS/FPU` 等，词边界精确匹配）：除上表所列外**零命中**。

## 2. 真阳性清单（实际影响任意设计的行为）

### 2.1 `lower_circuit` 无条件 `ORPHAN_FB = True`（最重的策略问题）
- lower.py:2779-2780：全电路 emit 路径把模块级旗标强制置 True，注释称
  "sound zero-extend"；但 `--orphan-fallback` 的 CLI 帮助写 **"(unsound)"**，
  且 `lower_circuit(orphan_width_fallback=...)` 形参**从未被读取**（死参数）。
- 后果：跨层级裸 UInt 推断（16 轮 fixpoint + 4 轮 hint 传播）仍解不出的宽度
  被**静默置 1**（`default_unresolved_widths`，lower.py:2813-2815 无条件调用）。
  BOOM/Rocket 上推断几乎全覆盖、余量无害；第三设计若裸宽度依赖更深/更长的
  传播链（hint 链 4 轮为界，层级 >4 的逐层新证据不收敛），会得到**错误的 1 位
  信号**而非报错。孤儿宽度默认行为对任意设计不保守。
- 另：`ORPHAN_FB` 是进程级全局，一次 `lower_circuit` 之后同进程内所有后续
  单模块 emit 也被污染。

### 2.2 `_wire_dep_order` 的 4000 叶阈值（正确性让位于性能）
- lower.py:1609：wire 叶 >4000 时跳过拓扑排序、维持注册序。排序本身的动机是
  **正确性**（lower.py:1515-1527 注释：pycc 对叶模块 fuse-comb 成环时按 IR
  行序求值，consumer-before-producer 会采样到 stale wire 值——DecodeUnit 案例）。
- 注释称"巨型层级模块 comb 分段不同、注册序无妨"，但该断言依赖 pycc 当前
  的分段行为，且 BOOM 最大的可对拍叶恰好 <4000。一个 wire 叶 >4000 的**叶
  模块**（大 PLA/大译码器，第三设计完全可能）会退回未排序路径，潜在错值
  且只有 eqcheck 能发现。
- 设计调参默认值，正确性风险未在 note 中计数。

### 2.3 byte_mem 单写口限制：深内存多写口静默 stub
- lower.py:1731-1732：同一字段 >1 写口 → `_RealMemCycle` → 捕获后
  `reads tied 0, writes dropped`（lower.py:1818-1826/1848-1853，仅 note）。
- 深度 >1024 的多写口存储（如双口 Tag/Data array）在第三设计上会**静默失去
  存储语义**；reg-bank 路径（≤1024）多写口是正确的（decl 序 mux fold）。
  阈值两侧语义能力不一致，是按 BOOM/Rocket 内存形状（≤1024 或单写口）调的。

### 2.4 emit 失败 stub 回退：管道连续性 > 显式失败
- lower.py:2822-2889：任一模块 emit 抛 LowerError → 该模块替换为常量 0 stub，
  全电路继续。对已知两设计这换来了"全顶层可编译"；对任意新设计，一条 stderr
  `[emit_error]` 之外**没有任何失败信号**，仿真结果静默错误。属策略取舍而非
  bug，但默认值对新设计不保守（建议聚合为退出码/汇总告警）。

### 2.5 【实测发现的语义 bug】SInt mux 结果扩宽零扩展（lower.py:2352）
- 代码：`return self._extend(ssa, otw, False, w, signed)` —— mux 结果恒按
  **无符号**扩展。当 mux 臂为 SInt 且随后被更宽的 mux 臂（如 SInt<32>）扩宽时，
  `_extend(..., from_s=False)` 发出 `zext`，FIRRTL 语义应为 `sext`
  （CIRCT MuxPad：结果 SInt 时按符号扩展）。
- 触发模式（riscv-mini ImmGen）：`mux(sel, SInt<32> Uimm, SInt<12> 链)`，
  负立即数被零扩展进 32 位 → `golden=0xfffffc38 vs cpp=0x00000c38`。
  BOOM 285/285、Rocket 抽样均未命中该模式——**两设计对拍通过≠语义正确**，
  是本次审计"过拟合"最直接的实证。
- 已验证修复（/tmp 补丁副本，未改仓库）：
  `_extend(ssa, otw, ot.kind == "sint", w, signed)`；Mini3 复现 FAIL→PASS、
  ImmGenWire FAIL→PASS、riscv-mini CSR/Control PASS、Rocket 6 叶 PASS（零回归）。

## 3. 上游缺陷 workaround 核验（任务 §c）

| workaround | 键控方式 | 结论 |
|---|---|---|
| `sext<64 倍数>` 绕过（lower.py:2680-2702） | `from_s and from_w % 64 == 0`（**纯宽度结构**） | ✓ 非名称键控；README 记为 pycc `sext<65,64>` 运行时缺陷 |
| 常量 >64 位 concat 拆分（lower.py:1943） | 值位宽 | ✓ 结构键控 |
| emit 失败 stub（lower.py:2822） | 任意 LowerError | ✓ 结构键控（策略问题见 §2.4） |
| F2P_TRACE/F2P_DEBUG 钩子 | 7 处中 **4 处按 `mod.name=='TageTable'` 过滤**（588/598/705/759），3 处通用（1883/1967/2019） | 调试专用、零行为影响；但在第三设计上 `F2P_DEBUG` 对这 4 处**无输出**（含 707 行 `'_tag_T_3'`、762-766 行 tag/ghist/update 信号名过滤） |

## 4. FIRRTL 特性覆盖缺口（第三个设计会撞上的）

解析层（fir_parser.py，缺项全部**显式报错**，无静默降级 ✓）：
- `attach` / Analog 连线：不支持（"unknown primop 'attach'"）→ 含模拟连线的
  设计直接失败。
- 新式 `mem` 块语句（LowerCHIRRTL 之后的 FIRRTL，如 `boom.low.fir` 那类）：
  只支持 CHIRRTL `cmem/smem/mport`；Chisel 直出 .fir 无碍，跑过 firtool 的
  low-FIRRTL 不行。
- Probe/RWProbe：类型解析为不透明标量，`define`/probe 读写不支持（BOOM 实测
  零 Probe 语句；Chisel6 probe-heavy 设计会撞）。
- `printf/stop/assert/assume/cover`：**静默跳过**（fir_parser.py:668-669,
  858-861）→ 断言语义丢失，eqcheck 只比对输出端口，仿真可观测性（printf）
  不可用；对正确性无影响但降低可信度。
- `public module`（新 spec）、tokenizer 字符串转义 `\"`：不支持（小项）。
- primop 白名单含 `asFixedPoint/asInterval/precise`（fir_parser.py:385-391），
  但 lower `_prim_type` 对它们 `unknown primop` 显式拒绝 → **FixedPoint 设计
  明确不支持**（诚实失败）。

Lowering 层：
- `readwrite/irread/iread` mport：parser 接受，读路径 raise（lower.py:1811,
  2335）→ 全电路模式退化为 §2.4 stub。Chisel 常规发射为 infer/read/write，
  但 readwriter 存在于 CHIRRTL 语义中。
- 时钟：所有 reg/内存绑到**主时钟**（第一个 clock 端口）；非直通 clockish
  驱动、`asClock(data)`、多时钟域全部近似为 primary（approx_clock 计数）。
  eqcheck 驱动只翻转第一个时钟（eqcheck.py:334-335）→ 多时钟设计对拍无意义。
- `asUInt(clock)` / `asSInt(reset)` 显式拒绝（lower.py:2417, 2423）。
- mem port 上的动态 subaccess 读：拒绝（dyn_read_err，lower.py:2108-2113）。
- 聚合值 prim（mux 以外部）在叶路径下：拒绝（"cannot take path of primop"）。
- 宽度推断：三个有界 fixpoint（模块内 16 轮、evalfir 16 轮、层级 hint 4 轮）
  + 孤儿置 1（§2.1）；`dshl` 移位量宽度按 12 位钳制（lower.py:267/2632）而
  evalfir 不钳——移位量 ≥13 位的极端设计两侧宽度口径不一致（对拍潜在假阳/假阴）。
- extmodule：无外部 Verilog 集成（与 verilog2pyc 不同，无 Yosys 路径）；
  输出=DEFAULT 参数/0。参数按**名字** `DEFAULT`（rocket-chip plusarg 约定）
  ——通用化程度：对含行为宏（RAM 模型等）的第三设计功能错误，但有计数。
- `_lhs_leaf_list` 捕获一切 `_leaf_kind` LowerError 归为 "rev"（lower.py:
  1032-1036, 1083-1088）：驱动 module input port / instance output 的非法
  连接被**静默丢弃**而非报错（诊断性问题；本次 malformed 复现中实测确认）。

evalfir（golden）层：
- 叶模块 only（inst/mem 即拒，eqcheck 无法对拍层级/存储模块）。
- `asClock/asAsyncReset/asFixedPoint/asInterval/validif/precise` 无实现
  （eval_prim 尾部 raise）；lower 支持 `validif` 而 golden 不支持 → 含
  validif 的 1.x 设计对拍直接 golden 侧失败。
- 聚合值 prim 仅 mux（op_path_ok）。

## 5. 第三设计实测（riscv-mini，papers/2020-essent .../generated-src/Tile.fir）

FIRRTL 1.x 风格（无版本头、`<=` 连接）、12 模块 / 2219 语句 / 2970 行：

| 阶段 | 结果 |
|---|---|
| `parse` | **OK**：12 模块全部解析，2219 语句 |
| `emit --module M` + `pyc-opt` | **12/12 PASS**：RegFile 465 行、CSR 871、Control 897、Datapath 1,995、Cache/Cache_1 45,247（mem reg-bank 展开）、Core 2,899、**全顶层 Tile 93,596 行全过** |
| `check`（对拍，20 拍） | AluArea **PASS**、BrCondArea **PASS**、CSR **PASS**（cycle1 起）、Control **PASS**、**ImmGenWire FAIL**（§2.5 bug）；RegFile/Cache 含 mem、Datapath/Core/MemArbiter 含 inst → golden 叶限不可检 |

ImmGenWire 失败最小化为 30 行复现（/tmp/f2p_repro/mini3.fir 模式）：
`%v25 = pyc.zext %v24 : i12 -> i32`（应为 sext）。单行修复验证：
ImmGenWire/复现 FAIL→PASS；CSR/Control（riscv-mini）与 Rocket 6 个叶模块
（IntXbar、NonSyncResetSynchronizer×2、SimpleClockGroupSource、
ClockGroupAggregator、ClockGroup）补丁后全 PASS，零回归。
（修复仅验证于 /tmp 副本，仓库未动。）

**总体判定**：结构与解析层通用性良好（第三设计零适配全过 emit/opt）；
语义层存在 1 个被两设计侥幸覆盖的真 bug（§2.5，已给最小修复）与 3 处
设计调参默认值（§2.1-2.3）；FIRRTL 覆盖缺口集中在 attach/analog、新式 mem、
probe、多时钟、readwrite 口与 FixedPoint。

## 6. 泛化建议（按收益/风险排序）

1. **修 lower.py:2352**（一行，已验证）：mux 结果按 `ot.kind=="sint"` 扩展。
   合入前建议跑一次 BOOM 285 叶全量对拍回归（本审计只抽测了 Rocket 6 叶）。
2. **ORPHAN_FB 语义修正**（lower.py:2779-2815）：让 `orphan_width_fallback`
   形参真正生效；全电路模式默认改为"孤儿宽度>0 个 → 显式失败（或至少
   note 汇总 + 非零告警退出）"，置 1 降级保留为显式 `--orphan-fallback`。
   同步修正 CLI 帮助与代码注释互相矛盾的 "sound/unsound" 表述。
3. **wire 排序阈值改为"计数的近似"**（lower.py:1609）：>4000 时仍排序但改用
   增量/缓存策略，或至少 note("dep_order_skipped") 计数暴露风险；文档写明
   大叶模块可能需要手动分段验证。
4. **byte_mem 多写口**：>1 写口字段先尝试按 decl 序 fold 成单写口（复用
   reg-bank 的 fold 逻辑），真正不支持再 stub；stub 时在 `emit` 返回值/退出
   码层面可见（目前仅 stderr note）。
5. **emit 失败 stub 聚合上报**：`lower_circuit` 结束时若有 `emit_errors`
   非空，输出汇总并在 CLI 层给非零退出码（或 `--strict` 旗标），避免新设计
   静默拿到常量 0 模型。
6. **调试钩子去名化**（lower.py:588/598/705/759）：`F2P_TRACE`/`F2P_DEBUG`
   的模块过滤改为 `F2P_MOD=<name>` 环境变量；`'_tag_T*'` 等信号名过滤改为
   `F2P_SIG=<substr>`。纯调试体验改进，零行为风险。
7. **dshl 口径统一**：evalfir 与 lower 用同一移位量宽度钳制（或都不钳、
   超 MAX_WIDTH 显式报错），消除 golden/model 宽度口径分叉。
8. **FIRRTL 覆盖路线**（按第三设计出现概率排序）：`readwrite` mport 读、
   新式 `mem` 块语句、多时钟（至少"每 reg 绑自己的 clock"而非 primary）、
   attach/analog（显式拒绝亦可，但需在文档列明）、evalfir 补 `validif`。
9. 小项：清理 lower.py:2077-2079 死代码；修 stats 双计（2890/2893）；
   masstest/eqcheck 默认工具链路径改为相对工作区解析或必须显式传入。
