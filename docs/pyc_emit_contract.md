# pyc textual MLIR 发射契约（手写 IR 导入器规范）

> 适用 checkout：`pyCircuit-dev`（dev 分支，checkout 038eead9；pyc4.0 / 0.40）。
> 工具链：`/home/lidongzhe/pycircuit-workspace/pyCircuit-dev/.pycircuit_out/toolchain/install/bin/{pycc,pyc-opt}`（LLVM/MLIR 19）。
> 本文档所有语法行均用该 `pyc-opt` 实际解析验证过（`pyc-opt <file> -o /dev/null; echo $?` 退出码 0），来源已逐条标注。
> 参考实现：`verilog2pyc/v2p/irgen.py`（能发射可运行 MLIR 的现成导入器）。

## 0. 三十秒速查

```mlir
module attributes {pyc.top = @Top, pyc.frontend.contract = "pycircuit"} {
  func.func @Top(%clk: !pyc.clock, %rst: !pyc.reset, %x: i4, %y: i4) -> i4
      attributes {arg_names = ["clk", "rst", "x", "y"], result_names = ["out"],
                  pyc.kind = "module", pyc.inline = "false", pyc.params = "{}", pyc.base = "Top",
                  pyc.struct.metrics = "{...}", pyc.struct.collections = "[]"} {
    %sum = pyc.add %x, %y : i4, i4 -> i4
    return %sum : i4
  }
}
```

验证命令：

```bash
PYC=/home/lidongzhe/pycircuit-workspace/pyCircuit-dev/.pycircuit_out/toolchain/install/bin
$PY/pyc-opt top.mlir -o /dev/null          # 仅解析+内置 verifier（最快语法检查）
$PY/pycc    top.mlir --emit=none           # 完整默认 pass 管线（含前端契约检查），不发输出
```

## 1. 类型语法

`pyc` 方言**只有两个自定义类型**，数据通路全部用 MLIR 标准 `iN` / `vector<...>`。
**没有 `!pyc.bits<N>`，也没有 signed/unsigned 类型区分**——有符号性是**运算符选择**（`pyc.slt` vs `pyc.ult`、`pyc.sdiv` vs `pyc.udiv`、`pyc.sext` vs `pyc.zext`）表达出来的，类型本身无符号标签。（依据：`compiler/mlir/lib/Dialect/PYC/PYCDialect.cpp:28-53` 的 `parseType` 只认 `clock`/`reset`；`PYCTypes.td` 只定义这两个类型。）

| 类型 | textual 语法 | 说明 |
|---|---|---|
| 时钟 | `!pyc.clock` | 仅作 reg/mem/fifo/instance 端口流转，无算术 |
| 复位 | `!pyc.reset` | 同上；`pyc.reset_active` 可转成 i1 |
| 标量数据 | `i1` `i4` `i8` … 任意宽度 | 标准 MLIR IntegerType，宽度任意（v2p 上限 4096） |
| 向量数据 | `vector<4xi8>` `vector<16x3xi64>` | 标准_mlir VectorType，逐元素语义 |

- `!pyc.bits<N>` 在本分支**不存在**；其他老分支（如 pyCircuit/ zxy/feat/cycle-combine-old）也用 `iN`。若 FIRRTL 导入器看到别的分支文档提到 `!pyc.bits`，一律按 `iN` 处理。
- signedness 完全由 op 决定：比较用 `pyc.ult`/`pyc.slt`，除/模用 `pyc.udiv/urem`/`pyc.sdiv/srem`，扩展用 `pyc.zext`/`pyc.sext`，右移用 `pyc.lshr/ashr`。加减乘与 `and/or/xor/not` 无符号性差别。
- `pyc.constant`、`pyc.wire`、算术 op 的结果只能是标量或 vector-of-integer；`pyc.constant` 结果必须是标量整数（vector 常量要用 `pyc.v_broadcast` 逐元素构造，实测 `pyc.constant 0 : vector<4xi8>` 报 "requires an integer result type"）。
- 时钟/复位可以穿过 `pyc.instance` / `pyc.comb` 的参数（AnyType）。

## 2. 模块结构与前端契约属性

### 2.1 顶层组织

```mlir
module attributes {pyc.top = @TopName, pyc.frontend.contract = "pycircuit"} {
  func.func @TopName(...) -> (...) attributes {...} { ... }
  func.func @Sub(...) -> (...) attributes {...} { ... }
}
```

- **`pyc.top = @TopName`**（FlatSymbolRefAttr，`compiler/mlir/tools/pycc.cpp:342`）：指明顶层 func 符号。Verilog/C++ 发射、probe manifest 都依赖它。
- **`pyc.frontend.contract = "pycircuit"`**：必须一字不差（`CheckFrontendContractPass.cpp:138-149`，错误码 PYC901/PYC902）。
- 一个 `func.func` = 一个硬件模块。子模块也是 `func.func`，被 `pyc.instance` 引用（不是 func.call）。

### 2.2 func 签名

```mlir
func.func @Counter(%clk: !pyc.clock, %rst: !pyc.reset, %en: i1, %inc: i8) -> i8
// 多结果：
func.func @M(...) -> (i8, i1)
```

- 参数序 = 端口序；时钟/复位参数用 `!pyc.clock`/`!pyc.reset`，数据用 `iN`。
- 返回值用 `-> t`（单结果）或 `-> (t1, t2, ...)`（多结果）；块内用 `return %a, %b : i8, i1` 收尾（pyc 设计模块必须有输出；无参模块 `func.func @Top() ...` 合法，块内 `return` 即可，见 `compiler/mlir/test/Inputs/comb_cycle_cross_instance_true.mlir:16`）。

### 2.3 func 必需属性（前端契约检查 PYC903~PYC952，缺一不发）

`pyc.frontend.contract = "pycircuit"` 时，`CheckFrontendContractPass` 要求**每个 func.func** 都带：

```mlir
attributes {arg_names = ["clk", "rst", "en", "inc"], result_names = ["count"],
            pyc.kind = "module", pyc.inline = "false", pyc.params = "{}",
            pyc.base = "Counter",
            pyc.struct.metrics = "{...}", pyc.struct.collections = "[]"}
```

