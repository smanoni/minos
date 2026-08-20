# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Simulates recovered RTL beside the netlist it was lifted from"""

import sys
import os
import re
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match
import expr

IVERILOG = os.environ.get("IVERILOG", "iverilog")
VVP = os.environ.get("VVP", "vvp")
CYCLES = int(os.environ.get("MINOS_CYCLES", "2000"))
SEED = int(os.environ.get("MINOS_SEED", "1"))

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# A name Verilog cannot spell plainly is written with a backslash and closed by
# a space, and a netlist written back out is full of them: uo_out_reg[0] is one
# name and not an index into anything. They are kept, since a register left out
# here is a register left unstarted, holding no value and agreeing with nothing.
DECL = re.compile(r"^\s*reg\s+(?:\[(\d+):0\]\s*)?"
                  r"(\\\S+|[A-Za-z_][A-Za-z0-9_$]*)\s*;", re.M)
ANY_DECL = re.compile(r"^\s*reg\s[^;]*;", re.M)

# A section this flow pulled out into a module of its own, and the name the
# design instantiates it under. Only these are followed: a library module
# carried along holds nothing the design declared.
PART = re.compile(r"^\s*(\w+_part\d+)\s+(u_part\d+)\s*\(", re.M)

# Inside such a module a register that leaves it is declared as its output,
# so the plain form above does not find it and the register goes unstarted.
INNER = re.compile(r"^\s*(?:output\s+)?reg\s+(?:\[(\d+):0\]\s*)?"
                   r"(\\\S+|[A-Za-z_][A-Za-z0-9_$]*)\s*;", re.M)
ANY_INNER = re.compile(r"^\s*(?:output\s+)?reg\s[^;]*;", re.M)


def registers(lifted):
    """Every register a module declares, with how wide it is.

    Read off the text rather than the netlist, because these are the names a
    testbench can reach into, and the two number their nets differently. What
    they are for is saying how much of the design a run actually stirred, and
    starting both modules from the same value: agreeing on the ports says
    little when most of the state behind them never moved.

    A register whose name cannot be written plainly comes back as None rather
    than being passed over. Verilog lets a name be escaped, and a netlist
    written back out carries names like \\U1776.IQ that no testbench can reach
    into; skipping those quietly leaves a module unstarted and holding no
    value, which then disagrees with everything and reads as a design that
    differs rather than as a run that never began.

    Only the design's own module is read. A file may carry a library module it
    instantiates, and what that holds is neither reachable by this name nor
    anything the design declared.
    """
    whole = open(lifted).read()
    text = whole.split("\nendmodule")[0]
    got = [(name, int(width) + 1 if width else 1)
           for width, name in DECL.findall(text)]
    if len(got) != len(ANY_DECL.findall(text)):
        return None
    # A section written as a module of its own is still this design's state,
    # and a testbench reaches it through the instance. Left out, those
    # registers start at no value and the run reads as a design that differs
    # rather than as one that was never started.
    bodies = {}
    for part in re.split(r"^module ", whole, flags=re.M)[1:]:
        bodies[part.split("(")[0].strip()] = part.split("\nendmodule")[0]
    for kind, tag in PART.findall(text):
        body = bodies.get(kind)
        if body is None:
            continue
        inner = [(name, int(width) + 1 if width else 1)
                 for width, name in INNER.findall(body)]
        if len(inner) != len(ANY_INNER.findall(body)):
            return None
        got += [("%s.%s" % (tag, name), width) for name, width in inner]
    return got


def pin_bits(module, pin):
    """Every net bit some register takes on the named pin"""
    out = set()
    for cell in module["cells"].values():
        if match.FLOP in cell["type"] and pin in cell["connections"]:
            out |= set(cell["connections"][pin])
    return out


def reset_level(module):
    """The value that holds a reset asserted, read off the registers using it.

    Which way round a reset runs is in the flop's name and nowhere else, so a
    testbench that guessed would either never assert it or never release it.
    """
    for cell in module["cells"].values():
        if match.FLOP in cell["type"] and "R" in cell["connections"]:
            return "0" if expr.flop_kind(cell["type"])[1] == "negedge" else "1"
    return None


