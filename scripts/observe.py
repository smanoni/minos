# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Names a recovered register from what it is seen to do"""

import sys
import os
import re
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cosim

IVERILOG = os.environ.get("IVERILOG", "iverilog")
VVP = os.environ.get("VVP", "vvp")
CYCLES = int(os.environ.get("MINOS_WATCH", "400"))
SEEDS = (1, 7919, 20260815)

# How many times a register has to be seen doing a thing before that thing is
# taken for what it is. A word of few bits does few things, and a handful of
# steps that happen to look alike is not evidence of anything: three moves of
# a two bit word are one bit at a time by chance about a third of the time.
MOVES = 8

# The names this pass gives. Run again over its own output it has to read
# these as work already done rather than as names to number around, or a
# second look at the same design renames flags0 to flags2.
EARNED = re.compile(r"^(?:flags|count|down|shift|gray|latched|onehot)\d+$")

VERBS = {"flags": "only ever gain bits", "count": "count up",
         "down": "count down", "shift": "shift", "latched": "latch and stay",
         "gray": "change one bit at a time", "onehot": "stay one hot"}


def properties(vals, width):
    """What a register was seen to do over one run, if it did one thing.

    Each of these is a claim about the whole run and is dropped the moment a
    single step contradicts it, so what comes back is not a guess about the
    shape of the logic but something the design was watched doing.
    """
    top, hi = 1 << width, 1 << (width - 1)
    steps = [(a, b) for a, b in zip(vals, vals[1:]) if a != b]
    if not steps:
        return "held"
    if width == 1:
        # One bit that moves once and stays is a flag something has happened.
        # Moving more often it is only a bit, since a bit that moves at all
        # has nowhere to move but back and forth.
        return "latched" if len(steps) == 1 and steps[0][0] < steps[0][1] else None
    if len(steps) < MOVES:
        return None
    if all(a & b == a for a, b in steps):
        return "flags"
    if all(b == (a + 1) % top for a, b in steps):
        return "count"
    if all(b == (a - 1) % top for a, b in steps):
        return "down"
    # A word shifted up keeps every bit it had bar the top one, moved along by
    # a place; shifted down it keeps every bit bar the bottom one. What comes
    # in at the far end is whatever the design puts there and is not asked.
    if all(b >> 1 == a & (hi - 1) for a, b in steps) \
            or all(b & (hi - 1) == a >> 1 for a, b in steps):
        return "shift"
    # A word changing one bit at a time is only telling if it does it often
    # and gets around: a few steps of a narrow word look like that by chance.
    if len(steps) >= 2 * width and len(set(vals)) >= width \
            and all(bin(a ^ b).count("1") == 1 for a, b in steps):
        return "gray"
    if len(set(vals)) >= 3 and all(bin(v).count("1") == 1 for v in vals):
        return "onehot"
    return None


def bench(top, clk, rst, data, out, level, held, seed):
    """A testbench that writes down every register once a cycle.

    The stimulus is the one the recovered RTL was checked under, so what is
    watched here is the run that was already vouched for rather than another.
    """
    lines = ["`timescale 1ns/1ps", "module tb;", "  integer i;",
             "  reg [31:0] state = %d;" % (seed or 1),
             "  function [31:0] draw;", "    input dummy;", "    begin",
             "      state = state ^ (state << 13);",
             "      state = state ^ (state >> 17);",
             "      state = state ^ (state << 5);",
             "      draw = state;", "    end", "  endfunction"]
    for name, width in clk + rst + data:
        lines.append("  reg %s%s;" % (cosim.span(width), name))
    for name, width in out:
        lines.append("  wire %s%s;" % (cosim.span(width), name))
    every = clk + rst + data + out
    lines.append("  %s u(%s);"
                 % (top, ", ".join(".%s(%s)" % (n, n) for n, _ in every)))
    for name, _ in clk:
        lines.append("  always #10 %s = ~%s;" % (name, name))
    lines.append("  initial begin")
    for name, _ in held:
        lines.append("    u.%s = 0;" % name)
    for name, _ in clk:
        lines.append("    %s = 0;" % name)
    for name, width in rst:
        lines.append("    %s = {%d{1'b%s}};" % (name, width, level))
    lines += ["    for (i = 0; i < %d; i = i + 1) begin" % CYCLES, "      #2;"]
    for name, _ in data:
        lines.append("      %s = draw(0);" % name)
    for name, width in rst:
        lines.append("      %s = (i < 3) ? {%d{1'b%s}} : {%d{1'b%s}};"
                     % (name, width, level, width,
                        "1" if level == "0" else "0"))
    lines.append("      #17;")
    for name, _ in held:
        lines.append("      $write(\"%%0d \", u.%s);" % name)
    lines += ["      $write(\"\\n\");", "      #1;", "    end",
              "    $finish(0);", "  end", "endmodule", ""]
    return "\n".join(lines)