| 属性 | 类型 | 必需值 / 约束 | 错误码 |
|---|---|---|---|
| `arg_names` | 字符串数组 | 元素数 = 参数个数；每个是合法 field path（`[A-Za-z_][A-Za-z0-9_]*`，可带 `.seg`/`[i]`，`:` 保留）；sanitize 后不得撞车（PYC92x/925） | PYC907/911/921 |
| `result_names` | 字符串数组 | 同上，对结果 | PYC908/912/931 |
| `pyc.kind` | 字符串 | `"module"` / `"function"` / `"template"`（手写导入器用 `"module"`） | PYC903/909 |
| `pyc.inline` | 字符串 | `"true"` / `"false"`；`"false"` 保模块边界（层级导入必须） | PYC904/910 |
| `pyc.params` | 字符串 | canonical 特化参数 JSON，恒 `"{}"` 即可 | PYC905 |
| `pyc.base` | 字符串 | 规范基名，通常 = 符号名 | PYC906 |
| `pyc.struct.metrics` | 字符串 | **合法 JSON object**，必须含非负整数字段：`source_loc, ast_node_count, hardware_call_count, loop_count, module_call_count, state_call_count, estimated_inline_cost, instance_count, state_alloc_count, collection_count, collection_instance_count, module_family_collection_count`，以及 `repeated_body_clusters`（数组，可为空）；键顺序无关 | PYC951/953~975 |
| `pyc.struct.collections` | 字符串 | 合法 JSON array，空数组 `"[]"` 即可 | PYC952/976~981 |
| `pyc.value_params` + `pyc.value_param_types` | 字符串数组（可选） | 要么都给要么都不给；元素必须出现在 `arg_names`；type 只能 `iN`/`!pyc.clock`/`!pyc.reset` 字符串。手写导入器不写即可（实测去掉后 `pycc --emit=none` 通过） | PYC913~918 |

**可直接照抄的全零 metrics 字符串**（v2p `irgen.py:_zero_metrics` 原文，JSON 转义进 MLIR 时把 `"` 写成 `\"`）：

```python
ZERO_METRICS = ('{"ast_node_count":0,"collection_count":0,"collection_instance_count":0,'
                '"estimated_inline_cost":0,"hardware_call_count":0,"instance_count":0,'
                '"loop_count":0,"module_call_count":0,"module_family_collection_count":0,'
                '"repeat_pressure":0,"repeated_body_clusters":[],"source_loc":0,'
                '"state_alloc_count":0,"state_call_count":0}')
# MLIR 里写： pyc.struct.metrics = "{...转义后...}"
```

（`instance_count`/`state_alloc_count` 建议按实际填写，非 0 值只影响内联启发，不影响正确性。）

### 2.4 子模块例化（pyc.instance）

- 子模块 = 普通 `func.func`（同样带全套契约属性，`pyc.inline = "false"`）。
- 例化：操作数序 = callee 参数序，结果序 = callee 返回序，类型逐个严格相等（`PYCOps.cpp InstanceOp::verify`）。

```mlir
// 出处：compiler/mlir/test/Inputs/instance_vector_cache.mlir:17-18
%0 = pyc.instance %in {callee = @child, name = "u_child"} :
    (vector<16x3xi64>) -> vector<16x3xi64>
// 出处：compiler/mlir/test/Inputs/comb_cycle_cross_instance_true.mlir:10-13
%a = pyc.instance %a_input
    {callee = @Identity, name = "a"} : (i1) -> i1
```

- **`name` 属性必需**（PYC941；pycc.cpp:1006 PYC974 再查一遍）：合法 ident、模块内唯一、路径段不重复；可选 `short_name`（Verilog 端口缩短名）。
- 类型用 functional-type 语法 `: (in1, in2) -> (out1, out2)`；单结果可写 `-> t`（实测）。
- 时钟/复位可以作 instance 参数传递（`(!pyc.clock, !pyc.reset, i1, i8) -> i8` 实测通过）。
- 层级策略默认 `--hierarchy-policy=strict`（保边界，要求 `pyc.inline=false` 且 `--inline-policy=off` 语义由 pycc 自动裁决）；`--flatten` 把所有 instance 内联。

## 3. op 语法逐条（含 verifier 规则与真实示例）

约定：`%x` 为 SSA 值；`attr-dict` 可选；**类型后缀不可省略**。以下每条给出：操作数/属性/结果、verifier 规则（`PYCOps.cpp`）、真实出处。

### 3.1 pyc.constant

```
语法： %c = pyc.constant <非负整数字面量> : iN
出处： verilog2pyc/v2p/irgen.py:1201  f"{c} = pyc.constant {v} : i{w}"
示例： %c1 = pyc.constant 1 : i1
      %c0 = pyc.constant 0 : i8
```

- 结果必须标量整数；字面量按结果宽度 **截断（mod 2^N）**：`pyc.constant 300 : i8` 重打印为 `44`（实测）。
- **禁止发射负数或超出 2^N 的字面量**：`pyc.constant -1 : i8` 实测被解析成 `15`（MLIR 按字符位数定 APInt 宽度后再截断，语义错误且无告警）。负常量需自行换算成 `2^N + v` 的无符号十进制。
- 打印侧恒为无符号十进制（`ConstantOp::print` 用 `getZExtValue`，PYCOps.cpp:49）。

### 3.2 pyc.wire / pyc.assign（时序切割用）

```
语法： %w = pyc.wire : iN            （attr-dict 可选： {pyc.name = "..."}）
      pyc.assign %w, %v : iN
出处： verilog2pyc/v2p/irgen.py:951  f"{qw} = pyc.wire : i{width}"
      verilog2pyc/v2p/irgen.py:1205 f"pyc.assign {d['q_wire']}, {d['q_ssa']} : i{d['width']}"
```

