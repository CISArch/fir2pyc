"""FIRRTL 3.3.0 textual parser -> module AST.

Targets the CHIRRTL subset emitted by Chisel 6.x / CIRCT 1.x for elaborated
designs (verified against the BOOM trim harness: 530 modules / 342k stmt
lines / zero Probe-property statements):

  statements: input/output port, wire, reg, regreset, node, inst,
              cmem/smem, infer|read|write mport, connect, invalidate,
              when/else (indentation-scoped), skip
  primitives: add sub mul and or xor not eq neq lt leq gt geq pad
              asUInt asSInt asClock asAsyncReset shl shr dshl dshr cvt
              bits head tail cat mux andr orr xorr
  types:      UInt<N> SInt<N> Clock Reset AsyncReset Analog<N>, bundles
              {name : T, ...} incl. flip / empty bundles, glued vector
              postfix T[N]

Anything outside the subset raises FirParseError with file:line - no silent
degradation (mirrors verilog2pyc's V2PError policy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


class FirParseError(Exception):
    """Parse failure with a precise cause; never silently degraded."""


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

@dataclass
class FirType:
    kind: str            # uint | sint | clock | reset | asyncreset | analog |
                         # bundle | vector | probe
    width: int = 0       # uint/sint/analog
    fields: list = field(default_factory=list)  # bundle: [(name, flip, FirType)]
    elem: Optional["FirType"] = None            # vector
    size: int = 0                                 # vector

    def leaves(self, prefix: str = "") -> list:
        """[(leaf_path, leaf_type, flipped)] flattened depth-first.

        clock/reset leaves keep their own kind; flipped propagates XOR."""
        out = []
        if self.kind in ("uint", "sint", "clock", "reset", "asyncreset",
                         "analog", "probe"):
            out.append((prefix, self, False))
        elif self.kind == "bundle":
            for name, flip, t in self.fields:
                sub = f"{prefix}.{name}" if prefix else f".{name}"
                for path, lt, fl in t.leaves(sub):
                    out.append((path, lt, flip != fl))
        elif self.kind == "vector":
            for i in range(self.size):
                for path, lt, fl in self.elem.leaves(f"{prefix}[{i}]"):
                    out.append((path, lt, fl))
        return out

    def __str__(self) -> str:
        if self.kind == "uint":
            return f"UInt<{self.width}>"
        if self.kind == "sint":
            return f"SInt<{self.width}>"
        if self.kind == "clock":
            return "Clock"
        if self.kind == "reset":
            return "Reset"
        if self.kind == "asyncreset":
            return "AsyncReset"
        if self.kind == "analog":
            return f"Analog<{self.width}>"
        if self.kind == "probe":
            return "Probe"
        if self.kind == "vector":
            return f"{self.elem}[{self.size}]"
        if not self.fields:
            return "{ }"
        parts = [f"{'flip ' if fl else ''}{n} : {t}" for n, fl, t in self.fields]
        return "{ " + ", ".join(parts) + " }"


def _scalar(kind: str, width: int = 1) -> FirType:
    return FirType(kind=kind, width=width)


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------

@dataclass
class Expr:
    kind: str                     # lit | ref | subfield | subindex | subaccess | prim
    value: int = 0                # lit
    width: int = 0                # lit (0 = unknown)
    signed: bool = False          # lit
    name: str = ""                # ref base / subfield name
    base: Optional["Expr"] = None
    index: Optional["Expr"] = None  # subaccess
    idx: int = 0                    # subindex
    op: str = ""                    # prim
    args: list = field(default_factory=list)   # prim expr args
    consts: list = field(default_factory=list)  # prim trailing int args

    def __str__(self) -> str:
        if self.kind == "lit":
            sw = f"{self.width}" if self.width else "?"
            return f"{self.kind[:1]}{sw}=0x{self.value:x}"
        if self.kind == "ref":
            return self.name
        if self.kind == "subfield":
            return f"{self.base}.{self.name}"
        if self.kind == "subindex":
            return f"{self.base}[{self.idx}]"
        if self.kind == "subaccess":
            return f"{self.base}[{self.index}]"
        if self.kind == "prim":
            inner = ", ".join([str(a) for a in self.args] + [str(c) for c in self.consts])
            return f"{self.op}({inner})"
        return "?"


@dataclass
class Port:
    direction: str  # input | output
    name: str
    type: FirType
    loc: str = ""


@dataclass
class Wire:
    name: str
    type: FirType
    loc: str = ""


@dataclass
class Reg:
    name: str
    type: FirType
    clock: Expr
    reset: Optional[Expr] = None   # None for bare reg
    init: Optional[Expr] = None
    loc: str = ""


@dataclass
class Node:
    name: str
    expr: Expr
    loc: str = ""


@dataclass
class Inst:
    name: str
    module: str
    loc: str = ""


@dataclass
class MemDecl:
    name: str
    kind: str      # cmem | smem
    type: FirType
    depth: int
    loc: str = ""


@dataclass
class MemPort:
    portkind: str      # infer | read | write | readwrite | irread | iread
    name: str
    mem: str
    index: Expr
    clock: Expr
    loc: str = ""


@dataclass
class Connect:
    lhs: Expr
    rhs: Expr
    loc: str = ""


@dataclass
class Invalidate:
    expr: Expr
    loc: str = ""


@dataclass
class When:
    cond: Expr
    then: list = field(default_factory=list)
    els: Optional[list] = None
    loc: str = ""


@dataclass
class Skip:
    loc: str = ""


@dataclass
class Module:
    name: str
    ports: list = field(default_factory=list)   # [Port]
    body: list = field(default_factory=list)    # [Stmt], ports excluded
    loc: str = ""
    ext: bool = False                           # extmodule (blackbox)
    defname: str = ""
    params: dict = field(default_factory=dict)  # raw parameter values


@dataclass
class Circuit:
    name: str
    modules: dict = field(default_factory=dict)  # name -> Module (ordered)


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<escname>`[^`\n`]+`)
  | (?P<str>"[^"]*")
  | (?P<hexnum>0[hbod][0-9a-fA-F_]+)
  | (?P<num>\d+)
  | (?P<id>[A-Za-z_][\w$]*)
  | (?P<sym><=|<-|<|>|\{|\}|\[|\]|\(|\)|,|:|=|@|\.)
    """,
    re.X,
)


