"""FIRRTL AST -> pyc textual MLIR lowering.

Strategy per module (mirrors firtool's LowerToLowFIRRTL in spirit, x-initial-0
convention of verilog2pyc):

  1. Flatten every aggregate (bundles/vectors incl. flip) into scalar leaves.
     UInt<w>/SInt<w> -> i<w> (SInt signedness carried per-leaf, expressed via
     op choice: slt/ult, sdiv/udiv, sext/zext, ashr/lshr).  Clock/Reset/
     AsyncReset leaves -> !pyc.clock / !pyc.reset and are NEVER pyc.wire's
     (pyc wire types are integer-only); their values resolve to the module's
     primary clock/reset argument through driver-chain tracing, with
     approximation counted whenever the chain is not a clean passthrough.
  2. Walk statements with a condition stack collecting per-leaf driver lists
     (last-connect-wins).  `invalidate` lowers to connect-to-zero, which is
     equivalent under the x-0 convention firtool also uses.
  3. Emit structurally: constants (hoisted to block top) -> wires -> memory
     banks -> regs (wire+assign cut for next) -> instances (program order) ->
     assigns -> return.  Everything else (node prims, mux folds, read muxes)
     is emitted lazily bottom-up with memoization, which guarantees SSA
     dominance by construction.
  4. Memories (cmem/smem) lower to scalar register banks with mux-tree reads;
     `read` ports register their address first (read latency 1); `infer`
     (cmem) ports read pre-edge data combinationally.  A mem write port's
     enable is its declaration condition; multiple write ports fold in
     declaration order (later wins on simultaneous hits).
  5. extmodule instances: outputs tied to their DEFAULT parameter (the
     plusarg_reader stub semantics of the verilog flow).
  6. v2p reset tie-off convention: every func gets a trailing %pyc_rst
     !pyc.reset argument (reads as 0 when undriven); bare FIRRTL regs use it
     so designs without reset keep their state.

Approximations (each counted via note() and reported):
  - bare `reg` (no reset): %pyc_rst + init 0 (FIRRTL leaves it undefined).
  - regreset whose reset expression is not the primary reset port.
  - reg whose clock expression is not the primary clock port reference.
  - asClock/asAsyncReset-from-data and non-passthrough clockish wires.

Emission contract: fir2pyc/docs/pyc_emit_contract.md (pyCircuit-dev, pyc4.0).
"""

from __future__ import annotations

import re
from collections import OrderedDict

from .fir_parser import (
    Circuit, Connect, Expr, FirType, Inst, Invalidate, MemDecl, MemPort,
    Module, Node, Port, Reg, Skip, When, Wire,
)


class LowerError(Exception):
    """Lowering failure with a precise cause; never silently degraded."""


MAX_WIDTH = 16384         # sanity cap (dshl widths can legitimately exceed 4k)
ORPHAN_FB = False         # module-level orphan width fallback flag
MAX_BANK_DEPTH = 1024     # register-bank memory limit; beyond -> real byte_mem


class _RealMemCycle(Exception):
    """Combinational read->write dependency through one byte_mem field."""
ZERO_METRICS = ('{"ast_node_count":0,"collection_count":0,'
                '"collection_instance_count":0,"estimated_inline_cost":0,'
                '"hardware_call_count":0,"instance_count":0,"loop_count":0,'
                '"module_call_count":0,"module_family_collection_count":0,'
                '"repeat_pressure":0,"repeated_body_clusters":[],"source_loc":0,'
                '"state_alloc_count":0,"state_call_count":0}')


# ---------------------------------------------------------------------------
# Leaves
# ---------------------------------------------------------------------------

class Leaf:
    """A scalar signal leaf after aggregate flattening."""

    __slots__ = ("root", "path", "kind", "_w", "leaf_id", "drivers",
                 "is_input", "is_zw")

    @property
    def width(self):
        return self._w

    @width.setter
    def width(self, v):
        # orphan fallback: unresolved bare scalars coerce to 1 at any stage
        if v is not None and v < 0 and ORPHAN_FB:
            v = 1
        self._w = v

    def __init__(self, root: str, path: str, ftype: FirType, leaf_id):
        self.root = root
        self.path = path            # "" or ".a.b" / "[2]" suffix from root
        self.kind = ftype.kind      # uint|sint|clock|reset|asyncreset|analog|probe
        self.width = getattr(ftype, "width", 1)
        self.is_zw = (self.kind in ("uint", "sint") and self.width == 0)
        self.leaf_id = leaf_id
        self.drivers = []           # [(cond_expr_or_None, value_expr_or_None)]
        self.is_input = False

    @property
    def is_clockish(self) -> bool:
        return self.kind in ("clock", "reset", "asyncreset")

    def key(self) -> str:
        return self.root + self.path

    def pyctype(self) -> str:
        if self.kind == "clock":
            return "!pyc.clock"
        if self.kind in ("reset", "asyncreset"):
            return "!pyc.reset"
        if self.kind in ("uint", "sint"):
            if self.width < 1:
                if ORPHAN_FB:
                    self.width = 1
                    self.is_zw = False
                else:
                    raise LowerError(
                        f"leaf {self.full()} has unresolved width (bare "
                        f"UInt/SInt with no inferable width); cannot emit")
            return f"i{self.width}"
        raise LowerError(f"leaf {self.full()} has non-emittable type {self.kind}")

    def signed(self) -> bool:
        return self.kind == "sint"

    def full(self) -> str:
        return f"{self.root}{self.path}"


def _name_join(segs):
    """Join path segments: identifiers joined by '.', array indexes attach
    directly to the preceding segment ('group[0]')."""
    out = []
    for seg in segs:
        if re.match(r"^\[\d+\]$", seg):
            if out:
                out[-1] = out[-1] + seg
            else:
                out.append(seg)
        else:
            out.append(seg)
    return out


def _strip_first_seg(path: str) -> str:
    """Drop the leading port-name segment: '.io.enq' -> '.enq'."""
    if not path:
        return path
    segs = _path_segments(path)
    return "".join(segs[1:])


def _path_segments(path: str) -> list:
    if not path:
        return []
    return re.findall(r"\.[^.\[\]]+|\[\d+\]", path)


def idx_width(n: int) -> int:
    return max(1, (max(n - 1, 0)).bit_length())


def decl_t(decl, env) -> FirType:
    """Port bundle of an instance declaration (parent view)."""
    sub = env.circuit.modules[decl.module]
    return env.port_bundle_of(sub)


# ---------------------------------------------------------------------------
# Expression typing (FIRRTL width rules)
# ---------------------------------------------------------------------------

def expr_type(e: Expr, env) -> FirType:
    k = e.kind
    if k == "lit":
        return FirType(kind="sint" if e.signed else "uint", width=e.width)
    if k == "ref":
        return env.ref_type(e.name)
    if k == "subfield":
        t = expr_type(e.base, env)
        if t is None:
            raise LowerError(f"subfield on unresolved type")
        if t.kind != "bundle":
            raise LowerError(f"subfield .{e.name} on non-bundle {t}")
        for name, _flip, ft in t.fields:
            if name == e.name:
                return ft
        raise LowerError(f"no field .{e.name} in bundle")
    if k == "subindex":
        t = expr_type(e.base, env)
        if t.kind != "vector":
            raise LowerError(f"subindex [{e.idx}] on non-vector {t}")
        if e.idx >= t.size:
            raise LowerError(f"subindex [{e.idx}] out of range {t.size}")
        return t.elem
    if k == "subaccess":
        t = expr_type(e.base, env)
        if t.kind != "vector":
            raise LowerError(f"subaccess on non-vector {t}")
        return t.elem
    if k == "prim":
        return _prim_type(e, env)
    raise LowerError(f"cannot type expression kind {k}")


def _prim_type(e: Expr, env) -> FirType:
    op = e.op
    if op in ("asClock", "asAsyncReset"):
        return FirType(kind="clock" if op == "asClock" else "asyncreset", width=1)
    if op == "asUInt":
        t = expr_type(e.args[0], env)
        if t is not None and t.kind in ("uint", "sint", "clock", "reset", "asyncreset"):
            return FirType(kind="uint", width=max(t.width, 1))
        raise LowerError(f"asUInt on {t}")
    if op == "asSInt":
        t = expr_type(e.args[0], env)
        if t.kind in ("uint", "sint"):
            return FirType(kind="sint", width=max(t.width, 1))
        raise LowerError(f"asSInt on {t}")
    if op == "mux":
        return _widen_type(expr_type(e.args[1], env), expr_type(e.args[2], env))
    if op in ("eq", "neq", "lt", "leq", "gt", "geq", "andr", "orr", "xorr"):
        return FirType(kind="uint", width=1)
    if op in ("add", "sub"):
        ta, tb = expr_type(e.args[0], env), expr_type(e.args[1], env)
        return FirType(kind=ta.kind, width=max(ta.width, tb.width) + 1)
    if op == "mul":
        ta, tb = expr_type(e.args[0], env), expr_type(e.args[1], env)
        return FirType(kind=ta.kind, width=ta.width + tb.width)
    if op == "div":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=t.width)
    if op == "rem":
        ta, tb = expr_type(e.args[0], env), expr_type(e.args[1], env)
        return FirType(kind=ta.kind, width=min(ta.width, tb.width))
    if op in ("and", "or", "xor"):
        ta, tb = expr_type(e.args[0], env), expr_type(e.args[1], env)
        return FirType(kind=ta.kind, width=max(ta.width, tb.width))
    if op == "not":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=t.width)
    if op == "neg":
        t = expr_type(e.args[0], env)
        return FirType(kind="sint" if t.kind == "sint" else "uint",
                       width=t.width + 1)
    if op == "pad":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=max(t.width, e.consts[0]))
    if op == "head":
        return FirType(kind=expr_type(e.args[0], env).kind, width=e.consts[0])
    if op == "tail":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=max(t.width - e.consts[0], 1))
    if op == "shl":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=t.width + e.consts[0])
    if op == "shr":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=max(t.width - e.consts[0], 1))
    if op == "dshl":
        ta, tb = expr_type(e.args[0], env), expr_type(e.args[1], env)
        bwidth = max(tb.width, 0)  # bare-UInt amount: clamp for width math
        return FirType(kind=ta.kind,
                       width=ta.width + (1 << min(bwidth, 12)) - 1)
    if op == "dshr":
        t = expr_type(e.args[0], env)
        return FirType(kind=t.kind, width=t.width)
    if op == "cvt":
        t = expr_type(e.args[0], env)
        return FirType(kind="sint", width=t.width + 1)
    if op == "bits":
        hi, lo = e.consts[0], e.consts[1]
        return FirType(kind="uint", width=hi - lo + 1)
    if op == "cat":
        wa = expr_type(e.args[0], env).width
        wb = expr_type(e.args[1], env).width
        return FirType(kind="uint", width=wa + wb)
    if op == "validif":
        return expr_type(e.args[1], env)
    raise LowerError(f"unknown primop {op!r}")


def _agg_total_width(t: FirType) -> int:
    if t.kind in ("bundle", "vector"):
        if t.kind == "bundle":
            return sum(_agg_total_width(ft) for _n, _fl, ft in t.fields)
        return t.size * _agg_total_width(t.elem)
    return getattr(t, "width", 1)


def _widen_type(a: FirType, b: FirType) -> FirType:
    ck = ("clock", "reset", "asyncreset")
    if a.kind in ck or b.kind in ck:
        if a.kind == b.kind:
            return a
        raise LowerError(f"mux of clockish arms {a} / {b}")
    if a.kind in ("bundle", "vector") or b.kind in ("bundle", "vector"):
        if a.kind == "bundle" and b.kind == "bundle" and \
                [n for n, _f, _t in a.fields] == [n for n, _f, _t in b.fields]:
            return a
        if (a.kind == "vector" and b.kind == "vector"
                and a.size == b.size and a.elem.kind == b.elem.kind
                and a.elem.width == b.elem.width):
            return a
        return FirType(kind="uint",
                       width=max(_agg_total_width(a), _agg_total_width(b), 1))
    kind = "sint" if "sint" in (a.kind, b.kind) else "uint"
    return FirType(kind=kind, width=max(a.width, b.width))


# ---------------------------------------------------------------------------
# Env: declarations of one module
# ---------------------------------------------------------------------------

class Env:
    def __init__(self, mod: Module, circuit: Circuit):
        self.mod = mod
        self.circuit = circuit
        self.decls: "OrderedDict[str, object]" = OrderedDict()
        self.types: "OrderedDict[str, FirType | None]" = OrderedDict()
        for p in mod.ports:
            self.decls[p.name] = p
            self.types[p.name] = p.type
        self._walk(mod.body)

    def _walk(self, stmts) -> None:
        for s in stmts:
            if isinstance(s, (Wire, Reg)):
                self.decls[s.name] = s
                self.types[s.name] = s.type
            elif isinstance(s, Node):
                self.decls[s.name] = s
                try:
                    self.types[s.name] = expr_type(s.expr, self)
                except LowerError:
                    self.types[s.name] = None
            elif isinstance(s, Inst):
                self.decls[s.name] = s
                sub = self.circuit.modules.get(s.module)
                if sub is None:
                    raise LowerError(f"inst {s.name}: unknown module {s.module}")
                self.types[s.name] = self.port_bundle_of(sub)
            elif isinstance(s, MemDecl):
                self.decls[s.name] = s
                self.types[s.name] = None
            elif isinstance(s, MemPort):
                self.decls[s.name] = s
                mem = self.decls.get(s.mem)
                self.types[s.name] = mem.type if isinstance(mem, MemDecl) else None
            elif isinstance(s, When):
                self._walk(s.then)
                if s.els:
                    self._walk(s.els)

    def port_bundle_of(self, mod: Module) -> FirType:
        fields = [(p.name, p.direction == "input", p.type) for p in mod.ports]
        return FirType(kind="bundle", fields=fields)

    def ref_type(self, name: str) -> FirType:
        if name not in self.types:
            raise LowerError(f"reference to undeclared {name!r}")
        t = self.types[name]
        if t is None:
            raise LowerError(f"type of {name!r} not resolved")
        return t


# ---------------------------------------------------------------------------
# Module lowering
# ---------------------------------------------------------------------------

