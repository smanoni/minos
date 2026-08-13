# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Proposes behavioural RTL for a region and keeps only what proves equivalent"""

import sys
import os
import json
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match


CANONICAL = {"clk": "clk", "rst": "rst", "en": "en", "d": "d"}


def shift_body(width, enable, reset, reg="q", role=None):
    """A register whose data comes from the stage before it, in given names"""
    role = role or CANONICAL
    shift = "%s <= {%s[%d:0], %s};" % (reg, reg, width - 2, role["d"])
    edge = "posedge %s" % role["clk"]
    if reset:
        edge += " or negedge %s" % role["rst"]
    body = ["  always @(%s)" % edge]
    if reset:
        body.append("    if (!%s) %s <= %d'd0;" % (role["rst"], reg, width))
        body.append("    else %s%s"
                    % ("if (%s) " % role["en"] if enable else "", shift))
    else:
        body.append("    %s%s" % ("if (%s) " % role["en"] if enable else "", shift))
    return body


def shift_register(width, enable=True, reset=True):
    """The same register as a standalone module with canonical port names"""
    body = ["module cand(clk, rst, en, d, q);",
            "  input clk, rst, en, d;",
            "  output reg [%d:0] q;" % (width - 1)]
    body += shift_body(width, enable, reset)
    return "\n".join(body + ["endmodule", ""])


CHAIN_TEMPLATES = [
    ("shift register, enable, reset", True, True),
    ("shift register, enable", True, False),
    ("shift register, reset", False, True),
    ("shift register", False, False)]


def prove_candidate(text, gold, workdir, tag):
    """Miters a proposed module against the recovered one"""
    path = "%s/%s.v" % (workdir, tag)
    open(path, "w").write(text)
    lines = ["read_verilog %s" % path, "hierarchy -top cand", "proc", "opt",
             "rename cand gate", "write_json %s/%s.json" % (workdir, tag)]
    code, log = match.yosys(lines, "%s/%s_elab.ys" % (workdir, tag))
    if code:
        return "candidate did not elaborate"
    return match.prove(gold, "%s/%s.json" % (workdir, tag), workdir,
                       "%s_prove" % tag)


def lift_chains(netlist, regions, workdir):
    """Behavioural RTL for every register chain that proves equivalent"""
    found = {}
    for index, region in enumerate(regions):
        if region["kind"] != "chain":
            continue
        got = match.extract(netlist, region, index, workdir)
        if not got:
            continue
        path, name = got
        ren = match.roles(path, name, region["registers"])
        conn, width = match.region_wiring(ren)
        if not match.canonicalise(path, name, conn, width, "gold", workdir):
            continue
        for label, enable, reset in CHAIN_TEMPLATES:
            verdict = prove_candidate(shift_register(width, enable, reset),
                                      "%s/gold.json" % workdir,
                                      workdir, "cand_%d" % index)
            print("  region %d  %-28s %s" % (index, label, verdict))
            if verdict == "PROVEN EQUIVALENT":
                found[index] = {"label": label, "width": width,
                                "enable": enable, "reset": reset,
                                "roles": {v: k for k, v in ren.items()}}
                break
    return found


def bus_map(netlist, regions):
    """Which register drives which bit of which recovered bus"""
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    aliases = {}
    for net, spec in module.get("netnames", {}).items():
        for bit in spec["bits"]:
            aliases.setdefault(bit, set()).add(net)
    buses = []
    for region in regions:
        if region["kind"] != "chain":
            continue
        bits = []
        for flop in reversed(region["registers"]):
            q = module["cells"][flop]["connections"]["Q"][0]
            bits.append(aliases.get(q, set()))
        buses.append(bits)
    return buses


def cone_wiring(path, module_name, buses):
    """Maps a cone's ports onto the recovered buses, by bit position"""
    design = json.load(open(path))
    ports = design["modules"][module_name]["ports"]
    conn, width = {}, 0
    for index, bits in enumerate(buses):
        for position, names in enumerate(bits):
            hit = [n for n in names if n in ports]
            if len(hit) == 1:
                conn[hit[0]] = "%s[%d]" % ("ab"[index], position)
                width = max(width, position + 1)
    outs = [n for n, p in ports.items() if p["direction"] == "output"]
    if len(outs) != 1 or len(conn) != sum(len(b) for b in buses):
        return None, 0
    conn[outs[0]] = "y"
    return conn, width


def cone_wrapper(name, inner, conn, width):
    return "\n".join([
        "module %s(a, b, y);" % name,
        "  input [%d:0] a, b;" % (width - 1),
        "  output y;",
        "  %s i_dut (%s);" % (inner, ", ".join(
            ".%s(%s)" % (p, e) for p, e in sorted(conn.items()))),
        "endmodule", ""])


def solve_constant(gold, width, workdir, tag):
    """Reads a satisfying assignment off the cone to learn what it compares to"""
    lines = ["read_json %s" % gold, "hierarchy -top gold",
             "sat -set y 1 -show a -show b"]
    code, out = match.yosys(lines, "%s/%s_solve.ys" % (workdir, tag))
    if code:
        return None
    values = {}
    for line in out.split("\n"):
        got = re.match(r"\s*\\?(a|b)\s+(\d+)\s", line)
        if got:
            values[got.group(1)] = int(got.group(2))
    if len(values) != 2:
        return None
    return values["a"] + values["b"]


SUM_TEMPLATES = [
    ("sum equals constant", "assign y = ((a + b) == %d'd%d);"),
    ("difference equals constant", "assign y = ((a - b) == %d'd%d);"),
]


