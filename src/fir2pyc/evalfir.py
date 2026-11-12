"""Cycle-accurate FIRRTL reference evaluator (golden model for leaf modules).

Interprets the parsed FIRRTL AST directly with FIRRTL width semantics on
Python ints (two's-complement bit patterns + per-leaf signed flag).  Used to
cross-check the lowering: same random stimulus into fir2pyc's emitted pyc
MLIR (compiled to C++ by pycc) and into this interpreter must agree per cycle.

v1 boundary: leaf modules only (no inst, no cmem/smem, no attach); bare regs
have undefined init -> 0 (x-0 convention, matching the lowering).
"""

from __future__ import annotations

from .fir_parser import (
    Connect, Expr, FirType, Inst, Invalidate, MemDecl, MemPort, Module, Node,
    Port, Reg, Skip, When, Wire,
)
from .lower import expr_type, LowerError


def _is_agg(e: Expr, ev) -> bool:
    try:
        t = expr_type(e, ev._env())
    except EvalError:
        try:
            from .lower import expr_type as _let
            t = _let(e, ev._lower_env())
        except Exception:
            return bool(getattr(e, "kind", "") == "ref")
    return t is not None and t.kind in ("bundle", "vector")


def to_signed(v: int, w: int) -> int:
    v &= (1 << w) - 1
    return v - (1 << w) if v >> (w - 1) else v


def op_path_ok(e: Expr, path: str) -> bool:
    """v1: prims evaluate on scalars only.  Aggregate-valued prims under a
    leaf path (e.g. a mux of bundles in a whole-aggregate connect) are not
    supported by this evaluator."""
    return path == ""


class EvalError(Exception):
    pass