- `assign` 的 dst **必须是 `pyc.wire` 的结果**（AssignOp::verify，PYCOps.cpp:1022-1026）；dst/src 同宽。
- **每条 wire 只允许一个 assign**：Verilog 发射拒绝多驱动（VerilogEmitter.cpp:841-846 `wireAssignCount > 1 → false`）。
- wire 是 SSA 占位：先定义、后使用、assign 通常放在消费点之后（见 §4 时序切割模式）。
- `pyc.name` 属性给 wire/alias/reg 命名（probe/VCD 用；`observe_named` 策略会保留这些名字）。

### 3.3 pyc.alias

```
语法： %a = pyc.alias %v : iN        （可带 {pyc.name = "..."}）
出处： compiler/mlir/test/Inputs/comb_cycle_reg_feedback.mlir:11
      %held = pyc.alias %q {pyc.name = "held"} : i1
```

- 恒等 op（SameOperandsAndResultType），用于命名/调试；带 `pyc.name` 等观测属性的 alias 不被 DCE 折叠。

### 3.4 pyc.comb + pyc.yield（融合组合区域）

```
语法： %o1, %o2 = pyc.comb(%in1, %in2) : (T1, T2) -> (O1, O2) {
      ^bb0(%a1 : T1, %a2 : T2):
        ...纯组合 op...
        pyc.yield %v1, %v2 : O1, O2
      }
出处： compiler/mlir/test/Inputs/comb_cycle_reg_feedback.mlir:12-16
      %driver, %other_next = pyc.comb(%held, %next) : (i1, i1) -> (i1, i1) {
      ^bb0(%q_arg : i1, %d_arg : i1):
        %other = pyc.not %d_arg : i1
        pyc.yield %q_arg, %other : i1, i1
      }
```

- **手写导入器不需要生成 comb 区域**——原始 SSA 即可，pycc 默认管线的 `pyc-fuse-comb` pass 会自动融合。comb 区域是编译器产物。
- 区域约束（CombOp::verify）：单块、块参数类型=输入类型、terminator 必须是 `pyc.yield`、yield 个数/类型=结果、**体内只许无副作用 op（alias 豁免）、不许嵌套区域**。
- yield 的操作数必须定义在区域内（区域外值实测报 "using value defined outside the region"）。
- 块参数名随意（`^bb0(%a : i1, ...)`）；参数不能与外层 SSA 同名。

### 3.5 pyc.reg（时序核心）

```
语法： %q = pyc.reg %clk, %rst, %en, %next, %init : T
出处： verilog2pyc/v2p/irgen.py:1048-1050
      f"{q} = pyc.reg {self.clk_name}, {self.rst_name}, {en}, {dv}, {init}{attr} : i{d['width']}"
      compiler/mlir/test/Inputs/comb_cycle_reg_feedback.mlir:10
      %q = pyc.reg %clk, %rst, %enable, %next, %init : i1
```

- **操作数顺序固定 5 个**：`clk(!pyc.clock), rst(!pyc.reset), en(i1), next(T), init(T)`；结果 `q: T`；next/init/q 三者类型必须相同（AllTypesMatch）。
- 语义 = `pyc_reg.v:12-17`：**posedge clk；rst 高 → q<=init（同步复位）；否则 en 高 → q<=next；en 低保持**。
  ```verilog
  always @(posedge clk) begin
    if (rst) q <= init;
    else if (en) q <= d;
  end
  ```
- 使能恒 1 就发一个常量：`%c1 = pyc.constant 1 : i1`。
- 复位端语义上属于**原语**（打进 init）；设计级复位若已折进 next 路 mux（async2sync 风格），接一个永远为 0 的 `!pyc.reset`（v2p 的 `%pyc_rst` 约定，见 §6）。
- 向量寄存器合法：`%q = pyc.reg %clk, %rst, %one, %nx, %c0 : vector<4xi8>`（实测；Verilog 发射仅支持标量 reg——CppEmitter 无此限制）。

### 3.6 pyc.delay_line / pyc.delay_tap（可选）

```
语法： %dl  = pyc.delay_line %clk, %rst, %en, %next, %init {depth = N : i64} : T
      %tap = pyc.delay_tap %dl {depth = M : i64} : T
出处： compiler/mlir/test/delay_line_verify_valid.mlir:6
      %q = pyc.delay_line %clk, %rst, %en, %in, %init {depth = 4 : i64} : i8
```

- delay_line = DEPTH 级同控制寄存器级联的紧凑形式，`depth > 1` 必须；操作数序与 reg 相同。
- delay_tap 只读 delay_line 结果的第 M 拍历史，`1 <= M <= line.depth`；line 必须由 `pyc.delay_line` 定义。
- 导入器可直接用 N 个 `pyc.reg` 链替代，不必用这两个 op。

### 3.7 pyc.reset_active

```
语法： %active = pyc.reset_active %rst : i1
```

- 把 `!pyc.reset` 端口视图转成 active-high i1（1=复位有效），用于数据路 gating。复位语义与 pyCircuit TB/SV 一致。

### 3.8 pyc.extract

```
语法： %b = pyc.extract %x {lsb = N} : iW -> i1            （单 bit）
      %f = pyc.extract %x {lsb = N, msb = M} : iW -> i(M-N+1)   （inclusive msb）
出处： verilog2pyc/v2p/irgen.py:769  f"{cur} = pyc.extract {a} {{lsb = 0}} : i{aw} -> i1"
      verilog2pyc/v2p/memgen.py:369  f"{tr} = pyc.extract {ra} {{lsb = {k}}} : i{abits} -> i1"
```

- **位序约定：`lsb` 是低位起点（bit N 从 0 数），结果宽度 = msb - lsb + 1**；`msb` 可省略（=lsb）；给了就必须满足 `msb == lsb + result_width - 1`（ExtractOp::verify PYCOps.cpp:948-953），否则 "msb must equal lsb + result_width - 1"。
- 越界报错：`lsb + width > input_width` 拒绝。
- 等价语义（CppEmitter）：`(x >> lsb) & mask`。

### 3.9 pyc.concat

```
语法： %c = pyc.concat(%hi, %mid, %lo) : (iW1, iW2, iW3) -> i(W1+W2+W3)
出处： verilog2pyc/v2p/irgen.py:530-533
      rparts = list(reversed(parts))          # LSB-first 数组反序
      f"{res} = pyc.concat({', '.join(rparts)}) : ({rtypes}) -> i{total}"
```