def lift_cones(netlist, regions, workdir):
    """Behavioural RTL for every output cone that proves equivalent"""
    buses = bus_map(netlist, regions)
    found = {}
    for index, region in enumerate(regions):
        if region["kind"] != "cone" or not buses:
            continue
        got = match.extract(netlist, region, index, workdir)
        if not got:
            continue
        path, name = got
        conn, width = cone_wiring(path, name, buses)
        if conn is None:
            print("  cone %s  no bus correspondence" % region["output"])
            continue
        wrap = "%s/cone_%d_wrap.v" % (workdir, index)
        open(wrap, "w").write(cone_wrapper("gold", name, conn, width))
        code, log = match.yosys(
            ["read_json %s" % path, "read_verilog %s" % wrap,
             "hierarchy -top gold", "flatten", "opt_clean",
             "write_json %s/gold.json" % workdir],
            "%s/cone_%d_wrap.ys" % (workdir, index))
        if code:
            continue
        constant = solve_constant("%s/gold.json" % workdir, width, workdir,
                                  "cone_%d" % index)
        if constant is None:
            print("  cone %s  no satisfying assignment" % region["output"])
            continue
        print("  cone %s  witness suggests constant %d"
              % (region["output"], constant))
        for label, form in SUM_TEMPLATES:
            text = "\n".join([
                "module cand(a, b, y);",
                "  input [%d:0] a, b;" % (width - 1),
                "  output y;",
                "  " + form % (width + 1, constant),
                "endmodule", ""])
            verdict = prove_candidate(text, "%s/gold.json" % workdir, workdir,
                                      "conecand_%d" % index)
            print("    %-28s %s" % (label, verdict))
            if verdict == "PROVEN EQUIVALENT":
                found[region["output"]] = {"label": label, "constant": constant,
                                            "width": width, "form": form}
                break
    return found


def bit_names(ports, direction):
    """Every port bit of a direction, under the name a split net carries"""
    out = {}
    for name, spec in ports.items():
        if spec["direction"] != direction:
            continue
        for i, bit in enumerate(spec["bits"]):
            out[name if len(spec["bits"]) == 1 else "%s[%d]" % (name, i)] = bit
    return out


def output_wiring(ports, chains, names):
    """Drives each output bit from a lifted register, a constant or an input"""
    driven, lines = {}, []
    for slot, (index, info) in enumerate(sorted(chains.items())):
        for role, port in info["roles"].items():
            if role.startswith("q"):
                driven[port] = "%s[%s]" % (names[index], role[1:])
    inputs = {bit: name for name, bit in bit_names(ports, "input").items()}
    for name, bit in sorted(bit_names(ports, "output").items()):
        if name in driven:
            lines.append("  assign %s = %s;" % (name, driven[name]))
        elif bit in ("0", "1"):
            lines.append("  assign %s = 1'b%s;" % (name, bit))
        elif bit in inputs:
            lines.append("  assign %s = %s;" % (name, inputs[bit]))
    return lines


def write_rtl(netlist, chains, cones, out):
    """Assembles the proven pieces into one readable module"""
    design = json.load(open(netlist))
    top = list(design["modules"])[0]
    ports = design["modules"][top]["ports"]
    order = sorted(ports)
    lines = ["module %s(%s);" % (top, ", ".join(order))]
    for name in order:
        spec = ports[name]
        span = "" if len(spec["bits"]) == 1 else "[%d:0] " % (len(spec["bits"]) - 1)
        lines.append("  %s %s%s;" % (spec["direction"], span, name))
    lines.append("")
    names = {}
    for slot, (index, info) in enumerate(sorted(chains.items())):
        reg = "r%d" % slot
        names[index] = reg
        lines.append("  reg [%d:0] %s;" % (info["width"] - 1, reg))
        lines += shift_body(info["width"], info["enable"], info["reset"],
                            reg, info["roles"])
        lines.append("")
    for output, info in sorted(cones.items()):
        expr = info["form"].replace("y", output, 1) % (info["width"] + 1,
                                                       info["constant"])
        for slot, letter in enumerate("ab"):
            expr = re.sub(r"\b%s\b" % letter, names.get(slot, letter), expr)
        lines.append("  " + expr)
    lines += output_wiring(ports, chains, names)
    lines += ["endmodule", ""]
    open(out, "w").write("\n".join(lines))
    return out


def main(netlist, regions_path, outdir, out=None):
    workdir = os.path.join(outdir, "tmp")
    os.makedirs(workdir, exist_ok=True)
    regions = json.load(open(regions_path))
    print("chains")
    chains = lift_chains(netlist, regions, workdir)
    print("  %d of %d chains lifted"
          % (len(chains), sum(1 for r in regions if r["kind"] == "chain")))
    print("cones")
    cones = lift_cones(netlist, regions, workdir)
    print("  %d of %d cones lifted"
          % (len(cones), sum(1 for r in regions if r["kind"] == "cone")))
    if out and (chains or cones):
        write_rtl(netlist, chains, cones, out)
        verdict = prove_candidate(open(out).read().replace(
            "module %s(" % list(json.load(open(netlist))["modules"])[0],
            "module cand("), netlist_as_gold(netlist, workdir), workdir, "rtl")
        print("rtl -> %s" % out)
        print("  whole module vs recovered netlist: %s" % verdict)
        return 0 if verdict == "PROVEN EQUIVALENT" else 1
    return 0 if chains or cones else 1


def netlist_as_gold(netlist, workdir):
    """The recovered netlist, renamed so it can stand as the reference"""
    top = list(json.load(open(netlist))["modules"])[0]
    match.yosys(["read_json %s" % netlist, "rename %s gold" % top,
                 "write_json %s/rtl_gold.json" % workdir],
                "%s/rtl_gold.ys" % workdir)
    return "%s/rtl_gold.json" % workdir


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
