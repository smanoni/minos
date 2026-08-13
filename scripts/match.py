# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Extracts recovered regions and shortlists the references worth proving"""

import sys
import os
import json
import glob
import subprocess
import collections

YOSYS = os.environ.get("YOSYS", "yosys")
FLOP = "DFF"


def signature(path, top=None):
    """Interface width and cell mix, the cheap test before proving anything"""
    design = json.load(open(path))
    name, module = None, None
    for name, module in design["modules"].items():
        if top is None or name == top:
            break
    ins = outs = 0
    for port in module.get("ports", {}).values():
        n = len(port["bits"])
        if port["direction"] == "input":
            ins += n
        else:
            outs += n
    cells = module.get("cells", {})
    return {"module": name, "inputs": ins, "outputs": outs,
            "flops": sum(1 for c in cells.values() if FLOP in c["type"]),
            "cells": len(cells),
            "mix": collections.Counter(c["type"] for c in cells.values())}


def extract(netlist, region, index, workdir):
    """Pulls one region out into a module of its own, via a tagged submod"""
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    top = list(design["modules"])[0].lstrip("\\")
    for cell in region["cells"]:
        module["cells"][cell].setdefault("attributes", {})["minos_region"] = "y"
    tagged = "%s/region_%d_tagged.json" % (workdir, index)
    json.dump(design, open(tagged, "w"))

    name = "minos_region_%d" % index
    script = "\n".join([
        "read_json %s" % tagged,
        "hierarchy -top %s" % top,
        "splitnets -ports",
        "submod -name %s a:minos_region" % name,
        "hierarchy -top %s" % name,
        "write_json %s/%s.json" % (workdir, name)])
    path = "%s/%s.ys" % (workdir, name)
    open(path, "w").write(script + "\n")
    run = subprocess.run(YOSYS.split() + ["-q", "-s", path],
                         capture_output=True, text=True)
    if run.returncode:
        print("  extraction failed: %s" % run.stderr.strip().split("\n")[-1])
        return None
    return "%s/%s.json" % (workdir, name), name


def port_of(module, bit):
    """The port carrying a given net bit, if any"""
    for name, port in module.get("ports", {}).items():
        if bit in port["bits"]:
            return name
    return None


def roles(path, module_name, chain):
    """Names a chain region's ports by what they do, so two can be compared.

    A chain has one clock, one reset, one serial input and one output per
    stage. The serial input is the port the first stage sees and the second
    does not; an enable reaches every stage, so it cannot be confused with it.
    """
    design = json.load(open(path))
    module = design["modules"][module_name]
    cells = module["cells"]
    order = list(reversed(chain))
    ren = {}
    for pin, name in (("C", "clk"), ("R", "rst")):
        bits = cells[order[0]]["connections"].get(pin)
        if bits:
            got = port_of(module, bits[0])
            if got:
                ren[got] = name
    for i, flop in enumerate(order):
        got = port_of(module, cells[flop]["connections"]["Q"][0])
        if got:
            ren[got] = "q%d" % i
    ins = [n for n, p in module["ports"].items()
           if p["direction"] == "input" and n not in ren]
    first = support_ports(module, cells, order[0])
    second = support_ports(module, cells, order[1]) if len(order) > 1 else set()
    serial = [n for n in ins if n in first - second]
    for n in ins:
        ren[n] = "d" if n in serial else "en"
    return ren


def support_ports(module, cells, flop):
    """Input ports reaching a flop's data pin"""
    driver = {}
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in bits:
                    driver[bit] = name
    seen, found = set(), set()
    queue = [b for p, bits in cells[flop]["connections"].items() for b in bits
             if cells[flop]["port_directions"].get(p) == "input" and p not in ("C", "R")]
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        got = port_of(module, bit)
        if got:
            found.add(got)
        src = driver.get(bit)
        if src is None or FLOP in cells[src]["type"]:
            continue
        queue += [b for p, bits in cells[src]["connections"].items() for b in bits
                  if cells[src]["port_directions"].get(p) == "input"]
    return found


CONTROL = (("clk", "clk"), ("rst", "rst"), ("clr", "clr"), ("en", "en"))


def ref_wiring(path, module, width):
    """Maps a reference's ports onto canonical roles by pulp naming convention.

    Ports are clk_i, rst_ni, en_i and so on, so the role of each is readable
    from its name. A clear the recovered design does not have is tied off.
    """
    design = json.load(open(path))
    port = design["modules"][module]["ports"]
    conn, taken = {}, set()
    for role, pattern in CONTROL:
        for name in sorted(port):
            if name in taken or port[name]["direction"] != "input":
                continue
            if pattern in name.lower():
                conn[name] = "1'b0" if role == "clr" else role
                taken.add(name)
                break
    data = [n for n in sorted(port)
            if n not in taken and port[n]["direction"] == "input"]
    outs = [n for n in sorted(port) if port[n]["direction"] == "output"]
    if len(data) != 1 or not outs:
        return None
    conn[data[0]] = "d"
    wide = [n for n in outs if len(port[n]["bits"]) == width]
    if not wide:
        return None
    conn[wide[0]] = "q"
    for n in outs:
        if n not in conn:
            conn[n] = ""
    return conn


def region_wiring(ren):
    """The same map for a region, whose roles came from its own structure"""
    conn, width = {}, 0
    for name, role in ren.items():
        if role.startswith("q"):
            index = int(role[1:])
            conn[name] = "q[%d]" % index
            width = max(width, index + 1)
        else:
            conn[name] = role
    return conn, width


