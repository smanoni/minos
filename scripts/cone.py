# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""What reaches one output of a design, and what one input reaches"""

import sys
import os
import json
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import structure

# How many register names to print before a rank is better summarised than
# listed: past a handful the count is the fact and the names are noise.
SHOWN = 12


def in_order(name):
    """A name sorted by the number in its brackets and not by its spelling"""
    base, _, rest = name.partition("[")
    return (base, int(rest[:-1]) if rest[:-1].isdigit() else -1)


def net_owner(module):
    """The port bit each net belongs to, for naming what a walk arrives at"""
    out = {}
    for name, spec in module.get("ports", {}).items():
        for at, bit in enumerate(spec["bits"]):
            if isinstance(bit, int):
                out[bit] = name if len(spec["bits"]) == 1 \
                    else "%s[%d]" % (name, at)
    return out


def grouped(regions):
    """Each flop under the region it belongs to and its place in it.

    A netlist calls a flop after the cell that happened to make it, and
    `U1418.A` says nothing a reader can use. The regions are already the
    grouping the recovered RTL is built on, so a flop is better spoken of as
    the word it is a bit of, which is what the RTL will call it too.
    """
    out = {}
    for at, region in enumerate(regions or ()):
        if region.get("kind") == "cone":
            continue
        for bit, flop in enumerate(region.get("registers", ())):
            out[flop] = "%s%d[%d]" % (region["kind"], at, bit)
    return out


def flop_name(module, cells, flop, group=()):
    """A flop under the name a reader can look for"""
    if flop in group:
        return group[flop]
    bits = cells[flop]["connections"].get("Q", [])
    if not bits:
        return flop
    for name, spec in module.get("netnames", {}).items():
        if bits[0] in spec["bits"]:
            at = spec["bits"].index(bits[0])
            return name if len(spec["bits"]) == 1 else "%s[%d]" % (name, at)
    return flop


def reaches(module, bits, stop_at_flops=True):
    """Everything a set of nets is built from, back to ports and registers.

    Stops at a register by default, which is what makes the answer small
    enough to read: a design is a shallow cloud of gates between one rank of
    registers and the next, and the rank behind is where the next question
    starts rather than part of this one.
    """
    cells = module["cells"]
    driver = {}
    for name, cell in cells.items():
        for port, conn in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in conn:
                    driver[bit] = name
    owner = net_owner(module)
    seen, flops, inputs, gates = set(), set(), set(), set()
    queue = list(bits)
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        if bit in owner:
            inputs.add(owner[bit])
        src = driver.get(bit)
        if src is None:
            continue
        if stop_at_flops and structure.is_flop(cells, src):
            flops.add(src)
            continue
        gates.add(src)
        queue += [b for port, conn in cells[src]["connections"].items()
                  for b in conn
                  if cells[src]["port_directions"].get(port) == "input"]
    return flops, inputs, gates


def stage(module, name, depth, group=()):
    """One output and the ranks of registers behind it, as far as asked.

    Each rank is what the rank in front of it is built from, so walking back
    a rank at a time says how deep the pipeline to this output really is and
    which inputs enter it at which stage.
    """
    cells = module["cells"]
    ports = module.get("ports", {})
    if name not in ports:
        return None
    front = list(ports[name]["bits"])
    ranks, held = [], set()
    for _ in range(depth):
        flops, inputs, gates = reaches(module, front)
        fresh = [f for f in flops if f not in held]
        # Only what this rank adds. A register reached again from further back
        # was already answered for, and repeating it buries the few names that
        # are new in the many that are not.
        ranks.append((sorted(inputs, key=in_order),
                      sorted((flop_name(module, cells, f, group)
                              for f in fresh), key=in_order), len(gates)))
        if not fresh:
            break
        held |= set(flops)
        front = [b for f in fresh
                 for port, conn in cells[f]["connections"].items()
                 for b in conn
                 if cells[f]["port_directions"].get(port) == "input"
                 and port != "C"]
    return ranks


def forward(module, name, group=()):
    """What one input reaches, which is the question a driver of it asks"""
    cells = module["cells"]
    ports = module.get("ports", {})
    if name not in ports:
        return None
    readers = collections.defaultdict(set)
    for cell, spec in cells.items():
        for port, conn in spec["connections"].items():
            if spec["port_directions"].get(port) != "output":
                for bit in conn:
                    readers[bit].add(cell)
    owner = net_owner(module)
    seen, hit, touched = set(), set(), set()
    queue = list(ports[name]["bits"])
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        if bit in owner and owner[bit].split("[")[0] != name:
            hit.add(owner[bit])
        for cell in readers[bit]:
            if structure.is_flop(cells, cell):
                touched.add(flop_name(module, cells, cell, group))
                continue
            queue += [b for port, conn in cells[cell]["connections"].items()
                      for b in conn
                      if cells[cell]["port_directions"].get(port) == "output"]
    return sorted(hit, key=in_order), sorted(touched, key=in_order)


def main(netlist, want, depth):
    module = list(json.load(open(netlist))["modules"].values())[0]
    ports = module.get("ports", {})
    beside = netlist.replace("_generic.json", "_regions.json")
    group = grouped(json.load(open(beside))) if os.path.exists(beside) \
        and beside != netlist else {}
    if want is None:
        for name, spec in sorted(ports.items()):
            print("  %-6s %-14s %d bit%s"
                  % (spec["direction"], name, len(spec["bits"]),
                     "" if len(spec["bits"]) == 1 else "s"))
        return 0
    if want not in ports:
        print("no port called %s" % want)
        return 1
    if ports[want]["direction"] == "input":
        hit, touched = forward(module, want, group)
        print("%s reaches" % want)
        print("  outputs   %s" % (", ".join(hit) or "none, only through registers"))
        print("  registers %d: %s%s"
              % (len(touched), ", ".join(touched[:SHOWN]) or "none",
                 ", ..." if len(touched) > SHOWN else ""))
        return 0
    ranks = stage(module, want, depth, group)
    print("%s is built from" % want)
    for at, (inputs, flops, gates) in enumerate(ranks):
        print("  rank %d: %d gates, %d new register%s"
              % (at, gates, len(flops), "" if len(flops) == 1 else "s"))
        print("    inputs %s" % (", ".join(inputs) or "none"))
        if flops:
            print("    from   %s%s"
                  % (", ".join(flops[:SHOWN]),
                     ", ..." if len(flops) > SHOWN else ""))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: cone.py <netlist.json> [port] [depth]")
        sys.exit(1)
    sys.exit(main(sys.argv[1],
                  sys.argv[2] if len(sys.argv) > 2 else None,
                  int(sys.argv[3]) if len(sys.argv) > 3 else 3))
