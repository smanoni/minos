# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Rebuilds the hierarchy synthesis flattened, from proven equivalent regions"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match


def submod_all(netlist, chains, workdir):
    """Cuts every region out of the parent at once, keeping the parent"""
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    top = list(design["modules"])[0]
    for index, region in chains:
        for cell in region["cells"]:
            module["cells"][cell].setdefault("attributes", {})[
                "minos_region%d" % index] = "y"
    tagged = "%s/emit_tagged.json" % workdir
    json.dump(design, open(tagged, "w"))

    lines = ["read_json %s" % tagged, "hierarchy -top %s" % top]
    lines += ["submod -name minos_region_%d a:minos_region%d" % (i, i)
              for i, _ in chains]
    lines += ["write_json %s/emit_parent.json" % workdir]
    code, log = match.yosys(lines, "%s/emit_submod.ys" % workdir)
    if code:
        print(log.strip().split("\n")[-1])
        return None, None
    return "%s/emit_parent.json" % workdir, top


def share(parent, chains, roles, shared, out):
    """Points every region instance at one module, wired by role"""
    design = json.load(open(parent))
    top = list(design["modules"])[0]
    first = "minos_region_%d" % chains[0][0]

    module = design["modules"][first]
    order = {}
    for old, role in roles[chains[0][0]].items():
        order[old] = role
    module["ports"] = {order.get(n, n): p for n, p in module["ports"].items()}
    design["modules"][shared] = module
    del design["modules"][first]

    for index, _ in chains:
        name = "minos_region_%d" % index
        design["modules"].pop(name, None)
        for cell in design["modules"][top]["cells"].values():
            if cell["type"] != name:
                continue
            cell["type"] = shared
            cell["connections"] = {roles[index].get(p, p): b
                                   for p, b in cell["connections"].items()}
            cell["port_directions"] = {roles[index].get(p, p): d
                                       for p, d in cell["port_directions"].items()}
    json.dump(design, open(out, "w"))
    return out


def main(netlist, regions_path, outdir, out, shared="minos_shift_register"):
    workdir = os.path.join(outdir, "tmp")
    os.makedirs(workdir, exist_ok=True)
    regions = json.load(open(regions_path))
    chains = [(i, r) for i, r in enumerate(regions) if r["kind"] == "chain"]
    if not chains:
        print("no regions to instantiate")
        return 1

    roles, keep = {}, []
    for index, region in chains:
        got = match.extract(netlist, region, index, workdir)
        if not got:
            continue
        path, name = got
        wiring = match.region_wiring(match.roles(path, name,
                                                 region["registers"]))
        if wiring is None:
            continue
        roles[index] = wiring
        keep.append((index, region))
    if not keep:
        print("no regions to instantiate")
        return 1

    proven = [keep[0]]
    base, bpath = keep[0][0], "%s/minos_region_%d.json" % (workdir, keep[0][0])
    bc, bw, bd, _ = roles[base]
    match.canonicalise(bpath, "minos_region_%d" % base, bc, bw, "gold",
                       workdir, bd)
    for index, region in keep[1:]:
        path = "%s/minos_region_%d.json" % (workdir, index)
        conn, width, dwidth, _ = roles[index]
        if width != bw:
            continue
        if not match.canonicalise(path, "minos_region_%d" % index, conn, width,
                                  "gate", workdir, dwidth):
            continue
        verdict = match.prove("%s/gold.json" % workdir, "%s/gate.json" % workdir,
                              workdir, "emit_prove_%d" % index)
        print("  region %d vs region %d: %s" % (base, index, verdict))
        if verdict == "PROVEN EQUIVALENT":
            proven.append((index, region))

    if len(proven) < 2:
        print("  no shared module found")
        return 1

    parent, top = submod_all(netlist, proven, workdir)
    if not parent:
        return 1
    merged = share(parent, proven, roles, shared, "%s/emit_shared.json" % workdir)

    code, log = match.yosys(["read_json %s" % merged,
                             "hierarchy -top %s" % top,
                             "write_verilog -noattr -sv %s" % out],
                            "%s/emit_write.ys" % workdir)
    if code:
        print(log.strip().split("\n")[-1])
        return 1
    print("  %d instances of %s -> %s" % (len(proven), shared, out))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
