# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Extracts recovered regions and shortlists the references worth proving"""

import sys
import os
import re
import json
import glob
import signal
import subprocess
import collections

import structure

YOSYS = os.environ.get("YOSYS", "yosys")
TIMEOUT = int(os.environ.get("MINOS_TIMEOUT", "300"))
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


def extract(netlist, region, index, workdir, expose=False):
    """Pulls one region out into a module of its own, via a tagged submod.

    A group that computes its own next state keeps its registers inside, so
    nothing of it would be observable; exposing them gives the region the
    outputs a template can be compared against.
    """
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    top = list(design["modules"])[0].lstrip("\\")
    for cell in region["cells"]:
        module["cells"][cell].setdefault("attributes", {})["minos_region"] = "y"
    tagged = "%s/region_%d_tagged.json" % (workdir, index)
    json.dump(design, open(tagged, "w"))

    name = "minos_region_%d" % index
    script = [
        "read_json %s" % tagged,
        "hierarchy -top %s" % top,
        "splitnets -ports",
        "submod -name %s a:minos_region" % name,
        "hierarchy -top %s" % name]
    if expose:
        script += ["cd %s" % name, "expose -dff", "cd .."]
    script = "\n".join(script + ["write_json %s/%s.json" % (workdir, name)])
    path = "%s/%s.ys" % (workdir, name)
    open(path, "w").write(script + "\n")
    run = subprocess.run(YOSYS.split() + ["-q", "-s", path],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if run.returncode:
        why = run.stdout.strip().split("\n")
        print("  extraction failed: %s" % (why[-1] if why else "no output"))
        return None
    return "%s/%s.json" % (workdir, name), name


def port_of(module, bit):
    """The port carrying a given net bit, if any"""
    for name, port in module.get("ports", {}).items():
        if bit in port["bits"]:
            return name
    return None


def driver_of(cells):
    """Which cell drives each net bit"""
    out = {}
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in bits:
                    out[bit] = name
    return out


def sync_port(module, cells, order):
    """The port that holds a region's registers at a constant, and at which.

    Read off the region rather than the netlist it came from, so the answer
    is a port of the copy being proved and needs matching back to nothing.
    A register with a reset pin already says so on the pin; this is for the
    reset that was synthesised into the data path and left no other trace.
    """
    driver = driver_of(cells)
    for name in sorted(module.get("ports", {})):
        spec = module["ports"][name]
        if spec["direction"] != "input" or len(spec["bits"]) != 1:
            continue
        got = structure.held_value(cells, driver, order, spec["bits"][0])
        if got:
            return name, got
    return None, None


def sync_held(path, module_name, order):
    """What a region's synchronous reset holds it at, where it has one"""
    module = json.load(open(path))["modules"][module_name]
    return sync_port(module, module["cells"], order)[1]


def roles(path, module_name, chain):
    """Names a chain region's ports by what they do, so two can be compared.

    A chain has one clock, one reset, one serial input and one output per
    stage. The serial input is the port the first stage sees and the second
    does not; an enable reaches every stage, so it cannot be confused with it.
    A role is what a port gets wired to, so no two ports may share one: a
    region with more inputs than roles is not a plain chain and is refused
    rather than described as one whose inputs happen to be tied together.

    A reset synthesised into the data path is a port like any other until it
    is recognised, so it is given a role of its own before the rest are
    counted; without that a chain that has both a reset and an enable looks
    like one with two enables and is refused.

    Every stage has to reach an output of its own as well. A stage that only
    the next one reads is no port of the region, and a template compared
    against it would be compared against a bit nothing drives.
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
    if sum(1 for r in ren.values() if r[:1] == "q") != len(order):
        return None
    ins = [n for n, p in module["ports"].items()
           if p["direction"] == "input" and n not in ren]
    first = support_ports(module, cells, order[0])
    second = support_ports(module, cells, order[1]) if len(order) > 1 else set()
    serial = [n for n in ins if n in first - second]
    rest = [n for n in ins if n not in serial]
    sync = sync_port(module, cells, order)[0]
    rest = [n for n in rest if n != sync]
    if len(serial) > 1 or len(rest) > 1:
        return None
    for n in serial:
        ren[n] = "d"
    for n in rest:
        ren[n] = "en"
    if sync:
        ren[sync] = "sr"
    return ren


def load_roles(path, module_name, order):
    """Names a loadable shift register's ports: a select and a value per stage.

    Every stage past the first takes its load value from a port of its own, so
    unlike a plain chain there is nothing to guess: the muxes say which port
    is the select, which is a load value and which stage each belongs to.
    """
    design = json.load(open(path))
    module = design["modules"][module_name]
    cells = module["cells"]
    driver = {}
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in bits:
                    driver[bit] = name
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
    def claim(port, role):
        if port is None or ren.get(port, role) != role:
            return False
        ren[port] = role
        return True

    if not claim(port_of(module, cells[order[0]]["connections"]["D"][0]), "d"):
        return None
    for i, flop in enumerate(order[1:], 1):
        link = structure.mux_link(cells, driver, flop)
        if link is None or link["prev"] != order[i - 1]:
            return None
        if not claim(port_of(module, link["load"]), "v%d" % i) or \
                not claim(port_of(module, link["sel"]), "ld"):
            return None
    return ren


def bank_roles(path, module_name, group):
    """Names a bank's ports: one output per register, one input loading each.

    Unlike a chain there is no order to recover, so the registers are numbered
    as they come. A port reaching every register is an enable; one reaching a
    single register loads it. Anything else is not a plain bank, so give up.
    """
    design = json.load(open(path))
    module = design["modules"][module_name]
    cells = module["cells"]
    ren, reach, outputs = {}, {}, {}
    for pin, name in (("C", "clk"), ("R", "rst")):
        bits = cells[group[0]]["connections"].get(pin)
        if bits:
            got = port_of(module, bits[0])
            if got:
                ren[got] = name
    for i, flop in enumerate(group):
        outputs[i] = port_of(module, cells[flop]["connections"]["Q"][0])
        reach[i] = support_ports(module, cells, flop)
    sync = sync_port(module, cells, group)[0]
    if sync:
        ren[sync] = "sr"
    loaded = {}
    for name in [n for n, p in module["ports"].items()
                 if p["direction"] == "input" and n not in ren]:
        owners = [i for i in reach if name in reach[i]]
        if len(owners) == len(group) > 1:
            ren[name] = "en"
        elif len(owners) == 1 and owners[0] not in loaded:
            loaded[owners[0]] = name
        else:
            return None
    if len(loaded) != len(group):
        return None
    order = sorted(loaded, key=lambda i: bit_order(loaded[i]))
    for bit, old in enumerate(order):
        ren[loaded[old]] = "d%d" % bit
        if outputs[old]:
            ren[outputs[old]] = "q%d" % bit
    return ren, [group[i] for i in order]


def state_roles(path, module_name, order):
    """Names a state group's ports: a clock, a reset, an enable and outputs.

    The group feeds itself, so the only inputs are control. One reaching every
    register is an enable; the registers arrive already in bit order.
    """
    design = json.load(open(path))
    module = design["modules"][module_name]
    cells = module["cells"]
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
    sync = sync_port(module, cells, order)[0]
    if sync:
        ren[sync] = "sr"
    rest = sorted(n for n, p in module["ports"].items()
                  if p["direction"] == "input" and n not in ren)
    if len(rest) > CONTROLS:
        return None
    for i, name in enumerate(rest):
        ren[name] = "c%d" % i
    return ren


CONTROLS = 4


def bit_order(name):
    """Sorts split net names the way their bus reads, so n[9] follows n[8]"""
    got = re.match(r"(.*)\[(\d+)\]$", name)
    return (got.group(1), int(got.group(2))) if got else (name, -1)


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
    """The same map for a region, whose roles came from its own structure.

    Every port has to reach a role of its own. Two sharing one would be wired
    to the same net and the region would be proved with its inputs tied
    together, so a repeat is refused rather than passed on. A region takes
    its control on one bus or the other, so the last width covers both.
    """
    if ren is None or len(set(ren.values())) != len(ren):
        return None
    conn, wide = {}, {"q": 0, "d": 0, "c": 0, "v": 0}
    for name, role in ren.items():
        if role[:1] in wide and role[1:].isdigit():
            index = int(role[1:])
            conn[name] = "%s[%d]" % (role[0], index)
            wide[role[0]] = max(wide[role[0]], index + 1)
        else:
            conn[name] = role
    return conn, wide["q"], wide["d"], max(wide["c"], wide["v"])


def escape(name):
    """Verilog escaped identifier, needed once split nets carry a bit index"""
    if name and (name[0].isalpha() or name[0] == "_") and \
            all(c.isalnum() or c == "_" for c in name):
        return name
    return "\\%s " % name


def state_wrapper(name, inner, conn, width, cwidth):
    """A state group's interface: control in, the whole register out"""
    return "\n".join([
        "module %s(clk, rst, sr, c, q);" % name,
        "  input clk, rst, sr;",
        "  input [%d:0] c;" % (max(cwidth, 1) - 1),
        "  output [%d:0] q;" % (width - 1),
        "  %s i_dut (%s);" % (inner, ", ".join(
            ".%s(%s)" % (escape(p), e) for p, e in sorted(conn.items()) if e)),
        "endmodule", ""])


def load_wrapper(name, inner, conn, width, vwidth):
    """A loadable shift register's interface: a select and a value per stage.

    The first stage takes the shifted-in bit whether the register loads or
    not, so v[0] is never wired to anything and stays out of the comparison.
    """
    return "\n".join([
        "module %s(clk, rst, sr, ld, v, d, q);" % name,
        "  input clk, rst, sr, ld, d;",
        "  input [%d:0] v;" % (max(vwidth, 1) - 1),
        "  output [%d:0] q;" % (width - 1),
        "  %s i_dut (%s);" % (inner, ", ".join(
            ".%s(%s)" % (escape(p), e) for p, e in sorted(conn.items()) if e)),
        "endmodule", ""])


def wrapper(name, inner, conn, width, dwidth=0):
    """A module with canonical ports, so two of them can be mitered by name"""
    ports = ["clk", "rst", "sr", "en", "d", "q"]
    body = ["module %s(%s);" % (name, ", ".join(ports)),
            "  input clk, rst, sr, en;",
            "  input %sd;" % ("[%d:0] " % (dwidth - 1) if dwidth else ""),
            "  output [%d:0] q;" % (width - 1),
            "  %s i_dut (%s);" % (inner, ", ".join(
                ".%s(%s)" % (escape(p), e) for p, e in sorted(conn.items()) if e)),
            "endmodule"]
    return "\n".join(body) + "\n"


def yosys(lines, path, timeout=None):
    """Runs a script, giving up rather than waiting on a proof that will not end.

    Induction on a large module can run for hours without converging, so every
    call is bounded. The process gets its own group because the tool is often
    reached through a wrapper, which would otherwise leave the real one behind.
    """
    open(path, "w").write("\n".join(lines) + "\n")
    run = subprocess.Popen(YOSYS.split() + ["-s", path],
                           universal_newlines=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           start_new_session=True)
    try:
        out, _ = run.communicate(timeout=timeout or TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(run.pid), signal.SIGKILL)
        run.communicate()
        return 124, "TIMEOUT"
    return run.returncode, out


def canonicalise(path, module, conn, width, as_name, workdir, dwidth=0,
                 build=None):
    """Wraps a module so its ports carry canonical names and widths"""
    wrap = "%s/%s_wrap.v" % (workdir, as_name)
    open(wrap, "w").write((build or wrapper)(as_name, module, conn, width,
                                             dwidth))
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
    if out == "TIMEOUT":
        return "NOT PROVEN, gave up after %ds" % TIMEOUT
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
        got = region_wiring(ren)
        if got is None:
            print("  region %d: more inputs than a chain has roles" % idx)
            continue
        conn, width, dwidth, _ = got
        if not canonicalise(path, name, conn, width, "gold", workdir, dwidth):
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
        wa, wb = region_wiring(ra), region_wiring(rb)
        if wa is None or wb is None:
            continue
        ca, wa, da, _ = wa
        cb, wb, db, _ = wb
        if canonicalise(pa, ma, ca, wa, "gold", workdir, da) and \
           canonicalise(pb, mb, cb, wb, "gate", workdir, db):
            print("  region %d vs region %d%s%s"
                  % (ia, ib, " " * 21,
                     prove("%s/gold.json" % workdir, "%s/gate.json" % workdir,
                           workdir, "prove_%d_%d" % (ia, ib))))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
