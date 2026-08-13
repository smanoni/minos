# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Recovers placement and connectivity from a placed-and-routed GDS as DEF"""

import sys
import re
import glob
import collections

import klayout.db as db

COND = [("li1", 67, 20), ("met1", 68, 20), ("met2", 69, 20),
        ("met3", 70, 20), ("met4", 71, 20), ("met5", 72, 20)]
CUT = [("mcon", 67, 44), ("via", 68, 44), ("via2", 69, 44),
       ("via3", 70, 44), ("via4", 71, 44)]
LABEL = [(n, l, 5) for n, l, _ in COND]
SUPPLY = ("VPWR", "VGND", "VPB", "VNB")
BOUNDARY = (235, 4)

ORIENT = {(0, False): "N", (90, False): "W", (180, False): "S", (270, False): "E",
          (0, True): "FS", (90, True): "FE", (180, True): "FN", (270, True): "FW"}


def read_lef(pdk):
    """Maps each MACRO to its pin directions and cell extent"""
    cells = {}
    for path in glob.glob(pdk + "/libs.ref/*/lef/*.lef"):
        macro, pin = None, None
        for line in open(path, errors="ignore"):
            t = line.split()
            if not t:
                continue
            if t[0] == "MACRO":
                macro, cells[t[1]] = t[1], {"pins": {}, "size": None}
            elif t[0] == "SIZE" and macro:
                cells[macro]["size"] = (float(t[1]), float(t[3]))
            elif t[0] == "PIN" and macro:
                pin = t[1]
            elif t[0] == "DIRECTION" and macro and pin:
                cells[macro]["pins"][pin] = t[1]
            elif t[0] == "END" and len(t) > 1 and t[1] == macro:
                macro = None
    return cells


def extract(gds):
    """Turns geometric connectivity into a hierarchical netlist of subcircuits"""
    ly = db.Layout()
    ly.read(gds)
    top = ly.top_cell()
    l2n = db.LayoutToNetlist(db.RecursiveShapeIterator(ly, top, []))
    reg = {n: l2n.make_polygon_layer(ly.layer(l, d), n) for n, l, d in COND + CUT}
    txt = {n: l2n.make_text_layer(ly.layer(l, d), n + "_lbl") for n, l, d in LABEL}
    stack = [c[0] for pair in zip(COND, CUT + [None]) for c in pair if c]
    for a, b in zip(stack, stack[1:]):
        l2n.connect(reg[a])
        l2n.connect(reg[a], reg[b])
    l2n.connect(reg[stack[-1]])
    for n, _, _ in COND:
        l2n.connect(reg[n], txt[n])
    l2n.extract_netlist()
    return ly, top, l2n, l2n.netlist().circuit_by_name(top.name)


def components(circuit, ly, lef):
    """Placed instances, keyed on the oriented lower-left corner DEF expects"""
    dbu, out = ly.dbu, []
    for sc in circuit.each_subcircuit():
        cell = sc.circuit_ref().name
        if cell.startswith("VIA_"):
            continue
        size = lef.get(cell, {}).get("size")
        if not size:
            b = ly.cell(cell).dbbox()
            size = (b.width(), b.height())
        t = sc.trans
        box = t * db.DBox(0, 0, *size)
        out.append([cell, round(box.left / dbu), round(box.bottom / dbu),
                    ORIENT[(round(t.angle) % 360, t.is_mirror())], sc])
    out.sort(key=lambda c: (c[2], c[1], c[0]))
    for i, c in enumerate(out):
        c.append("U%d" % i)
    return out


def nets(circuit, comps, lef):
    """Net to instance pins, dropping connections the PDK declares no pin for"""
    inst = {c[4].expanded_name(): c[5] for c in comps}
    cell = {c[4].expanded_name(): c[0] for c in comps}
    out, orphans = {}, []
    for net in circuit.each_net():
        conn, dirs = [], set()
        for ref in net.each_subcircuit_pin():
            sc, pin = ref.subcircuit().expanded_name(), ref.pin().name()
            if sc not in inst or pin in SUPPLY:
                continue
            known = lef.get(cell[sc], {}).get("pins")
            if known and pin not in known:
                orphans.append((inst[sc], cell[sc], pin or "<unnamed>",
                                net.expanded_name()))
                continue
            conn.append((inst[sc], pin))
            dirs.add((known or {}).get(pin, "INPUT"))
        if conn:
            out[net.expanded_name()] = (sorted(conn), dirs)
    return out, orphans


