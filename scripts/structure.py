# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Finds candidate regions in a recovered netlist for a reference to match"""

import sys
import json
import re
import collections

FLOP = "DFF"
CLOCK_PORTS = ("C", "CLK")
RESET_PORTS = ("R", "S", "RST")


def load(path):
    """Netlist as cells, per-bit drivers and the port each bit belongs to"""
    design = json.load(open(path))
    module = list(design["modules"].values())[0]
    cells = module["cells"]
    driver, sinks = {}, collections.defaultdict(list)
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            for bit in bits:
                if cell["port_directions"].get(port) == "output":
                    driver[bit] = name
                else:
                    sinks[bit].append(name)
    ports = {}
    for name, port in module.get("ports", {}).items():
        for i, bit in enumerate(port["bits"]):
            ports[bit] = name if len(port["bits"]) == 1 else "%s[%d]" % (name, i)
    return cells, driver, sinks, ports


def is_flop(cells, name):
    return FLOP in cells[name]["type"]


def support(cells, driver, ports, name):
    """Flops and ports feeding this cell, stopping at the first flop crossed"""
    seen, flops, inputs = set(), set(), set()
    queue = [b for p, bits in cells[name]["connections"].items()
             for b in bits if cells[name]["port_directions"].get(p) == "input"]
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        if bit in ports:
            inputs.add(ports[bit])
        src = driver.get(bit)
        if src is None:
            continue
        if is_flop(cells, src):
            flops.add(src)
            continue
        queue += [b for p, bits in cells[src]["connections"].items()
                  for b in bits if cells[src]["port_directions"].get(p) == "input"]
    return flops, inputs


def data_support(cells, driver, ports, flop):
    """Same, but only behind the data pin, so clock and reset are excluded"""
    conn = cells[flop]["connections"]
    data = [b for p, bits in conn.items() for b in bits
            if cells[flop]["port_directions"].get(p) == "input"
            and p not in CLOCK_PORTS + RESET_PORTS]
    seen, flops, inputs = set(), set(), set()
    queue = list(data)
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        if bit in ports:
            inputs.add(ports[bit])
        src = driver.get(bit)
        if src is None:
            continue
        if is_flop(cells, src):
            flops.add(src)
            continue
        queue += [b for p, bits in cells[src]["connections"].items()
                  for b in bits if cells[src]["port_directions"].get(p) == "input"]
    return flops, inputs


def control(cells, ports, driver, flop):
    """The clock and reset a flop is on, named where they reach a port"""
    out = []
    for port in CLOCK_PORTS + RESET_PORTS:
        bits = cells[flop]["connections"].get(port)
        if bits:
            bit = bits[0]
            out.append(ports.get(bit, "n%s" % bit))
    return tuple(out)


def cone_cells(cells, driver, ports, flops):
    """Combinational cells feeding these registers, stopping at any register"""
    inside, seen, queue = set(), set(), []
    for f in flops:
        queue += [b for p, bits in cells[f]["connections"].items() for b in bits
                  if cells[f]["port_directions"].get(p) == "input"]
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        src = driver.get(bit)
        if src is None or is_flop(cells, src) or src in inside:
            continue
        inside.add(src)
        queue += [b for p, bits in cells[src]["connections"].items() for b in bits
                  if cells[src]["port_directions"].get(p) == "input"]
    return sorted(inside)


def feedback_cells(cells, driver, group):
    """Cells on a path from these registers' outputs back to their own inputs.

    A register bank holds its value with a mux that feeds each output back to
    its input, so this is what tells an enable apart from a plain load.
    """
    inside, support, order = set(), {}, []
    queue = [b for f in group for p, bits in cells[f]["connections"].items()
             for b in bits if cells[f]["port_directions"].get(p) == "input"]
    seen = set()
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        src = driver.get(bit)
        if src is None or is_flop(cells, src):
            continue
        if src not in support:
            support[src] = set()
            order.append(src)
        queue += [b for p, bits in cells[src]["connections"].items() for b in bits
                  if cells[src]["port_directions"].get(p) == "input"]
    members = set(group)
    changed = True
    while changed:
        changed = False
        for cell in order:
            reach = set()
            for p, bits in cells[cell]["connections"].items():
                if cells[cell]["port_directions"].get(p) != "input":
                    continue
                for bit in bits:
                    src = driver.get(bit)
                    if src is None:
                        continue
                    reach |= {src} & members
                    reach |= support.get(src, set())
            if reach - support[cell]:
                support[cell] |= reach
                changed = True
    for cell in order:
        if support[cell]:
            inside.add(cell)
    return sorted(inside)


def banks(cells, driver, ports, claimed=()):
    """Registers loaded from something that is not another register.

    A chain's first stage is loaded the same way, so registers a chain has
    already accounted for are left out rather than described twice.
    """
    groups = collections.defaultdict(list)
    for name in cells:
        if not is_flop(cells, name) or name in claimed:
            continue
        srcs, _ = data_support(cells, driver, ports, name)
        if not srcs - {name}:
            groups[control(cells, ports, driver, name)].append(name)
    return groups