def interface(module):
    """The ports, split by the part each plays in driving the design.

    A clock has to be a waveform and a reset has to be released, so neither
    can be stimulated the way data is; both are recognised by the register
    pin they reach rather than by their name. Only a port that is one bit
    wide qualifies: a bus carrying a clock among its bits is better driven at
    random, which clocks the design irregularly but clocks both alike.
    """
    clocks, resets = pin_bits(module, "C"), pin_bits(module, "R")
    clk, rst, data, out = [], [], [], []
    for name, spec in sorted(module["ports"].items()):
        if not IDENT.match(name):
            return None
        entry, alone = (name, len(spec["bits"])), len(spec["bits"]) == 1
        if spec["direction"] == "output":
            out.append(entry)
        elif spec["direction"] != "input":
            return None
        elif alone and set(spec["bits"]) & clocks:
            clk.append(entry)
        elif alone and set(spec["bits"]) & resets:
            rst.append(entry)
        else:
            data.append(entry)
    if not out:
        return None
    return clk, rst, data, out


def span(width):
    return "" if width == 1 else "[%d:0] " % (width - 1)


def safe(name):
    """A name a testbench can declare a shadow of.

    A register inside a section of its own is reached as u_part0.n325, and a
    register the netlist escaped is written \\U1776.IQ. Neither can stand in
    the middle of a local identifier, so the shadow this bench keeps beside
    each register is named from the same letters with nothing else in them.
    """
    return re.sub(r"[^0-9A-Za-z_$]", "_", name).lstrip("_") or "reg"


def bench(top, clk, rst, data, out, level, cycles, held=(),
          start=()):
    """A testbench driving both modules from one clock and one stimulus.

    One value is driven per whole clock period, so a rising and a falling edge
    both see it and a design read off the wrong edge is a step ahead of one
    read off the right one rather than fed a different stream. Inputs settle
    well before either edge and outputs are read well after, so what is
    compared is state and not a race.

    The stimulus is drawn from a generator written out with the bench rather
    than the simulator's own, whose successive draws are correlated enough
    that a one bit wide port gets a visible pattern instead of a sample.

    A reference bit that is still unknown is skipped rather than reported:
    both start unreset, and only the recovered module having lost a value is
    a difference worth hearing about. How often the reference's own outputs
    move is counted alongside, because a design whose outputs sit still
    agrees with anything and a run over one has proven nothing.
    """
    lines = ["`timescale 1ns/1ps", "module tb;",
             "  integer i;",
             "  integer bad = 0;", "  integer moved = 0;",
             "  integer stirred = 0;",
             "  reg [31:0] state = %d;" % (SEED or 1),
             "  function [31:0] draw;", "    input dummy;", "    begin",
             "      state = state ^ (state << 13);",
             "      state = state ^ (state >> 17);",
             "      state = state ^ (state << 5);",
             "      draw = state;", "    end", "  endfunction"]
    for name, width in clk + rst + data:
        lines.append("  reg %s%s;" % (span(width), name))
    for name, width in out:
        lines += ["  wire %s%s_ref, %s_dut;" % (span(width), name, name),
                  "  reg %s%s_was;" % (span(width), name)]
    for name, width in held:
        lines += ["  reg %s%s_seen = %d'd0;" % (span(width), safe(name), width),
                  "  reg %s%s_last;" % (span(width), safe(name))]
    every = clk + rst + data
    wire = lambda tag: ", ".join(
        [".%s(%s)" % (n, n) for n, _ in every]
        + [".%s(%s_%s)" % (n, n, tag) for n, _ in out])
    lines += ["  %s_ref u_ref(%s);" % (top, wire("ref")),
              "  %s u_dut(%s);" % (top, wire("dut"))]
    for name, _ in clk:
        lines.append("  always #10 %s = ~%s;" % (name, name))
    lines += ["  task compare;", "    reg hit;", "    begin",
              "      hit = 0;"]
    for name, width in out:
        lines += ["      if ((%s_ref ^ %s_ref) === %d'd0 && "
                  "%s_ref !== %s_dut) begin" % (name, name, width, name, name),
                  "        if (bad == 0 && !hit) $display(\"  %s differs at cycle %%0d: "
                  "netlist %%b, recovered %%b\", i, %s_ref, %s_dut);"
                  % (name, name, name),
                  "        hit = 1;", "      end",
                  "      if (%s_ref !== %s_was) moved = moved + 1;"
                  % (name, name),
                  "      %s_was = %s_ref;" % (name, name)]
    for name, width in held:
        one, tag = "u_dut.%s" % name, safe(name)
        lines += ["      if ((%s ^ %s) === %d'd0 && (%s_last ^ %s_last) === "
                  "%d'd0)" % (one, one, width, tag, tag, width),
                  "        %s_seen = %s_seen | (%s ^ %s_last);"
                  % (tag, tag, one, tag),
                  "      %s_last = %s;" % (tag, one)]
    lines += ["      if (hit) bad = bad + 1;", "    end",
              "  endtask", "  initial begin"]
    # A design with no reset starts at no value at all and stays there, and
    # two modules that both hold nothing agree about nothing. Both are started
    # from zero so that such a design runs, and runs alike on either side.
    for tag, table in (("u_ref", start), ("u_dut", held)):
        for name, _ in table:
            lines.append("    %s.%s = 0;" % (tag, name))
    for name, _ in clk:
        lines.append("    %s = 0;" % name)
    for name, width in rst:
        lines.append("    %s = {%d{1'b%s}};" % (name, width, level))
    lines += ["    for (i = 0; i < %d; i = i + 1) begin" % cycles,
              "      #2;"]
    for name, width in data:
        lines.append("      %s = draw(0);" % name)
    for name, width in rst:
        lines.append("      %s = (i < 3 || i == %d) ? {%d{1'b%s}} : {%d{1'b%s}};"
                     % (name, cycles // 2, width, level, width,
                        "1" if level == "0" else "0"))
    lines += ["      #17 compare;", "      #1;", "    end"]
    total = sum(width for _, width in held)
    stirred = " + ".join("$countones(%s_seen)" % safe(name)
                         for name, _ in held)
    lines += ["    stirred = %s;" % (stirred or "0"),
              "    if (bad != 0) $display(\"  %0d of %0d cycles differ\","
              " bad, i);",
              "    else if (moved < 2) $display(\"  %0d cycles simulated, "
              "nothing moved on the outputs to compare\", i);",
              "    else $display(\"  %0d cycles simulated, %0d output changes,"
              " %0d of " + str(total) + " registers stirred, recovered RTL "
              "matches the netlist\", i, moved, stirred);",
              "    $finish(0);", "  end", "endmodule", ""]
    return "\n".join(lines)