- **参数序 MSB-first：第一个参数占结果最高位**（VerilogEmitter.cpp:508-517 直接发 `assign c = {hi, mid, lo};`）。
- 只收标量整数（vector 拒绝）；结果宽度 = 各输入宽度之和；至少一个输入。
- Yosys JSON bits 是 LSB-first 数组，喂给 concat 前**必须反序**（README「实现要点」第 1 条，探针实证）。

### 3.10 pyc.mux

```
语法： %m = pyc.mux %sel, %tval, %fval : i1, iW, iW -> iW
出处： verilog2pyc/v2p/irgen.py:792-794
      # pyc.mux %sel, %tval, %fval == sel ? tval : fval  ->  data order swaps.
      f"{res} = pyc.mux {s}, {b}, {a} : i1, i{w}, i{w} -> i{w}"
```

- **三参顺序：`%sel, %a, %b` ≡ `sel ? a : b`**（a 是 sel==1 的分支，b 是 sel==0）。**Yosys `$mux` 的 A 是 S==0 分支，数据序要交换**（README 实证第 2 条）。
- sel 必须 i1，或与 a/b 同 shape 的 vector-of-i1；两臂同型，或一臂 vector + 一臂同宽标量（标量广播，MixMux verifier PYCOps.cpp:846-864）。
- 结果类型 = vector 臂的类型（混合时）或两臂公共类型。

### 3.11 位运算 / 算术（二元，同宽）

```
语法： %r = pyc.<op> %lhs, %rhs : iW, iW -> iW
出处： verilog2pyc/v2p/irgen.py:639
      f"{res} = pyc.{pyc_op} {a}, {b} : i{w}, i{w} -> i{w}"
示例： %s = pyc.add %count, %inc : i8, i8 -> i8
      %d = pyc.and %a, %b : i8, i8 -> i8
```

| op | 语义 | signedness 备注 |
|---|---|---|
| `pyc.add/sub/mul` | 加/减/截断乘 | 无 signed 差别（补码一致） |
| `pyc.udiv / urem` | 无符号除/余 | 除 0 行为 = 0 |
| `pyc.sdiv / srem` | 有符号除/余 | — |
| `pyc.and / or / xor` | 按位 | — |
| `pyc.eq` | 相等 → i1 | 无 signed 差别 |
| `pyc.ult` | 无符号 < | `pyc.not` 组合出 ≤/>/≥（irgen `_cmp` 的完整映射） |
| `pyc.slt` | 有符号 < | — |

- verifier：两操作数 leaf 宽度必须相等；vector 形状必须一致（或一边标量广播——结果跟随 vector 臂）；比较结果 i1（vector 时 vector-of-i1）。混合符号除法/比较在 v2p 直接拒绝（先统一宽度+符号再发 op）。
- `>`/`>=`/`<=`/`!=` 没有专属 op，按 irgen `_cmp`（irgen.py:644-692）组合：
  - `gt = ult(b, a)`；`le = not(ult(b, a))`；`ge = not(ult(a, b))`；`ne = not(eq)`。

### 3.12 pyc.not（一元）

```
语法： %r = pyc.not %a : iW
出处： verilog2pyc/v2p/irgen.py:575  f"{n} = pyc.not {e} : i1"
```

- 按位取反；结果类型 = 输入类型（SameOperandsAndResultType）。

### 3.13 宽度变换 sext / zext / trunc

```
语法： %r = pyc.zext %a : iW1 -> iW2      （W2 > W1，高位补 0）
      %r = pyc.sext %a : iW1 -> iW2      （W2 > W1，按符号扩展）
      %r = pyc.trunc %a : iW1 -> iW2     （W2 < W1，截低位）
出处： verilog2pyc/v2p/irgen.py:537-548 extend()
      f"{res} = {op} {ssa} : i{from_w} -> i{to_w}"
```

- 方向错了 verifier 拒绝（zext/sext 要求变宽，trunc 要求变窄，PYCOps.cpp:907-931）；宽度相等时直接复用值不发射（irgen `extend` 第一行）。

### 3.14 移位

```
立即数移位（same type in/out，attr 必填 amount）：
  %r = pyc.shli  %a {amount = N} : iW        # 左移 N
  %r = pyc.lshri %a {amount = N} : iW        # 逻辑右移
  %r = pyc.ashri %a {amount = N} : iW        # 算术右移
动态移位（amount 是标量值，任意宽度）：
  %r = pyc.shl  %a, %amt : iW, iA
  %r = pyc.lshr %a, %amt : iW, iA
  %r = pyc.ashr %a, %amt : iW, iA
出处： verilog2pyc/v2p/irgen.py:898-915（$shl/$shr/$sshl/$sshr/$shift 全部按这套降）
      f"{res} = {op} {a2} {{amount = {const_amt}}} : i{yw}"
      f"{res} = {op} {a2}, {b} : i{yw}, i{bw}"
```

- amount 为负 verifier 拒绝；动态 amount 是标量整数（宽度任意），in/out 同型（含 vector）。
- 有符号数的右移选 `ashr/ashri`；`$shift/$shiftx` 的方向位判断见 irgen `_shiftx`（irgen.py:837-868）。

### 3.15 pyc.instance

见 §2.4。要点重申：`{callee = @Sym, name = "inst_name"}`，操作数/结果序严格对齐 callee 签名，functional-type 收尾。

### 3.16 存储器：pyc.byte_mem / pyc.sync_mem / pyc.sync_mem_dp

```
byte_mem（异步读已提交数组 + 同步写）：
  %rd = pyc.byte_mem %clk, %rst, %raddr, %wvalid, %waddr, %wdata, %wstrb
        {depth = <字节数> : i64, name = "m1"} : iAddrBits, iData, iStrbBits
sync_mem（1R1W，读数据打一拍寄存）：
  %rd = pyc.sync_mem %clk, %rst, %ren, %raddr, %wvalid, %waddr, %wdata, %wstrb
        {depth = <条目数> : i64, name = "m2"} : iAddrBits, iData, iStrbBits
sync_mem_dp（2R1W，双读口输出都寄存）：
  %r0, %r1 = pyc.sync_mem_dp %clk, %rst, %ren0, %raddr0, %ren1, %raddr1,
        %wvalid, %waddr, %wdata, %wstrb
        {depth = <条目数> : i64, name = "m3"} : iAddrBits, iData, iStrbBits
```

