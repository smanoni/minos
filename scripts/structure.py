# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Finds candidate regions in a recovered netlist for a reference to match"""

import sys
import json
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


def chains(cells, driver, ports):
    """Registers whose data comes from exactly one other register"""
    flops = [n for n in cells if is_flop(cells, n)]
    prev, feed = {}, {}
    for f in flops:
        srcs, inputs = data_support(cells, driver, ports, f)
        feed[f] = (srcs, inputs)
        others = srcs - {f}
        if len(others) == 1:
            prev[f] = others.pop()
    tails = set(prev.values())
    out = []
    for head in flops:
        if head in tails or head not in prev:
            continue
        chain, cur = [head], head
        while cur in prev:
            cur = prev[cur]
            chain.append(cur)
        out.append((chain, sorted(feed[chain[-1]][1])))
    return flops, prev, out


def cones(cells, driver, ports):
    """Combinational logic behind each output port"""
    outs = collections.defaultdict(lambda: (set(), set()))
    for bit, name in ports.items():
        src = driver.get(bit)
        if src is None or is_flop(cells, src):
            continue
        srcs, inputs = support(cells, driver, ports, src)
        outs[name] = (srcs, inputs)
    return outs


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
        regions.append({"kind": "chain", "depth": len(chain),
                        "cells": chain, "inputs": inputs})

    print("  output cones:")
    for name, (srcs, inputs) in sorted(cones(cells, driver, ports).items()):
        print("    %-12s %d registers, %d ports" % (name, len(srcs), len(inputs)))
        regions.append({"kind": "cone", "output": name,
                        "registers": sorted(srcs), "inputs": sorted(inputs)})

    if out:
        json.dump(regions, open(out, "w"), indent=1)
        print("  %d regions -> %s" % (len(regions), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