class ModuleLower:
    def __init__(self, circuit: Circuit, mod: Module, symbol: str,
                 note=lambda kind, detail: None,
                 stub_oversized_mems: bool = False,
                 mem_reg_bank_cap: int = MAX_BANK_DEPTH):
        self.stub_oversized_mems = stub_oversized_mems
        self.mem_reg_bank_cap = mem_reg_bank_cap
        self._mem_op_seq = 0
        self._fb_on = False
        self.circuit = circuit
        self.mod = mod
        self.symbol = symbol
        self.note = note
        self.env = Env(mod, circuit)
        self.leaves: "OrderedDict[str, Leaf]" = OrderedDict()
        self.leaf_seq = 0
        self.lines: list = []
        self.const_lines: list = []
        self.const_memo: dict = {}
        self.tmp_n = 0
        self.wire_ssa: dict = {}        # leaf key -> wire SSA (data wires only)
        self.reg_ssa: dict = {}         # leaf key -> Q SSA
        self.reg_next_wire: dict = {}   # leaf key -> next wire SSA
        self.reg_en_wire: dict = {}     # leaf key -> enable wire SSA
        self.inst_out_ssa: dict = {}    # (root, path) -> SSA from instance
        self.stub_ssa: dict = {}        # leaf key -> stubbed extmodule output
        self.node_memo: dict = {}       # (node name, path) -> (ssa, w, signed)
        self.node_busy: set = set()
        self.val_memo: dict = {}        # (id(expr), w, signed) -> (ssa, w, signed)
        self._val_keep: dict = {}       # id(expr) -> expr; keeps temporaries
                                        # alive so id() keys stay unique
        self.inst_info: dict = {}       # inst name -> info dict
        self.mems: dict = {}            # mem name -> info dict
        self.memport_info: dict = {}    # port name -> info dict
        self.reg_info: dict = {}        # reg root -> Reg stmt
        self.inst_names_used: set = set()
        self.inst_width_hints: dict = {}  # (child module name, path) -> width
        self.arg_names_used: set = set()
        self.clock_leaf: "Leaf | None" = None
        self.reset_leaf: "Leaf | None" = None
        self.fixmap: dict = {}
        self.stats = {"approx_reset": 0, "approx_clock": 0, "extmod_inst": 0,
                      "dyn_lhs": 0, "dyn_read_err": 0, "default_width": 0,
                      "stubbed_mem": 0}

    # ---------------- leaf registry ----------------
    def leaf(self, root: str, path: str, ftype: FirType, is_input: bool = False) -> Leaf:
        key = root + path
        lf = self.leaves.get(key)
        if lf is None:
            lf = Leaf(root, path, ftype, self.leaf_seq)
            self.leaf_seq += 1
            lf.is_input = is_input
            self.leaves[key] = lf
        return lf

    def leaves_of_decl(self, root: str, ftype: FirType, is_input=False) -> list:
        return [self.leaf(root, p, t, is_input) for p, t, _f in ftype.leaves()
                if not (t.kind in ("uint", "sint") and t.width == 0)]

    def port_leaves(self, port: Port) -> list:
        """Leaves of a module port with flip-XOR'd directions (zero-width dropped)."""
        base_in = port.direction == "input"
        out = []
        for p, t, flipped in port.type.leaves():
            if t.kind in ("uint", "sint") and t.width == 0:
                continue
            out.append(self.leaf(port.name, p, t, is_input=(base_in != flipped)))
        return out

    # ---------------- pass A: collect ----------------
    def collect(self) -> None:
        for p in self.mod.ports:
            if p.direction != "input":
                continue
            if p.type.kind == "clock" and self.clock_leaf is None:
                self.clock_leaf = self.leaf(p.name, "", p.type, is_input=True)
            if p.type.kind in ("reset", "asyncreset") and self.reset_leaf is None:
                self.reset_leaf = self.leaf(p.name, "", p.type, is_input=True)
        for p in self.mod.ports:
            self.port_leaves(p)
        self._collect(self.mod.body, None)
        self._infer_widths()

    def _collect(self, stmts, cond) -> None:
        for s in stmts:
            if isinstance(s, Wire):
                self.leaves_of_decl(s.name, s.type)
            elif isinstance(s, Reg):
                self.reg_info[s.name] = s
                self.leaves_of_decl(s.name, s.type)
            elif isinstance(s, Node):
                pass
            elif isinstance(s, Inst):
                sub = self.circuit.modules[s.module]
                ins, outs = [], []
                for p in sub.ports:
                    base_in = p.direction == "input"
                    for path, t, flipped in p.type.leaves():
                        if t.kind in ("uint", "sint") and t.width == 0:
                            continue
                        # embed the port name: scalar ports (clock/reset)
                        # would otherwise collide on the empty field path
                        lf = self.leaf(s.name, f".{p.name}" + path, t,
                                       is_input=(base_in != flipped))
                        (ins if lf.is_input else outs).append(lf)
                self.inst_info[s.name] = {"inst": s, "module": sub,
                                          "in": ins, "out": outs,
                                          "symbol": self.child_symbol(sub)}
            elif isinstance(s, MemDecl):
                self._collect_mem(s)
            elif isinstance(s, MemPort):
                self.memport_info[s.name] = {"decl": s, "cond": cond,
                                             "writes": [], "has_write": False}
            elif isinstance(s, Connect):
                self._collect_connect(s, cond)
            elif isinstance(s, Invalidate):
                for kind, lf, extra, _sfx in self._lhs_leaf_list(s.expr,
                                                                True):
                    if lf.is_input or kind == "memport":
                        continue  # invalidate on an input / write port: no-op
                    if extra is None:
                        c = cond
                    elif cond is None:
                        c = extra
                    else:
                        c = Expr(kind="prim", op="and", args=[cond, extra])
                    self._add_driver(kind, lf, c, None, "")
            elif isinstance(s, When):
                c = s.cond if cond is None else Expr(
                    kind="prim", op="and", args=[cond, s.cond])
                self._collect(s.then, c)
                if s.els:
                    nc = Expr(kind="prim", op="not", args=[s.cond])
                    c2 = nc if cond is None else Expr(
                        kind="prim", op="and", args=[cond, nc])
                    self._collect(s.els, c2)
            elif isinstance(s, Skip):
                pass
            else:
                raise LowerError(f"collect: unsupported stmt {type(s).__name__}")

    def sync_leaf_widths_from_types(self) -> None:
        """Final pass: re-read each leaf's width from its nav'd type, so
        leaves created before later inference rounds get the final width."""
        for lf in self.leaves.values():
            if lf.is_clockish or lf.leaf_id in ("clk", "rst"):
                continue
            try:
                t = self._nav(self.env.types[lf.root], lf.path, lf.root)
            except (LowerError, KeyError):
                continue
            if t is not None and t.kind in ("uint", "sint") and t.width >= 0:
                if lf.width != t.width:
                    lf.width = t.width
                    lf.is_zw = (t.width == 0)

    def _coerce_type_widths(self, t: FirType) -> None:
        """Recursively coerce all scalar widths <= 0 to 1 in a type tree."""
        if t is None:
            return
        if t.kind in ("uint", "sint") and t.width <= 0:
            t.width = 1
        elif t.kind == "bundle":
            for _n, _fl, ft in t.fields:
                self._coerce_type_widths(ft)
        elif t.kind == "vector":
            self._coerce_type_widths(t.elem)

    def _infer_widths(self) -> None:
        """Width-less FIRRTL scalars (`UInt` bare) infer from usage and
        drivers: bits()/eq-neq/connect pair constraints propagate through a
        fixed point (covers bare ports too), then first-driver typing covers
        wires/regs/output ports; undriven defaults to 1."""
        cons: list = []   # ("bits", chain_expr, lb) | ("pair", e1, e2)
        LB: dict = {}     # (root, path) -> inferred width for bare chains only

        def chain_key(e0):
            stack = [e0]
            seen = set()
            while stack:
                e = stack.pop()
                if e is None or id(e) in seen:
                    continue
                seen.add(id(e))
                try:
                    root, path = self._peel_static(e)
                except LowerError:
                    # width-max ops propagate the lower bound to operands
                    if e.kind == "prim" and e.op in ("and", "or", "xor", "not"):
                        stack.extend(e.args)
                    continue
                decl = self.env.decls.get(root)
                if decl is None:
                    return None
                if isinstance(decl, Node):
                    stack.append(decl.expr)
                    continue
                if not isinstance(decl, (Port, Wire, Reg)):
                    return None  # inst roots: widths not ours to infer here
                return (root, path)
            return None

        def nom_width(key):
            root, path = key
            try:
                t = self._nav(self.env.types[root], path, root)
                return t.width if t.kind in ("uint", "sint") else None
            except (LowerError, KeyError):
                return None

        def propagate_bits(e0, need0):
            import os as _os
            _dbg = _os.environ.get('F2P_TRACE') and self.mod.name == 'TageTable'
            if _dbg:
                print('PBITS:', str(e0)[:60], 'need', need0)
            """Push a width lower bound through width-monotone prim chains
            and node aliases onto the underlying bare leaves.  Returns True
            when any lower bound grew."""
            changed = False
            stack = [(e0, need0)]
            seen = set()
            import os as _os
            _dbg = _os.environ.get('F2P_DEBUG') and self.mod.name == 'TageTable'
            while stack:
                cur, need = stack.pop()
                if cur is None or need <= 0:
                    continue
                ck = (id(cur), need)
                if ck in seen:
                    continue
                seen.add(ck)
                if _dbg:
                    print('  prop:', str(cur)[:60], 'need', need)
                try:
                    root, path = self._peel_static(cur)
                except LowerError:
                    if cur.kind == "prim":
                        op = cur.op
                        if op in ("and", "or", "xor", "not"):
                            for a in cur.args:
                                stack.append((a, need))
                        elif op == "tail":
                            stack.append((cur.args[0], need + cur.consts[0]))
                        elif op == "shr":
                            stack.append((cur.args[0],
                                          need + max(cur.consts[0], 1) - 1 + 1))
                        elif op == "mux":
                            stack.append((cur.args[1], need))
                            stack.append((cur.args[2], need))
                        elif op == "cat":
                            wa = tb = None
                            try:
                                from fir2pyc.lower import expr_type as _et
                            except ImportError:
                                pass
                            # conservative split: bound both parts by need
                            for a in cur.args:
                                stack.append((a, need))
                        elif op in ("head", "pad", "shl", "cvt", "dshr"):
                            stack.append((cur.args[0], need))
                        continue
                    continue
                decl = self.env.decls.get(root)
                if decl is None:
                    continue
                if isinstance(decl, Node):
                    stack.append((decl.expr, need))
                    continue
                if not isinstance(decl, (Port, Wire, Reg)):
                    continue
                key = (root, path)
                nw = nom_width(key)
                if nw is not None and nw >= 0:
                    continue  # declared width is authoritative: FIRRTL bits()
                    # constraints never widen an explicit decl (patching it
                    # desyncs decl widths from Env's pre-inference node type
                    # snapshots and truncates shl arms -- DivSqrtRawFN_small
                    # rem_Z/sigX_Z bug).  Bare decls (nw < 0) still infer.
                if need > LB.get(key, -1):
                    LB[key] = need
                    changed = True
            return changed

        def width_of(e):
            key = chain_key(e)
            if key is not None and key in LB:
                return LB[key]
            try:
                t = expr_type(e, self.env)
                return t.width if t.kind in ("uint", "sint") else None
            except LowerError:
                return None

        def walk_expr(e):
            if e is None or e.kind in ("lit", "ref"):
                return
            if e.kind in ("subfield", "subindex"):
                walk_expr(e.base)
                return
            if e.kind == "subaccess":
                walk_expr(e.base)
                walk_expr(e.index)
                return
            if e.op == "bits":
                cons.append(("bits", e.args[0], e.consts[0] + 1))
            elif e.op in ("eq", "neq"):
                cons.append(("pair", e.args[0], e.args[1]))
            for a in e.args:
                walk_expr(a)

        def walk_stmts(sts):
            for st in sts:
                if isinstance(st, Node):
                    walk_expr(st.expr)
                elif isinstance(st, Connect):
                    # one-directional: LHS absorbs the RHS width (FIRRTL
                    # pads/truncates the RHS to the LHS width)
                    cons.append(("conn", st.lhs, st.rhs))
                    walk_expr(st.rhs)
                elif isinstance(st, When):
                    walk_expr(st.cond)
                    walk_stmts(st.then)
                    if st.els:
                        walk_stmts(st.els)
                elif isinstance(st, Reg):
                    walk_expr(st.init)
        walk_stmts(self.mod.body)

        import os as _os
        if _os.environ.get('F2P_DEBUG') and self.mod.name == 'TageTable':
            for i, c in enumerate(cons):
                if '_tag_T_3' in str(c[1]):
                    print('CONS idx', i, ':', c[0], str(c[1])[:70], c[2])
            print('total cons:', len(cons))
        # seed init-width lower bounds: a bare reg must hold its reset
        # init literal (FIRRTL InferWidths)
        for _dn, _dd in self.env.decls.items():
            if not isinstance(_dd, Reg) or _dd.init is None:
                continue
            _dt = _dd.type if hasattr(_dd, "type") else None
            if _dt is None or _dt.kind not in ("uint", "sint") \
                    or _dt.width >= 0:
                continue
            try:
                _it = expr_type(_dd.init, self.env)
            except LowerError:
                continue
            if _it.kind in ("uint", "sint") and _it.width > 0:
                _k = (_dn, "")
                LB[_k] = max(LB.get(_k, -1), _it.width)
        for _round in range(16):
            changed = False
            for c in cons:
                if c[0] == "bits":
                    changed |= propagate_bits(c[1], c[2])
                elif c[0] == "conn":
                    ka = chain_key(c[1])
                    if ka is None or nom_width(ka) is not None:
                        continue  # concrete LHS width is authoritative
                    wa = width_of(c[2])
                    if wa is not None and wa >= 1 and wa > LB.get(ka, -1):
                        LB[ka] = wa
                        changed = True
                else:
                    ka, kb = chain_key(c[1]), chain_key(c[2])
                    if ka is None and kb is None:
                        continue
                    wa, wb = width_of(c[1]), width_of(c[2])
                    cands = [x for x in (wa, wb) if x is not None]
                    if not cands:
                        continue
                    m = max(cands)
                    for key in (ka, kb):
                        # bare chains take the comparison width as their
                        # inferred width; concrete decls pad operands at use
                        if key is None or nom_width(key) is not None:
                            continue
                        if m > LB.get(key, -1):
                            LB[key] = m
                            changed = True
            if not changed:
                break
        import os as _os2
        if _os.environ.get('F2P_DEBUG') and self.mod.name == 'TageTable':
            print('LB after fixpoint (tag-related):')
            for k, w in sorted(LB.items()):
                if 'tag' in k[0] or 'ghist' in k[0] or 'update' in k[0]:
                    print('  LB', k, '=', w)
            for nn in ('_tag_T', '_tag_T_1', '_tag_T_2', '_tag_T_3', 'tag_history', 'tag_history_1', 'update_tag'):
                t = self.env.types.get(nn)
                print('  type', nn, '=', t.width if t is not None and t.kind in ('uint','sint') else (t.kind if t else None))

        for key, w in LB.items():
            root, path = key
            try:
                t = self._nav(self.env.types[root], path, root)
                if t.kind in ("uint", "sint") and t.width < w:
                    t.width = w
            except (LowerError, KeyError):
                continue
        # sync bare leaves from their (possibly patched) declared types
        for lf in self.leaves.values():
            if not getattr(lf, "bare", False):
                continue
            try:
                t = self._nav(self.env.types[lf.root], lf.path, lf.root)
            except (LowerError, KeyError):
                continue
            if t.kind in ("uint", "sint") and t.width >= 0 and lf.width != t.width:
                lf.width = t.width
                lf.is_zw = False

        for name, decl in self.env.decls.items():
            is_out_port = isinstance(decl, Port) and decl.direction == "output"
            if not isinstance(decl, (Wire, Reg)) and not is_out_port:
                continue
            t = decl.type
            if t.kind not in ("uint", "sint") or t.width >= 0:
                continue  # width known
            w = None
            for key, lf in self.leaves.items():
                if lf.root != name:
                    continue
                # FIRRTL InferWidths: a REG must also hold its reset INIT
                # value -> the init literal width is a width lower bound
                if isinstance(decl, Reg) and getattr(decl, "init", None) is not None:
                    try:
                        it = expr_type(decl.init, self.env)
                    except LowerError:
                        it = None
                    if it is not None and it.kind in ("uint", "sint") \
                            and it.width and it.width > 0:
                        w = it.width if w is None else max(w, it.width)
                for _c, v, _r in lf.drivers:
                    if v is None:
                        continue
                    try:
                        tv = expr_type(v, self.env)
                    except LowerError:
                        continue
                    # FIRRTL InferWidths: the wire must hold ANY driver's
                    # value -> take the MAX driver width, not the first
                    if tv.kind in ("uint", "sint") and tv.width and \
                            tv.width > 0:
                        w = tv.width if w is None else max(w, tv.width)
            if w is None:
                w = 1
            t.width = w

        # sync inferred widths into every leaf (ports included)
        for lf in self.leaves.values():
            decl = self.env.decls.get(lf.root)
            if decl is None or not hasattr(decl, "type"):
                continue
            try:
                t = self._nav(decl.type, lf.path, lf.root)
            except LowerError:
                continue
            if t.kind in ("uint", "sint"):
                lf.width = t.width
                lf.is_zw = (t.kind in ("uint", "sint") and t.width == 0)

    def _static_chains(self, e):
        """Yield (root_name, static_path) for every ref-rooted static chain
        inside an expression tree (prims recursed, subaccess skipped)."""
        stack = [e]
        while stack:
            cur = stack.pop()
            if cur is None:
                continue
            if cur.kind == "ref":
                yield cur.name, ""
            elif cur.kind in ("subfield", "subindex"):
                chain = []
                c2 = cur
                while c2.kind in ("subfield", "subindex"):
                    chain.append("." + c2.name if c2.kind == "subfield"
                                 else f"[{c2.idx}]")
                    c2 = c2.base
                if c2.kind == "ref":
                    chain.reverse()
                    yield c2.name, "".join(chain)
                elif c2.kind == "prim":
                    stack.append(c2)
            elif cur.kind == "prim":
                stack.extend(cur.args)

    def refresh_inst_hints(self) -> None:
        """Re-derive child port width hints from this module's known leaf
        widths: inst input drivers take the value width; inst output reads
        take the consumer leaf width."""
        self.inst_width_hints = {}
        for lf in self.leaves.values():
            if lf.kind not in ("uint", "sint") or lf.width is None or lf.width < 1:
                continue
            w = lf.width
            info = self.inst_info.get(lf.root)
            if info is not None and lf.is_input and not info["module"].ext:
                ch = info["module"]
                self.inst_width_hints[(ch.name,
                                       _strip_first_seg(lf.path))] = w
            for _c, v, _r in lf.drivers:
                if v is None:
                    continue
                for r, p in self._static_chains(v):
                    ri = self.inst_info.get(r)
                    if ri is not None and not ri["module"].ext:
                        ch = ri["module"]
                        key = (ch.name, _strip_first_seg(p))
                        cur = self.inst_width_hints.get(key)
                        if cur is None or w > cur:
                            self.inst_width_hints[key] = w

    def apply_port_width_hints(self, hints: dict, module_name: str) -> None:
        """Patch bare-UInt port leaf widths from parent instance-point evidence."""
        for (mname, path), w in hints.items():
            if mname != module_name or w is None or w < 1:
                continue
            for lf in self.leaves.values():
                if lf.path == path and lf.kind in ("uint", "sint") and lf.width < 1:
                    lf.width = w
                    lf.is_zw = False
                    self._patch_type_width(self.env.types.get(lf.root),
                                           lf.path, w)

    def default_unresolved_widths(self) -> None:
        """Orphan fallback: bare scalars still unresolved emit width 1
        (documented unsound; only safe for modules with no instance points)."""
        for lf in self.leaves.values():
            if lf.kind in ("uint", "sint") and lf.width < 1:
                lf.width = 1
                lf.is_zw = False
                self.stats["default_width"] += 1
                self.note("default_width", f"{lf.full()}: width defaulted to 1")
        for name, t in self.env.types.items():
            if t is not None:
                self._coerce_type_widths(t)

    def _patch_type_width(self, t: FirType, path: str, w: int) -> None:
        """Set the width of the scalar at `path` inside aggregate type t."""
        if t is None:
            return
        cur = t
        segs = _path_segments(path)
        for i, seg in enumerate(segs):
            last = i == len(segs) - 1
            if seg.startswith("."):
                name = seg[1:]
                nxt = None
                for n, _fl, ft in cur.fields:
                    if n == name:
                        nxt = ft
                        break
                if nxt is None:
                    return
                if last:
                    nxt.width = w
                cur = nxt
            else:
                if last:
                    cur.elem.width = w
                cur = cur.elem


    def _add_driver(self, kind, lf, cond, val, suffix: str = "") -> None:
        rel = ""
        if val is not None:
            try:
                tval = expr_type(val, self.env)
            except LowerError:
                tval = None
            if tval is not None and tval.kind in ("bundle", "vector"):
                rel = lf.path[len(suffix):]
            else:
                rel = ""  # scalar RHS ignores the leaf path
        if kind == "memport":
            self.memport_info[lf.root]["writes"].append((cond, lf.path, val, rel))
            self.memport_info[lf.root]["has_write"] = True
            return
        lf.drivers.append((cond, val, rel))

    def _collect_connect(self, s: Connect, cond) -> None:
        for kind, lf, extra, suffix in self._lhs_leaf_list(s.lhs):
            c = cond if extra is None else Expr(
                kind="prim", op="and", args=[cond, extra])
            if kind == "rev":
                # FIRRTL aggregate connect: flipped members transfer in the
                # opposite direction, i.e. the RHS leaf is driven from here.
                # When the RHS is an expression (not a static ref) there is no
                # drivable target: skip the reverse transfer for that leaf.
                try:
                    rroot, rpath = self._peel_static(s.rhs)
                except LowerError:
                    continue
                rdecl = self.env.decls.get(rroot)
                if rdecl is None or isinstance(rdecl, Node):
                    continue  # no drivable reverse target (node/mem rhs)
                rel = lf.path[len(suffix):]
                rfull = self._join_path(rpath, rel)
                rt = self._nav(self._decl_type(rdecl, rroot), rfull, rroot)
                if rt.kind in ("bundle", "vector"):
                    continue  # deeper mixed aggregate: handled per leaf
                if isinstance(rdecl, (Port, Inst)):
                    continue  # reverse side also undrivable: no-op
                rlf = self.leaf(rroot, rfull, rt)
                rlf.drivers.append(
                    (c, self._rebase(Expr(kind="ref", name=lf.root), lf.path), ""))
                continue
            self._add_driver(kind, lf, c, s.rhs, suffix)

    def _lhs_leaf_list(self, e: Expr, for_invalidate: bool = False) -> list:
        """[(kind, leaf, extra_cond_or_None, suffix)] for a connect/invalidate
        LHS.  With for_invalidate, leaves that cannot be driven (module input
        ports, instance outputs) are skipped instead of raising: invalidating
        them is a legal don't-care hint."""
        # peel the access chain
        chain = []
        cur = e
        while cur.kind in ("subfield", "subindex", "subaccess"):
            chain.append(cur)
            cur = cur.base
        if cur.kind != "ref":
            raise LowerError(f"connect LHS not rooted at a name: {e}")
        rootname = cur.name
        chain.reverse()
        decl = self.env.decls.get(rootname)
        if decl is None:
            raise LowerError(f"connect LHS undeclared {rootname!r}")
        # walk static prefix; on subaccess expand per-element with extra cond
        consumed = 0
        for link in chain:
            if link.kind == "subaccess":
                break
            consumed += 1
        prefix = chain[:consumed]
        rest = chain[consumed:]
        suffix = ""
        for link in prefix:
            if link.kind == "subfield":
                suffix += "." + link.name
            else:
                suffix += f"[{link.idx}]"
        base_t = self._decl_type(decl, rootname)
        out = []
        if not rest:
            t_at = self._nav(base_t, suffix, rootname)
            for path, t, f in t_at.leaves(suffix):
                if t.kind in ("uint", "sint") and t.width == 0:
                    continue
                if isinstance(decl, Wire) and f and path != suffix:
                    # wire-rooted bulk connect: a flipped sub-leaf is
                    # reverse-flow; forward-driving it fabricates
                    # combinational cycles (matches evalfir's rule)
                    continue
                lf = self.leaf(rootname, path, t)
                try:
                    kind = self._leaf_kind(decl, lf)
                except LowerError:
                    if for_invalidate:
                        continue
                    kind = "rev"  # mixed-direction aggregate member
                out.append((kind, lf, None, suffix))
            return out
        dyn_links = [lk for lk in rest if lk.kind == "subaccess"]
        rest_static = ""
        for lk in rest:
            if lk.kind == "subfield":
                rest_static += "." + lk.name
            elif lk.kind == "subindex":
                rest_static += f"[{lk.idx}]"
        self.stats["dyn_lhs"] += 1
        pre_t = self._nav(base_t, suffix, rootname)
        # walk dims: after each subaccess the type must be vector (or final)
        dims = []
        t_cur = pre_t
        for lk in dyn_links:
            if t_cur.kind != "vector":
                raise LowerError(
                    f"dynamic LHS index on non-vector at {rootname}{suffix}")
            dims.append((lk.index, t_cur.size))
            t_cur = t_cur.elem
        if rest_static:
            t_cur = self._nav(t_cur, rest_static, f"{rootname}{suffix}")

        def build_conds(k, acc):
            """product over dynamic dims -> [(cond, dyn_path_tail)]"""
            if k == len(dims):
                return [(acc, "")]
            idx, size = dims[k]
            iw = idx_width(size)
            out = []
            for ei in range(size):
                hit = Expr(kind="prim", op="eq", args=[
                    idx, Expr(kind="lit", value=ei, width=iw)])
                cc = hit if acc is None else Expr(kind="prim", op="and",
                                                  args=[acc, hit])
                for cc2, t2 in build_conds(k + 1, cc):
                    out.append((cc2, f"[{ei}]" + t2))
            return out
        for cond_full, dyn_tail in build_conds(0, None):
            t_at = t_cur
            for path, t, f in t_at.leaves(f"{suffix}{dyn_tail}{rest_static}"):
                if t.kind in ("uint", "sint") and t.width == 0:
                    continue
                if isinstance(decl, Wire) and f and path != f"{suffix}{dyn_tail}{rest_static}":
                    continue  # wire-rooted flipped sub-leaf: reverse-flow
                lf = self.leaf(rootname, path, t)
                try:
                    kind = self._leaf_kind(decl, lf)
                except LowerError:
                    if for_invalidate:
                        continue
                    kind = "rev"
                out.append((kind, lf, cond_full,
                            f"{suffix}{dyn_tail}" + rest_static))
        return out

    def _decl_type(self, decl, rootname: str) -> FirType:
        if isinstance(decl, (Port, Wire, Reg)):
            return decl.type
        if isinstance(decl, Inst):
            return decl_t(decl, self.env)
        if isinstance(decl, MemPort):
            mem = self.env.decls[decl.mem]
            if not isinstance(mem, MemDecl):
                raise LowerError(f"mem port {rootname} on non-mem")
            return mem.type
        if isinstance(decl, MemDecl):
            raise LowerError("direct connect to a memory (use mport)")
        raise LowerError(f"unsupported LHS decl {type(decl).__name__}")

    def _leaf_kind(self, decl, lf: Leaf) -> str:
        if isinstance(decl, Reg):
            return "reg"
        if isinstance(decl, Inst):
            if not lf.is_input:
                raise LowerError(f"cannot drive instance output {lf.full()}")
            return "inst"
        if isinstance(decl, MemPort):
            return "memport"
        if isinstance(decl, Port) and lf.is_input:
            raise LowerError(f"cannot drive module input port {lf.full()}")
        if lf.is_clockish:
            return "clockish"
        return "plain"

    def _nav(self, t: FirType, path: str, root: str) -> FirType:
        cur = t
        for seg in _path_segments(path):
            if seg.startswith("."):
                name = seg[1:]
                if cur.kind != "bundle":
                    raise LowerError(f"navigate .{name} on {cur} at {root}{path}")
                for n, _fl, ft in cur.fields:
                    if n == name:
                        cur = ft
                        break
                else:
                    raise LowerError(f"no field {name} at {root}{path}")
            else:
                idx = int(seg[1:-1])
                if cur.kind != "vector" or idx >= cur.size:
                    raise LowerError(f"bad index {seg} at {root}{path}")
                cur = cur.elem
        return cur

    # ---- memories ----------------
    def _collect_mem(self, s: MemDecl) -> None:
        if s.depth > self.mem_reg_bank_cap:
            if self.stub_oversized_mems:
                self.stats["stubbed_mem"] = self.stats.get("stubbed_mem", 0) + 1
                self.note("stubbed_mem",
                          f"{self.mod.name}.{s.name} depth={s.depth}: reads "
                          f"tied 0, writes dropped (--stub-oversized-mems)")
                self.mems[s.name] = {"decl": s, "stub": True,
                                     "leaf_types": [], "depth": s.depth}
                return
            # map to a real pyc.byte_mem (byte-addressed depth, one op per
            # field); unmappable fields degrade to stubs at emission time
            self.stats["real_mem"] = self.stats.get("real_mem", 0) + 1
            self.note("real_mem",
                      f"{self.mod.name}.{s.name} depth={s.depth}: "
                      f"pyc.byte_mem mapping")
            self.mems[s.name] = {"decl": s, "real": True,
                                 "leaf_types": [(p, t) for p, t, _f in s.type.leaves()],
                                 "depth": s.depth}
            return
        self.mems[s.name] = {"decl": s,
                             "leaf_types": [(p, t) for p, t, _f in s.type.leaves()],
                             "depth": s.depth}

    # ---------------- naming ----------------
    def child_symbol(self, mod: Module) -> str:
        s = re.sub(r"[^A-Za-z0-9_]", "_", mod.name)
        if not re.match(r"^[A-Za-z_]", s):
            s = "m_" + s
        return s

    def _unique(self, name: str, used: set) -> str:
        n = name
        k = 2
        while n in used:
            n = f"{name}_{k}"
            k += 1
        used.add(n)
        return n

    def _arg_name(self, lf: Leaf) -> str:
        memo = self.__dict__.setdefault("_arg_name_memo", {})
        if lf.leaf_id in memo:
            return memo[lf.leaf_id]
        if lf.leaf_id == "rst":
            memo[lf.leaf_id] = "pyc_rst"
            return "pyc_rst"
        if lf.leaf_id == "clk":
            memo[lf.leaf_id] = "pyc_clk"
            return "pyc_clk"
        segs = []
        for seg in _path_segments(lf.path):
            if seg.startswith("."):
                raw = seg[1:]
                ok = re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw)
                segs.append(raw if ok else sanitize_seg(raw, self.fixmap))
            else:
                segs.append(seg)
        name = lf.root + ".".join(_name_join(segs))
        name = re.sub(r"[^A-Za-z0-9_.\[\]]", "_", name)
        # canonical port path: a segment must follow ']' with a dot
        name = re.sub(r"\](?=[A-Za-z_])", "].", name)
        name = self._unique(name, self.arg_names_used)
        memo[lf.leaf_id] = name
        return name

    # ---------------- pass B: emit ----------------
    def infer(self) -> None:
        """Pure inference: collect + width fixpoint.  No emission."""
        self.collect()
        self._infer_widths()

    def emit(self) -> str:
        if not hasattr(self, 'sig_args'):
            self.infer()
            self._emitted = False
        if getattr(self, '_emitted', False):
            return self._emit_cached
        self._emitted = True
        siginfo = self._signature()
        self.lines.append(self._func_header(siginfo))
        self.rst_arg = self._arg_ssa(self.pyc_rst_leaf())
        self._emit_wires()
        self._emit_mems()
        self._emit_regs()
        self._emit_instances()
        self._emit_assigns()
        self._emit_return()
        self.lines.append("  }")
        header = self.lines[0]
        rest = self.lines[1:]
        consts = "".join(cl + "\n" for cl in self.const_lines)
        return header + "\n" + consts + "\n".join(rest) + "\n"

    def _arg_ssa(self, lf: Leaf) -> str:
        try:
            return self.arg_ssa_map[lf.leaf_id]
        except (AttributeError, KeyError):
            # not a signature argument (e.g. a child-port leaf): fall back to
            # the module's primary clock/reset argument by leaf kind
            if lf.kind == "clock" and self.clock_leaf is not None:
                return self._arg_ssa(self.clock_leaf)
            if lf.kind in ("reset", "asyncreset"):
                if self.reset_leaf is not None:
                    return self._arg_ssa(self.reset_leaf)
                return self.rst_arg
            return f"%a{lf.leaf_id}"

    def pyc_rst_leaf(self) -> Leaf:
        if not hasattr(self, "_pyc_rst"):
            lf = Leaf("pyc_rst", "", FirType(kind="reset", width=1), "rst")
            self._pyc_rst = lf
        return self._pyc_rst

    def pyc_clk_leaf(self) -> Leaf:
        """Never-toggling clock tie-off for modules without a clock port."""
        if not hasattr(self, "_pyc_clk"):
            lf = Leaf("pyc_clk", "", FirType(kind="clock", width=1), "clk")
            self._pyc_clk = lf
        return self._pyc_clk

    def _signature(self):
        args = []
        for p in self.mod.ports:
            for lf in self.port_leaves(p):
                if lf.is_input:
                    args.append(lf)
        args.append(self.pyc_clk_leaf())
        args.append(self.pyc_rst_leaf())
        self.sig_args = args
        self.arg_ssa_map = {lf.leaf_id: f"%arg{i}" for i, lf in enumerate(args)}
        sig_args = ", ".join(f"%arg{i}: {lf.pyctype()}" for i, lf in enumerate(args))
        results, rn = [], []
        for p in self.mod.ports:
            for lf in self.port_leaves(p):
                if not lf.is_input:
                    results.append(lf)
                    rn.append(self._arg_name(lf))
        self.sig_results = results
        if results:
            rts = ", ".join(lf.pyctype() for lf in results)
            if len(results) == 1:
                ret = f" -> {rts}"
            else:
                ret = f" -> ({rts})"
        else:
            ret = ""
        sig = f"func.func @{self.symbol}({sig_args}){ret}"
        return sig, results

    def interface(self) -> dict:
        """JSON-able port interface (after emit())."""
        def desc(lf):
            return {"name": self._arg_name(lf), "type": lf.pyctype(),
                    "width": lf.width, "kind": lf.kind,
                    "clock": lf.kind == "clock", "reset": lf.kind in ("reset", "asyncreset"),
                    "tieoff": lf.leaf_id in ("clk", "rst")}
        return {"symbol": self.symbol, "module": self.mod.name,
                "inputs": [desc(lf) for lf in self.sig_args],
                "results": [desc(lf) for lf in self.sig_results]}

    def _func_header(self, siginfo) -> str:
        sig, results = siginfo
        rn = [self._arg_name(lf) for lf in results]
        names = [self._arg_name(lf) for lf in
                 [l for l in self.leaves.values() if l.is_input] + []]
        # arg_names must match signature order; rebuild from signature inputs
        args = []
        for p in self.mod.ports:
            for lf in self.port_leaves(p):
                if lf.is_input:
                    args.append(lf)
        args.append(self.pyc_clk_leaf())
        args.append(self.pyc_rst_leaf())
        argn = [self._arg_name(lf) for lf in args]
        metrics = ZERO_METRICS.replace('"', '\\"')
        q = chr(34)
        return (f"  {sig} attributes {{arg_names = [{', '.join(q + n + q for n in argn)}], "
                f"result_names = [{', '.join(q + n + q for n in rn)}], "
                f"pyc.kind = {q}module{q}, pyc.inline = {q}false{q}, "
                f"pyc.params = {q}{{}}{q}, pyc.base = {q}{self.symbol}{q}, "
                f"pyc.struct.metrics = {q}{metrics}{q}, "
                f"pyc.struct.collections = {q}[]{q}}} {{")

    # ---- wires ----------------
    def _emit_wires(self) -> None:
        for lf in self.leaves.values():
            if lf.kind in ("uint", "sint") and lf.width is not None and lf.width < 1:
                lf.width = 1  # hard coerce: no i-1 / i0 emission
                lf.is_zw = False
        for key, lf in list(self.leaves.items()):
            decl = self.env.decls.get(lf.root)
            if isinstance(decl, Wire) and not lf.is_clockish:
                ssa = self.tmp()
                self.lines.append(f"  {ssa} = pyc.wire : {lf.pyctype()}")
                self.wire_ssa[key] = ssa

    # ---- memories ----------------
    def _emit_mems(self) -> None:
        for mname, info in self.mems.items():
            if info.get("real"):
                continue  # byte_mem ops are emitted lazily (dependency order)
            depth = info["depth"]
            abits = idx_width(depth)
            banks = {}
            for p, t in info["leaf_types"]:
                entries = []
                for _i in range(depth):
                    nw, q = self.tmp(), self.tmp()
                    self.lines.append(f"  {nw} = pyc.wire : i{t.width}")
                    banks.setdefault(p, []).append((nw, q))
                info.setdefault("entry_w", {})[p] = t.width
            info["banks_todo"] = banks
            info["abits"] = abits
        # regs for all bank entries (after all wires so wire list is stable)
        for mname, info in self.mems.items():
            if info.get("real"):
                continue
            for p, entries in info["banks_todo"].items():
                w = info["entry_w"][p]
                for nw, q in entries:
                    self.lines.append(
                        f"  {q} = pyc.reg {self._clock_arg()}, {self.rst_arg}, "
                        f"{self._const(1, 1)}, {nw}, {self._const(0, w)} : i{w}")
            info["banks"] = info.pop("banks_todo")

    def _clock_arg(self) -> str:
        if self.clock_leaf is None:
            self.stats["approx_clock"] += 1
            self.note("approx_clock",
                      f"{self.mod.name}: regs/mems without clock port -> "
                      f"pyc_clk tie-off")
            return self._arg_ssa(self.pyc_clk_leaf())
        return self._arg_ssa(self.clock_leaf)

    # ---- regs ----------------
    def _emit_regs(self) -> None:
        for root, r in self.reg_info.items():
            for key, lf in self.leaves.items():
                if lf.root != root or lf.is_clockish or (lf.kind in ("uint", "sint") and lf.width is not None and lf.width < 1):
                    if lf.kind in ("uint", "sint") and lf.width is not None and lf.width < 1:
                        lf.width = 1
                        lf.is_zw = False
                    continue
                nw, q = self.tmp(), self.tmp()
                self.lines.append(f"  {nw} = pyc.wire : {lf.pyctype()}")
                clk = self._reg_clock(r, root)
                rst, init = self._reg_reset_init(r, lf)
                has_uncond = any(c is None for c, _v, _r in lf.drivers)
                if lf.drivers and not has_uncond:
                    # conditional-only register: enable = OR(driver conds)
                    # so the reg HOLDS its Q when no condition fires (FIRRTL
                    # hold semantics); a const-0 D base is then harmless
                    en = self.tmp()
                    self.lines.append(f"  {en} = pyc.wire : i1")
                    self.reg_en_wire[key] = en
                elif lf.drivers:
                    en = self._const(1, 1)
                else:
                    # no FIRRTL connect at all: the reg must hold its value
                    # forever (en=1 + undriven D would load const 0 -- e.g.
                    # CSRFile reg_misa losing its reset value after cycle 0)
                    en = self._const(0, 1)
                self.lines.append(
                    f"  {q} = pyc.reg {clk}, {rst}, {en}, {nw}, {init} : {lf.pyctype()}")
                self.reg_ssa[key] = q
                self.reg_next_wire[key] = nw

    def _reg_clock(self, r: Reg, root: str) -> str:
        clk = self.clock_leaf
        if clk is None:
            self.stats["approx_clock"] += 1
            self.note("approx_clock",
                      f"{self.mod.name}.{root}: no clock port -> pyc_clk tie-off")
            return self._arg_ssa(self.pyc_clk_leaf())
        if r.clock.kind == "ref" and r.clock.name == clk.root:
            return self._arg_ssa(clk)
        self.stats["approx_clock"] += 1
        self.note("approx_clock",
                  f"{self.mod.name}.{root}: clock expr {r.clock} -> primary clock")
        return self._arg_ssa(clk)

    def _reg_reset_init(self, r: Reg, lf: Leaf) -> tuple:
        if r.reset is None:
            return self.rst_arg, self._const(0, lf.width)
        rs = self.reset_leaf
        if rs is None:
            self.stats["approx_reset"] += 1
            self.note("approx_reset",
                      f"{self.mod.name}.{lf.root}: no reset port -> pyc_rst tie-off")
            return self.rst_arg, self._resolve_init(r.init, lf)
        if r.reset.kind == "ref" and r.reset.name == rs.root:
            rst = self._arg_ssa(rs)
        else:
            self.stats["approx_reset"] += 1
            self.note("approx_reset",
                      f"{self.mod.name}.{lf.root}: reset expr {r.reset} -> primary reset")
            rst = self._arg_ssa(rs)
        return rst, self._resolve_init(r.init, lf)

    def _resolve_init(self, e, lf: Leaf) -> str:
        if e is None:
            return self._const(0, lf.width)
        try:
            tval = expr_type(e, self.env)
            rel = lf.path if (tval.kind in ("bundle", "vector")) else ""
        except LowerError:
            rel = lf.path
        try:
            ssa, w, sg = self._resolve_value(e, lf.width, lf.signed(), rel)
        except LowerError:
            # FIRRTL 1.x `reset => (rst, self_or_prior_reg)` init exprs may
            # reference registers whose SSA is not emitted yet (including
            # the reg being initialized) -- a reset-to-own/prior value has
            # no meaningful hardware init; use constant 0
            return self._const(0, max(lf.width, 1))
        ssa, w, _ = self._extend(ssa, w, sg, lf.width, lf.signed())
        return ssa

    # ---- instances ----------------
    def _emit_instances(self) -> None:
        for iname, info in self.inst_info.items():
            ins, outs = info["in"], info["out"]
            if info["module"].ext:
                self.stats["extmod_inst"] += 1
                for lf in outs:
                    dflt = info["module"].params.get("DEFAULT", "0")
                    try:
                        v = int(dflt)
                    except ValueError:
                        v = 0
                    v &= (1 << max(lf.width, 1)) - 1
                    self.stub_ssa[lf.key()] = self._const(v, max(lf.width, 1))
                continue
            argvals = []
            for lf in ins:
                if lf.is_clockish:
                    argvals.append(self._clockish_value(lf))
                else:
                    ssa, _w, _s = self._fold_driver(lf.drivers, lf)
                    argvals.append(ssa)
            # callee signature carries pyc_clk/pyc_rst tie-offs; pass the
            # parent's primary clock (or its own tie-off) + tie-off reset
            pclk = (self._arg_ssa(self.clock_leaf) if self.clock_leaf is not None
                    else self._arg_ssa(self.pyc_clk_leaf()))
            argvals = argvals + [pclk, self.rst_arg]
            args = ", ".join(argvals)
            base_tys = ", ".join(lf.pyctype() for lf in ins)
            tys_in = (base_tys + ", " if base_tys else "") + \
                "!pyc.clock, !pyc.reset"
            iname_u = self._unique(self._inst_name(iname), self.inst_names_used)
            callee = f"{{callee = @{info['symbol']}, name = \"{iname_u}\"}}"
            if outs:
                reslist = [self.tmp() for _ in outs]
                res_tys = ", ".join(lf.pyctype() for lf in outs)
                if len(outs) == 1:
                    ty = f" : ({tys_in}) -> {res_tys}"
                else:
                    ty = f" : ({tys_in}) -> ({res_tys})"
                self.lines.append(
                    f"  {', '.join(reslist)} = pyc.instance {args} {callee}{ty}")
                for lf, ssa in zip(outs, reslist):
                    self.inst_out_ssa[(lf.root, lf.path)] = ssa
            else:
                self.lines.append(
                    f"  pyc.instance {args} {callee} : ({tys_in}) -> ()")

    def _inst_name(self, iname: str) -> str:
        n = re.sub(r"[^A-Za-z0-9_]", "_", iname)
        return n if re.match(r"^[A-Za-z_]", n) else "i_" + n

    # ---- assigns ----------------
    def _wire_dep_order(self, keys) -> list:
        """Topologically order wire-leaf keys by wire->wire dependencies so a
        wire whose driver cone reads another wire is folded (its cone ops
        emitted) after that wire's pyc.assign.  Leaf-registration order does
        not respect producer order: a whole-bundle connect registers drivers
        for every field at the bundle connect's position, so consumers can
        otherwise precede producers in the emitted IR.  pycc evaluates
        leaf-module combs in IR order when fuse-comb clusters form dependency
        cycles, so consumer-before-producer line order samples stale wire
        cells (DecodeUnit: uop.* imm cone read cs.imm_sel before the PLA
        wrote it).  Values are order-independent (each leaf folds its own
        full driver list); only line order changes.  Cycles fall back to
        registration order for the remaining nodes."""
        keyset = set(keys)
        keys_by_root = {}
        for k in keys:
            keys_by_root.setdefault(self.leaves[k].root, []).append(k)

        def refs_of_expr(e, out, seen):
            if e is None or e.kind == "lit":
                return
            if e.kind == "subaccess":
                refs_of_expr(e.base, out, seen)
                refs_of_expr(e.index, out, seen)
                return
            if e.kind in ("subfield", "subindex", "ref"):
                # static chain: peel to (root, path); a leaf ref depends on
                # exactly the keys under that path (not the whole root, which
                # would create false same-root cycles).
                try:
                    root, path = self._peel_static(e)
                except LowerError:
                    root, path = e.name, ""
                if root in keys_by_root:
                    if path:
                        pre = root + path
                        out.update(k for k in keys_by_root[root]
                                   if k == pre or k.startswith(pre + ".")
                                   or k.startswith(pre + "["))
                    else:
                        out.update(keys_by_root[root])
                d = self.env.decls.get(root)
                if isinstance(d, Node) and root not in seen:
                    seen.add(root)
                    cache = getattr(self, "_noderefs_cache", None)
                    if cache is None:
                        cache = self._noderefs_cache = {}
                    hit = cache.get(root)
                    if hit is None:
                        sub: set = set()
                        refs_of_expr(d.expr, sub, set())
                        hit = cache[root] = sub
                    out.update(hit)
                return
            for a in e.args:
                refs_of_expr(a, out, seen)

        deps = {}
        for k in keys:
            lf = self.leaves[k]
            out: set = set()
            for cond, val, _rel in lf.drivers:
                refs_of_expr(cond, out, set())
                refs_of_expr(val, out, set())
            deps[k] = (out & keyset) - {k}

        indeg = {k: len(deps[k]) for k in keys}
        rdeps: dict = {}
        for k, ds in deps.items():
            for d in ds:
                rdeps.setdefault(d, []).append(k)
        ready = [k for k in keys if indeg[k] == 0]
        order: list = []
        while ready:
            k = ready.pop(0)
            order.append(k)
            for c in rdeps.get(k, ()):
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(order) != len(keys):    # cycle: append rest in reg order
            done = set(order)
            order.extend(k for k in keys if k not in done)
        return order

    def _emit_assigns(self) -> None:
        wire_keys = [
            key for key, lf in list(self.leaves.items())
            if isinstance(self.env.decls.get(lf.root), Wire)
            and not lf.is_clockish]
        # the dependency-ordered emission targets leaf-style modules (pycc
        # evaluates their combs in IR order); on giant hierarchical modules
        # the full driver-expression walk is too slow, and their comb
        # partitioning differs anyway -- keep registration order there
        if len(wire_keys) <= 4000:
            wire_keys = self._wire_dep_order(wire_keys)
        for key in wire_keys:
            lf = self.leaves[key]
            ssa, _w, _s = self._fold_driver(lf.drivers, lf)
            self.lines.append(
                f"  pyc.assign {self.wire_ssa[key]}, {ssa} : {lf.pyctype()}")
        for key, nw in self.reg_next_wire.items():
            lf = self.leaves[key]
            ssa, _w, _s = self._fold_driver(lf.drivers, lf)
            self.lines.append(f"  pyc.assign {nw}, {ssa} : {lf.pyctype()}")
        for key, enw in self.reg_en_wire.items():
            lf = self.leaves[key]
            en = None
            for c, _v, _r in lf.drivers:
                if c is None:
                    continue
                cs, cw, cs_ = self._resolve_value(c, 1, False)
                cs, cw, _ = self._extend(cs, cw, cs_, 1, False)
                if en is None:
                    en = cs
                else:
                    o = self.tmp()
                    self.lines.append(f"  {o} = pyc.or {en}, {cs} : i1, i1 -> i1")
                    en = o
            if en is None:
                en = self._const(1, 1)
            self.lines.append(f"  pyc.assign {enw}, {en} : i1")
        self._emit_mem_writes()

    def _emit_mem_writes(self) -> None:
        for mname, info in self.mems.items():
            if info.get("stub"):
                self.memport_info.update({k: v for k, v in
                                          self.memport_info.items()
                                          if v["decl"].mem != mname})
        wports_by_mem: dict = {}
        for pname, pinfo in self.memport_info.items():
            if pinfo["writes"]:
                wports_by_mem.setdefault(pinfo["decl"].mem, []).append((pname, pinfo))
        for mname, info in self.mems.items():
            if info.get("real"):
                self._real_mem_ensure_all(info)
                continue
            wports = wports_by_mem.get(mname, [])
            for p, entries in info["banks"].items():
                w = info["entry_w"][p]
                for i, (nw, q) in enumerate(entries):
                    acc = q
                    for pname, pinfo in wports:
                        en, addr = self._memport_en_addr(pname, pinfo)
                        data = self._memport_data(pinfo, p, w)
                        hit = self._addr_en_hit(addr, en, i, info["depth"])
                        acc = self._emit_mux(hit, data, acc, w)
                    self.lines.append(f"  pyc.assign {nw}, {acc} : i{w}")

    def _memport_en_addr(self, pname: str, pinfo: dict) -> tuple:
        decl = pinfo["decl"]
        cond = pinfo["cond"]
        en = self._const(1, 1) if cond is None else self._resolve_cond(cond)
        abits = idx_width(self.mems[decl.mem]["depth"])
        addr, _w, _s = self._resolve_value(decl.index, abits, False)
        return en, addr

    def _addr_en_hit(self, addr: str, en: str, i: int, depth: int) -> str:
        abits = idx_width(depth)
        ci = self._const(i, abits)
        eq = self.tmp()
        self.lines.append(f"  {eq} = pyc.eq {addr}, {ci} : i{abits}, i{abits} -> i1")
        hit = self.tmp()
        self.lines.append(f"  {hit} = pyc.and {en}, {eq} : i1, i1 -> i1")
        return hit

    def _memport_data(self, pinfo: dict, field: str, w: int) -> str:
        vals = [(c, v, r) for (c, path, v, r) in pinfo["writes"] if path == field]
        use = vals if vals else whole
        if not use:
            return self._const(0, w)
        ssa, _w, _s = self._fold_pairs(use, w, False)
        return ssa

    # ---- real memories (pyc.byte_mem; contract verified against pyCircuit-dev) ----
    # pyc.byte_mem: async read, sync write, DEPTH in BYTES, byte address =
    # word address << log2(bytes-per-entry); wstrb width == ceil(dataW/8).
    # One op per field (leaf path); read ports are replicated ops that share
    # the write operands.  CHIRRTL cmem/smem reads map exactly: "infer" ports
    # read asynchronously, "read" ports reuse the registered addr_q (so data
    # is the committed array at the registered address, like the reg-bank
    # lowering).  pyc.sync_mem is not used: it is registered old-data read
    # (reg-banks return new data) and supports a single read port.
    def _mem_op_name(self) -> str:
        self._mem_op_seq += 1
        n = f"{self.symbol}_mem_{self._mem_op_seq}"
        while n in self.arg_names_used:  # PYC945: no port-path collision
            n += "_"
        return n

    def _or1(self, a: str, b: str) -> str:
        r = self.tmp()
        self.lines.append(f"  {r} = pyc.or {a}, {b} : i1, i1 -> i1")
        return r

    def _real_mem_state(self, info: dict, field: str, lw: int) -> dict:
        st = info.setdefault("fields", {}).setdefault(field, {})
        if "abw" not in st:
            nbytes = max((lw + 7) // 8, 1)
            nb2 = 1 << (nbytes - 1).bit_length()  # pow2 byte stride
            st["w"] = lw
            st["pad"] = nb2 * 8
            st["nb2"] = nb2
            st["abw"] = idx_width(info["depth"]) + (nb2 - 1).bit_length()
        return st

    def _real_mem_write_ops(self, info: dict, field: str) -> tuple:
        """(wvalid, waddr, wdata, wstrb) for one field's single write port."""
        s = info["decl"].name
        st = info["fields"][field]
        pad, nb2, abw = st["pad"], st["nb2"], st["abw"]
        abits = idx_width(info["depth"])
        wports = [(pn, pi) for pn, pi in self.memport_info.items()
                  if pi["decl"].mem == s
                  and any(path == field for _c, path, _v, _r in pi["writes"])]
        if len(wports) > 1:
            raise _RealMemCycle(f"{len(wports)} write ports on one field")
        if not wports:
            return (self._const(0, 1), self._const(0, abw),
                    self._const(0, pad), self._const(0, nb2))
        _pn, pi = wports[0]
        wv = None
        for c, path, _v, _r in pi["writes"]:
            if path != field:
                continue
            cv = self._const(1, 1) if c is None else self._resolve_cond(c)
            wv = cv if wv is None else self._or1(wv, cv)
        if wv is None:
            wv = self._const(0, 1)
        wd = self._memport_data(pi, field, pad)
        addr, _aw, _as = self._resolve_value(pi["decl"].index, abits, False)
        wa, _w2, _s2 = self._extend(addr, abits, False, abw, False)
        if nb2 == 1:
            ws = wv
        else:
            wvn, _xw, _xs = self._extend(wv, 1, False, nb2, False)
            ones = self._const((1 << nb2) - 1, nb2)
            ws = self.tmp()
            self.lines.append(
                f"  {ws} = pyc.and {wvn}, {ones} : i{nb2}, i{nb2} -> i{nb2}")
        return (wv, wa, wd, ws)

    def _real_mem_op(self, info: dict, field: str, raddr_b: str) -> str:
        st = info["fields"][field]
        if st.get("resolving"):
            raise _RealMemCycle("comb read->write cycle")
        st["resolving"] = True
        try:
            wops = st.get("wops")
            if wops is None:
                wops = self._real_mem_write_ops(info, field)
                st["wops"] = wops
        except _RealMemCycle:
            st["failed"] = True
            raise
        finally:
            st["resolving"] = False
        wv, wa, wd, ws = wops
        bm = self.tmp()
        self.lines.append(
            f"  {bm} = pyc.byte_mem {self._clock_arg()}, {self.rst_arg}, "
            f"{raddr_b}, {wv}, {wa}, {wd}, {ws} "
            f"{{depth = {info['depth'] * st['nb2']}, "
            f"name = \"{self._mem_op_name()}\"}} : "
            f"i{st['abw']}, i{st['pad']}, i{st['nb2']}")
        return bm

    def _real_mem_raddr(self, info: dict, decl, st: dict) -> str:
        abits = idx_width(info["depth"])
        declinfo = self.memport_info[decl.name]
        if decl.portkind == "read":
            q_addr = declinfo.get("addr_q")
            if q_addr is None:
                en, addr = self._memport_en_addr(decl.name, declinfo)
                nw, q_addr = self.tmp(), self.tmp()
                self.lines.append(f"  {nw} = pyc.wire : i{abits}")
                self.lines.append(
                    f"  {q_addr} = pyc.reg {self._clock_arg()}, {self.rst_arg}, "
                    f"{en}, {nw}, {self._const(0, abits)} : i{abits}")
                self.lines.append(f"  pyc.assign {nw}, {addr} : i{abits}")
                declinfo["addr_q"] = q_addr
            eaddr = q_addr
        else:  # infer: asynchronous read at the declared index
            eaddr, _aw, _as = self._resolve_value(decl.index, abits, False)
        raddr, _w2, _s2 = self._extend(eaddr, abits, False, st["abw"], False)
        return raddr

    def _real_mem_read(self, info: dict, field: str, lw: int, ls: bool,
                       decl) -> tuple:
        st = self._real_mem_state(info, field, lw)
        if st.get("failed"):
            return self._const(0, lw), lw, ls
        cache = st.setdefault("reads", {})
        if decl.name in cache:
            return cache[decl.name], lw, ls
        if decl.portkind not in ("infer", "read"):
            raise LowerError(f"read of {decl.portkind} mem port {decl.name}")
        if st.get("resolving"):
            raise _RealMemCycle("comb read->write cycle")
        try:
            raddr_b = self._real_mem_raddr(info, decl, st)
            bm = self._real_mem_op(info, field, raddr_b)
        except _RealMemCycle as exc:
            if not st.get("failed"):
                st["failed"] = True
                self.stats["stubbed_mem"] = self.stats.get("stubbed_mem", 0) + 1
                self.note("stubbed_mem",
                          f"{self.mod.name}.{info['decl'].name}{field}: "
                          f"byte_mem unsupported ({exc}); reads tied 0, "
                          f"writes dropped")
            return self._const(0, lw), lw, ls
        ssa = bm
        if st["pad"] != lw:
            ssa, _tw = self._trunc(ssa, st["pad"], lw)
        cache[decl.name] = ssa
        return ssa, lw, ls

    def _real_mem_ensure_all(self, info: dict) -> None:
        """Emit byte_mem ops for write-only fields (read fields emit lazily)."""
        s = info["decl"].name
        for field, t in info["leaf_types"]:
            st = self._real_mem_state(info, field, t.width)
            if st.get("failed") or st.get("wops"):
                continue
            has_writes = any(pi["decl"].mem == s
                             and any(path == field
                                     for _c, path, _v, _r in pi["writes"])
                             for pi in self.memport_info.values())
            if not has_writes and not st.get("reads"):
                continue  # dead field: no storage op needed
            try:
                self._real_mem_op(info, field, self._const(0, st["abw"]))
            except _RealMemCycle as exc:
                st["failed"] = True
                self.stats["stubbed_mem"] = self.stats.get("stubbed_mem", 0) + 1
                self.note("stubbed_mem",
                          f"{self.mod.name}.{s}{field}: byte_mem unsupported "
                          f"({exc}); reads tied 0, writes dropped")

    # ---- return ----------------
    def _emit_return(self) -> None:
        outs = []
        for p in self.mod.ports:
            for lf in self.port_leaves(p):
                if not lf.is_input:
                    outs.append(lf)
        if not outs:
            self.lines.append("  return")
            return
        vals = []
        for lf in outs:
            if lf.is_clockish:
                vals.append(self._clockish_value(lf))
            else:
                ssa, _w, _s = self._fold_driver(lf.drivers, lf)
                vals.append(ssa)
        if len(vals) == 1:
            self.lines.append(f"  return {vals[0]} : {outs[0].pyctype()}")
        else:
            self.lines.append(
                f"  return {', '.join(vals)} : "
                f"{', '.join(lf.pyctype() for lf in outs)}")

    # ---------------- clockish resolution ----------------
    def _clockish_value(self, lf: Leaf, _seen=None) -> str:
        """Resolve a clock/reset leaf to a primary !pyc.clock/!pyc.reset arg."""
        import os as _os
        if _os.environ.get('F2P_TRACE'):
            print('CLKV:', self.mod.name, lf.full(), 'kind', lf.kind,
                  'is_input', lf.is_input, 'ndrivers', len(lf.drivers))
        decl = self.env.decls.get(lf.root)
        if lf.is_input and isinstance(decl, Port):
            return self._arg_ssa(lf)
        seen = _seen or set()
        k = lf.key()
        if k in seen:
            raise LowerError(f"clockish cycle at {lf.full()}")
        seen.add(k)
        # single unconditional passthrough driver?
        passthrough = None
        ok = True
        for cond, val, _rel in lf.drivers:
            if cond is None and val is not None and val.kind == "ref":
                passthrough = val.name
            else:
                ok = False
                break
        if ok and passthrough is not None and passthrough != lf.root:
            decl = self.env.decls.get(passthrough)
            if decl is not None and not isinstance(decl, (MemPort, MemDecl)):
                target_kind = None
                if isinstance(decl, Port):
                    target_kind = decl.type.kind
                elif isinstance(decl, (Wire, Reg)):
                    target_kind = decl.type.kind
                if target_kind in ("clock", "reset", "asyncreset"):
                    tlf = self.leaf(passthrough, "", decl.type,
                                    is_input=isinstance(decl, Port)
                                    and decl.direction == "input")
                    return self._clockish_value(tlf, seen)
        # approximation: primary clock/reset, else the tie-off args
        primary = self.clock_leaf if lf.kind == "clock" else self.reset_leaf
        if primary is None:
            tie = self.pyc_clk_leaf() if lf.kind == "clock" else self.pyc_rst_leaf()
            self.stats["approx_clock"] += 1
            self.note("approx_clock",
                      f"{self.mod.name}.{lf.full()}: clockish drivers "
                      f"({len(lf.drivers)}) -> {tie.root} tie-off")
            return self._arg_ssa(tie)
        self.stats["approx_clock"] += 1
        self.note("approx_clock",
                  f"{self.mod.name}.{lf.full()}: clockish drivers "
                  f"({len(lf.drivers)}) -> primary {primary.root}")
        return self._arg_ssa(primary)

    # ---------------- value resolution (lazy) ----------------
    def tmp(self) -> str:
        self.tmp_n += 1
        return f"%v{self.tmp_n}"

    def _const(self, val: int, width: int) -> str:
        width = max(width, 1)
        val &= (1 << width) - 1
        key = (val, width)
        ssa = self.const_memo.get(key)
        if ssa is not None:
            return ssa
        if val >> 64:  # active bits > 64: pyc.constant would crash the parser
            lo = val & ((1 << 64) - 1)
            hi_width = width - 64
            hi = val >> 64
            lo_ssa = self._const(lo, 64)
            hi_ssa = self._const(hi, hi_width) if hi_width > 64 else \
                self._const_raw(hi, hi_width)
            ssa = self.tmp()
            self.const_lines.append(
                f"  {ssa} = pyc.concat({hi_ssa}, {lo_ssa}) : "
                f"(i{hi_width}, i64) -> i{width}")
            self.const_memo[key] = ssa
            return ssa
        ssa = self._const_raw(val, width)
        self.const_memo[key] = ssa
        return ssa

    def _const_raw(self, val: int, width: int) -> str:
        ssa = self.tmp()
        self.const_lines.append(f"  {ssa} = pyc.constant {val} : i{width}")
        return ssa

    def _fold_driver(self, drivers, lf: Leaf, path: str = "") -> tuple:
        import os as _os
        if _os.environ.get('F2P_TRACE') and lf.width is not None and lf.width <= 0 \
                and lf.kind in ("uint", "sint"):
            import traceback as _tb
            print('FOLD zw/w0 leaf:', lf.full(), 'width', lf.width)
            _tb.print_stack()
        if lf.is_clockish:
            return self._clockish_value(lf), 1, False
        return self._fold_pairs(drivers, lf.width, lf.signed())

    def _fold_pairs(self, pairs, width: int, signed: bool) -> tuple:
        acc = None
        # FIRRTL connect semantics: LAST connect wins.  Pairs are registered
        # in file order, so folding in ORIGINAL order lets later drivers
        # overwrite earlier ones (default+when pattern works correctly).
        for cond, val, rel in pairs:
            if val is None:
                vssa = self._const(0, width)
            else:
                vssa, vw, vs = self._resolve_value(val, width, signed, rel)
                vt = expr_type(val, self.env)
                if vt is not None and vt.kind in ("reset", "asyncreset"):
                    # reset read as data in a fold: expose the boolean view
                    z = self.tmp()
                    self.lines.append(f"  {z} = pyc.reset_active {vssa} : i1")
                    vssa, vw = z, 1
                vssa, vw, vs = self._extend(vssa, vw, vs, width, vs)
            cssa = None if cond is None else self._resolve_cond(cond)
            if acc is None:
                acc = vssa if cssa is None else self._emit_mux(
                    cssa, vssa, self._const(0, width), width)
            else:
                acc = vssa if cssa is None else self._emit_mux(cssa, vssa, acc, width)
        if acc is None:
            acc = self._const(0, width)
        return acc, width, signed

    def _resolve_cond(self, e: Expr) -> str:
        ssa, w, _s = self._resolve_value(e, None, None)
        if w != 1:
            # over-inferred bare scalar used as a condition: take the LSB
            # (the true FIRRTL width was 1)
            ssa, w2 = self._trunc(ssa, max(w, 1), 1)
            ssa = ssa
        return ssa

    def _resolve_value(self, e: Expr, w, signed, path: str = ""):
        key = (id(e), w, signed, path)
        memo = self.val_memo.get(key)
        if memo is not None:
            return memo
        out = self._resolve_inner(e, w, signed, path)
        import os as _os
        if _os.environ.get('F2P_TRACE') and out[1] is not None and out[1] <= 0:
            import traceback as _tb
            print('RESOLVE w<=0:', str(e)[:90], '| w=', w, '| got', out[1])
            tb = _tb.extract_stack()[-4:-1]
            for fr in reversed(tb):
                print('   via', fr.name, ':', fr.line[:70] if fr.line else '')
        self.val_memo[key] = out
        # keep the expr alive: a freed temporary's address can be reused by a
        # later temporary, which would make this id()-keyed entry a false hit
        self._val_keep[id(e)] = e
        return out

    def _resolve_inner(self, e: Expr, w, signed, path: str = "") -> tuple:
        k = e.kind
        if k == "lit":
            if path:
                raise LowerError(f"cannot take path {path} of a literal")
            width = e.width if e.width else max(1, e.value.bit_length())
            ssa = self._const(e.value, width)
            return self._extend(ssa, width, e.signed, w, signed)
        if k == "ref":
            return self._resolve_ref(e.name, path)
        if k in ("subfield", "subindex"):
            if not self._chain_has_dyn(e):
                root, p = self._peel_static(e)
                return self._resolve_ref(root, p + path if not path else
                                         self._join_path(p, path))
        if k == "subaccess" or self._chain_has_dyn(e):
            # chain shape: [ref + static chain][subaccess][static suffix]
            # e.g. entries[idx].tag -> root=entries, idx dynamic, rest=".tag";
            # caller's relative path (leaf suffix within the value) appends
            # after rest so per-element reads stay leaf-accurate.
            rest = ""
            cur = e
            while cur.kind in ("subfield", "subindex"):
                rest = ("." + cur.name if cur.kind == "subfield"
                        else f"[{cur.idx}]") + rest
                cur = cur.base
            if cur.kind != "subaccess":
                raise LowerError(f"access chain rooted in {cur.kind} (aggregate "
                                 f"SSA subfield unsupported in v1)")
            idx_expr = cur.index
            root, sroot_path = self._peel_static(cur.base)
            return self._resolve_subaccess_read(root, sroot_path, idx_expr, w,
                                                signed, rest + path)
        if k == "prim":
            return self._resolve_prim(e, w, signed, path)
        raise LowerError(f"resolve: unsupported expr kind {k}")

    @staticmethod
    def _chain_has_dyn(e: Expr) -> bool:
        cur = e
        while cur is not None and cur.kind in ("subfield", "subindex",
                                               "subaccess"):
            if cur.kind == "subaccess":
                return True
            cur = cur.base
        return False
        if k == "prim":
            return self._resolve_prim(e, w, signed, path)
        raise LowerError(f"resolve: unsupported expr kind {k}")

    @staticmethod
    def _join_path(prefix: str, suffix: str) -> str:
        """Append a leaf suffix (like \"[0]\" or \".f[2]\") to a peeled path."""
        if not suffix:
            return prefix
        if not prefix:
            return suffix
        # suffix may start with '.' or '['; prefix ends with a full segment
        return prefix + suffix

    def _peel_static(self, e: Expr) -> tuple:
        parts = []
        cur = e
        while cur.kind in ("subfield", "subindex"):
            parts.append(("f", cur.name) if cur.kind == "subfield"
                         else ("i", cur.idx))
            cur = cur.base
        if cur.kind != "ref":
            raise LowerError(f"access chain rooted in {cur.kind} (aggregate "
                             f"SSA subfield unsupported in v1)")
        parts.reverse()
        path = "".join("." + v if k == "f" else f"[{v}]" for k, v in parts)
        return cur.name, path

    def _resolve_subaccess_read(self, root, path, idx, w, signed,
                                suffix: str = "") -> tuple:
        decl = self.env.decls.get(root)
        if isinstance(decl, MemPort):
            # dynamic index select over per-element reads is not expressible
            # through a single port; v1 rejects (counted)
            self.stats["dyn_read_err"] += 1
            raise LowerError(
                f"dynamic subaccess read on mem port {root}{path} unsupported")
        # static structure leaf-by-leaf select tree
        base_t = self._decl_type(decl, root) if decl is not None else None
        if base_t is None:
            raise LowerError(f"subaccess on {root} ({type(decl).__name__})")
        pre_t = self._nav(base_t, path, root)
        if pre_t.kind != "vector":
            raise LowerError(f"subaccess on non-vector {root}{path}")
        t_at = self._nav(pre_t.elem, suffix, f"{root}{path}[i]") if suffix \
            else pre_t.elem
        acc = None
        ib = idx_width(pre_t.size)
        lw = ls = None
        for ei in range(pre_t.size):
            ssa, lw, ls = self._resolve_ref(
                root, f"{path}[{ei}]" + suffix)
            cond = self._resolve_cond(Expr(kind="prim", op="eq", args=[
                idx, Expr(kind="lit", value=ei, width=ib)]))
            if acc is None:
                # out-of-range index -> 0 (x-0 convention, matches evalfir's
                # no-driver default); element 0 must be muxed like the rest
                acc = self._const(0, lw)
            acc = self._emit_mux(cond, ssa, acc, lw)
        return acc, lw, ls

    def _resolve_ref(self, root: str, path: str) -> tuple:
        decl = self.env.decls.get(root)
        if decl is None:
            raise LowerError(f"reference to undeclared {root!r}")
        if isinstance(decl, Port):
            t = self._nav(decl.type, path, root)
            if t.kind in ("bundle", "vector"):
                return self._agg_value(root, path, t, is_input=None)
            lf = self.leaf(root, path, t)
            if lf.is_zw:
                return self._const(0, 1), 1, False
            if lf.is_input:
                return self._leaf_value(lf)
            return self._fold_driver(lf.drivers, lf)
        if isinstance(decl, (Wire, Reg)):
            t = self._nav(decl.type, path, root)
            if t.kind in ("bundle", "vector"):
                return self._agg_value(root, path, t)
            lf = self.leaf(root, path, t)
            if lf.is_clockish:
                return self._clockish_value(lf), 1, False
            if lf.is_zw:
                return self._const(0, 1), 1, False
            store = self.wire_ssa if isinstance(decl, Wire) else self.reg_ssa
            ssa = store.get(lf.key())
            if ssa is None:
                raise LowerError(f"leaf {lf.full()} missing SSA")
            return ssa, lf.width, lf.signed()
        if isinstance(decl, Node):
            return self._resolve_node(decl, path)
        if isinstance(decl, Inst):
            t = self._nav(decl_t(decl, self.env), path, root)
            if t.kind in ("bundle", "vector"):
                return self._agg_value_inst(root, path, t)
            lf = self.leaf(root, path, t, is_input=True)
            out = self.inst_out_ssa.get((lf.root, lf.path))
            if out is not None:
                if lf.is_clockish:
                    return out, 1, False
                return out, lf.width, lf.signed()
            if lf.is_clockish:
                return self._clockish_value(lf), 1, False
            ssa, _w, _s = self._fold_driver(lf.drivers, lf)
            return ssa, lf.width, lf.signed()
        if isinstance(decl, MemPort):
            return self._resolve_memport_read(decl, path)
        if isinstance(decl, MemDecl):
            raise LowerError(f"direct read of memory {root} (use mport)")
        raise LowerError(f"reference {root} of unsupported kind")

    def _agg_value_inst(self, root: str, path: str, t: FirType) -> tuple:
        """Whole-aggregate value of an instance result bundle."""
        def build(p, tt):
            if tt.kind in ("uint", "sint") and tt.width == 0:
                return None
            if tt.kind == "bundle":
                parts = [x for x in (build(p + "." + n, ft)
                                     for n, _fl, ft in tt.fields) if x]
            elif tt.kind == "vector":
                parts = [x for x in (build(p + f"[{i}]", tt.elem)
                                     for i in range(tt.size)) if x]
                parts.reverse()
            else:
                ssa = self.inst_out_ssa.get((root, p))
                if ssa is None:
                    raise LowerError(f"instance output leaf {root}{p} missing")
                return ssa, tt.width, False
            total = sum(x[1] for x in parts)
            ssa = self.tmp()
            args = ", ".join(x[0] for x in parts)
            tys = ", ".join(f"i{x[1]}" for x in parts)
            self.lines.append(
                f"  {ssa} = pyc.concat({args}) : ({tys}) -> i{total}")
            return ssa, total, False
        return build(path, t)

    def _agg_value(self, root: str, path: str, t: FirType,
                   is_input: bool = False) -> tuple:
        """Whole-aggregate value = concat of leaf values (firtool layout:
        bundle fields msb-first in declaration order; vector [0] = lsb)."""
        def build(p: str, tt: FirType) -> tuple:
            if tt.kind in ("uint", "sint") and tt.width == 0:
                return (self._const(0, 1), 1, False)
            if tt.kind in ("bundle", "vector"):
                parts = []
                if tt.kind == "bundle":
                    for i, (n, _fl, ft) in enumerate(tt.fields):
                        x = build(p + "." + n, ft)
                        if x is not None:
                            parts.append(x)
                else:
                    for i in range(tt.size):
                        x = build(p + f"[{i}]", tt.elem)
                        if x is not None:
                            parts.append(x)
                    parts.reverse()  # vector lsb-first -> msb-first concat args
                total = sum(x[1] for x in parts)
                ssa = self.tmp()
                args = ", ".join(x[0] for x in parts)
                tys = ", ".join(f"i{x[1]}" for x in parts)
                self.lines.append(
                    f"  {ssa} = pyc.concat({args}) : ({tys}) -> i{total}")
                return ssa, total, False
            lf = self.leaf(root, p, tt, is_input=(is_input is True))
            if lf.is_input:
                return (self._arg_ssa(lf), lf.width, lf.signed())
            ssa, w, s = self._fold_driver(lf.drivers, lf)
            return ssa, w, s
        return build(path, t)

    def _leaf_value(self, lf: Leaf) -> tuple:
        if lf.is_clockish:
            # raw !pyc.clock/!pyc.reset; data-context consumers convert
            return self._arg_ssa(lf), 1, False
        return self._arg_ssa(lf), lf.width, lf.signed()

    def _resolve_node(self, node: Node, path: str) -> tuple:
        key = (node.name, path)
        if key in self.node_memo:
            ssa, nw, ns = self.node_memo[key]
            return ssa, nw, ns
        if key in self.node_busy:
            raise LowerError(f"combinational cycle through node {node.name}{path}")
        self.node_busy.add(key)
        try:
            t = expr_type(node.expr, self.env)
            lt = self._nav(t, path, node.name) if path else t
            if path and node.expr.kind == "prim":
                # structural prim (e.g. mux of bundles): pass the sub-path
                # through to the prim resolver instead of rebasing the expr
                ssa, nw, ns = self._resolve_value(
                    node.expr, lt.width, lt.kind == "sint", path)
            else:
                val = node.expr
                if path:
                    val = self._rebase(val, path)
                ssa, nw, ns = self._resolve_value(val, lt.width,
                                                  lt.kind == "sint")
            self.node_memo[key] = (ssa, nw, ns)
        finally:
            self.node_busy.discard(key)
        return ssa, nw, ns

    def _rebase(self, e: Expr, path: str) -> Expr:
        segs = _path_segments(path)
        for seg in segs:  # leftmost segment wraps first (outermost)
            if seg.startswith("."):
                e = Expr(kind="subfield", base=e, name=seg[1:])
            else:
                e = Expr(kind="subindex", base=e, idx=int(seg[1:-1]))
        return e

    def _resolve_memport_read(self, decl: MemPort, path: str) -> tuple:
        info = self.mems[decl.mem]
        if info.get("stub"):
            return self._const(0, 1), 1, False
        field = path
        if field and not field.startswith((".", "[")):
            field = "." + field
        names = [p for p, _t in info["leaf_types"]]
        if field == "" and len(names) == 1:
            field = names[0]
        elif field not in names:
            raise LowerError(
                f"mem port read {decl.name}{path}: field {field!r} not in mem "
                f"{decl.mem} ({names})")
        lt = dict(info["leaf_types"])[field]
        lw, ls = lt.width, lt.kind == "sint"
        if info.get("real"):
            return self._real_mem_read(info, field, lw, ls, decl)
        depth = info["depth"]
        abits = idx_width(depth)
        banks = info["banks"][field]
        declinfo = self.memport_info[decl.name]
        if decl.portkind == "infer":
            addr, _aw, _as = self._resolve_value(decl.index, abits, False)
            acc = None
            for i, (_nw, q) in enumerate(banks):
                hit = self._addr_en_hit(addr, self._const(1, 1), i, depth)
                acc = q if acc is None else self._emit_mux(hit, q, acc, lw)
            return acc, lw, ls
        if decl.portkind == "read":
            q_addr = declinfo.get("addr_q")
            if q_addr is None:
                en, addr = self._memport_en_addr(decl.name, declinfo)
                nw, q_addr = self.tmp(), self.tmp()
                self.lines.append(f"  {nw} = pyc.wire : i{abits}")
                self.lines.append(
                    f"  {q_addr} = pyc.reg {self._clock_arg()}, {self.rst_arg}, "
                    f"{en}, {nw}, {self._const(0, abits)} : i{abits}")
                self.lines.append(f"  pyc.assign {nw}, {addr} : i{abits}")
                declinfo["addr_q"] = q_addr
            acc = None
            for i, (_nw, q) in enumerate(banks):
                hit = self._addr_en_hit(q_addr, self._const(1, 1), i, depth)
                acc = q if acc is None else self._emit_mux(hit, q, acc, lw)
            return acc, lw, ls
        raise LowerError(f"read of {decl.portkind} mem port {decl.name}")

    # ---------------- primitives ----------------
    def _resolve_prim(self, e: Expr, w, signed, path: str = "") -> tuple:
        op = e.op
        if op == "mux":
            sel = self._resolve_cond(e.args[0])
            ta, tb = expr_type(e.args[1], self.env), expr_type(e.args[2], self.env)
            ot = _widen_type(ta, tb)
            otw = max(_agg_total_width(ot), 1)
            a, aww, aas = self._resolve_value(e.args[1], otw,
                                              ot.kind == "sint", path)
            b, bww, bbs = self._resolve_value(e.args[2], otw,
                                              ot.kind == "sint", path)
            a, _aw2, _ = self._extend(a, aww, aas, otw, aas)
            b, _bw2, _ = self._extend(b, bww, bbs, otw, bbs)
            ssa = self._emit_mux(sel, a, b, otw)
            # a SInt mux result must sign-extend on further widening
            # (FIRRTL MuxPad); extending unsigned truncates high 1s
            return self._extend(ssa, otw, ot.kind == "sint", w, signed)
        if path:
            raise LowerError(f"cannot take path {path} of primop {op} result "
                             f"(aggregate primops unsupported in v1)")
        if op == "cat":
            parts = []
            for arg in e.args:
                t = expr_type(arg, self.env)
                if t.kind in ("uint", "sint") and t.width == 0:
                    continue  # zero-width operand contributes nothing
                ssa, aw, _ = self._resolve_value(arg, None, None)
                parts.append((ssa, aw))
            if not parts:
                return self._const(0, max(w, 1)), max(w, 1), False
            if len(parts) == 1:
                ssa, tw = parts[0]
            else:
                total = sum(x[1] for x in parts)
                ssa = self.tmp()
                args = ", ".join(x[0] for x in parts)
                tys = ", ".join(f"i{x[1]}" for x in parts)
                self.lines.append(
                    f"  {ssa} = pyc.concat({args}) : ({tys}) -> i{total}")
                tw = total
            tw = max(tw, 1)
            return self._extend(ssa, tw, False, w, signed)
        if op == "bits":
            x, xw, _ = self._resolve_value(e.args[0], None, None)
            hi, lo = e.consts[0], e.consts[1]
            if hi >= xw or lo > hi:
                raise LowerError(
                    f"bits(i{xw})[{hi}:{lo}] out of range: arg={e.args[0]} "
                    f"module={self.mod.name}")
            n = hi - lo + 1
            if lo == 0 and n == xw:
                ssa = x
            else:
                ssa = self.tmp()
                attr = f"{{lsb = {lo}}}" if n == 1 else f"{{lsb = {lo}, msb = {hi}}}"
                self.lines.append(
                    f"  {ssa} = pyc.extract {x} {attr} : i{xw} -> i{n}")
            return self._extend(ssa, n, False, w, signed)
        if op in ("head", "tail", "pad", "shl", "shr"):
            return self._resolve_widthop(e, w, signed)
        if op == "cvt":
            x, xw, xs = self._resolve_value(e.args[0], None, None)
            ssa = self.tmp()
            self.lines.append(
                f"  {ssa} = pyc.{'sext' if xs else 'zext'} {x} : i{xw} -> i{xw + 1}")
            return self._extend(ssa, xw + 1, True, w, signed)
        if op == "neg":
            t = expr_type(e.args[0], self.env)
            x, xw, xs0 = self._resolve_value(e.args[0], None, None)
            x, xw, _ = self._extend(x, xw, xs0, t.width + 1, False)
            z = self._const(0, t.width + 1)
            ssa = self.tmp()
            self.lines.append(
                f"  {ssa} = pyc.sub {z}, {x} : i{t.width + 1}, i{t.width + 1} -> i{t.width + 1}")
            return self._extend(ssa, t.width + 1, t.kind == "sint", w, signed)
        if op in ("andr", "orr", "xorr"):
            return self._resolve_reduce(e, w, signed)
        if op in ("asUInt", "asSInt"):
            t = expr_type(e.args[0], self.env)
            if t.kind == "reset" or t.kind == "asyncreset":
                if op == "asSInt":
                    raise LowerError("asSInt on reset unsupported in v1")
                x, _xw, _xs = self._resolve_value(e.args[0], None, None)
                ssa = self.tmp()
                self.lines.append(f"  {ssa} = pyc.reset_active {x} : i1")
                return self._extend(ssa, 1, False, w, signed)
            if t.kind == "clock":
                raise LowerError("asUInt on clock unsupported in v1")
            x, xw, _xs = self._resolve_value(e.args[0], None, None)
            # asSInt reinterprets the bit pattern as signed: any later width
            # growth must SIGN-extend (from_s=True); asUInt zero-extends
            return self._extend(x, xw, op == "asSInt", w, signed)
        if op in ("asClock", "asAsyncReset"):
            primary = self.clock_leaf if op == "asClock" else self.reset_leaf
            if primary is None:
                primary = (self.pyc_clk_leaf() if op == "asClock"
                           else self.pyc_rst_leaf())
            self.stats["approx_clock"] += 1
            self.note("approx_clock",
                      f"{self.mod.name}: {op}(data) -> primary {primary.root}")
            return self._arg_ssa(primary), 1, False
        if op in ("add", "sub", "mul", "and", "or", "xor", "not"):
            return self._resolve_arith(e, w, signed)
        if op in ("eq", "neq", "lt", "leq", "gt", "geq"):
            return self._resolve_cmp(e, w, signed)
        if op in ("dshl", "dshr"):
            return self._resolve_dynshift(e, w, signed)
        if op in ("div", "rem"):
            return self._resolve_div(e, w, signed)
        if op == "validif":
            return self._resolve_value(e.args[1], w, signed, path)
        raise LowerError(f"primop {op} unsupported")

    def _resolve_widthop(self, e: Expr, w, signed) -> tuple:
        op = e.op
        t = expr_type(e.args[0], self.env)
        xs = t.kind == "sint"
        x, xw, _ = self._resolve_value(e.args[0], t.width, xs)
        n = e.consts[0]
        if op == "pad":
            out = max(xw, n)
            xe, _ew, _ = self._extend(x, xw, xs, out, xs)
            return self._extend(xe, out, xs, w, signed)
        if op == "head":
            if n > xw:
                raise LowerError(f"head(i{xw},{n})")
            if n == 0:
                return self._const(0, 1), 1, False
            ssa = self.tmp()
            self.lines.append(
                f"  {ssa} = pyc.extract {x} {{lsb = {xw - n}, msb = {xw - 1}}} : i{xw} -> i{n}")
            return self._extend(ssa, n, xs, w, signed)
        if op == "tail":
            keep = xw - n
            if keep <= 0:
                if xs:
                    s0 = self.tmp()
                    self.lines.append(
                        f"  {s0} = pyc.extract {x} {{lsb = {xw - 1}}} : i{xw} -> i1")
                    ssa, _ew, _ = self._extend(s0, 1, True, 1, True)
                else:
                    ssa = self._const(0, 1)
                return ssa, 1, xs
            ssa = self.tmp()
            self.lines.append(
                f"  {ssa} = pyc.extract {x} {{lsb = 0, msb = {keep - 1}}} : i{xw} -> i{keep}")
            return self._extend(ssa, keep, xs, w, signed)
        if op == "shl":
            out = xw + n
            xe, _ew, _ = self._extend(x, xw, xs, out, xs)
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.shli {xe} {{amount = {n}}} : i{out}")
            return self._extend(ssa, out, xs, w, signed)
        if op == "shr":
            out = xw - n
            if out <= 0:
                # FIRRTL shr width = max(xw - n, 0); a 0-bit result is 0
                # (e.g. shr(UInt<2>(3), 2) -> UInt<0>), not the last bit.
                return self._const(0, 1), 1, False
            sh = self.tmp()
            self.lines.append(
                f"  {sh} = pyc.{'ashri' if xs else 'lshri'} {x} {{amount = {n}}} : i{xw}")
            ssa, _ = self._trunc(sh, xw, out)
            return self._extend(ssa, out, xs, w, signed)
        raise LowerError(f"widthop {op}")

    def _resolve_reduce(self, e: Expr, w, signed) -> tuple:
        op = e.op
        t = expr_type(e.args[0], self.env)
        x, xw, xs0 = self._resolve_value(e.args[0], None, None)
        x, xw, _ = self._extend(x, xw, xs0, t.width, t.kind == "sint")
        if op == "andr":
            ones = self._const((1 << xw) - 1, xw)
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.eq {x}, {ones} : i{xw}, i{xw} -> i1")
        elif op == "orr":
            z = self._const(0, xw)
            q = self.tmp()
            self.lines.append(f"  {q} = pyc.eq {x}, {z} : i{xw}, i{xw} -> i1")
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.not {q} : i1")
        else:
            cur, curw = x, xw
            while curw > 1:
                half = curw // 2
                hiw = curw - half
                lo = self.tmp()
                self.lines.append(
                    f"  {lo} = pyc.extract {cur} {{lsb = 0, msb = {half - 1}}} : i{curw} -> i{half}")
                hi = self.tmp()
                self.lines.append(
                    f"  {hi} = pyc.extract {cur} {{lsb = {half}, msb = {curw - 1}}} : i{curw} -> i{hiw}")
                # pad shorter half to match longer before xor
                w_max = max(half, hiw)
                if hiw != half:
                    shorter = lo if half < hiw else hi
                    zex = self.tmp()
                    self.lines.append(
                        f"  {zex} = pyc.zext {shorter} : i{min(half, hiw)} -> i{w_max}")
                    lo_s = zex if half < hiw else lo
                    hi_s = hi if half < hiw else zex
                else:
                    lo_s, hi_s = lo, hi
                nxt = self.tmp()
                self.lines.append(
                    f"  {nxt} = pyc.xor {lo_s}, {hi_s} : i{w_max}, i{w_max} -> i{w_max}")
                cur, curw = nxt, w_max
            ssa = cur
        return self._extend(ssa, 1, False, w, signed)

    def _resolve_arith(self, e: Expr, w, signed) -> tuple:
        op = e.op
        if op == "not":
            t = expr_type(e.args[0], self.env)
            x, xw, xs = self._resolve_value(e.args[0], t.width, t.kind == "sint")
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.not {x} : i{xw}")
            return self._extend(ssa, xw, xs, w, signed)
        if e.args[0] is None or e.args[1] is None:
            raise LowerError(f"prim {e.op} None arg: " + str(e)[:80])
        ta, tb = expr_type(e.args[0], self.env), expr_type(e.args[1], self.env)
        if op == "not":
            x, xw, xs = self._resolve_value(e.args[0], ta.width, ta.kind == "sint")
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.not {x} : i{xw}")
            return self._extend(ssa, xw, xs, w, signed)
        s = (ta.kind == "sint") or (tb.kind == "sint")
        if op in ("add", "sub"):
            out = max(max(ta.width, 0), max(tb.width, 0)) + 1
        elif op == "mul":
            out = max(max(ta.width, 0), 1) + max(max(tb.width, 0), 1)
        else:
            out = max(max(ta.width, 0), max(tb.width, 0), 1)
        a, aw, asg = self._resolve_value(e.args[0], None, None)
        b, bw, bsg = self._resolve_value(e.args[1], None, None)
        a, aw, _ = self._extend(a, aw, asg, out, asg)
        b, bw, _ = self._extend(b, bw, bsg, out, bsg)
        ssa = self.tmp()
        self.lines.append(
            f"  {ssa} = pyc.{op} {a}, {b} : i{out}, i{out} -> i{out}")
        rs = s if op in ("add", "sub", "and", "or", "xor") else (
            ta.kind == "sint" if op == "mul" else False)
        return self._extend(ssa, out, rs, w, signed)

    def _resolve_cmp(self, e: Expr, w, signed) -> tuple:
        op = e.op
        if e.args[1] is None or e.args[0] is None:
            raise LowerError(
                f"prim {e.op} has None arg: args={[str(a)[:40] for a in e.args]}")
        if e.args[0] is None or e.args[1] is None:
            raise LowerError(f"prim {e.op} None arg: " + str(e)[:80])
        ta, tb = expr_type(e.args[0], self.env), expr_type(e.args[1], self.env)
        s = (ta.kind == "sint") or (tb.kind == "sint")
        out = max(max(ta.width, 0), max(tb.width, 0), 1)
        a, aw, asg = self._resolve_value(e.args[0], None, None)
        b, bw, bsg = self._resolve_value(e.args[1], None, None)
        a, _aw3, _ = self._extend(a, aw, asg, out, asg)
        b, _bw3, _ = self._extend(b, bw, bsg, out, bsg)
        if op == "eq":
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.eq {a}, {b} : i{out}, i{out} -> i1")
        elif op == "neq":
            q = self.tmp()
            self.lines.append(f"  {q} = pyc.eq {a}, {b} : i{out}, i{out} -> i1")
            ssa = self.tmp()
            self.lines.append(f"  {ssa} = pyc.not {q} : i1")
        else:
            base = "slt" if s else "ult"
            if op == "lt":
                ssa = self.tmp()
                self.lines.append(f"  {ssa} = pyc.{base} {a}, {b} : i{out}, i{out} -> i1")
            elif op == "leq":
                q = self.tmp()
                self.lines.append(f"  {q} = pyc.{base} {b}, {a} : i{out}, i{out} -> i1")
                ssa = self.tmp()
                self.lines.append(f"  {ssa} = pyc.not {q} : i1")
            elif op == "gt":
                ssa = self.tmp()
                self.lines.append(f"  {ssa} = pyc.{base} {b}, {a} : i{out}, i{out} -> i1")
            else:
                q = self.tmp()
                self.lines.append(f"  {q} = pyc.{base} {a}, {b} : i{out}, i{out} -> i1")
                ssa = self.tmp()
                self.lines.append(f"  {ssa} = pyc.not {q} : i1")
        return self._extend(ssa, 1, False, w, signed)

    def _resolve_dynshift(self, e: Expr, w, signed) -> tuple:
        op = e.op
        if e.args[0] is None or e.args[1] is None:
            raise LowerError(f"prim {e.op} None arg: " + str(e)[:80])
        ta, tb = expr_type(e.args[0], self.env), expr_type(e.args[1], self.env)
        sa = ta.kind == "sint"
        x, xw, xs0 = self._resolve_value(e.args[0], None, None)
        x, xw, _ = self._extend(x, xw, xs0, ta.width, sa)
        amt, amw, _ = self._resolve_value(e.args[1], None, None)
        if op == "dshl":
            full = ta.width + (1 << min(max(tb.width, 0), 12)) - 1
            if full > MAX_WIDTH:
                raise LowerError(f"dshl width {full} > cap {MAX_WIDTH}")
            xe, _ew, _ = self._extend(x, xw, sa, full, False)
            ssa = self.tmp()
            self.lines.append(
                f"  {ssa} = pyc.shl {xe}, {amt} : i{full}, i{amw}")
            return self._extend(ssa, full, False, w, signed)
        ssa = self.tmp()
        self.lines.append(
            f"  {ssa} = pyc.{'ashr' if sa else 'lshr'} {x}, {amt} : i{xw}, i{amw}")
        return self._extend(ssa, xw, sa, w, signed)

    def _resolve_div(self, e: Expr, w, signed) -> tuple:
        op = e.op
        if e.args[0] is None or e.args[1] is None:
            raise LowerError(f"prim {e.op} None arg: " + str(e)[:80])
        ta, tb = expr_type(e.args[0], self.env), expr_type(e.args[1], self.env)
        s = (ta.kind == "sint") or (tb.kind == "sint")
        a, aw, asg = self._resolve_value(e.args[0], ta.width, s)
        b, bw, bsg = self._resolve_value(e.args[1], ta.width, s)
        a, _aw3, _ = self._extend(a, aw, asg, ta.width, asg)
        b, _bw3, _ = self._extend(b, bw, bsg, ta.width, bsg)
        pycop = (("sdiv" if s else "udiv") if op == "div"
                 else ("srem" if s else "urem"))
        ssa = self.tmp()
        self.lines.append(
            f"  {ssa} = pyc.{pycop} {a}, {b} : i{ta.width}, i{ta.width} -> i{ta.width}")
        outw = ta.width if op == "div" else min(ta.width, tb.width)
        ssa2, _ = self._trunc(ssa, ta.width, outw)
        return self._extend(ssa2, outw, s, w, signed)

    # ---------------- emit helpers ----------------
    def _emit_mux(self, sel: str, a: str, b: str, width: int) -> str:
        ssa = self.tmp()
        self.lines.append(
            f"  {ssa} = pyc.mux {sel}, {a}, {b} : i1, i{width}, i{width} -> i{width}")
        return ssa

    def _extend(self, ssa, from_w, from_s, to_w, to_s):
        if to_w is None:
            return ssa, from_w, from_s
        if from_w is not None and from_w <= 0:
            # zero-width value: the extension is all-zeros of the target
            return self._const(0, max(to_w, 1)), max(to_w, 1), False
        if from_w == to_w:
            return ssa, to_w, from_s
        if to_w > from_w:
            if from_s and from_w % 64 == 0:
                # pyc::cpp::sext<Out,In> with InWidth % 64 == 0 clobbers the
                # whole low word when the sign bit is set (topFill fills the
                # entire sign word): sign-extend via concat of the replicated
                # sign bit instead (extract+concat/mux are word-safe).
                grow = to_w - from_w
                sb = self.tmp()
                self.lines.append(
                    f"  {sb} = pyc.extract {ssa} {{lsb = {from_w - 1}}} : i{from_w} -> i1")
                if grow == 1:
                    s2 = self.tmp()
                    self.lines.append(
                        f"  {s2} = pyc.concat({sb}, {ssa}) : (i1, i{from_w}) -> i{to_w}")
                else:
                    ones = self._const((1 << grow) - 1, grow)
                    zeros = self._const(0, grow)
                    m = self.tmp()
                    self.lines.append(
                        f"  {m} = pyc.mux {sb}, {ones}, {zeros} : i1, i{grow}, i{grow} -> i{grow}")
                    s2 = self.tmp()
                    self.lines.append(
                        f"  {s2} = pyc.concat({m}, {ssa}) : (i{grow}, i{from_w}) -> i{to_w}")
                return s2, to_w, from_s
            op = "sext" if from_s else "zext"
            s2 = self.tmp()
            self.lines.append(f"  {s2} = pyc.{op} {ssa} : i{from_w} -> i{to_w}")
            return s2, to_w, from_s
        s2, _ = self._trunc(ssa, from_w, to_w)
        return s2, to_w, from_s

    def _trunc(self, ssa, from_w, to_w):
        if to_w >= from_w:
            return ssa, from_w
        s2 = self.tmp()
        self.lines.append(f"  {s2} = pyc.trunc {ssa} : i{from_w} -> i{to_w}")
        return s2, to_w


def sanitize_seg(seg: str, fixmap: dict) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", seg):
        return seg
    fixed = fixmap.get(seg)
    if fixed is None:
        fixed = "f" + re.sub(r"[^A-Za-z0-9_]", "_", seg)
        fixmap[seg] = fixed
    return fixed


# ---------------------------------------------------------------------------
# Circuit-level driver
# ---------------------------------------------------------------------------

def reachable_modules(circuit: Circuit, top: str) -> list:
    seen, order = set(), []
    stack = [top]
    while stack:
        name = stack.pop(0)
        if name in seen:
            continue
        if name not in circuit.modules:
            raise LowerError(f"unknown module {name!r}")
        seen.add(name)
        order.append(name)
        stack.extend(_insts_in(circuit.modules[name].body))
    return order


def _insts_in(stmts) -> list:
    out = []
    for st in stmts:
        if isinstance(st, Inst):
            out.append(st.module)
        elif isinstance(st, When):
            out.extend(_insts_in(st.then))
            if st.els:
                out.extend(_insts_in(st.els))
    return out


def lower_circuit(circuit: Circuit, top: str, note=lambda k, d: None,
                  orphan_width_fallback: bool = False,
                  stub_oversized_mems: bool = False,
                  mem_reg_bank_cap: int = MAX_BANK_DEPTH) -> str:
    order = reachable_modules(circuit, top)
    used_symbols = set()
    symbols = {}
    for name in order:
        mod = circuit.modules[name]
        s = re.sub(r"[^A-Za-z0-9_]", "_", mod.name)
        if not re.match(r"^[A-Za-z_]", s):
            s = "m_" + s
        base, k = s, 2
        while s in used_symbols:
            s = f"{base}_{k}"
            k += 1
        used_symbols.add(s)
        symbols[name] = s
    lines = [f"module attributes {{pyc.top = @{symbols[top]}, "
             f"pyc.frontend.contract = \"pycircuit\"}} {{"]
    global ORPHAN_FB
    ORPHAN_FB = True  # unresolved bare widths coerce to 1 (sound zero-extend)
    lowered_any = True
    lowers = []
    for name in order:
        ml = ModuleLower(circuit, circuit.modules[name], symbols[name],
                         note=note, stub_oversized_mems=stub_oversized_mems,
                         mem_reg_bank_cap=mem_reg_bank_cap)
        lowers.append(ml)
    # bottom-up: infer children first so their bare-port widths are known
    # before parents resolve instance connections
    for ml in reversed(lowers):
        ml.collect()
        ml._infer_widths()

    hint_map = {}
    for _round in range(4):
        changed = False
        for ml in lowers:
            ml.apply_port_width_hints(hint_map, ml.mod.name)
        for ml in lowers:
            ml._infer_widths()
        for ml in lowers:
            before = set(hint_map.items())
            ml.refresh_inst_hints()
            for key, w in ml.inst_width_hints.items():
                cur = hint_map.get(key)
                if cur is None or w > cur:
                    hint_map[key] = w
                    changed = True
            if set(hint_map.items()) != before:
                changed = True
        if not changed:
            break
    for ml in lowers:
        ml._fb_on = True
        ml.default_unresolved_widths()
    # emit-time leaf width sync: leaf.width was copied from the type at
    # creation; by now all inference (child, hint, LB) has patched the types
    # -- re-read the final type width into every leaf
    for ml in lowers:
        ml.sync_leaf_widths_from_types()
    stats_total = {}
    emit_errors = []
    for ml in lowers:
        try:
            lines.append(ml.emit().rstrip("\n"))
        except LowerError as exc:
            emit_errors.append((ml.mod.name, str(exc)[:160]))
            # build stub from the PARENT's inst_info (guaranteed correct count)
            _sa, _sr = [], []
            for _pm2 in lowers:
                for _ii in _pm2.inst_info.values():
                    if _ii["module"] is ml.mod:
                        _sa, _sr = _ii["in"], _ii["out"]
                        break
                if _sa:
                    break
            if _sa:
                a_strs = []
                for lf in _sa:
                    try:
                        t = lf.pyctype()
                    except Exception:
                        t = "i1"
                    a_strs.append(f"%arg{len(a_strs)}: {t}")
                r_strs = []
                for lf in _sr:
                    try:
                        r_strs.append(lf.pyctype())
                    except Exception:
                        r_strs.append("i1")
                an = [f"arg{i}" for i in range(len(_sa))]
                an += ["pyc_clk", "pyc_rst"]
                rn = [f"res{i}" for i in range(len(r_strs))]
                full_metrics = ('{\\22source_loc\\22:0,\\22ast_node_count\\22:0,'
                                '\\22collection_count\\22:0,\\22collection_instance_count\\22:0,'
                                '\\22estimated_inline_cost\\22:0,\\22hardware_call_count\\22:0,'
                                '\\22instance_count\\22:0,\\22loop_count\\22:0,'
                                '\\22module_call_count\\22:0,'
                                '\\22module_family_collection_count\\22:0,'
                                '\\22repeat_pressure\\22:0,\\22repeated_body_clusters\\22:[],'
                                '\\22state_alloc_count\\22:0,\\22state_call_count\\22:0}')
                # add the 2 tieoff args (pyc_clk + pyc_rst) to match the
                # child's sig which always has these trailing args
                n = len(a_strs)
                a_strs.append(f"%arg{n}: !pyc.clock")
                a_strs.append(f"%arg{n + 1}: !pyc.reset")
                a_s = ", ".join(a_strs)
                ret = (f" -> {r_strs[0]}" if len(r_strs) == 1
                       else (f" -> ({', '.join(r_strs)})" if r_strs else ""))
                body_lines = []
                ret_vals = []
                for i, t in enumerate(r_strs):
                    body_lines.append(f"    %r{i} = pyc.constant 0 : {t}")
                    ret_vals.append(f"%r{i}")
                if ret_vals:
                    ret_stmt = (f"    return {', '.join(ret_vals)} : "
                                + ", ".join(r_strs))
                else:
                    ret_stmt = "    return"
                lines.append(
                    f"  func.func @{ml.symbol}({a_s}){ret} attributes {{"
                    f"arg_names = [{', '.join(chr(34) + x + chr(34) for x in an)}], "
                    f"result_names = [{', '.join(chr(34) + x + chr(34) for x in rn)}], "
                    f"pyc.kind = \"module\", pyc.inline = \"false\", "
                    f"pyc.params = \"{{}}\", pyc.base = \"{ml.symbol}\", "
                    f"pyc.struct.metrics = \"{full_metrics}\", pyc.struct.collections = \"[]\" "
                    f"}} {{\n" + "\n".join(body_lines) + "\n" + ret_stmt + "\n  }")
            else:
                lines.append(f"  func.func @{ml.symbol}() attributes {{arg_names = [], result_names = [], pyc.kind = \"module\", pyc.inline = \"false\", pyc.params = \"{{}}\", pyc.base = \"{ml.symbol}\", pyc.struct.metrics = \"{{}}\", pyc.struct.collections = \"[]\"}} {{\n    return\n  }}")
        for k, v in ml.stats.items():
            stats_total[k] = stats_total.get(k, 0) + v
    lines.append("}  // end of circuit")
    for ml in lowers:
        for k, v in ml.stats.items():
            stats_total[k] = stats_total.get(k, 0) + v
    for k, v in stats_total.items():
        if v:
            note("stats", f"{k}={v}")
    for mname, msg in emit_errors:
        note("emit_error", f"{mname}: {msg}")
    return "\n".join(lines) + "\n"