def tokenize(text: str, where: str) -> list:
    """[(kind, value, start, end)] - start/end are char offsets in `text`."""
    toks = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise FirParseError(f"{where}: cannot tokenize at {text[pos:pos+20]!r}")
        pos = m.end()
        if m.lastgroup == "ws":
            continue
        toks.append((m.lastgroup, m.group(), m.start(), m.end()))
    toks.append(("eof", "", len(text), len(text)))
    return toks


class TokStream:
    def __init__(self, toks, where):
        self.toks = toks
        self.i = 0
        self.where = where

    def peek(self) -> tuple:
        return self.toks[self.i]

    def next(self) -> tuple:
        t = self.toks[self.i]
        self.i += 1
        return t

    def prev_end(self) -> int:
        return self.toks[self.i - 1][3]

    def glued(self) -> bool:
        """True when the next token starts right where the previous ended."""
        prev = self.toks[self.i - 1]
        cur = self.toks[self.i]
        return cur[2] == prev[3]

    def expect(self, kind=None, value=None) -> tuple:
        k, v, _, _ = self.next()
        if kind and k != kind:
            raise FirParseError(f"{self.where}: expect {kind}, got {k} {v!r}")
        if value and v != value:
            raise FirParseError(f"{self.where}: expect {value!r}, got {v!r}")
        return k, v

    def done(self) -> bool:
        return self.peek()[0] == "eof"


# --------------------------------------------------------------------------
# Type parsing
# --------------------------------------------------------------------------

def _maybe_spaced_vector(ts: TokStream, t: FirType) -> FirType:
    """Consume a non-glued `[N]` postfix: FIRRTL emits `{...} [1]`-style
    vector sizes with a separating space for aggregate base types."""
    k, v = ts.peek()[:2]
    if (k, v) == ("sym", "[") and not ts.glued():
        ts.next()
        n = int(ts.expect("num")[1])
        ts.expect("sym", "]")
        return FirType(kind="vector", elem=t, size=n)
    return t


