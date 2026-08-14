# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Rewrites the gates a template did not recognise as Verilog expressions"""

import json
import collections

FLOP = "DFF"

GATES = {
    "$_AND_": "%(A)s & %(B)s",
    "$_OR_": "%(A)s | %(B)s",
    "$_XOR_": "%(A)s ^ %(B)s",
    "$_XNOR_": "~(%(A)s ^ %(B)s)",
    "$_NAND_": "~(%(A)s & %(B)s)",
    "$_NOR_": "~(%(A)s | %(B)s)",
    "$_ANDNOT_": "%(A)s & ~%(B)s",
    "$_ORNOT_": "%(A)s | ~%(B)s",
    "$_NOT_": "~%(A)s",
    "$_MUX_": "%(S)s ? %(B)s : %(A)s",
    "$_NMUX_": "~(%(S)s ? %(B)s : %(A)s)",
}


def flop_kind(kind):
    """Reads a generic flop name as edge, reset polarity and reset value.

    The names run $_DFF_<edge>_ and $_DFF_<edge><reset><value>_, so the letters
    after the prefix say everything the always block needs.
    """
    tag = kind[len("$_DFF_"):].rstrip("_")
    edge = "posedge" if tag[:1] == "P" else "negedge"
    if len(tag) < 3:
        return edge, None, None
    return edge, "negedge" if tag[1] == "N" else "posedge", tag[2]


def load(path):
    module = list(json.load(open(path))["modules"].values())[0]
    cells = module["cells"]
    driver, fanout = {}, collections.Counter()
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            for bit in bits:
                if cell["port_directions"].get(port) == "output":
                    driver[bit] = (name, port)
                else:
                    fanout[bit] += 1
    return module, cells, driver, fanout


def net_name(bit):
    return "n%s" % bit


def transcribe(path, skip, alias, label=None):
    """Wires, assignments and always blocks for every cell not skipped.

    A net that more than one gate reads becomes a wire of its own, so the
    output stays a readable set of assignments rather than one nested term.
    An alias says a net is already driven elsewhere and its gate is dropped;
    a label only gives the net a better name than its number.
    """
    module, cells, driver, fanout = load(path)
    wires, assigns, always = [], [], []
    named, label = dict(alias), dict(label or {})

    def show(bit):
        return label.get(bit, net_name(bit))

    def read(bit):
        if bit in named:
            return named[bit]
        if bit in label:
            return label[bit]
        if bit in ("0", "1"):
            return "1'b%s" % bit
        if bit in ("x", "z"):
            return "1'b0"
        src = driver.get(bit)
        if src is None:
            return net_name(bit)
        return net_name(bit)

    order, seen = [], set()
    for name, cell in cells.items():
        if name in skip or FLOP in cell["type"]:
            continue
        order.append(name)
    for name in order:
        cell = cells[name]
        form = GATES.get(cell["type"])
        out = [b for p, bits in cell["connections"].items() for b in bits
               if cell["port_directions"].get(p) == "output"]
        if not form or not out or out[0] in named:
            continue
        args = {p: read(bits[0]) for p, bits in cell["connections"].items()
                if cell["port_directions"].get(p) == "input"}
        target = out[0]
        if target in seen:
            continue
        seen.add(target)
        wires.append("  wire %s;" % show(target))
        assigns.append("  assign %s = %s;" % (show(target), form % args))

    for name, cell in cells.items():
        if name in skip or FLOP not in cell["type"]:
            continue
        q = cell["connections"]["Q"][0]
        if q in named:
            continue
        edge, redge, value = flop_kind(cell["type"])
        reg = show(q)
        wires.append("  reg %s;" % reg)
        clock = read(cell["connections"]["C"][0])
        data = read(cell["connections"]["D"][0])
        if redge is None:
            always += ["  always @(%s %s) %s <= %s;" % (edge, clock, reg, data)]
            continue
        rst = read(cell["connections"]["R"][0])
        always += ["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                   "    if (%s%s) %s <= 1'b%s;"
                   % ("!" if redge == "negedge" else "", rst, reg, value),
                   "    else %s <= %s;" % (reg, data)]
    return wires, assigns, always


def net_of(path, name):
    """The bit a named port carries, so a caller can alias it"""
    module = list(json.load(open(path))["modules"].values())[0]
    return module["ports"][name]["bits"]