def watch(lifted, workdir, text):
    """Runs one testbench and reads back a column per register"""
    tb = "%s/observe_tb.v" % workdir
    open(tb, "w").write(text)
    exe = "%s/observe.vvp" % workdir
    build = subprocess.run(IVERILOG.split() + ["-g2012", "-o", exe, lifted, tb],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
    if build.returncode:
        return None
    run = subprocess.run(VVP.split() + [exe], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    rows = []
    for line in run.stdout.splitlines():
        got = line.split()
        if got and all(piece.isdigit() for piece in got):
            rows.append([int(piece) for piece in got])
    if not rows or len({len(r) for r in rows}) != 1:
        return None
    return list(zip(*rows))


def rename(text, table):
    """The recovered text under the names the run earned it.

    A word carries its operands with it, named after it, and they are the same
    word's business under whatever it comes to be called. Every name is put
    aside before any is put back, since one register can be taking the name
    another is giving up and a rename done in order would lose one of them.
    """
    aside = {}
    for at, old in enumerate(table):
        aside[old] = "minos$%d" % at
        text = re.sub(r"\b%s(?=\b|_)" % re.escape(old), aside[old], text)
    for old, new in table.items():
        text = text.replace(aside[old], new)
    return text


def main(netlist, lifted, outdir):
    workdir = os.path.join(outdir, "tmp")
    design = json.load(open(netlist))
    top = list(design["modules"])[0]
    parts = cosim.interface(design["modules"][top])
    if parts is None:
        return 0
    clk, rst, data, out = parts
    if not clk:
        return 0
    level = cosim.reset_level(design["modules"][top]) or "0"
    held = cosim.registers(lifted)
    if not held:
        return 0
    runs = []
    for seed in SEEDS:
        cols = watch(lifted, workdir,
                     bench(top, clk, rst, data, out, level, held, seed))
        if cols is None or len(cols) != len(held):
            print("  nothing to watch the recovered RTL with")
            return 0
        runs.append([properties(col, width)
                     for col, (_, width) in zip(cols, held)])
    # A run only shows what its own stimulus reached, so a name is kept only
    # where every run, from a different start, saw the same thing.
    agreed = [set(seen) for seen in zip(*runs)]
    text = open(lifted).read()
    # A name this pass gave last time is not in the way of giving it again,
    # so only what the design is otherwise called counts as taken.
    words = {w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", text)
             if not EARNED.match(w)}
    # A register carrying a port already answers to the name the rest of the
    # world calls it by, which no run can improve on and renaming would break.
    kept = set(design["modules"][top]["ports"])
    table, count, still, earned, noted = {}, {}, 0, [], []
    for (name, _), saw in zip(held, agreed):
        if len(saw) != 1:
            continue
        kind = saw.pop()
        if kind is None:
            continue
        if kind == "held":
            still += 1
            continue
        if name in kept or any(name.startswith(p + "_") for p in kept):
            noted.append((name, kind))
            continue
        index = count.get(kind, 0)
        while "%s%d" % (kind, index) in words:
            index += 1
        count[kind] = index + 1
        earned.append(kind)
        if "%s%d" % (kind, index) != name:
            table[name] = "%s%d" % (kind, index)
    if table:
        open(lifted, "w").write(rename(text, table))
    # What is reported is what the design is now called, not what this run
    # changed, so looking twice does not read as having found nothing.
    kinds = {}
    for kind in earned:
        kinds[kind] = kinds.get(kind, 0) + 1
    print("  %d of %d registers named from what they were seen to do%s"
          % (len(earned), len(held),
             ": " + ", ".join("%d %s" % (n, k)
                              for k, n in sorted(kinds.items())) if earned else ""))
    # A register carrying an output keeps the port's name, but what it was
    # seen doing is worth saying even so: it is usually the one register in
    # the design a reader wants to know about.
    for name, kind in noted:
        print("  %s carries an output and was seen to %s" % (name, VERBS[kind]))
    if still:
        print("  %d never moved under this stimulus" % still)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