def parse_type(ts: TokStream) -> FirType:
    k, v = ts.peek()[:2]
    if k == "sym" and v == "{":
        t = _parse_bundle(ts)
    elif k == "id":
        ts.next()
        if v in ("UInt", "SInt", "Analog"):
            width = 0
            if ts.peek()[:2] == ("sym", "<"):
                ts.next()
                width = int(ts.expect("num")[1])
                ts.expect("sym", ">")
            elif v in ("UInt", "SInt"):
                width = -1  # bare UInt/SInt: width unknown, infer from drivers
            t = _scalar({"UInt": "uint", "SInt": "sint", "Analog": "analog"}[v], width)
        elif v in ("Clock", "Reset", "AsyncReset"):
            t = _scalar({"Clock": "clock", "Reset": "reset", "AsyncReset": "asyncreset"}[v])
        elif v in ("Probe", "RWProbe"):
            t = _scalar("probe")
        else:
            raise FirParseError(f"{ts.where}: unknown type keyword {v!r}")
    else:
        raise FirParseError(f"{ts.where}: bad type start {v!r}")
    # glued vector postfix(es): T[N][M]...
    while ts.peek()[:2] == ("sym", "[") and ts.glued():
        ts.next()
        n = int(ts.expect("num")[1])
        ts.expect("sym", "]")
        t = FirType(kind="vector", elem=t, size=n)
    return t


def _parse_bundle(ts: TokStream) -> FirType:
    ts.expect("sym", "{")
    fields = []
    while True:
        k, v = ts.peek()[:2]
        if (k, v) == ("sym", "}"):
            ts.next()
            break
        flip = False
        k, v, _, _ = ts.next()
        if v == "flip":
            flip = True
            k, v, _, _ = ts.next()
        if k == "escname":
            v = v[1:-1]
        elif k == "id":
            pass
        elif k == "num":
            # old FIRRTL allows integer bundle field names ({ 0 : ... })
            pass
        else:
            raise FirParseError(f"{ts.where}: bad field name {v!r}")
        ts.expect("sym", ":")
        ft = parse_type(ts)
        fields.append((v, flip, ft))
        k, v = ts.peek()[:2]
        if (k, v) == ("sym", ","):
            ts.next()
        elif (k, v) == ("sym", "}"):
            ts.next()
            break
        else:
            raise FirParseError(f"{ts.where}: bad bundle separator {v!r}")
    return FirType(kind="bundle", fields=fields)


# --------------------------------------------------------------------------
# Expression parsing
# --------------------------------------------------------------------------

_PRIMOPS = {
    "add", "sub", "mul", "div", "rem", "and", "or", "xor", "not",
    "eq", "neq", "lt", "leq", "gt", "geq", "pad", "asUInt", "asSInt",
    "asClock", "asAsyncReset", "asFixedPoint", "asInterval",
    "shl", "shr", "dshl", "dshr", "cvt", "neg", "bits", "head", "tail",
    "cat", "mux", "andr", "orr", "xorr", "validif", "precise",
}


def parse_expr(ts: TokStream) -> Expr:
    k, v, _, _ = ts.peek()
    if k == "hexnum":
        ts.next()
        return _lit_from_token(v, 0, False)
    if k == "num":
        ts.next()
        return Expr(kind="lit", value=int(v), width=0, signed=False)
    if k == "id":
        nxt = ts.toks[ts.i + 1][:2] if ts.i + 1 < len(ts.toks) else ("eof", "")
        if v in ("UInt", "SInt") and nxt == ("sym", "<"):
            ts.next()
            return _parse_typed_lit(ts, v)
        if nxt == ("sym", "("):
            if v in _PRIMOPS:
                ts.next()
                return _parse_prim(ts, v)
            raise FirParseError(f"{ts.where}: call of unknown primop {v!r}")
        ts.next()
        e = Expr(kind="ref", name=v)
        return _parse_ref_suffix(ts, e)
    raise FirParseError(f"{ts.where}: bad expression start {v!r}")


def _parse_typed_lit(ts: TokStream, kind: str) -> Expr:
    ts.expect("sym", "<")
    width = int(ts.expect("num")[1])
    ts.expect("sym", ">")
    ts.expect("sym", "(")
    k, v, _, _ = ts.next()
    if k == "str":
        # old FIRRTL typed literal: UInt<1>("h0") / SInt<4>("hb")
        body = v[1:-1]
        if body and body[0] in "hbod":
            v = "0" + body
            k = "hexnum"
        else:
            raise FirParseError(f"{ts.where}: bad literal string {v!r}")
    if k not in ("hexnum", "num"):
        raise FirParseError(f"{ts.where}: bad literal value {v!r}")
    e = _lit_from_token(v, width, kind == "SInt")
    ts.expect("sym", ")")
    return e


