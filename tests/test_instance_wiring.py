"""Instance/port wiring regressions for the FIRRTL lowerer.

Both cases below used to lower *silently* to constant 0 instead of the real
driver, which is the worst failure mode for a hardware importer: the model
still compiles and simulates, it just does not do what the RTL says.  Rocket's
TestHarness hit exactly these shapes, and the resulting model could never boot
(BootROM/AXI4 responses tied to 0).

Run with:  PYTHONPATH=src python -m pytest tests -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def emit(tmp_path: Path, fir_text: str, extra: tuple[str, ...] = ()) -> str:
    fir = tmp_path / "dut.fir"
    fir.write_text(fir_text)
    out = tmp_path / "dut.mlir"
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    subprocess.run(
        [sys.executable, "-m", "fir2pyc.cli", "emit", str(fir), "-o", str(out),
         *extra],
        check=True, capture_output=True, text=True, env=env,
    )
    return out.read_text()


def func_body(mlir: str, name: str) -> str:
    m = re.search(rf"^  func\.func @{name}\(.*?^  \}}$", mlir,
                  re.S | re.M)
    assert m, f"function @{name} not found"
    return m.group(0)


def instance_results(body: str) -> set[str]:
    out: set[str] = set()
    for m in re.finditer(r"^\s*(.+?)\s*=\s*pyc\.instance", body, re.M):
        for tok in m.group(1).split(","):
            tok = tok.strip()
            if tok.startswith("%"):
                out.add(tok)
    return out


def constants(body: str) -> set[str]:
    return {m.group(1) for m in
            re.finditer(r"^\s*(%\w+) = pyc\.constant", body, re.M)}


# ---------------------------------------------------------------------------
# 1. A connect may precede the `inst` statement it refers to.
#
# LHS direction must come from the *declaration*, not from whichever caller
# created the leaf first: `_lhs_leaf_list` used to create the leaf with
# `is_input=False`, so the instance input was then classified as an output and
# the connect was dropped as "undrivable".
# ---------------------------------------------------------------------------

CYCLE_FIR = """\
FIRRTL version 1.1.0
circuit Top :
  module Mem :
    input clock : Clock
    input reset : UInt<1>
    input in : UInt<8>
    output out : UInt<8>
    out <= add(in, UInt<8>("h1"))

  module Ldut :
    input clock : Clock
    input reset : UInt<1>
    input in : UInt<8>
    output out : UInt<8>
    out <= add(in, UInt<8>("h2"))

  module Top :
    input clock : Clock
    input reset : UInt<1>
    output out : UInt<8>
    inst mem of Mem
    mem.in <= ldut.out
    ldut.in <= mem.out
    mem.clock <= clock
    mem.reset <= reset
    inst ldut of Ldut
    ldut.clock <= clock
    ldut.reset <= reset
    out <= mem.out
"""


def test_connect_before_inst_declaration(tmp_path):
    body = func_body(emit(tmp_path, CYCLE_FIR), "Top")

    # Both instances keep their single input: two `pyc.wire`s break the cycle
    # and are driven afterwards by `pyc.assign`.
    insts = re.findall(r"= pyc\.instance .*callee = @(?:Mem|Ldut)", body)
    assert len(insts) == 2, body
    assert body.count("pyc.instance") == 2
    assert body.count("pyc.wire") == 2
    assert body.count("pyc.assign") == 2
    # No instance may gain an extra result (the old bug turned `ldut.in` into
    # an output, giving @Ldut two results and no inputs).
    for name in ("Mem", "Ldut"):
        m = re.search(rf"= pyc\.instance [^\n]*callee = @{name}\b[^\n]*", body)
        assert m, f"{name} instance missing"
        assert " -> i8" in m.group(0).split(" : ")[-1], m.group(0)


# ---------------------------------------------------------------------------
# 2. Bulk connect through a flipped bundle member.
#
# `s.auto.in <- auto.slave_in` mixes directions: the request leaves flow into
# the instance, the response leaves (`ready`) flow back out to the host.  The
# reverse half used to be discarded for every Port/Inst-rooted RHS, leaving the
# host output undriven.
# ---------------------------------------------------------------------------

FLIPPED_FIR = """\
FIRRTL version 1.1.0
circuit Top :
  module Slave :
    input clock : Clock
    input reset : UInt<1>
    output auto : { flip in : { a : { flip ready : UInt<1>, valid : UInt<1> } } }
    auto is invalid
    auto.in.a.ready <= UInt<1>("h1")

  module Top :
    input clock : Clock
    input reset : UInt<1>
    output auto : { flip slave_in : { a : { flip ready : UInt<1>, valid : UInt<1> } } }
    inst s of Slave
    s.auto.in <- auto.slave_in
    s.clock <= clock
    s.reset <= reset
