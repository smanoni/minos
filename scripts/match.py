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

# What in a parameter's name says it sizes the module. A module is worth
# trying at a region's width only if something about it can be set to that
# width, and the rest of what a library module takes is behaviour rather than
# size.
SIZING = re.compile(r"(?i)width|depth|size|num|indices")

# sv2v writes one copy of a module per set of type parameters it was
# instantiated with, and names the copies after a hash. Those are inner
# workings of the modules that used them, not anything a design would be
# built from, so they are not offered.
INNER = re.compile(r"_[0-9A-F]{5}(?:_[0-9A-F]{5})*$")

# How far the search for a parameter may go. A ceiling below the region being
# matched is not a ceiling but a wrong answer: the puzzle's eighty bit group
# was reported as having nothing to try at all, when what had happened was
# that no reference had been allowed to grow wide enough to hold it. So the
# region sets it, and this is only the floor, for the modules whose parameter
# counts something rather than sizing it and has to run well past the width.
WIDEST = int(os.environ.get("MINOS_CC_WIDEST", "64"))


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
            "widths": sorted((len(p["bits"]) for p in module.get("ports", {}).values()
                              if p["direction"] != "input"), reverse=True),
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


def ref_state_wiring(path, module, width, cwidth):
    """Maps a reference onto a state group's roles, tying off what it lacks.

    A group that feeds itself takes no data, so every input a reference has
    beyond its clock, reset and enable is something the design never drove and
    is held at zero: a library counter offers a load value and a direction, and
    a design that only ever counts up from where it was uses neither. The
    enable follows the region — where a region has no enable the reference is
    held on, or it would be proved against a counter that never counts.
    """
    port = json.load(open(path))["modules"][module]["ports"]
    conn, taken = {}, set()
    for role, pattern in CONTROL:
        for name in sorted(port):
            if name in taken or port[name]["direction"] != "input":
                continue
            if pattern in name.lower():
                conn[name] = {"clr": "1'b0",
                              "en": "c[0]" if cwidth else "1'b1"}.get(role, role)
                taken.add(name)
                break
    for name in sorted(port):
        if port[name]["direction"] == "input" and name not in taken:
            conn[name] = "%d'b0" % len(port[name]["bits"])
    outs = [n for n in sorted(port) if port[n]["direction"] == "output"]
    wide = [n for n in outs if len(port[n]["bits"]) == width]
    if not wide:
        return None
    conn[wide[0]] = "q"
    for name in outs:
        conn.setdefault(name, "")
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


def widest(sig):
    """The widest thing a module puts out, which is its data if it has any"""
    return sig["widths"][0] if sig["widths"] else 0


def compatible(region, ref):
    """A reference cannot match a region of a different shape.

    Built to order the shapes are close by construction, so what is left to
    check is loose: a reference may hold state a region does not, a counter
    keeping an overflow bit the design never kept, and still agree on every
    output the design has. What it may not do is be smaller than the region,
    since nothing that has fewer registers can hold what the region holds.
    """
    return ref["flops"] >= region["flops"]


def library(source):
    """Every module in the library that something about it can size.

    Held as a list once, this was six modules somebody had thought of. Read
    off the source it is every one there is, which is the difference between
    asking whether a design uses the modules we guessed at and asking whether
    it uses any of them.
    """
    text = open(source).read()
    out = []
    for name, params in re.findall(
            r"^module (cc_[A-Za-z0-9_]+) \([^;]*?\);\n((?:\tparameter[^\n]*\n)*)",
            text, re.M):
        if INNER.search(name):
            continue
        for one in re.findall(
                r"parameter(?: signed| \[[^\]]*\])? ([A-Za-z_][A-Za-z0-9_]*) = ",
                params):
            if SIZING.search(one):
                out.append((name, one))
                break
    return out


def needs(source, module):
    """A library module's own text and every module it stands on.

    Instantiating one is only readable if what it names can still be read and
    still be simulated, so what a lifted design uses is carried in it rather
    than left as a reference to a file somebody has to find.
    """
    text = open(source).read()
    bodies, want, out = {}, [module], []
    for got in re.finditer(r"^module ([A-Za-z_][A-Za-z0-9_]*) \(.*?^endmodule",
                           text, re.M | re.S):
        bodies[got.group(1)] = got.group(0)
    seen = set()
    while want:
        name = want.pop(0)
        if name in seen or name not in bodies:
            continue
        seen.add(name)
        out.append(bodies[name])
        for inner in re.findall(r"^\t([A-Za-z_][A-Za-z0-9_]*) (?:#\(|i_)",
                                bodies[name], re.M):
            if inner in bodies and inner not in seen:
                want.append(inner)
    return out


def elaborate(source, module, param, value, libdir, workdir):
    """One library module built at one parameter value, kept once it is built.

    Building is under half a second and the answer never changes, so what has
    been asked for once is read off disk from then on, across regions and
    across designs.
    """
    name = "%s_%s%d" % (module, param, value)
    path = "%s/%s.json" % (libdir, name)
    if os.path.exists(path):
        return name, path
    # A module that will not build at a width will not build at it next time
    # either, and there are enough of them that retrying would cost more than
    # the whole search.
    if os.path.exists(path + ".no"):
        return None, None
    script = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "cc_lib.ys")).read()
    for was, now in (("IN_V", source),
                     ("TOPSPEC", "-top %s -chparam %s %d" % (module, param, value)),
                     ("LOG", "/dev/null"), ("OUT_JSON", path),
                     ("OUT_V", "%s/%s.v" % (workdir, name))):
        script = script.replace(was, now)
    code, log = yosys(script.strip().split("\n"), "%s/%s.ys" % (workdir, name))
    if code or not os.path.exists(path):
        open(path + ".no", "w").write("")
        return None, None
    return name, path


