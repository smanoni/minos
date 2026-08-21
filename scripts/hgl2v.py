# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Writes the cells a gate library describes as Verilog a synthesiser can read

A netlist is only a list of cell names until something says what those cells
do. Where the cells come with a Liberty file the synthesiser reads that
itself; where they do not, all that is to be had is a library description,
and this turns one into the Verilog models the rest of the flow needs. The
descriptions are HAL's .hgl files, which give every output pin a Boolean
function and every flop the four expressions that fix its behaviour, and that
is enough to write a model of each cell without knowing anything else about
the technology.
"""

import sys
import os
import re
import json

# The expressions in a library description are written in a language that is
# already Verilog: names, parentheses, and the three operators. Anything else
# is a library this pass has not been taught to read, and it says so rather
# than writing a model that is quietly wrong.
TOKEN = re.compile(r"0b[01]+|[A-Za-z_][A-Za-z0-9_]*|[01]|[()&|!^]|\s+")
CONST = re.compile(r"(?<![\w'])(?:0b([01]+)|([01]))(?![\w'])")
SIGNAL = re.compile(r"^\(?\s*(!)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)?$")


def readable(expr):
    """Whether the whole of an expression is in the language we know"""
    at = 0
    while at < len(expr):
        got = TOKEN.match(expr, at)
        if not got:
            return False
        at = got.end()
    return True


def edge(expr):
    """The edge to wake on, for an expression that is one signal or its inverse

    A flop is woken by a clock going one way and cleared by a reset arriving,
    and both are given as expressions rather than as pins. An expression over
    a single signal says which pin and which way; anything wider is a cell
    whose waking cannot be written as a sensitivity list.
    """
    got = SIGNAL.match(expr.strip())
    if not got:
        return None
    return "%sedge %s" % ("neg" if got.group(1) else "pos", got.group(2))


# A pin a module has no way to declare: the library names the node a flop
# holds so that its own expressions can refer to it, and the model declares
# that as a register rather than as a port.
FACING = ("input", "output", "inout")


def declare(cell):
    """The port list and directions, with a pin group of many pins a vector"""
    names, decls = [], []
    for group in cell["pin_groups"]:
        if group["direction"] not in FACING:
            continue
        pins = group["pins"]
        names.append(group["name"] if len(pins) > 1 else pins[0]["name"])
        span = ""
        if len(pins) > 1:
            first = group["start_index"]
            last = first + len(pins) - 1
            span = ("[%d:%d] " % (first, last) if group.get("ascending")
                    else "[%d:%d] " % (last, first))
        decls.append("  %s %s%s;" % (group["direction"], span, names[-1]))
    return names, decls


def value(expr):
    """An expression as Verilog, which it already is bar the constants

    A library writes a constant either bare or as `0b1`, and neither is a
    Verilog literal; everything else in these expressions already is one.
    """
    def sized(got):
        bits = got.group(1) or got.group(2)
        return "%d'b%s" % (len(bits), bits)
    return CONST.sub(sized, expr)


def drive(pin):
    """What an output pin is assigned, tristate written as the value or z"""
    if "z_function" in pin:
        return "%s ? 1'bz : %s" % (value(pin["z_function"]),
                                   value(pin["function"]))
    return value(pin["function"])


def outputs(cell):
    """The assignments every output pin with a function of its own gets"""
    lines = []
    for group in cell["pin_groups"]:
        for pin in group["pins"]:
            if pin["direction"] == "output" and "function" in pin:
                lines.append("  assign %s = %s;" % (pin["name"], drive(pin)))
    return lines


def state(cell, config, clocked):
    """The body of a cell that holds something.

    Clear and preset are asynchronous, so they belong in the sensitivity list
    beside the clock; a latch has no clock and follows its input for as long
    as it is let through. Where a cell says what it does when clear and preset
    arrive together, that corner is not written: no synthesiser will infer a
    flop from it, and no netlist worth reading drives both at once.
    """
    wake, body = [], []
    if clocked:
        woken = edge(config["clocked_on"])
        if woken is None:
            return None
        wake.append(woken)
    for which, settle in (("clear_on", "1'b0"), ("preset_on", "1'b1")):
        if which not in config:
            continue
        woken = edge(config[which])
        if woken is None:
            return None
        wake.append(woken)
        body.append("%sif (%s) %s <= %s;"
                    % ("else " if body else "", value(config[which]),
                       config["state"], settle))
    if clocked:
        body.append("%s%s <= %s;" % ("else " if body else "",
                                     config["state"],
                                     value(config["next_state"])))
    elif "data_in" in config:
        body.append("%sif (%s) %s <= %s;"
                    % ("else " if body else "", value(config["enable_on"]),
                       config["state"], value(config["data_in"])))
    lines = ["  reg %s;" % config["state"]]
    if "neg_state" in config:
        lines.append("  wire %s = !%s;" % (config["neg_state"], config["state"]))
    lines.append("  always @(%s)" % (" or ".join(wake) if wake else "*"))
    lines += ["    " + one for one in body]
    return lines


def model(cell):
    """One cell as a module, or as a black box where we cannot say what it does"""
    names, decls = declare(cell)
    head = ["module %s (%s);" % (cell["name"], ", ".join(names))]
    body = None
    if "ff_config" in cell:
        body = state(cell, cell["ff_config"], True)
    elif "latch_config" in cell:
        body = state(cell, cell["latch_config"], False)
    else:
        body = []
    said = outputs(cell)
    if body is None or not (body or said):
        return ["(* blackbox *)"] + head + decls + ["endmodule", ""]
    return head + decls + body + said + ["endmodule", ""]


def main(library, out):
    cells = json.load(open(library))["cells"]
    lines = ["// Written by hgl2v.py from %s. Do not edit."
             % os.path.basename(library), ""]
    dark = 0
    for cell in sorted(cells, key=lambda c: c["name"]):
        for group in cell["pin_groups"]:
            for pin in group["pins"]:
                for which in ("function", "z_function"):
                    if which in pin and not readable(pin[which]):
                        print("%s.%s is written in a language this pass does "
                              "not read" % (cell["name"], pin["name"]))
                        return 1
        text = model(cell)
        dark += text[0].startswith("(* blackbox")
        lines += text
    open(out, "w").write("\n".join(lines))
    print("  %d cells modelled%s -> %s"
          % (len(cells), ", %d left as black boxes" % dark if dark else "", out))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