三个全部实测通过解析（`pyc-opt` 退出码 0，探针文件 `/tmp/pyc_contract_probe/mem2.mlir`）。规则（verifier PYCOps.cpp:1091-1208 + 契约 pass）：

- 操作数序固定：`clk, rst, [ren,] raddr...`，写侧固定为 `wvalid, waddr, wdata, wstrb`。
- 类型段 `: iAddr, iData, iStrb`（raddr 型、rdata 型、wstrb 型；**没有 `->`**——rdata 类型已含在列表第 2 项）。多结果 op（sync_mem_dp）结果绑定写 `%r0, %r1 =`。
- `wstrb` 宽度必须 = `ceil(data_width/8)`（字节选通）。
- **`depth` 单位不同**：byte_mem 以**字节**计（DEPTH in bytes），sync_mem/sync_mem_dp 以**条目**计（verifier 错误消息明示 "depth (bytes)" vs "depth (entries)"）。
- **`name` 属性必需**（PYC942）：`[A-Za-z_][A-Za-z0-9_]*`、模块内唯一、不与端口名撞（PYC943~945）。
- 读延迟语义：
  - `byte_mem`：**组合读**已提交数组（本拍边沿前看到旧值，write-first 更新发生在 tick 后），写 posedge 生效。v2p 全部存储器降为 byte_mem + 外套 `pyc.reg` 实现同步读（memgen.py:1-16 docstring：`pyc.sync_mem` old-data-only 所以不用）。
  - `sync_mem`：读数据寄存一拍（RD_LATENCY=1）。
- `pyc.sync_mem_2w` / `pyc.sync_mem_nm`（memgen.py 里出现）**不在本分支 dialect 里**，是其他实验分支的 op——本分支手写 IR 不要用。
- **byte_mem 无 INIT**：上电全 0；带初值存储器请先降级成寄存器堆或 reg+mux（v2p 的 `--memory-map` 路线）。
- v2p 字节写选通合并模板（memgen.py:201-224）：额外发一份「读写地址旧值」的 byte_mem（shadow read），按位 mask 合并 `old | (wdata & bitmask)` 后写回，使 per-bit WR_EN 退化成 per-byte strb 仍保语义。

### 3.17 pyc.fifo / pyc.async_fifo / pyc.cdc_sync（简要）

```
%in_ready, %out_valid, %out_data = pyc.fifo %clk, %rst, %in_valid, %in_data, %out_ready
      {depth = N : i64} : T
出处（打印形，实测）： %in_ready, %out_valid, %out_data = pyc.fifo %arg0, %arg1, %21, %5, %30 {depth = 4 : i64} : i8
%in_ready, %out_valid, %out_data = pyc.async_fifo %in_clk, %in_rst, %out_clk, %out_rst,
      %in_valid, %in_data, %out_ready {depth = N : i64} : T
%out = pyc.cdc_sync %clk, %rst, %in {stages = N : i64} : T
```

- fifo：单时钟 ready/valid 严格握手，`depth > 0`；async_fifo：双时钟，`depth >= 2` 且**必须 2 的幂**（灰码指针）；cdc_sync：宽 1..64，`stages >= 1`（可省略，默认级数见 runtime）。
- in_data/out_data 同型；结果序固定 `in_ready, out_valid, out_data`。

### 3.18 pyc.assert

```
pyc.assert %cond {msg = "why"}
```

- 仅仿真；cond 必须 i1；msg 可选但给了必须非空。

### 3.19 vector ops（可选能力，导入器初期可不用）

```
%e   = pyc.v_get %v[2] : vector<4xi8> -> i8                  # 下标取元素
%b   = pyc.v_broadcast %s to 4 : i8 -> vector<4xi8>          # 标量广播成 1-D
%w   = pyc.v_create(%e, %s, %e, %s) : (i8, i8, i8, i8) -> vector<4xi8>   # 拼元素，带括号！
%r   = pyc.v_or_reduce %w {mode = "tree"} : vector<4xi8> -> i8
%r2  = pyc.v_and_reduce %w {mode = "tree"} : vector<4xi8> -> i8
%r3  = pyc.v_add_reduce %w {mode = "tree"} : vector<4xi8> -> i8
```

- `mode` 必填且只能是 `"chain"` 或 `"tree"`（verifyVectorReduce，PYCOps.cpp:1557-1561）。
- v_get 索引越界拒绝；v_create 元素个数 = 结果第 0 维；v_broadcast 的 `size` 必须等于结果第 0 维。
- 全部逐元素语义，支持标量/向量混合广播（mux 同款规则）。

## 4. 时序切割与 SSA 结构规则（wire+assign 模式）

pyc 是 SSA：**值只能定义一次**，而硬件里有反馈环。标准做法（出处：`comb_cycle_reg_feedback.mlir` 全文 + irgen docstring + README）：

1. **reg/存储器读口的 Q 侧用 `pyc.wire` 代理**：先 `pyc.wire` 占位，组合逻辑照常用它，`pyc.reg`/`byte_mem` op 放在组合 cell 之后发射，最后 `pyc.assign` 把真实 Q 接回 wire。这样存储器写口不构成读数据的组合前驱。
   ```mlir
   %held__next = pyc.wire {pyc.name = "held__next"} : i1
   %enable = pyc.constant 1 : i1
   %init = pyc.constant 0 : i1
   %q = pyc.reg %clk, %rst, %enable, %held__next, %init : i1
   %held = pyc.alias %q {pyc.name = "held"} : i1
   ...组合逻辑用 %held...
   pyc.assign %held__next, <D 值> : i1
   ```