def state_groups(cells, driver, ports):
    """The largest set of registers whose next value depends only on the set.

    A counter, an LFSR and a toggle all look like this: a group closed under
    itself. Asking a whole control group to be closed finds nothing as soon
    as a design keeps one register that watches something else, so the group
    is narrowed until what is left holds. That is the state machine with its
    stragglers dropped rather than nothing at all. How many of the group a
    register depends on also orders them, since in a counter bit k is the one
    that watches k of its neighbours.
    """
    groups, support = collections.defaultdict(list), {}
    for name in cells:
        if is_flop(cells, name):
            groups[control(cells, ports, driver, name)].append(name)
            support[name] = data_support(cells, driver, ports, name)[0]
    out = {}
    for ctrl, group in groups.items():
        keep = set(group)
        while True:
            drop = {f for f in keep
                    if not support[f] or not support[f] <= keep}
            if not drop:
                break
            keep -= drop
        if keep:
            weight = {f: len(support[f] & keep) for f in keep}
            out[ctrl] = sorted(keep, key=lambda f: (weight[f], f))
    return out


def _and(a, b):
    return 0 if 0 in (a, b) else (1 if a == 1 and b == 1 else None)


def _or(a, b):
    return 1 if 1 in (a, b) else (0 if a == 0 and b == 0 else None)


def _not(a):
    return None if a is None else 1 - a


def _xor(a, b):
    return None if a is None or b is None else a ^ b


def _mux(a, b, s):
    if s == 0 or s == 1:
        return b if s else a
    return a if a is not None and a == b else None


LOGIC = {
    "$_AND_": lambda v: _and(v["A"], v["B"]),
    "$_NAND_": lambda v: _not(_and(v["A"], v["B"])),
    "$_OR_": lambda v: _or(v["A"], v["B"]),
    "$_NOR_": lambda v: _not(_or(v["A"], v["B"])),
    "$_XOR_": lambda v: _xor(v["A"], v["B"]),
    "$_XNOR_": lambda v: _not(_xor(v["A"], v["B"])),
    "$_ANDNOT_": lambda v: _and(v["A"], _not(v["B"])),
    "$_ORNOT_": lambda v: _or(v["A"], _not(v["B"])),
    "$_NOT_": lambda v: _not(v["A"]),
    "$_MUX_": lambda v: _mux(v["A"], v["B"], v["S"]),
    "$_NMUX_": lambda v: _not(_mux(v["A"], v["B"], v["S"])),
}


def evaluate(cells, driver, bit, forced, memo):
    """A net's value with some nets held, where the gates alone decide it.

    Every gate is read for what it settles on rather than for what it computes,
    so an and with one input low is nothing regardless of the other. Where a
    net still depends on something unheld it comes back unknown, which is what
    keeps a value that only sometimes holds from being read as a constant.
    """
    if bit in forced:
        return forced[bit]
    if bit in ("0", "1"):
        return int(bit)
    if bit in memo:
        return memo[bit]
    memo[bit] = None
    src = driver.get(bit)
    if src is None or is_flop(cells, src) or cells[src]["type"] not in LOGIC:
        return None
    got = {p: evaluate(cells, driver, bits[0], forced, memo)
           for p, bits in cells[src]["connections"].items()
           if cells[src]["port_directions"].get(p) == "input"}
    memo[bit] = LOGIC[cells[src]["type"]](got)
    return memo[bit]


def held_value(cells, driver, flops, bit):
    """The value registers take while a net is held, where one net does that.

    A synchronous reset leaves nothing on the register to recognise, being
    logic in front of the data pin rather than a pin of its own. What marks it
    is that holding a single net settles every data pin at once, whatever the
    rest of the design is doing, so both levels are tried and the one that
    settles them all comes back with the word it settles them to.
    """
    for level in (0, 1):
        memo, value = {}, []
        for flop in flops:
            data = cells[flop]["connections"].get("D")
            if not data:
                break
            got = evaluate(cells, driver, data[0], {bit: level}, memo)
            if got is None:
                break
            value.append(got)
        if len(value) == len(flops):
            return level, value
    return None


def mux_link(cells, driver, flop):
    """The stage a flop shifts from, when a select chooses it against a load.

    A loadable shift register gives every stage a mux: one arm is the stage
    before it, the other is the value that stage takes when the register is
    loaded, and the select is what tells the two apart. A stage that holds
    its value has a mux too, but its other arm is the stage itself, and that
    is an enable rather than a load.
    """
    conn = cells[flop]["connections"]
    if "D" not in conn:
        return None
    src = driver.get(conn["D"][0])
    if src is None or cells[src]["type"] != "$_MUX_":
        return None
    mux, own = cells[src]["connections"], conn["Q"][0]
    for arm, other, shift_on in (("A", "B", 0), ("B", "A", 1)):
        prev = driver.get(mux[arm][0])
        if prev is not None and prev != flop and is_flop(cells, prev) \
                and mux[other][0] != own:
            return {"prev": prev, "load": mux[other][0], "sel": mux["S"][0],
                    "shift_on": shift_on, "mux": src}
    return None