def fitted(source, module, param, width, libdir, workdir):
    """The parameter at which a library module puts out as wide a word.

    Width is the thing that has to agree: a design's counter is as wide as it
    is, and a reference narrower than it cannot hold what it holds however
    many registers it has. Register counts are not the key, because they need
    not agree at all — a library counter keeps an overflow bit that a design
    which never needed one does not.

    A module's width grows with its parameter but not in step with it, and
    writing that relation down for each would be one more thing to keep true
    as the library grows, so it is searched for by halving. Seven builds settle
    any width the corpus has, and each is paid once for the whole of it. The
    search runs at least as far as the region is wide, or a region wider than
    the ceiling is reported as having nothing worth trying rather than as
    having been failed to reach.
    """
    low, high = 1, max(WIDEST, width)
    while low <= high:
        mid = (low + high) // 2
        name, path = elaborate(source, module, param, mid, libdir, workdir)
        if path is None:
            return None
        got = signature(path)
        if widest(got) == width:
            return name, path, got, mid
        if widest(got) < width:
            low = mid + 1
        else:
            high = mid - 1
    return None


def library_for(source, width, flops, libdir, workdir):
    """Every library module built to one region's size, worth proving against.

    The width wanted is the one the region's own roles imply and not anything
    read off its ports: a region comes out of extraction with its bus split
    into a port per bit, so its widest port is one however wide the word is.
    """
    out = []
    for module, param in library(source):
        got = fitted(source, module, param, width, libdir, workdir)
        if got and got[2]["flops"] >= flops:
            out.append(got + (param,))
    return out


def main(netlist, regions_path, libdir, outdir):
    workdir = os.path.join(outdir, "tmp")
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(libdir, exist_ok=True)
    regions = json.load(open(regions_path))
    source = "%s/common_cells.v" % workdir
    if not os.path.exists(source):
        print("no library to match against, run make cc first")
        return 1

    print("regions")
    found, chains = {}, []
    for index, region in enumerate(regions):
        kind = region["kind"]
        if kind not in ("chain", "state"):
            continue
        got = extract(netlist, region, index, workdir, expose=(kind == "state"))
        if not got:
            continue
        path, name = got
        sig = signature(path, name)
        ren = (state_roles if kind == "state" else roles)(
            path, name, region["registers"])
        wiring = region_wiring(ren)
        if wiring is None:
            print("  region %d (%s)  no roles of its own" % (index, kind))
            continue
        conn, width, dwidth, cwidth = wiring
        if kind == "chain":
            chains.append((index, path, name, ren))
        # A group that feeds itself is wrapped by its control and its whole
        # register; a chain by its serial input and its taps.
        build = None
        if kind == "state":
            build = lambda n, i, c, w, d, at=cwidth: state_wrapper(n, i, c, w, at)
        if not canonicalise(path, name, conn, width, "gold", workdir,
                            dwidth, build):
            print("  region %d (%s)  could not be wrapped" % (index, kind))
            continue
        tried = library_for(source, width, sig["flops"], libdir, workdir)
        print("  region %d (%s)  %d wide, %d registers, %d to try"
              % (index, kind, width, sig["flops"], len(tried)))
        for refname, refpath, refsig, value, param in tried:
            wired = (ref_state_wiring(refpath, refsig["module"], width, cwidth)
                     if kind == "state"
                     else ref_wiring(refpath, refsig["module"], width))
            if wired is None:
                continue
            if not canonicalise(refpath, refsig["module"], wired, width,
                                "gate", workdir, dwidth, build):
                continue
            verdict = prove("%s/gold.json" % workdir, "%s/gate.json" % workdir,
                            workdir, "prove_%d_%s" % (index, refname))
            print("      vs %-26s %s" % (refname, verdict))
            if verdict == "PROVEN EQUIVALENT":
                found[str(index)] = {"module": refsig["module"],
                                     "built": refname, "width": width,
                                     "kind": kind, "param": param,
                                     "value": value, "wired": wired}
                break

    design = os.path.basename(netlist)
    for tail in ("_generic.json", "_faithful.json", ".json"):
        if design.endswith(tail):
            design = design[:-len(tail)]
            break
    out = "%s/%s_matches.json" % (outdir, design)
    json.dump(found, open(out, "w"), indent=1, sort_keys=True)
    print("  %d of %d regions are a library module -> %s"
          % (len(found), len(regions), out))

    chains = [(i, p, n, region_wiring(r)) for i, p, n, r in chains]
    chains = [(i, p, n, w) for i, p, n, w in chains if w]
    for a in range(len(chains) - 1):
        ia, pa, ma, wa = chains[a]
        ib, pb, mb, wb = chains[a + 1]
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