2. **每条 wire 恰好一个 assign**；wire 类型只允许标量/vector-of-integer（不能是时钟）。
3. **常量提升到块首**：`pyc.constant` 无操作数，必须支配所有使用者；irgen 在最后统一把常量 def 插到块顶（irgen.py:1199-1203）。
4. 组合逻辑保持拓扑序发射（irgen 用 Kahn 算法，irgen.py:1054-1097）；发射顺序只影响可读性，`pyc-fuse-comb`/调度 pass 会重排，但**支配关系必须先由发射序保证**。
5. 组合环（无 reg 隔断的 wire→wire 反馈）会被 `pyc-check-comb-cycles` pass 拒绝；reg 反馈环（wire 只被 reg Q 驱动）合法——这正是 §3.5 示例的模式。
6. **逻辑深度门**：时序边界之间的组合深度默认 ≤32（`--logic-depth`）；深解码链要手动配平（irgen 的 $pmux 平衡树，深度 ~2·log2(lanes)，irgen.py:798-835）。
7. 未驱动网络 tie 成 `pyc.constant 0`（x-initial-0 约定，与 Verilator `--x-initial 0` 对齐，irgen.py:1149-1182）。

## 5. verilog2pyc 发射器约定逐条（v1 契约，全部照抄自 irgen.py docstring + README「实现要点」）

- Yosys 0.67 JSON bits 数组 **LSB-first**（index i = 第 i 位，探针实证）；`pyc.concat` 首参数是**最高位段**，concat 处数组反序。
- `pyc.mux %sel, %tval, %fval ≡ sel ? tval : fval`；Yosys `$mux` A 是 S==0 分支，**数据序交换**。
- `pyc.extract %x [i] -> i1`；`{lsb, msb}` → `i(msb-lsb+1)`，msb inclusive。
- `pyc.reg %clk, %rst, %en, %next, %init : T`（posedge、同步复位到 init）。
- **原语复位用 tie-off 端口**：模块参数表末尾追加 `%pyc_rst : !pyc.reset`，测试台不驱动时读作 0，原语复位恒不生效。设计 rst/reset 端口是普通 i1 数据（复位已折进 D 路 mux，proc/async2sync 与 $adff lowering 同近似）——否则没复位的触发器和 SRAM 会在设计复位期被清掉，byte_mem 还会丢写。
- 时序切割用 `pyc.wire + pyc.assign`（§4 第 1 条模式）；每个 DFF、每个存储器读口各一个 wire 代理；存储器写口不进读数据组合拓扑。
- `$adff` 降为同步 reg：D' = `ARST ? ARST_VALUE : D`（与 Yosys async2sync 同近似；激励只在时钟沿变复位时逐拍一致）。
- **每个 cell 操作数先做宽度归一**（按端口 signedness 选 zext/sext、否则 trunc）再发对应 pyc op（irgen `extend()`）。
- 混合符号的除法/有序比较 v2p 直接报错拒绝（先统一符号再发射是导入器自己的责任）。
- `$pmux` 降为平衡优先级树（低 index 优先，深度 ~2·log2(lanes)），线性链会撞 `--logic-depth` 门。
- `{n{x}}` 位重复降为倍增法（log2(n) 步 `x |= x << (k*w)` 后 trunc）。
- 常量统一提升块首；未驱动网络 tie 0。
- 端口名 sanitize 后全模块唯一；时钟端口名 = 设计端口名（clk 或 clock 都可能，C++ 成员名跟随 func 参数名）。

## 6. pycc 用法

二进制：`$PYC/pycc`（完整优化管线）与 `$PYC/pyc-opt`（纯 mlir-opt + pyc pass 注册，`--pass-pipeline="builtin.module(func.func(pyc-eliminate-wires))"` 可单跑 pass，实测可用）。

### 6.1 关键旗标（`pycc --help` 实录摘录）

| 旗标 | 作用 |
|---|---|
| `<input .mlir>`（positional）/ `-` | 输入文件（默认 stdin） |
| `--emit=verilog\|cpp\|none` | 发射目标；`none` = 只跑管线当语法/契约检查 |
| `-o <path>` | 输出文件（verilog 单文件；cpp 配 `--cpp-split=none` 时单文件 `gen.cpp` 实测可行） |
| `--out-dir=<dir>` | **cpp 推荐方式**：按模块拆 `Top.hpp/Top.cpp/...`，写 `manifest.json`、`cpp_compile_manifest.json`、`compile_stats.json` |
| `--verilog=<path>` / `--cpp=<path>` | 直发等价形式（= `--emit=X -o`） |
| `--flatten` | 内联全部 instance 成单模块 |
| `--hierarchical` / `--hierarchy-policy=strict\|instantiate` | 保层级；默认 strict（要求 `pyc.inline=false`、禁 inliner，pycc 自动裁决） |
| `--inline-policy=off\|default\|threshold:N` / `--noinline` | 内联控制 |
| `--logic-depth=<uint>` | 组合深度门（默认 32） |
| `--emit-structural=auto\|on\|off` | comb 融合结构化发射策略 |
| `--state-pack-width=<uint>` / `--no-state-opt` / `--no-seq-bank` | 状态优化开关 |
| `--unroll-vector` | IR 级向量展开 |
| `--sim-mode=cpp-only` / `--cpp-only-preserve-ops` | C++ 调度形态 |
| `--dump-pass-ir=auto`（配 `--out-dir`） | 逐 pass dump IR 到 `<out-dir>/pass_ir` |
| `--include-primitives` | Verilog 输出含 `include "pyc_*.v"`（默认开） |

### 6.2 默认 pass 管线（pycc.cpp:2659-2722 硬编码顺序）

```
check-frontend-contract → inline-functions → [flatten-instances] → check-hierarchy-discipline
→ symbol-dce → [inliner] → canonicalizer → cse → sccp → remove-dead-values
→ eliminate-dead-instances → symbol-dce
→ lower-scf-to-pyc-static → [vector-unroll] → eliminate-wires → eliminate-dead-state
→ [cpp: state-optimize(merge/retime/pack)] → slp-pack-wires → comb-canonicalize → shl-chain-fold
→ check-clock-domains → pack-i1-regs → [cpp: seq-bank-partition] → [cpp: apply-observation-demand]
→ fuse-comb → canonicalizer → cse → remove-dead-values → eliminate-dead-instances → symbol-dce
→ check-comb-cycles → change-driven-schedule → check-flat-types → check-no-dynamic
→ check-logic-depth → collect-compile-stats
```