def chains(cells, driver, ports):
    """Registers whose data comes from exactly one other register.

    Where one stage feeds two, walking back from each of them arrives at the
    same registers twice, and two regions holding the same register would each
    be lifted as if it were theirs and drive it twice over. The longest chain
    is kept and anything overlapping it is left for another detector, so what
    comes out never describes a register more than once.
    """
    flops = [n for n in cells if is_flop(cells, n)]
    prev, feed = {}, {}
    for f in flops:
        srcs, inputs = data_support(cells, driver, ports, f)
        feed[f] = (srcs, inputs)
        others = srcs - {f}
        if len(others) == 1:
            prev[f] = others.pop()
    tails = set(prev.values())
    found = []
    for head in flops:
        if head in tails or head not in prev:
            continue
        chain, cur = [head], head
        while cur in prev and prev[cur] not in chain:
            cur = prev[cur]
            chain.append(cur)
        found.append(chain)
    out, claimed = [], set()
    for chain in sorted(found, key=len, reverse=True):
        if claimed.intersection(chain):
            continue
        claimed |= set(chain)
        out.append((chain, sorted(feed[chain[-1]][1])))
    return flops, prev, out


def bus_of(name):
    """The bus a port bit belongs to, so O[0] and O[1] are one datapath"""
    got = re.match(r"(.*)\[(\d+)\]$", name)
    return (got.group(1), int(got.group(2))) if got else (name, 0)


def cones(cells, driver, ports):
    """Combinational logic behind each output bus.

    A datapath is only recognisable a whole bus at a time: taken one bit at a
    time a multiplier is eight unrelated functions, so bits of one port are
    gathered into a single region.
    """
    outs = collections.defaultdict(
        lambda: (set(), set(), set(), []))
    for bit, name in ports.items():
        src = driver.get(bit)
        if src is None or is_flop(cells, src):
            continue
        srcs, inputs = support(cells, driver, ports, src)
        inside, seen, queue = {src}, set(), [bit]
        while queue:
            b = queue.pop()
            if b in seen:
                continue
            seen.add(b)
            cell = driver.get(b)
            if cell is None or is_flop(cells, cell):
                continue
            inside.add(cell)
            queue += [x for p, bits in cells[cell]["connections"].items()
                      for x in bits
                      if cells[cell]["port_directions"].get(p) == "input"]
        bus, index = bus_of(name)
        have = outs[bus]
        outs[bus] = (have[0] | srcs, have[1] | inputs, have[2] | inside,
                     have[3] + [(index, name)])
    return {bus: (s, i, sorted(c), [n for _, n in sorted(b)])
            for bus, (s, i, c, b) in outs.items()}


def main(path, out=None):
    cells, driver, sinks, ports = load(path)
    flops, prev, found = chains(cells, driver, ports)

    print("%s" % path)
    print("  %d cells, %d of them registers, %d ports"
          % (len(cells), len(flops), len(set(ports.values()))))

    groups = collections.Counter(control(cells, ports, driver, f) for f in flops)
    print("  register groups by control:")
    for ctrl, n in groups.most_common():
        print("    %-40s %d registers" % (" ".join(ctrl), n))

    print("  register chains:")
    regions = []
    for chain, inputs in sorted(found, key=lambda c: -len(c[0])):
        if len(chain) < 2:
            continue
        print("    %2d deep, serial input from %s"
              % (len(chain), ", ".join(inputs) or "logic"))
        logic = cone_cells(cells, driver, ports, chain)
        regions.append({"kind": "chain", "depth": len(chain),
                        "cells": chain + logic, "registers": chain,
                        "inputs": inputs})

    claimed = {f for r in regions for f in r["registers"]}
    print("  register banks:")
    for ctrl, group in sorted(banks(cells, driver, ports, claimed).items(),
                              key=lambda g: -len(g[1])):
        logic = feedback_cells(cells, driver, group)
        print("    %2d wide on %-24s %d cells of feedback"
              % (len(group), " ".join(ctrl), len(logic)))
        regions.append({"kind": "bank", "width": len(group),
                        "cells": sorted(group) + logic,
                        "registers": sorted(group), "inputs": list(ctrl)})

    print("  state groups:")
    for ctrl, group in sorted(state_groups(cells, driver, ports).items(),
                              key=lambda g: -len(g[1])):
        print("    %2d wide on %s" % (len(group), " ".join(ctrl)))
        regions.append({"kind": "state", "width": len(group),
                        "cells": group + cone_cells(cells, driver, ports, group),
                        "registers": group, "inputs": list(ctrl)})

    print("  output cones:")
    for name, (srcs, inputs, inside, bits) in sorted(
            cones(cells, driver, ports).items()):
        print("    %-12s %2d wide, %d registers, %d ports, %d cells"
              % (name, len(bits), len(srcs), len(inputs), len(inside)))
        regions.append({"kind": "cone", "output": name, "bits": bits,
                        "cells": inside, "registers": sorted(srcs),
                        "inputs": sorted(inputs)})

    if out:
        json.dump(regions, open(out, "w"), indent=1)
        print("  %d regions -> %s" % (len(regions), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
