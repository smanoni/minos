# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Rewrites the connectivity of a DEF as a gate-level Verilog netlist"""

import sys
import re
import collections

PHYSICAL = ("__tapvpwrvgnd", "__decap", "__fill", "__tap_", "__diode")


def parse_def(path):
    """Reads COMPONENTS, PINS and NETS, ignoring routing, rows and tracks"""
    text = open(path).read()

    def section(name):
        m = re.search(r"^%s\s+\d+\s*;(.*?)^END %s" % (name, name), text, re.S | re.M)
        return m.group(1) if m else ""

    comps = dict(re.findall(r"-\s+(\S+)\s+(\S+)\s+\+", section("COMPONENTS")))
    pins = {}
    for entry in re.split(r"\n\s+-\s+", section("PINS"))[1:]:
        m = re.match(r"(\S+).*?DIRECTION\s+(\w+)", entry, re.S)
        if m:
            pins[m.group(1)] = m.group(2)
    nets = {}
    for entry in re.split(r"\n\s+-\s+", section("NETS"))[1:]:
        head = entry.split("+")[0]
        name = head.split()[0]
        nets[name] = [(i, p) for i, p in
                      re.findall(r"\(\s*(\S+)\s+(\S+)\s*\)", head) if i != "PIN"]
    design = re.search(r"^DESIGN\s+(\S+)\s*;", text, re.M).group(1)
    return design, comps, pins, nets


def buses(names):
    """Groups indexed pins back into a single vector declaration"""
    scalar, wide = [], collections.defaultdict(list)
    for n in names:
        m = re.match(r"(\w+)\[(\d+)\]$", n)
        wide[m.group(1)].append(int(m.group(2))) if m else scalar.append(n)
    decls = {n: "" for n in scalar}
    for base, idx in wide.items():
        decls[base] = "[%d:%d] " % (max(idx), min(idx))
    return decls


def ident(name):
    """Turns a DEF net name into a legal Verilog identifier"""
    if re.match(r"^\w+(\[\d+\])?$", name):
        return name
    return "n" + re.sub(r"\W", "", name)


def natural(s):
    """Sort key ordering embedded numbers numerically"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def write_v(path, design, comps, pins, nets):
    """Emits the structural netlist"""
    net_of = {}
    for n, conn in nets.items():
        for c in conn:
            net_of[c] = ident(n)

    decls = buses(pins)
    ports = sorted(decls)
    w = ["module %s (%s);" % (design, ", ".join(ports))]
    for p in ports:
        direction = pins.get(p) or pins.get(p + "[0]") or "input"
        w.append("  %s %s%s;" % (direction.lower(), decls[p], p))
    w.append("")
    wires = sorted({ident(n) for n in nets if n not in pins}, key=natural)
    w += ["  wire %s;" % n for n in wires] + [""]

    for inst in sorted(comps, key=natural):
        cell = comps[inst]
        conn = sorted((p, net_of[(inst, p)]) for (i, p) in net_of if i == inst)
        if any(k in cell for k in PHYSICAL) and not conn:
            w.append("  %s %s ();" % (cell, inst))
        else:
            w.append("  %s %s (%s);"
                     % (cell, inst, ", ".join(".%s(%s)" % c for c in conn)))
    w += ["", "endmodule", ""]
    open(path, "w").write("\n".join(w))
    return len(wires)


def read_v(path):
    """Reads cell type and connections per instance from a structural netlist"""
    text = re.sub(r"//.*", "", open(path).read())
    out = {}
    for m in re.finditer(r"(sky130_\w+)\s+(\\?\S+?)\s*\(([^;]*)\);", text, re.S):
        conn = re.findall(r"\.(\w+)\s*\(\s*([^)]*?)\s*\)", m.group(3))
        out[m.group(2)] = (m.group(1), [(p, n) for p, n in conn if n])
    return out


def check(ours, ref):
    """Compares cell histogram and net structure up to renaming"""
    a, b = read_v(ours), read_v(ref)
    ca = collections.Counter(c for c, _ in a.values())
    cb = collections.Counter(c for c, _ in b.values())
    ok = ca == cb
    print("  cells       %5d vs %5d   %s" % (sum(ca.values()), sum(cb.values()),
                                             "MATCH" if ok else "DIFFER"))
    for k in sorted(set(ca) | set(cb)):
        if ca[k] != cb[k]:
            print("      %-40s ours %d  ref %d" % (k, ca[k], cb[k]))

    def sigs(d):
        nets = collections.defaultdict(list)
        for inst, (cell, conn) in d.items():
            for pin, net in conn:
                nets[net].append((cell, pin))
        return collections.Counter(tuple(sorted(v)) for v in nets.values())

    sa, sb = sigs(a), sigs(b)
    ok2 = sa == sb
    print("  nets        %5d vs %5d   %s" % (sum(sa.values()), sum(sb.values()),
                                             "MATCH" if ok2 else "DIFFER"))
    for k, v in list((sa - sb).items())[:5]:
        print("      only ours x%d: %s" % (v, k))
    for k, v in list((sb - sa).items())[:5]:
        print("      only ref  x%d: %s" % (v, k))
    return ok and ok2


def main(indef, out, ref=None):
    design, comps, pins, nets = parse_def(indef)
    n = write_v(out, design, comps, pins, nets)
    print("%s -> %s" % (indef, out))
    print("  module %s: %d instances, %d wires, %d ports"
          % (design, len(comps), n, len(buses(pins))))
    if ref:
        print("checking against %s" % ref)
        return 0 if check(out, ref) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