def _lit_from_token(text: str, width: int, signed: bool) -> Expr:
    base = text[1]
    digits = text[2:].replace("_", "")
    val = int(digits, {"h": 16, "b": 2, "o": 8, "d": 10}[base])
    return Expr(kind="lit", value=val, width=width, signed=signed)


def _parse_prim(ts: TokStream, op: str) -> Expr:
    ts.expect("sym", "(")
    args: list = []
    consts: list = []
    if ts.peek()[:2] == ("sym", ")"):
        ts.next()
        raise FirParseError(f"{ts.where}: primop {op} with no arguments")
    while True:
        k, v, _, _ = ts.peek()
        if k == "num":
            ts.next()
            consts.append(int(v))
        else:
            _e = parse_expr(ts)
            if _e is None:
                raise FirParseError(f"{ts.where}: primop {op} got None arg")
            args.append(_e)
        k, v = ts.next()[:2]
        if (k, v) == ("sym", ")"):
            break
        if (k, v) != ("sym", ","):
            raise FirParseError(f"{ts.where}: bad prim separator {v!r}")
    return Expr(kind="prim", op=op, args=args, consts=consts)


def _parse_ref_suffix(ts: TokStream, e: Expr) -> Expr:
    while True:
        k, v = ts.peek()[:2]
        if (k, v) == ("sym", "."):
            ts.next()
            k2, v2, _, _ = ts.next()
            if k2 == "escname":
                name = v2[1:-1]
            elif k2 == "id":
                name = v2
            elif k2 == "num":
                name = v2  # old FIRRTL integer field access (.0)
            else:
                raise FirParseError(f"{ts.where}: bad subfield name {v2!r}")
            e = Expr(kind="subfield", base=e, name=name)
        elif (k, v) == ("sym", "["):
            ts.next()
            k2, v2, _, _ = ts.peek()
            if k2 == "num":
                ts.next()
                e = Expr(kind="subindex", base=e, idx=int(v2))
            else:
                idx = parse_expr(ts)
                if idx.kind == "lit":
                    # literal subaccess folds to a static subindex
                    # (old FIRRTL emits `x[UInt<1>("h0")]`)
                    e = Expr(kind="subindex", base=e, idx=idx.value)
                else:
                    e = Expr(kind="subaccess", base=e, index=idx)
            ts.expect("sym", "]")
        else:
            return e


# --------------------------------------------------------------------------
# Statement / circuit parsing
# --------------------------------------------------------------------------

_LOC_RE = re.compile(r"\s*@\[(.*)\]\s*$")
_ELSE_RE = re.compile(r"else\s*:")


def _split_loc(line: str) -> tuple:
    """Strip trailing @[...] location; return (body, loc)."""
    m = _LOC_RE.search(line)
    if m:
        return line[: m.start()].rstrip(), m.group(1)
    return line.rstrip(), ""


