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


def bench(top, clk, rst, data, out, level, cycles):
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
    lines += ["      if (hit) bad = bad + 1;", "    end",
              "  endtask", "  initial begin"]
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
    lines += ["      #17 compare;", "      #1;", "    end",
              "    if (bad != 0) $display(\"  %0d of %0d cycles differ\","
              " bad, i);",
              "    else if (moved < 2) $display(\"  %0d cycles simulated, "
              "nothing moved on the outputs to compare\", i);",
              "    else $display(\"  %0d cycles simulated, %0d output changes,"
              " recovered RTL matches the netlist\", i, moved);",
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
    for cycles in (CYCLES, CYCLES * 10):
        out_text = simulate(path, lifted, workdir,
                            bench(top, clk, rst, data, out, level, cycles))
        if out_text is None:
            return 1
        if "nothing moved" not in out_text:
            break
    print(out_text.rstrip())
    return 1 if "cycles differ" in out_text else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