def write_def(path, top, dbu, comps, nts, ports, die):
    """Emits the DEF"""
    w = ["VERSION 5.8 ;", 'DIVIDERCHAR "/" ;', 'BUSBITCHARS "[]" ;',
         "DESIGN %s ;" % top, "UNITS DISTANCE MICRONS %d ;" % round(1 / dbu),
         "DIEAREA ( %d %d ) ( %d %d ) ;" % die, "",
         "COMPONENTS %d ;" % len(comps)]
    w += ["    - %s %s + PLACED ( %d %d ) %s ;" % (c[5], c[0], c[1], c[2], c[3])
          for c in comps]
    w += ["END COMPONENTS", "", "PINS %d ;" % len(ports)]
    for name, (net, direction) in sorted(ports.items()):
        w += ["    - %s + NET %s + DIRECTION %s + USE SIGNAL" % (name, net, direction),
              "      + PORT + LAYER met3 ( -300 -300 ) ( 300 300 ) ;"]
    w += ["END PINS", ""]
    power = {n: v for n, v in nts.items() if n in SUPPLY}
    signal = {n: v for n, v in nts.items() if n not in SUPPLY}
    w += ["NETS %d ;" % len(signal)]
    for name, (conn, _) in sorted(signal.items()):
        pin = " ( PIN %s )" % name if name in ports else ""
        w.append("    - %s%s %s + USE SIGNAL ;"
                 % (name, pin, " ".join("( %s %s )" % c for c in conn)))
    w += ["END NETS", "", "SPECIALNETS %d ;" % len(power)]
    for name, _ in sorted(power.items()):
        w.append("    - %s + USE %s ;" % (name, "POWER" if name == "VPWR" else "GROUND"))
    w += ["END SPECIALNETS", "", "END DESIGN", ""]
    open(path, "w").write("\n".join(w))


def parse_def(path):
    """Reads back COMPONENTS and NETS from a DEF, ignoring routing"""
    text = open(path).read()

    def section(name):
        m = re.search(r"^%s\s+\d+\s*;(.*?)^END %s" % (name, name), text,
                      re.S | re.M)
        return m.group(1) if m else ""

    comps, cells = [], {}
    for m in re.finditer(r"-\s+(\S+)\s+(\S+).*?\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\w+)\s*;",
                         section("COMPONENTS"), re.S):
        comps.append((m.group(2), int(m.group(3)), int(m.group(4)), m.group(5)))
        cells[m.group(1)] = m.group(2)
    nts = []
    for entry in re.split(r"\n\s+-\s+", section("NETS"))[1:]:
        head = entry.split("+")[0]
        conn = [(a, b) for a, b in re.findall(r"\(\s*(\S+)\s+(\S+)\s*\)", head)]
        nts.append(sorted((cells.get(i, i), p) for i, p in conn))
    return comps, nts


def check(comps, nts, ports, ref):
    """Compares placement exactly and connectivity up to renaming"""
    rc, rn = parse_def(ref)
    ours = collections.Counter((c[0], c[1], c[2], c[3]) for c in comps)
    ok = ours == collections.Counter(rc)
    print("  components  %5d vs %5d   %s" % (sum(ours.values()), len(rc),
                                             "MATCH" if ok else "DIFFER"))
    if not ok:
        for k, v in (ours - collections.Counter(rc)).items():
            print("      only ours x%d: %s" % (v, k))
        for k, v in (collections.Counter(rc) - ours).items():
            print("      only ref  x%d: %s" % (v, k))
    cell = {c[5]: c[0] for c in comps}
    sig = collections.Counter(
        tuple(sorted([(cell[i], p) for i, p in conn]
                     + ([("PIN", name)] if name in ports else [])))
        for name, (conn, _) in nts.items())
    rsig = collections.Counter(tuple(n) for n in rn)
    ok2 = sig == rsig
    print("  nets        %5d vs %5d   %s" % (sum(sig.values()), len(rn),
                                             "MATCH" if ok2 else "DIFFER"))
    if not ok2:
        for k, v in list((sig - rsig).items())[:5]:
            print("      only ours x%d: %s" % (v, k))
        for k, v in list((rsig - sig).items())[:5]:
            print("      only ref  x%d: %s" % (v, k))
    return ok and ok2


def main(gds, pdk, out, ref=None):
    lef = read_lef(pdk)
    ly, top, l2n, circuit = extract(gds)
    dbu = ly.dbu
    comps = components(circuit, ly, lef)
    nts, orphans = nets(circuit, comps, lef)

    labels = {s.text.string for _, l, d in LABEL
              for s in ly.top_cell().shapes(ly.layer(l, d)).each(db.Shapes.STexts)}
    ports = {n: (n, "OUTPUT" if "OUTPUT" in nts[n][1] else "INPUT")
             for n in labels if n in nts and n not in SUPPLY}

    box = ly.top_cell().dbbox(ly.layer(*BOUNDARY))
    if box.empty():
        box = ly.top_cell().dbbox()
    die = tuple(round(v / dbu) for v in (box.left, box.bottom, box.right, box.top))

    write_def(out, top.name, dbu, comps, nts, ports, die)
    print("%s -> %s" % (gds, out))
    print("  %d components, %d nets, %d ports" % (len(comps), len(nts), len(ports)))
    for inst, cell, pin, net in orphans:
        print("  WARNING: %s (%s) touches net %s at %s, which the PDK does not "
              "declare as a pin -- connection dropped" % (inst, cell, net, pin))
    if ref:
        print("checking against %s" % ref)
        return 0 if check(comps, nts, ports, ref) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