class ModuleEval:
    def __init__(self, circuit, mod: Module):
        self.mod = mod
        self.circuit = circuit
        for s in mod.body:
            if isinstance(s, (Inst, MemDecl, MemPort)):
                raise EvalError(f"leaf-only evaluator: {mod.name} has "
                                f"{type(s).__name__}")
        self.regs: dict = {}     # (root, path) -> int bit pattern
        self.reg_meta: dict = {} # (root, path) -> (width, signed, has_reset, init_expr, reset_expr)
        self.types: dict = {}    # name -> FirType
        self.node_exprs: dict = {}  # name -> node value expr
        self._collect()
        self._infer_bare_widths()

    # ---------------- declarations ----------------
    def _collect(self) -> None:
        self._decl_ports()
        self._walk(self.mod.body)

    def _decl_ports(self) -> None:
        for p in self.mod.ports:
            self.types[p.name] = p.type

    def _walk(self, stmts) -> None:
        for s in stmts:
            if isinstance(s, Wire):
                self.types[s.name] = s.type
            elif isinstance(s, Reg):
                self.types[s.name] = s.type
                self._decl_reg(s)
            elif isinstance(s, Node):
                self.types[s.name] = expr_type(s.expr, self._env())
                self.node_exprs[s.name] = s.expr
            elif isinstance(s, When):
                self._walk(s.then)
                if s.els:
                    self._walk(s.els)

    class _Env:
        def __init__(self, ev):
            self.ev = ev

        def ref_type(self, name):
            t = self.ev.types.get(name)
            if t is None:
                raise EvalError(f"undeclared {name}")
            return t

    def _env(self):
        return ModuleEval._Env(self)

    def _lower_env(self):
        if not hasattr(self, "_lenv"):
            from .lower import Env as LEnv
            self._lenv = LEnv(self.mod, self.circuit)
        return self._lenv

    def _decl_reg(self, s: Reg) -> None:
        t = s.type
        for path, lt, _f in t.leaves():
            key = (s.name, path)
            self.regs[key] = 0
            self.reg_meta[key] = (lt.width, lt.kind == "sint",
                                  s.reset is not None, s.init, s.reset,
                                  s.name, lt.kind)

    def _infer_bare_widths(self) -> None:
        """Resolve bare UInt/SInt decls (parser width < 0) from driver widths
        (fixpoint); unresolved -> 1, matching the lowering's orphan fallback.
        Patches the module's own FirType objects in place, so run the lowering
        on a pristine circuit BEFORE building this evaluator (full mode does)."""
        self._drivers_of()
        for _round in range(16):
            changed = False
            for root, t in self.types.items():
                for path, lt, _fl in t.leaves():
                    if lt.kind not in ("uint", "sint") or lt.width >= 0:
                        continue
                    w = self._driver_width(root, path)
                    if w > 0:
                        lt.width = w
                        meta = self.reg_meta.get((root, path))
                        if meta is not None:
                            self.reg_meta[(root, path)] = (w,) + meta[1:]
                        changed = True
            if not changed:
                break
        for root, t in self.types.items():
            for path, lt, _fl in t.leaves():
                if lt.kind in ("uint", "sint") and lt.width < 0:
                    lt.width = 1
                    meta = self.reg_meta.get((root, path))
                    if meta is not None:
                        self.reg_meta[(root, path)] = (1,) + meta[1:]

    def _driver_width(self, root: str, path: str) -> int:
        w = 0
        for _cond, val, _dbase in self.drv.get((root, path), []):
            try:
                vt = expr_type(val, self._env())
                if path:
                    vt = self._nav(vt, path, root)
            except Exception:
                continue
            if vt.kind in ("uint", "sint") and vt.width > 0:
                w = max(w, vt.width)
        return w

    # ---------------- cycle ----------------
    def reset_cycle(self, inputs: dict) -> dict:
        """One cycle with reset asserted (regreset regs take init)."""
        return self.cycle(inputs, reset=True)

    def cycle(self, inputs: dict, reset: bool = False) -> dict:
        """Advance one clock edge.  inputs: {(portname, leafpath): value}."""
        self.values = {}
        self.busy = set()
        self.comb_memo = {}   # (root, path) -> value; comb values are
                              # deterministic within a cycle, so memoizing
                              # makes each wire/node/leaf evaluate ONCE per
                              # cycle regardless of fanout
        for k, v in inputs.items():
            self.values[k] = v
        # sample next-values from CURRENT state
        nxt = {}
        for key, (w, sg, has_reset, init_e, rst_e, _root, kind) in self.reg_meta.items():
            d = self._reg_next(key)
            if has_reset and reset:
                if init_e is None:
                    nxt[key] = 0
                else:
                    p = key[1] if _is_agg(init_e, self) else ""
                    nxt[key] = self.eval_expr(init_e, p)[0]
            else:
                nxt[key] = d
        outs = self._outputs()
        # commit
        for k, v in nxt.items():
            w = self.reg_meta[k][0]
            self.regs[k] = v & ((1 << w) - 1)
        return outs

    def _outputs(self) -> dict:
        outs = {}
        for p in self.mod.ports:
            base_in = p.direction == "input"
            for path, t, flipped in p.type.leaves():
                if t.kind in ("uint", "sint") and t.width == 0:
                    continue
                is_in = base_in != flipped
                if not is_in:
                    if t.kind in ("clock", "reset", "asyncreset"):
                        outs[(p.name, path)] = 0
                        continue
                    v, _w, _s = self._leaf_value(p.name, path, t)
                    outs[(p.name, path)] = v & ((1 << t.width) - 1)
        return outs

    # ---------------- driver resolution ----------------
    def _drivers_of(self) -> None:
        self.drv: dict = {}   # (root, path) -> [(cond_expr_or_None, val_expr_or_None)]
        reg_roots = {k[0] for k in self.reg_meta}
        port_names = {p.name for p in self.mod.ports}
        # wires = declared roots that are neither ports, regs, nor nodes
        self._wire_roots = {r for r in self.types
                            if r not in reg_roots and r not in port_names
                            and r not in self.node_exprs}
        self._collect_conn(self.mod.body, None)

    def _collect_conn(self, stmts, cond) -> None:
        if not hasattr(self, "drv"):
            self.drv = {}
        for s in stmts:
            if isinstance(s, Connect):
                self._collect_connect(s.lhs, s.rhs, cond)
            elif isinstance(s, Invalidate):
                for rootname, path in self._lhs_paths(s.expr):
                    self.drv.setdefault((rootname, path), []).append(
                        (cond, None, ""))
            elif isinstance(s, When):
                self._collect_conn(s.then, s.cond if cond is None else
                                   self._and(cond, s.cond))
                if s.els:
                    nc = Expr(kind="prim", op="not", args=[s.cond])
                    self._collect_conn(s.els, nc if cond is None else
                                       self._and(cond, nc))

    def _collect_connect(self, lhs: Expr, rhs, cond) -> None:
        """Register drivers for one Connect.  A dynamic subaccess LHS
        (connect ras[io.idx], x, optionally under further subfields like
        entries[idx].age) expands into per-index connects guarded by
        eq(idx, i), matching the lowering's dynamic-LHS mux lowering."""
        chain = []
        cur = lhs
        while cur.kind in ("subfield", "subindex", "subaccess"):
            chain.append(cur)
            cur = cur.base
        if cur.kind != "ref":
            raise EvalError(f"lhs root {cur.kind}")
        di = next((i for i, lk in enumerate(chain)
                   if lk.kind == "subaccess"), None)
        if di is None:
            rootname, base = self._lhs_base(lhs)
            # Bulk connect to a wire: FIRRTL transfers flipped members in the
            # reverse direction, so a flat forward driver on a wire's
            # reverse-flow leaf is a pseudo-driver that can close a nominal
            # combinational loop (e.g. connect anonOut.a, anonIn.a making
            # anonOut.a.ready depend on anonIn.a.ready).  Skip those sub-leaves
            # (only for leaves strictly below the connect base: a direct leaf
            # connect whose LHS is exactly that leaf registers normally) --
            # they keep whatever their own side's connects/invalidate give.
            wire_flips = ({p: f for p, _t, f in self.types[rootname].leaves()}
                          if rootname in self._wire_roots else None)
            for rootname2, path in self._lhs_paths(lhs):
                if (wire_flips is not None and path != base
                        and wire_flips.get(path, False)):
                    continue
                self.drv.setdefault((rootname2, path), []).append(
                    (cond, rhs, base))
            return
        lk = chain[di]
        t = expr_type(lk.base, self._env())
        if t is None or t.kind != "vector":
            raise EvalError(f"subaccess lhs on non-vector {t}")
        for i in range(t.size):
            eq = Expr(kind="prim", op="eq", args=[
                lk.index, Expr(kind="lit", value=i, width=0, signed=False)])
            new = Expr(kind="subindex", base=lk.base, idx=i)
            for lk2 in chain[di + 1:]:
                if lk2.kind == "subfield":
                    new = Expr(kind="subfield", base=new, name=lk2.name)
                elif lk2.kind == "subindex":
                    new = Expr(kind="subindex", base=new, idx=lk2.idx)
                else:
                    new = Expr(kind="subaccess", base=new, index=lk2.index)
            self._collect_connect(
                new, rhs, eq if cond is None else self._and(cond, eq))

    def _and(self, a, b):
        return Expr(kind="prim", op="and", args=[a, b])

    def _lhs_base(self, e: Expr) -> tuple:
        parts = []
        cur = e
        while cur.kind in ("subfield", "subindex"):
            parts.append(("f", cur.name) if cur.kind == "subfield"
                         else ("i", cur.idx))
            cur = cur.base
        if cur.kind != "ref":
            raise EvalError(f"lhs root {cur.kind}")
        parts.reverse()
        return cur.name, "".join("." + v if k == "f" else f"[{v}]"
                                 for k, v in parts)

    def _lhs_paths(self, e: Expr) -> list:
        rootname, base = self._lhs_base(e)
        t = self.types.get(rootname)
        if t is None:
            raise EvalError(f"lhs undeclared {rootname}")
        out = []
        # navigate to the subtype at `base` first: leaves() extends a prefix,
        # it does not descend, so t.leaves(base) would mis-key aggregate LHS.
        for path, _t, _fl in self._nav(t, base, rootname).leaves(base):
            if _t.kind in ("uint", "sint") and _t.width == 0:
                continue
            out.append((rootname, path))
        return out

    def _reg_next(self, key) -> int:
        if not hasattr(self, "drv"):
            self._drivers_of()
        if key in self.busy:
            raise EvalError(f"combinational cycle at {key[0]}{key[1]}")
        self.busy.add(key)
        try:
            root, path = key
            pairs = self.drv.get(key, [])
            acc = self.regs[key]
            w, sg, _hr, _ie, _re, _r, _k = self.reg_meta[key]
            for cond, val, dbase in pairs:
                c = True if cond is None else bool(self.eval_expr(cond)[0])
                rel = path[len(dbase):] if path.startswith(dbase) else ""
                v = 0 if val is None else self.eval_expr(
                    val, rel if _is_agg(val, self) else "")[0]
                acc = v if c else acc
            return acc & ((1 << w) - 1)
        finally:
            self.busy.discard(key)

    def _leaf_value(self, root: str, path: str, t: FirType) -> tuple:
        if not hasattr(self, "drv"):
            self._drivers_of()
        decl = None
        for p in self.mod.ports:
            if p.name == root:
                decl = p
                break
        base_in = decl is not None and decl.direction == "input"
        # recompute flip for this specific path
        if decl is not None:
            for pp, _t, flipped in decl.type.leaves():
                if pp == path:
                    base_in = base_in != flipped
                    break
        key = (root, path)
        if base_in:
            if key not in self.values:
                raise EvalError(f"input {root}{path} not driven this cycle")
            return self.values[key], t.width, t.kind == "sint"
        if key in self.busy:
            raise EvalError(f"combinational cycle at {root}{path}")
        m = getattr(self, "comb_memo", None)
        if m is not None and key in m:
            return m[key], t.width, t.kind == "sint"
        self.busy.add(key)
        try:
            pairs = self.drv.get((root, path), [])
            acc, accw = 0, t.width
            for cond, val, dbase in pairs:
                c = True if cond is None else bool(self.eval_expr(cond)[0])
                rel = path[len(dbase):] if path.startswith(dbase) else ""
                v = 0 if val is None else self.eval_expr(
                    val, rel if _is_agg(val, self) else "")[0]
                acc = v if c else acc
            r = acc & ((1 << t.width) - 1)
            m = getattr(self, "comb_memo", None)
            if m is not None:
                m[key] = r
            return r, t.width, t.kind == "sint"
        finally:
            self.busy.discard(key)

    # ---------------- expression evaluation ----------------
    def eval_expr(self, e: Expr, path: str = "") -> tuple:
        k = e.kind
        if k == "lit":
            if path:
                raise EvalError(f"path {path} of literal")
            return e.value & ((1 << e.width) - 1) if e.width else e.value, \
                e.width, e.signed
        if k == "ref":
            return self._ref_value(e.name, path)
        if k in ("subfield", "subindex", "subaccess"):
            root, p = self._peel(e)
            return self._ref_value(root, p + path)
        if k == "prim":
            if op_path_ok(e, path):
                return self.eval_prim(e)
            if e.op == "mux":
                # aggregate mux under a leaf path: push the path into both
                # arms (mux(cond, a.path, b.path)) and select per the cond
                c, _, _ = self.eval_expr(e.args[0])
                a, w, s = self.eval_expr(e.args[1], path)
                b, w2, s2 = self.eval_expr(e.args[2], path)
                w = max(w, w2)
                if c:
                    v, vw, vs = a, w, s
                else:
                    v, vw, vs = b, w2, s2
                # FIRRTL mux pads the narrower arm to the result width per
                # its own sign kind (UInt zeros / SInt sign bits), matching
                # CIRCT MuxPad -> PadPrimOp.
                if vw < w and vs and vw > 0 and (v >> (vw - 1)) & 1:
                    v |= ((1 << (w - vw)) - 1) << vw
                return v & ((1 << w) - 1), w, s or s2
            raise EvalError(f"path {path} of prim {e.op}")
        raise EvalError(f"expr kind {k}")

    def _peel(self, e: Expr) -> tuple:
        parts = []
        cur = e
        while cur.kind in ("subfield", "subindex", "subaccess"):
            if cur.kind == "subfield":
                parts.append(("f", cur.name))
            elif cur.kind == "subindex":
                parts.append(("i", cur.idx))
            else:
                # dynamic subaccess read: evaluate the index expr
                parts.append(("d", self.eval_expr(cur.index)[0]))
            cur = cur.base
        if cur.kind != "ref":
            raise EvalError("aggregate SSA peel")
        parts.reverse()
        return cur.name, "".join("." + v if kk == "f" else f"[{v}]"
                                 for kk, v in parts)

    def _ref_value(self, root: str, path: str) -> tuple:
        t = self.types.get(root)
        if t is None:
            raise EvalError(f"undeclared {root}")
        if root in self.node_exprs:
            # nodes have no driver entries; evaluate their value expr directly
            nk = ("node", root, path)
            m = getattr(self, "comb_memo", None)
            if m is not None and nk in m:
                return m[nk]
            v = self.eval_expr(self.node_exprs[root], path)
            if m is not None:
                m[nk] = v
            return v
        tt = self._nav(t, path, root)
        if tt.kind in ("bundle", "vector") and path == "":
            return self._agg_value(root, path, tt)
        if (root, path) in self.regs:
            return self.regs[(root, path)], tt.width, tt.kind == "sint"
        return self._leaf_value(root, path, tt)

    def _agg_value(self, root: str, path: str, tt: FirType) -> tuple:
        def build(p, t2):
            if t2.kind == "bundle":
                parts = [build(p + "." + n, ft) for n, _fl, ft in t2.fields]
            elif t2.kind == "vector":
                parts = [build(p + f"[{i}]", t2.elem) for i in range(t2.size)]
                parts.reverse()  # [0] = lsb
            else:
                if (root, p) in self.regs:
                    return self.regs[(root, p)], t2.width
                v, w, _s = self._leaf_value(root, p, t2)
                return v & ((1 << w) - 1), w
            total = sum(x[1] for x in parts)
            acc = 0
            for v, w in parts:
                acc = (acc << w) | (v & ((1 << w) - 1))
            return acc, total
        return build(path, tt)

    def _nav(self, t: FirType, path: str, root: str) -> FirType:
        cur = t
        import re as _re
        for seg in _re.findall(r"\.[^.\[\]]+|\[\d+\]", path):
            if seg.startswith("."):
                name = seg[1:]
                for n, _fl, ft in cur.fields:
                    if n == name:
                        cur = ft
                        break
                else:
                    raise EvalError(f"no field {name}")
            else:
                cur = cur.elem
        return cur

    def eval_prim(self, e: Expr) -> tuple:
        op = e.op
        if op == "mux":
            c, _, _ = self.eval_expr(e.args[0])
            a, w, s = self.eval_expr(e.args[1])
            b, w2, s2 = self.eval_expr(e.args[2])
            w = max(w, w2)
            if c:
                v, vw, vs = a, w, s
            else:
                v, vw, vs = b, w2, s2
            # FIRRTL mux pads the narrower arm to the result width per its
            # own sign kind (UInt zeros / SInt sign bits), matching CIRCT
            # MuxPad -> PadPrimOp.
            if vw < w and vs and vw > 0 and (v >> (vw - 1)) & 1:
                v |= ((1 << (w - vw)) - 1) << vw
            return v & ((1 << w) - 1), w, s or s2
        args = [self.eval_expr(a) for a in e.args]
        if op in ("add", "sub", "mul", "and", "or", "xor", "eq", "neq",
                  "lt", "leq", "gt", "geq", "div", "rem", "dshl", "dshr",
                  "cat"):
            (av, aw, asg), (bv, bw, bsg) = args[0], args[1]
            s = asg or bsg
            if op == "add":
                w = max(aw, bw) + 1
                return ((to_signed(av, aw) + to_signed(bv, bw)) if s
                        else (av + bv)) & ((1 << w) - 1), w, s
            if op == "sub":
                w = max(aw, bw) + 1
                return ((to_signed(av, aw) - to_signed(bv, bw)) if s
                        else (av - bv)) & ((1 << w) - 1), w, s
            if op == "mul":
                w = aw + bw
                return ((to_signed(av, aw) * to_signed(bv, bw)) if s
                        else (av * bv)) & ((1 << w) - 1), w, s
            if op in ("and", "or", "xor"):
                w = max(aw, bw)
                ea = to_signed(av, aw) if s else av
                eb = to_signed(bv, bw) if s else bv
                r = (ea & eb) if op == "and" else \
                    (ea | eb) if op == "or" else (ea ^ eb)
                return r & ((1 << w) - 1), w, s
            if op in ("eq", "neq", "lt", "leq", "gt", "geq"):
                ea = to_signed(av, aw) if s else av
                eb = to_signed(bv, bw) if s else bv
                r = {"eq": ea == eb, "neq": ea != eb, "lt": ea < eb,
                     "leq": ea <= eb, "gt": ea > eb, "geq": ea >= eb}[op]
                return int(r), 1, False
            if op == "div":
                ea = to_signed(av, aw) if s else av
                eb = to_signed(bv, bw) if s else bv
                q = 0 if eb == 0 else int(ea / eb) if s else av // bv
                return q & ((1 << aw) - 1), aw, s
            if op == "rem":
                ea = to_signed(av, aw) if s else av
                eb = to_signed(bv, bw) if s else bv
                r = 0 if eb == 0 else (ea - int(ea / eb) * eb) if s else av % bv
                return r & ((1 << min(aw, bw)) - 1), min(aw, bw), s
            if op == "dshl":
                w = aw + (1 << bw) - 1
                return (av << bv) & ((1 << w) - 1), w, asg
            if op == "dshr":
                r = (to_signed(av, aw) >> bv) if asg else (av >> bv)
                return r & ((1 << aw) - 1), aw, asg
            if op == "cat":
                return ((av << bw) | bv) & ((1 << (aw + bw)) - 1), aw + bw, False
        if op in ("andr", "orr", "xorr"):
            v, w, _ = args[0]
            if op == "andr":
                return int(v == (1 << w) - 1), 1, False
            if op == "orr":
                return int(v != 0), 1, False
            return int(bin(v).count("1") & 1), 1, False
        if op == "not":
            v, w, s = args[0]
            return (~v) & ((1 << w) - 1), w, s
        if op == "neg":
            v, w, s = args[0]
            return (-v) & ((1 << (w + 1)) - 1), w + 1, s
        if op == "pad":
            v, w, s = args[0]
            n = e.consts[0]
            out = max(w, n)
            x = to_signed(v, w) if s else v
            return x & ((1 << out) - 1), out, s
        if op == "head":
            v, w, s = args[0]
            n = e.consts[0]
            return (v >> (w - n)) & ((1 << n) - 1), n, s
        if op == "tail":
            v, w, s = args[0]
            n = e.consts[0]
            keep = max(w - n, 1)
            if s and w - n <= 0:
                sign = (v >> (w - 1)) & 1
                return (-(1 << (keep - 1)) if sign else 0) & ((1 << keep) - 1), keep, s
            return v & ((1 << keep) - 1), keep, s
        if op == "shl":
            v, w, s = args[0]
            n = e.consts[0]
            return (v << n) & ((1 << (w + n)) - 1), w + n, s
        if op == "shr":
            v, w, s = args[0]
            n = e.consts[0]
            r = (to_signed(v, w) >> n) if s else (v >> n)
            return r & ((1 << max(w - n, 1)) - 1), max(w - n, 1), s
        if op == "cvt":
            v, w, s = args[0]
            # cvt = value-preserving convert to SInt: sign-extend a signed
            # operand, ZERO-extend an unsigned one (reinterpreting an
            # unsigned bit pattern as signed is wrong -- DivSqrtRawFN_small
            # trialRem bug)
            x = to_signed(v, w) if s else v
            return x & ((1 << (w + 1)) - 1), w + 1, True
        if op == "bits":
            v, w, s = args[0]
            hi, lo = e.consts[0], e.consts[1]
            return (v >> lo) & ((1 << (hi - lo + 1)) - 1), hi - lo + 1, False
        if op == "asUInt":
            v, w, s = args[0]
            return v & ((1 << max(w, 1)) - 1), max(w, 1), False
        if op == "asSInt":
            v, w, s = args[0]
            return v & ((1 << max(w, 1)) - 1), max(w, 1), True
        raise EvalError(f"prim {op}")