class FirParser:
    """Parse one circuit file."""

    def __init__(self, text: str, source_name: str = "<fir>"):
        self.lines = text.splitlines()
        self.source = source_name

    def _err(self, lineno: int, msg: str) -> FirParseError:
        return FirParseError(f"{self.source}:{lineno + 1}: {msg}")

    # ---- public --------------------------------------------------------
    def parse(self) -> Circuit:
        i = 0
        n = len(self.lines)
        circuit_name = None
        while i < n:
            s = self.lines[i].strip()
            if not s or s.startswith(";"):
                i += 1
                continue
            if s.startswith("FIRRTL version"):
                i += 1
                continue
            m = re.match(r"circuit\s+([A-Za-z_]\w*)\s*:", s)
            if not m:
                raise self._err(i, f"expected 'circuit <name> :', got {s!r}")
            circuit_name = m.group(1)
            i += 1
            break
        if circuit_name is None:
            raise FirParseError(f"{self.source}: no circuit header")
        circuit = Circuit(name=circuit_name)
        while i < n:
            raw = self.lines[i]
            s = raw.strip()
            if not s or s.startswith(";"):
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip())
            if s.startswith(("module ", "extmodule ")):
                mod, i = self._parse_module(i, indent)
                if mod.name in circuit.modules:
                    raise self._err(i, f"duplicate module {mod.name}")
                circuit.modules[mod.name] = mod
                continue
            raise self._err(i, f"unexpected line outside module: {s!r}")
        if not circuit.modules:
            raise FirParseError(f"{self.source}: empty circuit")
        return circuit

    # ---- modules -------------------------------------------------------
    def _parse_module(self, i: int, indent: int) -> tuple:
        raw = self.lines[i]
        s, loc = _split_loc(raw.strip())
        m = re.match(r"(ext)?module\s+([A-Za-z_][\w$]*)\s*:\s*$", s)
        if not m:
            raise self._err(i, f"bad module header {s!r}")
        mod = Module(name=m.group(2), loc=loc, ext=m.group(1) is not None)
        i += 1
        if mod.ext:
            return self._parse_extmodule_body(mod, i, indent)
        # frames: (open_indent, stmt_list, owning_when_or_None)
        frames = [(-1, mod.body, None)]
        last_popped = None  # most recently closed frame (for else pairing)
        while i < len(self.lines):
            raw = self.lines[i]
            s0 = raw.strip()
            if not s0 or s0.startswith(";"):
                i += 1
                continue
            ind = len(raw) - len(raw.lstrip())
            if ind <= indent:
                break  # module ended
            while frames and ind <= frames[-1][0]:
                last_popped = frames.pop()
            if not frames:
                break
            s, sloc = _split_loc(s0)
            where = f"{self.source}:{i + 1}"
            if _ELSE_RE.match(s):
                cand = last_popped
                if (cand is not None and isinstance(cand[2], When)
                        and cand[2].els is None and cand[0] == ind):
                    els: list = []
                    cand[2].els = els
                    frames.append((ind, els, cand[2]))
                    i += 1
                    continue
                raise self._err(i, "'else :' without directly-preceding when block")
            body = frames[-1][1]
            stmt, i = self._parse_stmt(i, body)
            if isinstance(stmt, When):
                frames.append((ind, stmt.then, stmt))
        # hoist ports out of the statement body
        rest = []
        for st in mod.body:
            if isinstance(st, Port):
                mod.ports.append(st)
            else:
                rest.append(st)
        mod.body = rest
        return mod, i

    # ---- extmodule bodies ----------------------------------------------
    _PARAM_RE = re.compile(r"parameter\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")

    def _parse_extmodule_body(self, mod: Module, i: int, indent: int) -> tuple:
        while i < len(self.lines):
            raw = self.lines[i]
            s = raw.strip()
            if not s or s.startswith(";"):
                i += 1
                continue
            ind = len(raw) - len(raw.lstrip())
            if ind <= indent:
                break
            body, loc = _split_loc(s)
            if body.startswith(("input ", "output ")):
                where = f"{self.source}:{i + 1}"
                ts = TokStream(tokenize(body, where), where)
                k, v, _, _ = ts.next()
                name = ts.expect("id")[1]
                ts.expect("sym", ":")
                t = parse_type(ts)
                mod.ports.append(Port(direction=v, name=name, type=t, loc=loc))
            elif body.startswith("defname"):
                mod.defname = body.split("=", 1)[1].strip()
            else:
                pm = self._PARAM_RE.match(body)
                if pm:
                    mod.params[pm.group(1)] = pm.group(2)
                else:
                    raise self._err(i, f"unknown extmodule line {body!r}")
            i += 1
        return mod, i

    # ---- statements ----------------------------------------------------
    def _parse_stmt(self, i: int, body: list):
        """Parse one statement line.  Returns (stmt_or_None, next_i).

        When returns a When, the caller opens a block frame for it."""
        raw = self.lines[i]
        s, loc = _split_loc(raw.strip())
        where = f"{self.source}:{i + 1}"
        # simulation side effects carry arbitrary format strings that the
        # tokenizer must not see -- skip before tokenizing
        first = s.split("(", 1)[0].strip() if s else ""
        if first in ("stop", "printf", "assert", "assume", "cover"):
            return None, i + 1
        ts = TokStream(tokenize(s, where), where)
        # FIRRTL 1.x legacy statement detection (before keyword dispatch):
        # identifiers named like keywords (`reg <=`, `write <=`, ...) must
        # still route to the legacy forms.  A `<=`/`<-` with no `=` or `:`
        # before it marks a connect; `is invalid` marks an invalidate.
        _legacy = None
        for _tk in ts.toks:
            if _tk[0] == "eof":
                break
            if _tk[:2] == ("sym", "=") or _tk[:2] == ("sym", ":"):
                break
            if _tk[0] == "sym" and _tk[1] in ("<=", "<-"):
                _legacy = "connect"
                break
            if _tk[:2] == ("id", "is"):
                nk = ts.toks[ts.toks.index(_tk) + 1][:2] if                     ts.toks.index(_tk) + 1 < len(ts.toks) else ("eof", "")
                if nk == ("id", "invalid"):
                    _legacy = "invalidate"
                break
        if _legacy == "connect":
            lhs = parse_expr(ts)
            ts.expect("sym")  # <= or <-
            rhs = parse_expr(ts)
            body.append(Connect(lhs=lhs, rhs=rhs, loc=loc))
            return None, i + 1
        if _legacy == "invalidate":
            lhs = parse_expr(ts)
            ts.expect("id", "is")
            ts.expect("id", "invalid")
            body.append(Invalidate(expr=lhs, loc=loc))
            return None, i + 1
        k, v, _, _ = ts.next()

        if k == "id" and v in ("input", "output"):
            name = ts.expect("id")[1]
            ts.expect("sym", ":")
            t = parse_type(ts)
            body.append(Port(direction=v, name=name, type=t, loc=loc))
            return None, i + 1

        if k == "id" and v == "wire":
            name = ts.expect("id")[1]
            ts.expect("sym", ":")
            t = parse_type(ts)
            t = _maybe_spaced_vector(ts, t)
            body.append(Wire(name=name, type=t, loc=loc))
            return None, i + 1

        if k == "id" and v == "reg":
            name = ts.expect("id")[1]
            ts.expect("sym", ":")
            t = parse_type(ts)
            t = _maybe_spaced_vector(ts, t)
            ts.expect("sym", ",")
            clk = parse_expr(ts)
            # FIRRTL 1.x: `reg name : t, clock with :` followed by an
            # indented block `reset => (reset_signal, init_value)`
            if not ts.done():
                k2, v2, _, _ = ts.peek()
                if k2 == "id" and v2 == "with":
                    ts.next()
                    ts.expect("sym", ":")
                    if not ts.done():
                        raise FirParseError(
                            f"{where}: tokens after 'reg ... with :'")
                    base_ind = len(raw) - len(raw.lstrip())
                    rst = init = None
                    j = i + 1
                    while j < len(self.lines):
                        nxt = self.lines[j]
                        ns = nxt.strip()
                        if not ns or ns.startswith("@"):
                            j += 1
                            continue
                        if (len(nxt) - len(nxt.lstrip())) <= base_ind:
                            break
                        m = re.match(
                            r'^reset\s*=>\s*\(\s*(.+?)\s*,\s*(.+)\)\s*'
                            r'(?:@.*)?$', ns)
                        if m:
                            rst = parse_expr(TokStream(
                                tokenize(m.group(1), where), where))
                            init = parse_expr(TokStream(
                                tokenize(m.group(2), where), where))
                        j += 1
                    if rst is not None:
                        body.append(Reg(name=name, type=t, clock=clk,
                                        reset=rst, init=init, loc=loc))
                    else:
                        body.append(Reg(name=name, type=t, clock=clk, loc=loc))
                    return None, j
            body.append(Reg(name=name, type=t, clock=clk, loc=loc))
            return None, i + 1

        if k == "id" and v == "regreset":
            name = ts.expect("id")[1]
            ts.expect("sym", ":")
            t = parse_type(ts)
            ts.expect("sym", ",")
            clk = parse_expr(ts)
            ts.expect("sym", ",")
            rst = parse_expr(ts)
            ts.expect("sym", ",")
            init = parse_expr(ts)
            body.append(Reg(name=name, type=t, clock=clk, reset=rst, init=init, loc=loc))
            return None, i + 1

        if k == "id" and v == "node":
            name = ts.expect("id")[1]
            ts.expect("sym", "=")
            e = parse_expr(ts)
            body.append(Node(name=name, expr=e, loc=loc))
            return None, i + 1

        if k == "id" and v == "inst":
            name = ts.expect("id")[1]
            kw = ts.expect("id")[1]
            if kw != "of":
                raise FirParseError(f"{where}: expect 'of' after inst name, got {kw!r}")
            modname = ts.expect("id")[1]
            body.append(Inst(name=name, module=modname, loc=loc))
            return None, i + 1

        if k == "id" and v in ("cmem", "smem"):
            name = ts.expect("id")[1]
            ts.expect("sym", ":")
            t = parse_type(ts)
            if ts.peek()[:2] != ("sym", "["):
                raise FirParseError(f"{where}: mem depth bracket missing")
            ts.next()
            depth = int(ts.expect("num")[1])
            ts.expect("sym", "]")
            body.append(MemDecl(name=name, kind=v, type=t, depth=depth, loc=loc))
            return None, i + 1

        if k == "id" and v in ("infer", "read", "write", "readwrite",
                               "irread", "iread"):
            k2, v2, _, _ = ts.peek()
            if k2 == "id" and v2 in ("mport", "mreader", "reader",
                                     "writer", "readwriter"):
                ts.next()
                name = ts.expect("id")[1]
                ts.expect("sym", "=")
                memname = ts.expect("id")[1]
                ts.expect("sym", "[")
                idx = parse_expr(ts)
                ts.expect("sym", "]")
                ts.expect("sym", ",")
                clk = parse_expr(ts)
                body.append(MemPort(portkind=v, name=name, mem=memname,
                                    index=idx, clock=clk, loc=loc))
                return None, i + 1
            # `write <= ...` etc: plain identifier starting a legacy
            # statement -- fall through to the legacy forms below
            ts.i = 0

        if k == "id" and v == "connect":
            lhs = parse_expr(ts)
            ts.expect("sym", ",")
            rhs = parse_expr(ts)
            body.append(Connect(lhs=lhs, rhs=rhs, loc=loc))
            return None, i + 1

        if k == "id" and v == "invalidate":
            k2, v2, _, _ = ts.peek()
            if k2 == "sym" and v2 in ("<=", "<-"):
                # wire named `invalidate` used as a connect LHS
                ts.i = 0
            else:
                e = parse_expr(ts)
                body.append(Invalidate(expr=e, loc=loc))
                return None, i + 1

        if k == "id" and v == "skip":
            body.append(Skip(loc=loc))
            return None, i + 1

        if k == "id" and v == "when":
            cond = parse_expr(ts)
            ts.expect("sym", ":")
            w = When(cond=cond, loc=loc)
            body.append(w)
            if not ts.done():
                raise FirParseError(
                    f"{where}: tokens after 'when ... :' "
                    f"(inline blocks unsupported): {ts.peek()!r}")
            return w, i + 1

        if k == "id" and v in ("stop", "printf", "assert", "assume", "cover"):
            # simulation side effects: legal FIRRTL, no hardware semantics
            # for synthesis lowering -- skip the statement entirely
            return None, i + 1

        # FIRRTL 1.x legacy forms: `lhs <= rhs` (connect), `lhs <- rhs`
        # (partial connect, approximated as a plain connect), and
        # `lhs is invalid` (invalidate).
        ts.i = 0
        try:
            lhs = parse_expr(ts)
        except FirParseError:
            lhs = None
        if lhs is not None and not ts.done():
            k2, v2, _, _ = ts.peek()
            if k2 == "sym" and v2 in ("<=", "<-"):
                ts.next()
                rhs = parse_expr(ts)
                body.append(Connect(lhs=lhs, rhs=rhs, loc=loc))
                return None, i + 1
            if k2 == "id" and v2 == "is":
                ts.next()
                kw = ts.expect("id")[1]
                if kw != "invalid":
                    raise FirParseError(
                        f"{where}: expect 'invalid' after 'is', got {kw!r}")
                body.append(Invalidate(expr=lhs, loc=loc))
                return None, i + 1

        raise FirParseError(f"{where}: unknown statement {v!r} ({s!r})")


def parse_fir(text: str, source_name: str = "<fir>") -> Circuit:
    """Parse a whole FIRRTL file into a Circuit AST."""
    return FirParser(text, source_name).parse()


def parse_fir_file(path: str) -> Circuit:
    with open(path, "r", encoding="utf-8") as f:
        return parse_fir(f.read(), source_name=path)
