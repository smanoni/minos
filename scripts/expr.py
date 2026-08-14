# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Rewrites the gates a template did not recognise as Verilog expressions"""

import json
import collections

FLOP = "DFF"

# How tightly each form binds, so that folding one gate into another can tell
# when the result needs brackets round it. A gate's operands are ranked by the
# operator they belong to and not by the brackets already in the form: the ~()
# of a nand keeps the result together but does nothing for what is inside it,
# where a conditional would still be pulled apart by the &.
ATOM, NOT, AND, XOR, OR, MUX = 5, 4, 3, 2, 1, 0

GATES = {
    "$_AND_": ("%(A)s & %(B)s", AND, AND),
    "$_OR_": ("%(A)s | %(B)s", OR, OR),
    "$_XOR_": ("%(A)s ^ %(B)s", XOR, XOR),
    "$_XNOR_": ("~(%(A)s ^ %(B)s)", NOT, XOR),
    "$_NAND_": ("~(%(A)s & %(B)s)", NOT, AND),
    "$_NOR_": ("~(%(A)s | %(B)s)", NOT, OR),
    "$_ANDNOT_": ("%(A)s & ~%(B)s", AND, {"A": AND, "B": NOT}),
    "$_ORNOT_": ("%(A)s | ~%(B)s", OR, {"A": OR, "B": NOT}),
    "$_NOT_": ("~%(A)s", NOT, NOT),
    "$_MUX_": ("%(S)s ? %(B)s : %(A)s", MUX, OR),
    "$_NMUX_": ("~(%(S)s ? %(B)s : %(A)s)", NOT, OR),
}


def binding(need, port):
    """How tightly one operand of a form has to bind"""
    return need[port] if isinstance(need, dict) else need


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

    # What has to keep a name of its own. A net two gates read would otherwise
    # be written out twice over, which loses the fact that it is one net; a
    # port has to stay addressable; a clock or a reset is read by an event list
    # that cannot hold an expression; and whatever a replaced region reads is
    # left named for its template to wire itself to, since the template was
    # matched against the netlist's own nets rather than against this text.
    pinned = set(named) | set(label)
    outside = set()
    for spec in module.get("ports", {}).values():
        pinned |= set(spec["bits"])
        if spec["direction"] == "input":
            outside |= set(spec["bits"])
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                continue
            if name in skip or (FLOP in cell["type"] and port in ("C", "R")):
                pinned |= set(bits)
    # A register clocked from anywhere but a port of the module keeps its data
    # on a wire. Such a clock is made by the design out of its own state, so it
    # arrives a moment after that state moved rather than once everything has
    # settled, and then it matters whether a value is read from a wire or
    # worked out again where it is used. The netlist being compared against
    # reads every register's data off a wire, so this reads it off one too.
    for name, cell in cells.items():
        if name in skip or FLOP not in cell["type"]:
            continue
        if cell["connections"]["C"][0] not in outside:
            pinned |= set(cell["connections"]["D"])

    def folds(bit):
        """The gate to write in place of a net, where it is right to fold one"""
        src = driver.get(bit)
        if src is None or bit in pinned or fanout[bit] != 1:
            return None
        if src[0] in skip or cells[src[0]]["type"] not in GATES:
            return None
        return src[0]

    def expand(name):
        """A gate as an expression, with whatever folds into it folded in"""
        cell = cells[name]
        form, prec, need = GATES[cell["type"]]
        args = {p: build(bits[0], binding(need, p))
                for p, bits in cell["connections"].items()
                if cell["port_directions"].get(p) == "input"}
        return form % args, prec

    def term(bit):
        if bit in named:
            return named[bit], ATOM
        if bit in label:
            return label[bit], ATOM
        if bit in ("0", "1"):
            return "1'b%s" % bit, ATOM
        if bit in ("x", "z"):
            return "1'b0", ATOM
        got = folds(bit)
        return expand(got) if got else (net_name(bit), ATOM)

    def build(bit, want):
        text, prec = term(bit)
        return "(%s)" % text if prec < want else text

    seen = set()
    for name, cell in cells.items():
        if name in skip or FLOP in cell["type"] \
                or cell["type"] not in GATES:
            continue
        out = [b for p, bits in cell["connections"].items() for b in bits
               if cell["port_directions"].get(p) == "output"]
        if not out or out[0] in named or out[0] in seen:
            continue
        target = out[0]
        if folds(target):
            continue
        seen.add(target)
        wires.append("  wire %s;" % show(target))
        assigns.append("  assign %s = %s;" % (show(target), expand(name)[0]))

    for name, cell in cells.items():
        if name in skip or FLOP not in cell["type"]:
            continue
        q = cell["connections"]["Q"][0]
        if q in named:
            continue
        edge, redge, value = flop_kind(cell["type"])
        reg = show(q)
        wires.append("  reg %s;" % reg)
        clock = build(cell["connections"]["C"][0], ATOM)
        data = build(cell["connections"]["D"][0], MUX)
        if redge is None:
            always += ["  always @(%s %s) %s <= %s;" % (edge, clock, reg, data)]
            continue
        rst = build(cell["connections"]["R"][0], ATOM)
        always += ["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                   "    if (%s%s) %s <= 1'b%s;"
                   % ("!" if redge == "negedge" else "", rst, reg, value),
                   "    else %s <= %s;" % (reg, data)]
    return wires, assigns, always


def net_of(path, name):
    """The bit a named port carries, so a caller can alias it"""
    module = list(json.load(open(path))["modules"].values())[0]
    return module["ports"][name]["bits"]