pycc 成功后 stderr 打一行 `stats: state_policy=..., regs=N (M bits), ... max_depth=D/32, WNS=..., fuse_comb=on`（寄存器数/深度快速自检用）。

### 6.3 输出物

- `--emit=verilog`：单文件多 module，头部自动 `` `include "pyc_reg.v" `` 等原语（原语在 `install/include/verilog/` 与 `runtime/verilog/`）。
- `--emit=cpp --out-dir=D`：`<Module>.hpp/.cpp` 每模块一对 + `manifest.json`（`top`、`cpp_modules`、`verilog_modules`）+ `cpp_compile_manifest.json`（含 **runtime include/lib 路径、`-std=c++17`、每个源文件清单**）。
- `manifest.json` 的 `runtime` 段直接给出链接参数（本机实测值）：
  - include：`.../toolchain/install/include`（内含 `cpp/pyc_sim.hpp` 等约 24 个头）
  - lib：`.../toolchain/install/lib64/libpyc4_runtime.a`（**注意是 lib64 不是 lib**）
  - CMake 替代：`pycircuit::pyc4_runtime` target（`install/share/pycircuit/cmake`）

## 7. 生成 C++ 的编译与驱动（runtime API）

### 7.1 编译命令（实测通过）

```bash
g++ -O2 -std=c++17 \
    -I<out-dir> \
    -I/home/lidongzhe/pycircuit-workspace/pyCircuit-dev/.pycircuit_out/toolchain/install/include \
    <out-dir>/*.cpp drv.cpp \
    /home/lidongzhe/pycircuit-workspace/pyCircuit-dev/.pycircuit_out/toolchain/install/lib64/libpyc4_runtime.a \
    -o sim
```

### 7.2 模型 API（`namespace pyc::gen`，struct 名 = top 符号名）

```cpp
#include "Top.hpp"
pyc::gen::Top dut;                          // 各端口是成员 Wire<N>
dut.clk  = pyc::cpp::Bits<1>{0};
dut.x    = pyc::cpp::Bits<4>{3};
uint32_t v = dut.out.value();               // 读端口值
dut.comb();                                 // 组合求值（输出 Wire 在此刷新）
dut.tick();                                 // posedge：采样 D
dut.commit();                               // 发布 Q
// 内部另有 eval()/step()/tick_compute()/negedge_update() 等分层 API；驱动层只用 comb/tick/commit
```

### 7.3 每拍周期（必须严格按此序；出处 `verilog2pyc/v2p/cpp_runner_template.cpp:25-40` 与 README）

```cpp
static void Clock(Dut& dut) {
  dut.comb();                        // D 随新输入收敛
  dut.clk = pyc::cpp::Bits<1>{1};
  dut.tick(); dut.commit();          // 上升沿采样/发布
  dut.comb();                        // 输出线刷新（必须！否则读到旧值）
  dut.comb();                        // 再 comb 一次：wire/assign 反馈链定点收敛
  dut.clk = pyc::cpp::Bits<1>{0};
  dut.tick(); dut.commit();          // negedge 记账（commit 是 no-op，保不变量）
  dut.comb();                        // 重发布
}
// 一个仿真周期 = Drive → comb → posedge tick/commit → comb → comb → negedge tick/commit → comb
```

### 7.4 现成 runner 模板

- `verilog2pyc/v2p/cpp_runner_template.cpp`：ST 行协议（`ST <tick> <in0> <in1> ...`）trace 驱动，复位 2 拍约定，输出行 `ST <c> <out...>`。
- `verilog2pyc/v2p/gen_runner.py`：`python -m v2p.gen_runner <model.mlir> <top> <model.cpp> <outdir>` 从 MLIR 文本正则抽取参数表自动填模板（列映射规则：列 = func 参数序，**跳过 clk、保留 rst**；`!pyc.clock/!pyc.reset` 参数不进 Drive 列表）。
- TB/VCD：`install/include/cpp/pyc_tb.hpp` 提供 `Testbench`（`enableVcd(path, top, timescale)` dump 波形）与 `pyc_trace_vcd` 探针；probe 注册走 `pyc_register_probes(pyc::cpp::ProbeRegistry&, prefix)`。
- 陷阱：**响应式握手信号**（DUT 沿上 NBA 更新的 ready/rdata）必须沿前影子采样进 trace 列，否则 C++ runner 等于提前一拍（picorv32 曾因此误判，README 有记）。

### 7.5 保留名（实测踩坑）

生成 struct 的成员名 = `arg_names`/`result_names` 原文 + 内部方法（`comb/eval/step/tick/commit/tick_compute/eval_comb_pass/negedge_update/pyc_register_probes/pyc_trace_vcd` 等）与 `_pyc_*`/`pyc_*` 前缀。**端口取名避开这些 C++ 标识符**——实测端口叫 `step` 时 `pyc::gen::Counter::step()` 方法与 `Wire<8> step` 成员撞名，C++ 编译直接失败（生成 C++ 无法编译，但 pycc 全程无告警）。

## 8. 完整可编译最小例子（两模块 + instance，端到端验证）

文件全文（`/tmp/pyc_contract_probe/final/demo.mlir`，31 行）：

```mlir
module attributes {pyc.top = @Top, pyc.frontend.contract = "pycircuit"} {
  // 子模块：8bit 使能计数器
  func.func @Counter(%clk: !pyc.clock, %rst: !pyc.reset, %en: i1, %inc: i8) -> i8
      attributes {arg_names = ["clk", "rst", "en", "inc"], result_names = ["count"],
                  pyc.kind = "module", pyc.inline = "false", pyc.params = "{}", pyc.base = "Counter",
                  pyc.struct.metrics = "{\"ast_node_count\":0,\"collection_count\":0,\"collection_instance_count\":0,\"estimated_inline_cost\":0,\"hardware_call_count\":0,\"instance_count\":0,\"loop_count\":0,\"module_call_count\":0,\"module_family_collection_count\":0,\"repeat_pressure\":0,\"repeated_body_clusters\":[],\"source_loc\":0,\"state_alloc_count\":1,\"state_call_count\":0}",
                  pyc.struct.collections = "[]"} {
    %c0 = pyc.constant 0 : i8
    %next = pyc.wire {pyc.name = "count__next"} : i8
    %count = pyc.reg %clk, %rst, %en, %next, %c0 : i8
    %sum = pyc.add %count, %inc : i8, i8 -> i8
    pyc.assign %next, %sum : i8
    return %count : i8
  }

  // 顶层：例化两个 Counter，一个每拍 +1，一个每拍 +10，输出异或
  func.func @Top(%clk: !pyc.clock, %rst: !pyc.reset, %en: i1) -> i8
      attributes {arg_names = ["clk", "rst", "en"], result_names = ["out"],
                  pyc.kind = "module", pyc.inline = "false", pyc.params = "{}", pyc.base = "Top",
                  pyc.struct.metrics = "{\"ast_node_count\":0,\"collection_count\":0,\"collection_instance_count\":0,\"estimated_inline_cost\":0,\"hardware_call_count\":0,\"instance_count\":2,\"loop_count\":0,\"module_call_count\":2,\"module_family_collection_count\":0,\"repeat_pressure\":0,\"repeated_body_clusters\":[],\"source_loc\":0,\"state_alloc_count\":0,\"state_call_count\":0}",
                  pyc.struct.collections = "[]"} {
    %c1 = pyc.constant 1 : i8
    %c10 = pyc.constant 10 : i8
    %lo = pyc.instance %clk, %rst, %en, %c1 {callee = @Counter, name = "u_lo"} : (!pyc.clock, !pyc.reset, i1, i8) -> i8
    %hi = pyc.instance %clk, %rst, %en, %c10 {callee = @Counter, name = "u_hi"} : (!pyc.clock, !pyc.reset, i1, i8) -> i8
    %out = pyc.xor %lo, %hi : i8, i8 -> i8
    return %out : i8
  }
}
```

验证记录（2026-11-11，pyCircuit-dev 工具链实测）：

| 命令 | 结果 |
|---|---|
| `pyc-opt demo.mlir -o /dev/null` | 退出码 0（解析+verifier 通过） |
| `pycc demo.mlir --emit=none --out-dir=out_none` | 退出码 0；stats 行 `regs=1 (8 bits) ... max_depth=1/32` |
| `pycc demo.mlir --emit=verilog -o demo.v` | 退出码 0（两 module + pyc_reg 原语 include） |
| `pycc demo.mlir --emit=cpp --out-dir=out_cpp` | 退出码 0；产出 `Counter.hpp/.cpp Top.hpp/.cpp manifest.json cpp_compile_manifest.json` |
| `g++ ... out_cpp/*.cpp drv.cpp lib64/libpyc4_runtime.a -o sim && ./sim` | `cycle 0: out=11` / `cycle 1: out=22` / `cycle 2: out=29`（=1^10, 2^20, 3^30，功能正确） |

驱动程序（`drv.cpp`，§7.3 周期）：

```cpp
#include <cstdio>
#include "Top.hpp"
int main() {
  pyc::gen::Top dut;
  dut.clk = pyc::cpp::Bits<1>{0};
  dut.rst = pyc::cpp::Bits<1>{1};
  dut.en  = pyc::cpp::Bits<1>{1};
  auto cyc = [&]() {
    dut.comb();
    dut.clk = pyc::cpp::Bits<1>{1}; dut.tick(); dut.commit();
    dut.clk = pyc::cpp::Bits<1>{0}; dut.tick(); dut.commit();
    dut.comb();
  };
  cyc(); cyc();                    // 复位 2 拍
  dut.rst = pyc::cpp::Bits<1>{0};
  for (int i = 0; i < 3; ++i) { cyc(); printf("cycle %d: out=%u\n", i, dut.out.value()); }
  return 0;
}
```

## 9. 陷阱清单（每条都实测踩过）

1. **常量字面量**：只发 `0..2^N-1` 无符号十进制；负数（`-1`→15）与超宽（300→44）都会被静默截成错误值。
2. **端口名撞生成方法**：`step`/`comb`/`eval`/`tick`/`commit` 等撞 C++ 方法，pycc 不报错、编译才炸；命名前查 §7.5。
3. **concat 参数序**：MSB-first；Yosys LSB-first 数组要 reverse。
4. **mux 参数序**：`sel, then-value, else-value`；Yosys $mux A/B 相反。
5. **extract**：`msb` 给了就必须 `== lsb + width - 1`；单 bit 只给 `lsb`。
6. **wire 多驱动**：一条 wire 一个 assign；Verilog 路直接拒绝。
7. **instance 忘 `name`**：PYC941/PYC974 硬失败。
8. **mem 忘 `name` / depth 单位搞混**：byte_mem depth 是字节、sync_mem 是条目（PYC942 / verifier）。
9. **本分支没有 `sync_mem_2w`/`sync_mem_nm`/`!pyc.bits`**；别从其他分支文档抄。
10. **func 契约属性缺一个**（PYC901~952）整条管线失败；metrics 必须是合法 JSON object 且字段名精确。
11. **yield 用了区域外的值**：comb 体内结果必须在区域内重算。
12. **vector 常量**：没有 `pyc.constant : vector<...>`，用 `v_broadcast` 构造。
13. **复位 tie-off**：设计复位是数据 i1，原语 rst 接恒 0 的 `!pyc.reset`；别把设计 rst 直接接进每个 reg 的 rst 端（会把无复位状态清掉、丢 byte_mem 写）。
14. **pyc.constant 不能做 vector**；`pyc.reg` 结果/Verilog 发射要求标量（C++ 发射支持 vector reg）。
15. **lib 路径是 `install/lib64/`** 不是 `install/lib/`；C++ 最低 `-std=c++17`。