"""


def test_bulk_connect_flipped_member_reverse_direction(tmp_path):
    body = func_body(emit(tmp_path, FLIPPED_FIR), "Top")
    results = instance_results(body)
    consts = constants(body)
    m = re.search(r"^\s*return (.*?) :", body, re.M)
    assert m, body
    returned = [t.strip() for t in m.group(1).split(",")]

    # The host's `ready` output must come from the Slave instance, not 0.
    driven = [r for r in returned if r in results]
    assert driven, f"no output driven by an instance:\n{body}"
    assert not [r for r in returned if r in consts], (
        f"output tied to a constant:\n{body}")


# ---------------------------------------------------------------------------
# 3. Bulk connect rooted at a `wire`, with the mirrored bundle on the RHS.
#
# `w <- auto.in` transfers flipped members in the reverse direction, so
# `auto.in.*` (a module output) must be driven from the wire.  The lowerer used
# to skip those leaves outright, which left Rocket's BootROM output tied to 0
# behind a 513-entry ROM lookup that pycc then removed as dead code.
# ---------------------------------------------------------------------------

WIRE_BULK_FIR = """\
FIRRTL version 1.1.0
circuit Top :
  module Top :
    input clock : Clock
    input reset : UInt<1>
    output auto : { flip in : { d : { valid : UInt<1>, flip ready : UInt<1> } } }
    wire w : { d : { valid : UInt<1>, flip ready : UInt<1> } }
    w is invalid
    w <- auto.in
    w.d.ready <= UInt<1>("h1")
"""


def test_wire_bulk_connect_drives_module_output(tmp_path):
    body = func_body(emit(tmp_path, WIRE_BULK_FIR), "Top")
    consts = constants(body)
    m = re.search(r"^\s*return (.*?) :", body, re.M)
    assert m, body
    returned = [t.strip() for t in m.group(1).split(",")]
    assert not [r for r in returned if r in consts], (
        f"module output tied to a constant:\n{body}")


VECIDX_FIR = """
circuit T :
  module Pass :
    input clock : Clock
    input reset : UInt<1>
    input in : UInt<8>
    output out : UInt<8>
    out <= in
  module VecMod :
    input clock : Clock
    input reset : UInt<1>
    output out : UInt<8>[2]
    out[0] <= UInt<8>("h11")
    out[1] <= UInt<8>("h22")
  module T :
    input clock : Clock
    input reset : UInt<1>
    output dst : UInt<8>
    inst a of Pass
    inst v of VecMod
    a.in <= v.out[1]
    dst <= a.out
"""


def test_forward_reference_through_vector_index(tmp_path):
    """A forward reference to an instance output reached through a vector
    index must be wire-broken, not folded to constant 0.

    The wire-breaking pre-scan spells the source path with `_expr_to_path`,
    which read the dynamic-subaccess field (`index`) instead of the static
    one (`idx`).  Every vector-indexed source path therefore came out as
    `[None]`, never matched a source leaf, and the consuming input silently
    kept the constant-0 default -- Rocket's PTW `io.requestor[1]` bundle
    (privilege/status to the frontend) was dropped this way, which made the
    I-cache treat the fetch as uncacheable and never refill.
    """
    body = func_body(emit(tmp_path, VECIDX_FIR), "T")
    wires = [ln for ln in body.splitlines() if "pyc.wire" in ln]
    assert wires, body
    consumer = [ln for ln in body.splitlines()
                if "pyc.instance" in ln and 'name = "a"' in ln]
    assert consumer, body
    wire_ssa = wires[0].split("=")[0].strip()
    assert wire_ssa in consumer[0], consumer[0]
    assert f"pyc.assign {wire_ssa}" in body, body