def escape(name):
    """Verilog escaped identifier, needed once split nets carry a bit index"""
    if name and (name[0].isalpha() or name[0] == "_") and \
            all(c.isalnum() or c == "_" for c in name):
        return name
    return "\\%s " % name


def wrapper(name, inner, conn, width):
    """A module with canonical ports, so two of them can be mitered by name"""
    ports = ["clk", "rst", "en", "d", "q"]
    body = ["module %s(%s);" % (name, ", ".join(ports)),
            "  input clk, rst, en, d;",
            "  output [%d:0] q;" % (width - 1),
            "  %s i_dut (%s);" % (inner, ", ".join(
                ".%s(%s)" % (escape(p), e) for p, e in sorted(conn.items()) if e)),
            "endmodule"]
    return "\n".join(body) + "\n"


def yosys(lines, path):
    open(path, "w").write("\n".join(lines) + "\n")
    run = subprocess.run(YOSYS.split() + ["-s", path], capture_output=True, text=True)
    return run.returncode, run.stdout + run.stderr


def canonicalise(path, module, conn, width, as_name, workdir):
    """Wraps a module so its ports carry canonical names and widths"""
    wrap = "%s/%s_wrap.v" % (workdir, as_name)
    open(wrap, "w").write(wrapper(as_name, module, conn, width))
    lines = ["read_json %s" % path, "read_verilog %s" % wrap,
             "hierarchy -top %s" % as_name, "flatten", "opt_clean",
             "write_json %s/%s.json" % (workdir, as_name)]
    code, log = yosys(lines, "%s/%s_wrap.ys" % (workdir, as_name))
    return code == 0


def prove(a, b, workdir, tag):
    """Proves two canonicalised modules equivalent over all time"""
    lines = ["read_json %s" % a, "read_json %s" % b,
             "miter -equiv -flatten -make_assert gold gate miter",
             "async2sync",
             "sat -verify -prove-asserts -tempinduct -set-init-zero miter"]
    code, out = yosys(lines, "%s/%s.ys" % (workdir, tag))
    if code == 0 and "SUCCESS!" in out and "FAIL!" not in out:
        return "PROVEN EQUIVALENT"
    if "proof did fail" in out or "FAIL!" in out:
        return "NOT EQUIVALENT"
    if "ERROR" in out:
        return "no miter: %s" % out.split("ERROR:")[-1].strip().split("\n")[0]
    return "NOT EQUIVALENT"


def compatible(region, ref):
    """A reference cannot match a region of a different shape"""
    return (region["flops"] == ref["flops"]
            and region["inputs"] <= ref["inputs"] + 2
            and region["outputs"] <= ref["outputs"] + 2)


def main(netlist, regions_path, libdir, outdir):
    workdir = os.path.join(outdir, "tmp")
    os.makedirs(workdir, exist_ok=True)
    regions = json.load(open(regions_path))
    refs = {}
    for path in sorted(glob.glob(libdir + "/*.json")):
        name = os.path.basename(path)[:-5]
        refs[name] = signature(path)

    print("references")
    for name, sig in refs.items():
        print("  %-28s %2d in %2d out %3d cells %2d flops"
              % (name, sig["inputs"], sig["outputs"], sig["cells"], sig["flops"]))

    print("regions")
    chains = []
    for i, region in enumerate(regions):
        if region["kind"] != "chain":
            continue
        got = extract(netlist, region, i, workdir)
        if not got:
            continue
        path, name = got
        sig = signature(path, name)
        print("  %-20s %2d in %2d out %3d cells %2d flops"
              % ("region %d (%s)" % (i, region["kind"]),
                 sig["inputs"], sig["outputs"], sig["cells"], sig["flops"]))
        cand = [n for n, r in refs.items() if compatible(sig, r)]
        print("      candidates: %s" % (", ".join(cand) if cand else "none"))
        chains.append((i, path, name, roles(path, name, region["registers"]), cand))

    print("proofs")
    for idx, path, name, ren, cand in chains:
        conn, width = region_wiring(ren)
        if not canonicalise(path, name, conn, width, "gold", workdir):
            print("  region %d: could not be wrapped" % idx)
            continue
        for ref in cand:
            refpath = "%s/%s.json" % (libdir, ref)
            wiring = ref_wiring(refpath, refs[ref]["module"], width)
            if wiring is None:
                print("  region %d vs %-26s no role correspondence" % (idx, ref))
                continue
            if not canonicalise(refpath, refs[ref]["module"], wiring, width,
                                "gate", workdir):
                print("  region %d vs %-26s reference could not be wrapped"
                      % (idx, ref))
                continue
            print("  region %d vs %-26s %s"
                  % (idx, ref, prove("%s/gold.json" % workdir,
                                     "%s/gate.json" % workdir, workdir,
                                     "prove_%d_%s" % (idx, ref))))

    for a in range(len(chains) - 1):
        ia, pa, ma, ra, _ = chains[a]
        ib, pb, mb, rb, _ = chains[a + 1]
        ca, wa = region_wiring(ra)
        cb, wb = region_wiring(rb)
        if canonicalise(pa, ma, ca, wa, "gold", workdir) and \
           canonicalise(pb, mb, cb, wb, "gate", workdir):
            print("  region %d vs region %d%s%s"
                  % (ia, ib, " " * 21,
                     prove("%s/gold.json" % workdir, "%s/gate.json" % workdir,
                           workdir, "prove_%d_%d" % (ia, ib))))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