def reference(netlist, top, workdir):
    """The netlist as a module of its own name, so both can be elaborated"""
    path = "%s/cosim_ref.v" % workdir
    code, log = match.yosys(["read_json %s" % netlist,
                             "rename %s %s_ref" % (top, top),
                             "write_verilog -noattr %s" % path],
                            "%s/cosim_ref.ys" % workdir)
    return None if code else path


def simulate(reference_path, lifted, workdir, text):
    """Builds and runs one testbench, or says why it would not build"""
    tb = "%s/cosim_tb.v" % workdir
    open(tb, "w").write(text)
    exe = "%s/cosim.vvp" % workdir
    build = subprocess.run(IVERILOG.split() + ["-g2012", "-o", exe,
                                               reference_path, lifted, tb],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
    if build.returncode:
        print("  simulation would not build: %s" % build.stdout.strip())
        return None
    run = subprocess.run(VVP.split() + [exe], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    return run.stdout


def main(netlist, lifted, outdir):
    workdir = os.path.join(outdir, "tmp")
    design = json.load(open(netlist))
    top = list(design["modules"])[0]
    parts = interface(design["modules"][top])
    if parts is None:
        print("  no outputs to simulate against")
        return 0
    clk, rst, data, out = parts
    level = reset_level(design["modules"][top]) or "0"
    path = reference(netlist, top, workdir)
    if path is None:
        print("  netlist would not write back as verilog")
        return 1
    # A design whose outputs never moved has been compared against nothing,
    # so it is given a longer run before that is what gets reported.
    held, start = registers(lifted), registers(path)
    if held is None or start is None:
        which = "recovered RTL" if held is None else "netlist"
        print("  cannot start the %s: it names a register no testbench can "
              "reach, so a run would compare against a module holding "
              "nothing" % which)
        return 1
    for cycles in (CYCLES, CYCLES * 10):
        out_text = simulate(path, lifted, workdir,
                            bench(top, clk, rst, data, out, level, cycles,
                                  held, start))
        if out_text is None:
            return 1
        if "nothing moved" not in out_text:
            break
    print(out_text.rstrip())
    return 1 if "cycles differ" in out_text else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
