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
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match
import expr
import structure


CANONICAL = {"clk": "clk", "rst": "rst", "sr": "sr", "en": "en",
             "d": "d"}

RISING = ("posedge", "negedge", "0")


def top_module(netlist):
    """The one module a recovered netlist holds"""
    return list(json.load(open(netlist))["modules"].values())[0]


def flop_form(module, flops):
    """The clock edge and reset a region's registers agree on.

    A template speaks for every register at once, so it can only be written
    when they clock on the same edge; nothing is returned when they do not.
    A reset arm needs them to agree on a polarity and a held value as well,
    and is offered only when they do. Reading all three off the generic flop
    names is what keeps the recovered RTL simulating the way its netlist
    does: a proof cannot see a clock edge, so assuming a rising one would
    pass and still be wrong.
    """
    kinds = [expr.flop_kind(module["cells"][f]["type"]) for f in flops]
    if not kinds or len({k[0] for k in kinds}) != 1:
        return None
    if len(set(kinds)) != 1:
        return (kinds[0][0], None, None)
    return kinds[0]


def clears(form, sync):
    """The reset arms a region could have, tried most specific first.

    A register with a reset pin resets to the value its own name carries. A
    region that one of its ports holds at a constant resets to that constant
    instead, whatever the constant is, which is what lets a reset synthesised
    into the data path be written as a reset rather than left as the gates
    that encode it. Having neither is always worth trying last.
    """
    out = []
    if form[1] is not None:
        out.append(("rst", form[1] == "negedge", None, True))
    if sync is not None:
        out.append(("sr", sync[0] == 0, sync[1], False))
    return out + [None]


def sensitivity(role, clear, form):
    """The event list the region's own registers call for"""
    out = "%s %s" % (form[0], role["clk"])
    if clear and clear[3]:
        out += " or %s %s" % (form[1], role[clear[0]])
    return out


def held(role, clear, form, reg, width):
    """The value a region takes while its reset is asserted"""
    word = ("{%d{1'b%s}}" % (width, form[2]) if clear[2] is None else
            "%d'h%x" % (width, sum(b << i for i, b in enumerate(clear[2]))))
    return "    if (%s%s) %s <= %s;" % ("!" if clear[1] else "",
                                        role[clear[0]], reg, word)


def guarded(width, clear, reg, role, form, guard, move):
    """A template's always block, with the reset arm only where there is one"""
    lines = ["  always @(%s)" % sensitivity(role, clear, form)]
    if clear:
        return lines + [held(role, clear, form, reg, width),
                        "    else %s%s" % (guard, move)]
    return lines + ["    %s%s" % (guard, move)]


def shift_body(width, enable, clear, reg="q", role=None, form=RISING):
    """A register whose data comes from the stage before it, in given names"""
    role = role or CANONICAL
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role["en"] if enable else "",
                   "%s <= {%s[%d:0], %s};" % (reg, reg, width - 2, role["d"]))


def shift_register(width, enable, clear, form=RISING):
    """The same register as a standalone module with canonical port names"""
    body = ["module cand(clk, rst, sr, en, d, q);",
            "  input clk, rst, sr, en, d;",
            "  output reg [%d:0] q;" % (width - 1)]
    body += shift_body(width, enable, clear, form=form)
    return "\n".join(body + ["endmodule", ""])


def named_clear(clear):
    """How a reset arm reads in a template's name"""
    return "" if clear is None else ", reset" if clear[3] else ", sync reset"


def chain_templates(form, sync):
    """Every shifting form worth trying, named the way it will read"""
    for clear in clears(form, sync):
        for enable in (True, False):
            yield ("shift register%s%s"
                   % (", enable" if enable else "", named_clear(clear)),
                   enable, clear)


def value_of(width, role):
    """The word a loadable shift register takes, per stage.

    The first stage shifts whether the register loads or not, so it keeps the
    serial input where the others take a value of their own.
    """
    bits = [role["v%d" % i] for i in reversed(range(1, width))] + [role["d"]]
    return "{%s}" % ", ".join(bits)


def load_shift_body(width, shift_on, reg="q", role=None, form=RISING):
    """A shift register that takes a whole word when its select says so"""
    role = role or dict(CANONICAL, ld="ld",
                        **{"v%d" % i: "v[%d]" % i for i in range(1, width)})
    return ["  always @(%s %s)" % (form[0], role["clk"]),
            "    if (%s%s) %s <= %s;"
            % ("!" if shift_on else "", role["ld"], reg,
               value_of(width, role)),
            "    else %s <= {%s[%d:0], %s};"
            % (reg, reg, width - 2, role["d"])]


def load_shift(width, shift_on, form=RISING):
    """The same register as a standalone module with canonical port names"""
    body = ["module cand(clk, rst, sr, ld, v, d, q);",
            "  input clk, rst, sr, ld, d;",
            "  input [%d:0] v;" % (width - 1),
            "  output reg [%d:0] q;" % (width - 1)]
    return "\n".join(body + load_shift_body(width, shift_on, form=form)
                     + ["endmodule", ""])


def tap_mask(path, module_name, chain):
    """Which stages the feedback exclusive-ors, read off the first stage.

    An LFSR shifts like any other chain; what tells it apart is that its
    serial input is a function of its own stages, and which stages those are
    is exactly the registers behind the first one's data pin.
    """
    module = json.load(open(path))["modules"][module_name]
    cells = module["cells"]
    order = list(reversed(chain))
    place = {flop: i for i, flop in enumerate(order)}
    driver = {}
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in bits:
                    driver[bit] = name
    first = cells[order[0]]
    queue = [b for p, bits in first["connections"].items() for b in bits
             if first["port_directions"].get(p) == "input" and p not in ("C", "R")]
    seen, mask = set(), 0
    while queue:
        bit = queue.pop()
        if bit in seen:
            continue
        seen.add(bit)
        src = driver.get(bit)
        if src is None:
            continue
        if match.FLOP in cells[src]["type"]:
            if src in place:
                mask |= 1 << place[src]
            continue
        queue += [b for p, bits in cells[src]["connections"].items() for b in bits
                  if cells[src]["port_directions"].get(p) == "input"]
    return mask


def lfsr_body(width, mask, enable, clear, reg="q", role=None, form=RISING):
    """A chain whose serial input is the parity of some of its own stages"""
    role = role or CANONICAL
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role["en"] if enable else "",
                   "%s <= {%s[%d:0], ^(%s & %d'd%d)};"
                   % (reg, reg, width - 2, reg, width, mask))


def lfsr(width, mask, enable, clear, form=RISING):
    """The same shift register as a module with canonical port names"""
    body = ["module cand(clk, rst, sr, en, d, q);",
            "  input clk, rst, sr, en, d;",
            "  output reg [%d:0] q;" % (width - 1)]
    body += lfsr_body(width, mask, enable, clear, form=form)
    return "\n".join(body + ["endmodule", ""])


def load_body(width, enable, clear, reg="q", role=None, form=RISING):
    """A register loaded from somewhere that is not another register"""
    role = role or CANONICAL
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role["en"] if enable else "",
                   "%s <= %s;" % (reg, role["d"]))


def register(width, enable, clear, form=RISING):
    """The same register as a standalone module with canonical port names"""
    body = ["module cand(clk, rst, sr, en, d, q);",
            "  input clk, rst, sr, en;",
            "  input [%d:0] d;" % (width - 1),
            "  output reg [%d:0] q;" % (width - 1)]
    body += load_body(width, enable, clear, form=form)
    return "\n".join(body + ["endmodule", ""])


def bank_templates(form, sync):
    """Every loading form worth trying, named the way it will read"""
    for clear in clears(form, sync):
        for enable in (True, False):
            yield ("register%s%s" % (", enable" if enable else "",
                                     named_clear(clear)), enable, clear)


def bank_attempt(netlist, region, index, workdir, inside, extra, form):
    """One reading of a bank: these cells, their roles, and a form that proves"""
    got = match.extract(netlist, inside, index, workdir)
    if not got:
        return None
    path, name = got
    got = match.bank_roles(path, name, region["registers"])
    if got is None:
        return None
    ren, flops = got
    got = match.region_wiring(ren)
    if got is None:
        return None
    conn, width, dwidth, _ = got
    if width != dwidth or not match.canonicalise(path, name, conn, width,
                                                 "gold", workdir, dwidth):
        return None
    sync = match.sync_held(path, name, flops)
    for label, enable, clear in bank_templates(form, sync):
        verdict = prove_candidate(register(width, enable, clear, form),
                                  "%s/gold.json" % workdir,
                                  workdir, "bank_%d" % index)
        print("  region %d  %-28s %s" % (index, label, verdict))
        if verdict == "PROVEN EQUIVALENT":
            return {"label": label, "width": width, "edges": form,
                    "enable": enable, "clear": clear, "flops": flops,
                    "inside": extra,
                    "roles": {v: k for k, v in ren.items()}}
    return None


def lift_banks(netlist, regions, workdir, taken=()):
    """Behavioural RTL for every register bank that proves equivalent.

    A register that computes its own next value is offered twice, once as a
    bank and once as the state group it also is, and where the state lift has
    already proved it a counter the bank reading is the same registers said
    less well. Skipping those is not only work saved: counted, they were eight
    of the corpus's seventeen banks and every one of them a refusal.
    """
    top, found, skipped = top_module(netlist), {}, 0
    for index, region in enumerate(regions):
        if region["kind"] != "bank":
            continue
        if set(region["registers"]) <= set(taken):
            skipped += 1
            continue
        form = flop_form(top, region["registers"])
        if form is None:
            print("  region %d  registers do not share a clock edge" % index)
            continue
        for inside, extra in cuts(top, region, region["registers"]):
            got = bank_attempt(netlist, region, index, workdir, inside, extra,
                               form)
            if got:
                found[index] = got
                break
        if index not in found:
            print("  region %d  not a plain bank" % index)
    if skipped:
        print("  %d already lifted as a counter or a chain" % skipped)
    return found, skipped


def count_body(width, clear, en, updown, step, reg="q", role=None, form=RISING):
    """A register that walks its own value on, the shape of every counter"""
    role = dict(CANONICAL, **(role or {}))
    if updown is None:
        move = "%s <= %s %s %d'd1;" % (reg, reg, step, width)
    else:
        move = ("%s <= %s ? %s + %d'd1 : %s - %d'd1;"
                % (reg, role.get(updown, updown), reg, width, reg, width))
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role.get(en, en) if en else "", move)


def counter(width, cwidth, clear, en, updown, step, form=RISING):
    """The same counter as a module, controls kept whether used or not"""
    role = {"c%d" % i: "c[%d]" % i for i in range(cwidth)}
    body = ["module cand(clk, rst, sr, c, q);",
            "  input clk, rst, sr;",
            "  input [%d:0] c;" % (max(cwidth, 1) - 1),
            "  output reg [%d:0] q;" % (width - 1)]
    body += count_body(width, clear, en, updown, step, "q", role, form)
    return "\n".join(body + ["endmodule", ""])


def state_templates(cwidth, form, sync):
    """Counting forms, and which control could be the enable or direction"""
    controls = [None] + ["c%d" % i for i in range(cwidth)]
    for clear in clears(form, sync):
        for en in controls:
            for step in ("+", "-"):
                yield ("counter %s%s%s" % ("up" if step == "+" else "down",
                                           ", enable" if en else "",
                                           named_clear(clear)),
                       clear, en, None, step)
            for updown in controls[1:]:
                if updown != en:
                    yield ("counter up/down%s%s"
                           % (", enable" if en else "", named_clear(clear)),
                           clear, en, updown, "+")


def lift_states(netlist, regions, workdir, taken):
    """A counting form for every self-contained state group that proves"""
    top, found = top_module(netlist), {}
    for index, region in enumerate(regions):
        if region["kind"] != "state" or set(region["registers"]) & taken:
            continue
        form = flop_form(top, region["registers"])
        if form is None:
            print("  region %d  registers do not share a clock edge" % index)
            continue
        got = match.extract(netlist, region, index, workdir, expose=True)
        if not got:
            continue
        path, name = got
        ren = match.state_roles(path, name, region["registers"])
        if ren is None:
            continue
        got = match.region_wiring(ren)
        if got is None:
            continue
        conn, width, _, cwidth = got
        if width != region["width"] or not match.canonicalise(
                path, name, conn, width, "gold", workdir, cwidth,
                match.state_wrapper):
            continue
        sync = match.sync_held(path, name, region["registers"])
        for label, clear, en, updown, step in state_templates(cwidth, form,
                                                              sync):
            verdict = prove_candidate(
                counter(width, cwidth, clear, en, updown, step, form),
                "%s/gold.json" % workdir, workdir, "state_%d" % index)
            if verdict == "PROVEN EQUIVALENT":
                print("  region %d  %-28s %s" % (index, label, verdict))
                found[index] = {"label": label, "width": width, "step": step,
                                "en": en, "updown": updown, "clear": clear,
                                "enable": bool(en), "edges": form,
                                "flops": list(region["registers"]),
                                "roles": {v: k for k, v in ren.items()}}
                break
        if index not in found:
            print("  region %d  no counting form" % index)
    return found


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


def sync_net(module, flops):
    """The net of the top module that holds a region's registers at a constant.

    Looked for here rather than in the extracted region: a reset folded into
    the data path is gates like any other, and a region drawn round the
    registers alone leaves them outside, so it has to be found before the
    region is cut in order to widen it.
    """
    drivers = driver_map(module)
    for name, spec in sorted(module["ports"].items()):
        if spec["direction"] != "input" or len(spec["bits"]) != 1:
            continue
        got = structure.held_value(module["cells"], drivers, flops,
                                   spec["bits"][0])
        if got:
            return spec["bits"][0], got
    return None, None


def reset_cells(module, flops, bit, level):
    """The gates a held net settles, back from the registers it holds.

    A gate whose output goes constant while the net is held is part of what
    does the holding; one that carries on computing is the logic being held
    back and belongs outside. Taking only the settled gates keeps the region
    to the reset itself rather than to everything the reset happens to reach,
    which for a reset threaded through a design is most of it.
    """
    cells, drivers = module["cells"], driver_map(module)
    inside, seen, memo = set(), set(), {}
    queue = [cells[f]["connections"]["D"][0] for f in flops
             if "D" in cells[f]["connections"]]
    while queue:
        b = queue.pop()
        if b in seen:
            continue
        seen.add(b)
        src = drivers.get(b)
        if src is None or structure.is_flop(cells, src):
            continue
        if structure.evaluate(cells, drivers, b, {bit: level}, memo) is None:
            continue
        inside.add(src)
        queue += [x for p, bits in cells[src]["connections"].items()
                  for x in bits
                  if cells[src]["port_directions"].get(p) == "input"]
    return sorted(inside)


def link_cells(module, flops):
    """Cells carrying one stage of a chain to the next, and nothing before it.

    A region cut round everything feeding a chain takes in whatever computes
    its serial input, and that arrives as ports of its own, so the region has
    several inputs where a shift register has one and cannot be named. What
    lies between the stages is the register; what feeds the first one belongs
    to whoever computes it and is better left outside.
    """
    cells, drivers = module["cells"], driver_map(module)
    readers = collections.defaultdict(set)
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "input":
                for b in bits:
                    readers[b].add(name)
    ahead, seen = set(), set()
    queue = [cells[f]["connections"]["Q"][0] for f in flops
             if "Q" in cells[f]["connections"]]
    while queue:
        b = queue.pop()
        if b in seen:
            continue
        seen.add(b)
        for name in readers[b]:
            if structure.is_flop(cells, name) or name in ahead:
                continue
            ahead.add(name)
            queue += [x for p, bits in cells[name]["connections"].items()
                      for x in bits
                      if cells[name]["port_directions"].get(p) == "output"]
    behind, seen = set(), set()
    queue = [cells[f]["connections"]["D"][0] for f in flops
             if "D" in cells[f]["connections"]]
    while queue:
        b = queue.pop()
        if b in seen:
            continue
        seen.add(b)
        src = drivers.get(b)
        if src is None or structure.is_flop(cells, src):
            continue
        behind.add(src)
        queue += [x for p, bits in cells[src]["connections"].items()
                  for x in bits
                  if cells[src]["port_directions"].get(p) == "input"]
    return ahead & behind


def cuts(module, region, flops, links=False):
    """The ways this region could be drawn, the one that says most first.

    Each cut is an attempt, not a commitment. The gates of a folded-in reset
    come in so a template can speak for it, but they bring whatever they
    share with the data path along; the logic feeding a chain's first stage
    can be dropped so the serial input becomes a port. A region that will not
    take roles one way is better described another way than not at all.
    """
    bit, sync = sync_net(module, flops)
    whole = set(region.get("cells", ()))
    extra = [] if bit is None else reset_cells(module, flops, bit, sync[0])
    bases = [(whole, [])]
    if links:
        inner = link_cells(module, flops) | set(flops)
        if inner < whole:
            bases.append((inner, []))
    out = []
    for base, _ in bases:
        if extra:
            out.append(({"cells": sorted(base | set(extra))}, list(extra)))
        out.append(({"cells": sorted(base)}, []))
    return out


def load_links(module, flops):
    """Each stage's mux, when the whole chain loads on one select.

    Read off the top module, so the select and the load values come back as
    nets that can be named where the RTL is written, not as ports of a copy.
    """
    cells, drivers = module["cells"], driver_map(module)
    links = []
    for i, flop in enumerate(flops[1:], 1):
        link = structure.mux_link(cells, drivers, flop)
        if link is None or link["prev"] != flops[i - 1]:
            return None
        links.append(link)
    if not links or len({l["sel"] for l in links}) != 1 or \
            len({l["shift_on"] for l in links}) != 1:
        return None
    return links


def lift_loads(netlist, region, flops, index, workdir, form):
    """A loadable shift register, extracted with its muxes and nothing else.

    Everything driving the serial input stays outside: it belongs to whatever
    computes it, and leaving it in would hide the shifted-in bit behind logic
    the region cannot give a name to.
    """
    module = top_module(netlist)
    links = load_links(module, flops)
    if links is None:
        return None
    inside = {"cells": list(flops) + [l["mux"] for l in links]}
    got = match.extract(netlist, inside, index, workdir)
    if not got:
        return None
    path, name = got
    ren = match.load_roles(path, name, flops)
    got = match.region_wiring(ren)
    if got is None:
        return None
    conn, width, _, vwidth = got
    if width != len(flops) or not match.canonicalise(
            path, name, conn, width, "gold", workdir, vwidth,
            match.load_wrapper):
        return None
    shift_on = links[0]["shift_on"]
    verdict = prove_candidate(load_shift(width, shift_on, form),
                              "%s/gold.json" % workdir, workdir,
                              "load_%d" % index)
    label = "loadable shift register%s" % ("" if shift_on else ", load high")
    if verdict != "PROVEN EQUIVALENT":
        return None
    print("  region %d  %-28s %s" % (index, label, verdict))
    return {"label": label, "width": width, "form": "load", "mask": 0,
            "enable": False, "clear": None, "flops": list(flops),
            "edges": form,
            "shift_on": shift_on, "sel": links[0]["sel"],
            "loads": [l["load"] for l in links],
            "muxes": [l["mux"] for l in links],
            "roles": {v: k for k, v in ren.items()}}


def chain_attempt(netlist, region, index, workdir, inside, extra, form):
    """One reading of a chain: these cells, their roles, and a form that proves.

    The stages are exposed because a chain only shows its last one: every
    other is read by its neighbour alone and would leave the region with
    fewer outputs than it has registers.
    """
    got = match.extract(netlist, inside, index, workdir, expose=True)
    if not got:
        return None
    path, name = got
    order = list(reversed(region["registers"]))
    ren = match.roles(path, name, region["registers"])
    got = match.region_wiring(ren)
    if got is None:
        return None
    conn, width, dwidth, _ = got
    if not match.canonicalise(path, name, conn, width, "gold", workdir, dwidth):
        return None
    sync = match.sync_held(path, name, order)
    for label, enable, clear in chain_templates(form, sync):
        verdict = prove_candidate(shift_register(width, enable, clear, form),
                                  "%s/gold.json" % workdir,
                                  workdir, "cand_%d" % index)
        if verdict == "PROVEN EQUIVALENT":
            print("  region %d  %-28s %s" % (index, label, verdict))
            return {"label": label, "width": width, "form": "shift",
                    "mask": 0, "enable": enable, "clear": clear,
                    "edges": form, "inside": extra, "flops": order,
                    "roles": {v: k for k, v in ren.items()}}
    mask = tap_mask(path, name, region["registers"])
    if not mask:
        return None
    # An LFSR has no serial input, so what looked like one is control.
    spare = {k: ("en" if v == "d" else v) for k, v in ren.items()}
    got = match.region_wiring(spare)
    if not got:
        return None
    conn, width, dwidth = got[:3]
    if not match.canonicalise(path, name, conn, width, "gold", workdir, dwidth):
        return None
    for _, enable, clear in chain_templates(form, sync):
        verdict = prove_candidate(lfsr(width, mask, enable, clear, form),
                                  "%s/gold.json" % workdir, workdir,
                                  "cand_%d" % index)
        if verdict == "PROVEN EQUIVALENT":
            label = ("lfsr taps %#x%s%s"
                     % (mask, ", enable" if enable else "", named_clear(clear)))
            print("  region %d  %-28s %s" % (index, label, verdict))
            return {"label": label, "width": width, "form": "lfsr",
                    "mask": mask, "enable": enable, "clear": clear,
                    "edges": form, "inside": extra, "flops": order,
                    "roles": {v: k for k, v in spare.items()}}
    return None


def lift_chains(netlist, regions, workdir):
    """Behavioural RTL for every register chain that proves equivalent"""
    top, found = top_module(netlist), {}
    for index, region in enumerate(regions):
        if region["kind"] != "chain":
            continue
        order = list(reversed(region["registers"]))
        form = flop_form(top, region["registers"])
        if form is None:
            print("  region %d  registers do not share a clock edge" % index)
            continue
        got = lift_loads(netlist, region, order, index, workdir, form)
        if got:
            found[index] = got
            continue
        for inside, extra in cuts(top, region, order, links=True):
            got = chain_attempt(netlist, region, index, workdir, inside, extra,
                                form)
            if got:
                found[index] = got
                break
        if index not in found:
            print("  region %d  no shifting form" % index)
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


OPERANDS = "ab"


def cone_wiring(path, module_name, buses):
    """Maps a cone's ports onto the recovered buses, by bit position.

    An arithmetic form takes two operands and no more, so a design offering
    more recovered buses than that has no reading here and is left alone
    rather than being wired to an operand that does not exist.
    """
    if len(buses) > len(OPERANDS):
        return None, 0
    design = json.load(open(path))
    ports = design["modules"][module_name]["ports"]
    conn, width = {}, 0
    for index, bits in enumerate(buses):
        for position, names in enumerate(bits):
            hit = [n for n in names if n in ports]
            if len(hit) == 1:
                conn[hit[0]] = "%s[%d]" % (OPERANDS[index], position)
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


# Tried simplest first, so a one bit wide operand pair is reported as the and
# it reads as rather than the product it is also equal to.
DATAPATH_OPS = [("bitwise and", "&"), ("bitwise or", "|"),
                ("bitwise xor", "^"), ("sum", "+"), ("difference", "-"),
                ("product", "*"),
                ("shift left", "<<"), ("shift right", ">>")]

# Forms whose result is one bit however wide the operands are. Kept apart from
# the rest because a comparison proved against a wide result would be proved
# against its bottom bit alone, which is a different claim.
COMPARE_OPS = [("equal", "=="), ("not equal", "!="),
               ("less than", "<"), ("less or equal", "<="),
               ("greater than", ">"), ("greater or equal", ">=")]

# A word carrying a sign is a word like any other in a netlist, since two's
# complement makes the adder that builds it the same adder. The sign only
# shows in what is asked of the word afterwards, so these are the forms that
# tell a signed bus from an unsigned one, and the only place it can be told.
SIGN_TESTS = [("positive", "$signed(a) > 0"),
              ("negative", "$signed(a) < 0"),
              ("not negative", "$signed(a) >= 0"),
              ("not positive", "$signed(a) <= 0")]

# What one operand alone can be. A cone the width of its input is a bitwise
# form of it; a cone one bit wide is a question asked of all of it.
UNARY_OPS = [("bitwise not", "~a"), ("negation", "-a"),
             ("increment", "a + 1"), ("decrement", "a - 1")]

REDUCE_OPS = [("or of every bit", "|a"), ("and of every bit", "&a"),
              ("xor of every bit", "^a"), ("nor of every bit", "~|a"),
              ("nand of every bit", "~&a")]


def port_buses(path, module_name, direction):
    """A region's ports of one direction, grouped into buses by their name"""
    module = json.load(open(path))["modules"][module_name]
    groups = collections.defaultdict(list)
    for name, spec in module["ports"].items():
        if spec["direction"] == direction:
            base, index = match.bit_order(name)
            groups[base].append((index, name))
    return {base: [n for _, n in sorted(v)] for base, v in groups.items()}


def operand_splits(buses):
    """Every way two operands could be read out of the region's inputs.

    A datapath either takes two buses or one bus carrying both operands, so
    whole buses and their halves are the candidates worth trying.
    """
    names = sorted(buses)
    for a in names:
        for b in names:
            if a != b:
                yield buses[a], buses[b]
    for name in names:
        bits = buses[name]
        if len(bits) >= 2 and len(bits) % 2 == 0:
            half = len(bits) // 2
            yield bits[:half], bits[half:]
            yield bits[half:], bits[:half]


def operand_pairs(buses):
    """The same splits, then the same again with either operand reversed.

    Which end of a bus is the least significant bit is a convention the layout
    does not record, so it is guessed only after the natural order has failed.
    """
    splits = list(operand_splits(buses))
    for a, b in splits:
        yield a, b
    for a, b in splits:
        for flip in ((1, 0), (0, 1), (1, 1)):
            yield (a[::-1] if flip[0] else a), (b[::-1] if flip[1] else b)


def datapath_wrapper(name, inner, a, b, rest, y):
    """Names the region's ports as two operands, a result and what is left.

    Leftover inputs stay on the interface rather than being tied off, so what
    gets proven holds for every value of them, not just the convenient one.
    """
    conn = {}
    for i, port in enumerate(a):
        conn[port] = "a[%d]" % i
    for i, port in enumerate(b):
        conn[port] = "b[%d]" % i
    for i, port in enumerate(rest):
        conn[port] = "c[%d]" % i
    for i, port in enumerate(y):
        conn[port] = "y[%d]" % i
    body = ["module %s(a, b, c, y);" % name,
            "  input [%d:0] a;" % (len(a) - 1),
            "  input [%d:0] b;" % (len(b) - 1),
            "  input [%d:0] c;" % (max(len(rest), 1) - 1),
            "  output [%d:0] y;" % (len(y) - 1),
            "  %s i_dut (%s);" % (inner, ", ".join(
                ".%s(%s)" % (match.escape(p), e)
                for p, e in sorted(conn.items()))),
            "endmodule", ""]
    return "\n".join(body)


def datapath_candidate(op, awidth, bwidth, cwidth, ywidth):
    return "\n".join([
        "module cand(a, b, c, y);",
        "  input [%d:0] a;" % (awidth - 1),
        "  input [%d:0] b;" % (bwidth - 1),
        "  input [%d:0] c;" % (max(cwidth, 1) - 1),
        "  output [%d:0] y;" % (ywidth - 1),
        "  assign y = a %s b;" % op,
        "endmodule", ""])


def output_bus(region, outs):
    """The bus a cone was drawn around, told apart from what extraction exposed.

    Cutting a region out of a netlist makes a port of every net crossing its
    boundary, so a cone whose result is one bus comes back carrying dozens of
    them: present's ciphertext arrives with thirty-seven, open8's address with
    ninety-eight. Counting them therefore says nothing, and the cone would be
    passed over for having too many results. The region records the net it was
    drawn around, and that one is the result; the rest are working nets that a
    reader of the recovered RTL never sees.
    """
    if region.get("output") in outs:
        return outs[region["output"]]
    return list(outs.values())[0] if len(outs) == 1 else None


def plausible(a, b, y):
    """Whether two operands could be what a result of this width was made from.

    The same exposed ports that hide the result also offer hundreds of things
    to read as an operand, and trying every pair against every form would cost
    more than the rest of the flow. Width rules most of them out for nothing:
    a bitwise form, a sum and a difference are as wide as their result, a
    product is made of its halves, and a shift takes an amount that need not
    be wide at all. What survives is small enough to prove one at a time.
    """
    n = len(y)
    return (len(a) == len(b) == n
            or len(a) == len(b) == (n + 1) // 2
            or (len(a) == n and len(b) <= n))


def one_operand_wrapper(name, inner, a, rest, y):
    """Names the region's ports as a single operand, a result and what is left"""
    conn = {}
    for i, port in enumerate(a):
        conn[port] = "a[%d]" % i
    for i, port in enumerate(rest):
        conn[port] = "c[%d]" % i
    for i, port in enumerate(y):
        conn[port] = "y[%d]" % i
    body = ["module %s(a, c, y);" % name,
            "  input [%d:0] a;" % (len(a) - 1),
            "  input [%d:0] c;" % (max(len(rest), 1) - 1),
            "  output [%d:0] y;" % (len(y) - 1),
            "  %s i_dut (%s);" % (inner, ", ".join(
                ".%s(%s)" % (match.escape(p), e)
                for p, e in sorted(conn.items()))),
            "endmodule", ""]
    return "\n".join(body)


def one_operand_candidate(form, awidth, cwidth, ywidth):
    return "\n".join([
        "module cand(a, c, y);",
        "  input [%d:0] a;" % (awidth - 1),
        "  input [%d:0] c;" % (max(cwidth, 1) - 1),
        "  output [%d:0] y;" % (ywidth - 1),
        "  assign y = %s;" % form,
        "endmodule", ""])


def compare_candidate(op, awidth, bwidth, cwidth):
    return "\n".join([
        "module cand(a, b, c, y);",
        "  input [%d:0] a;" % (awidth - 1),
        "  input [%d:0] b;" % (bwidth - 1),
        "  input [%d:0] c;" % (max(cwidth, 1) - 1),
        "  output y;",
        "  assign y = (a %s b);" % op,
        "endmodule", ""])


def constant_at_zero(gold, workdir, tag):
    """What a cone puts out when its operand is nothing.

    A form written against a constant cannot be guessed at and cannot be swept
    for, since the constant is as wide as the bus. Held at zero the cone
    reports it directly: an exclusive or gives back the constant itself and a
    sum gives back the amount added, so one call replaces the sweep.
    """
    code, out = match.yosys(
        ["read_json %s" % gold, "hierarchy -top gold", "sat -set a 0 -show y"],
        "%s/%s_zero.ys" % (workdir, tag))
    if code:
        return None
    for line in out.split("\n"):
        got = re.match(r"\s*\\?y\s+(\d+)\s", line)
        if got:
            return int(got.group(1))
    return None


# How many operand pairs one cone is worth. A cone whose ports are all one bit
# wide offers hundreds of pairs and none of them is a datapath, so the search
# is stopped rather than allowed to cost more than every other region together.
PAIR_CEILING = 32

# A form of one operand reads a whole word, so the buses worth offering it are
# the widest few. A cone left holding a hundred single nets has no word among
# them and would otherwise pay a proof for each.
OPERAND_CEILING = 8


def operand_when_true(gold, workdir, tag):
    """The operand a one bit cone answers yes to, which is what it tests for"""
    code, out = match.yosys(
        ["read_json %s" % gold, "hierarchy -top gold", "sat -set y 1 -show a"],
        "%s/%s_true.ys" % (workdir, tag))
    if code:
        return None
    for line in out.split("\n"):
        got = re.match(r"\s*\\?a\s+(\d+)\s", line)
        if got:
            return int(got.group(1))
    return None


def one_operand_forms(gold, awidth, ywidth, workdir, tag):
    """Every form of one operand worth trying against a cone of this shape"""
    forms = []
    if awidth == ywidth:
        forms += UNARY_OPS
    if ywidth == 1:
        forms += REDUCE_OPS
        forms += SIGN_TESTS
        seen = operand_when_true(gold, workdir, tag)
        if seen is not None:
            forms.append(("equals %d" % seen, "a == %d'd%d" % (awidth, seen)))
    if awidth == ywidth:
        held = constant_at_zero(gold, workdir, tag)
        if held:
            forms.append(("exclusive or with %d" % held,
                          "a ^ %d'd%d" % (ywidth, held)))
            forms.append(("sum with %d" % held, "a + %d'd%d" % (ywidth, held)))
    return forms


def lift_one_operand(path, name, named, ins, y, index, workdir):
    """A form written against a single operand, where the cone reads as one"""
    for a in sorted(named.values(), key=len, reverse=True)[:OPERAND_CEILING]:
        if len(a) != len(y) and len(y) != 1:
            continue
        rest = sorted(p for bits in ins.values() for p in bits
                      if p not in set(a))
        wrap = "%s/up_%d_wrap.v" % (workdir, index)
        open(wrap, "w").write(one_operand_wrapper("gold", name, a, rest, y))
        code, log = match.yosys(
            ["read_json %s" % path, "read_verilog %s" % wrap,
             "hierarchy -top gold", "flatten", "opt_clean",
             "write_json %s/gold.json" % workdir],
            "%s/up_%d_wrap.ys" % (workdir, index))
        if code:
            continue
        gold = "%s/gold.json" % workdir
        for label, form in one_operand_forms(gold, len(a), len(y),
                                             workdir, "up_%d" % index):
            verdict = prove_candidate(
                one_operand_candidate(form, len(a), len(rest), len(y)),
                gold, workdir, "up_%d" % index)
            if verdict == "PROVEN EQUIVALENT":
                return {"label": label, "form": form, "a": a, "y": y}
    return None


def lift_two_operand(path, name, named, ins, y, index, workdir):
    """An arithmetic or comparing form written against two of a cone's buses"""
    pairs = [(a, b) for a, b in operand_pairs(named) if plausible(a, b, y)]
    pairs.sort(key=lambda ab: len(ab[0]) + len(ab[1]), reverse=True)
    for a, b in pairs[:PAIR_CEILING]:
        used = set(a) | set(b)
        rest = sorted(p for bits in ins.values() for p in bits
                      if p not in used)
        wrap = "%s/dp_%d_wrap.v" % (workdir, index)
        open(wrap, "w").write(datapath_wrapper("gold", name, a, b, rest, y))
        code, log = match.yosys(
            ["read_json %s" % path, "read_verilog %s" % wrap,
             "hierarchy -top gold", "flatten", "opt_clean",
             "write_json %s/gold.json" % workdir],
            "%s/dp_%d_wrap.ys" % (workdir, index))
        if code:
            continue
        gold = "%s/gold.json" % workdir
        for label, op in DATAPATH_OPS:
            verdict = prove_candidate(
                datapath_candidate(op, len(a), len(b), len(rest), len(y)),
                gold, workdir, "dp_%d" % index)
            if verdict == "PROVEN EQUIVALENT":
                return {"label": label, "op": op, "a": a, "b": b, "y": y}
        if len(y) != 1:
            continue
        for label, op in COMPARE_OPS:
            verdict = prove_candidate(
                compare_candidate(op, len(a), len(b), len(rest)),
                gold, workdir, "cmp_%d" % index)
            if verdict == "PROVEN EQUIVALENT":
                return {"label": label, "op": op, "a": a, "b": b, "y": y,
                        "compare": True}
    return None


def datapath_line(info):
    """A proven cone written back as the assignment it was proved to be.

    The left side is the cone's own bits and not the port they belong to: a
    region drawn round one bit of an eight bit output is a claim about that
    bit, and writing the port's name there would drive the other seven from a
    proof that never mentioned them.
    """
    return "assign %s = %s;" % (info.get("target") or slice_of(info["y"]),
                                datapath_form(info))


def datapath_form(info):
    """A proven form written out against the buses it was proved against"""
    if "b" not in info:
        return re.sub(r"\ba\b", slice_of(info["a"]), info["form"])
    body = "%s %s %s" % (slice_of(info["a"]), info["op"], slice_of(info["b"]))
    return "(%s)" % body if info.get("compare") else body


def shared_target(netlist, region):
    """The name a cone's result carries when more than one port bit reads it.

    Synthesis drives seven bits of an output from one net where the design
    said so, and the region is then drawn round that net but recorded under
    only one of those bits. Writing the form back to that one bit would be a
    claim about all seven, and the six left over would lose the logic that
    drove them. The net has a name of its own in the recovered RTL, the one
    the output wiring already reads, and that is what the assignment takes.

    None when the result is not shared, where a slice of the port says it.
    """
    module = list(json.load(open(netlist))["modules"].values())[0]
    ports = module.get("ports", {})
    where = collections.defaultdict(list)
    for name, spec in ports.items():
        if spec["direction"] == "input":
            continue
        for i, bit in enumerate(spec["bits"]):
            if not isinstance(bit, str):
                where[bit].append((name, i))
    for name in region.get("bits", []):
        base, index = match.bit_order(name)
        spec = ports.get(base)
        if spec is None or index < 0 or index >= len(spec["bits"]):
            continue
        seats = where[spec["bits"][index]]
        if len(seats) > 1:
            port, first = seats[0]
            return port if len(ports[port]["bits"]) == 1 else \
                "%s_%d" % (port, first)
    return None


SLICE = re.compile(r"^(\w+)\[(\d+):(\d+)\]$")


def driven_names(lines):
    """Every name an assignment drives, a slice counted as the bits it covers.

    Wiring an output port up asks, bit by bit, whether that bit already has a
    driver. A form proved for a whole word is written as the slice it is, and
    a slice left as the text it was written in answers for no single bit: the
    port is then wired again, bit by bit, to names that the proof replaced and
    nothing drives any more. Yosys reads those as constants and still proves
    the module; a simulator refuses to bind them, which is how this was found.
    """
    out = set()
    for one in re.findall(r"assign (\S+?) =", "\n".join(lines)):
        out.add(one)
        got = SLICE.match(one)
        if got:
            base, first, last = got.group(1), int(got.group(2)), int(got.group(3))
            out.update("%s[%d]" % (base, i)
                       for i in range(min(first, last), max(first, last) + 1))
    return out


SLOT = re.compile(r"^(\w+)\[(\d+)\]$")


def known_buses(netlist, regions, chains, states, banks, names):
    """Every word of registers the recovered RTL will have, as bits in order.

    A cone reads registers, and until the registers have been put back into
    the words they were split from there is nothing for it to read them as.
    Two passes have already done that work between them: lifting proves a
    chain, a counter or a bank and gives it a name, and the transcription
    gathers whatever is left into words by what each register is loaded from.
    Neither was reaching the cones, because both ran after them. Running the
    transcription once first costs no proof and no synthesis, and hands the
    cones the words they were missing.
    """
    module = list(json.load(open(netlist))["modules"].values())[0]
    skip, alias, label = naming(module, regions, chains, states, banks,
                                {}, {}, names)
    record = {}
    expr.transcribe(netlist, skip, alias, label, record=record)
    slots = collections.defaultdict(dict)
    for bit, name in record.items():
        got = SLOT.match(name)
        if got:
            slots[got.group(1)][int(got.group(2))] = bit
    out = []
    for name in sorted(slots):
        seats = slots[name]
        if len(seats) > 1 and sorted(seats) == list(range(len(seats))):
            out.append((name, [seats[i] for i in range(len(seats))]))
    return out


def bus_seats(module, ports, region_ports):
    """Which of a region's ports each top module net bit arrives on"""
    seat = {}
    for one in region_ports:
        bit = top_bit(module, ports, one)
        if bit is not None:
            seat.setdefault(bit, one)
    return seat


def read_buses(module, ports, ins, regs):
    """The buses a cone can be written against, and how each one is spelt.

    A region's ports carry the netlist's own names for the nets crossing its
    boundary, and those names mean nothing in the recovered RTL: the netlist
    calls a net n1435 where the RTL calls it after the bit it is or after the
    register it belongs to. So a form is only offered buses whose spelling on
    both sides is known, and every such bus is carried with the spelling to
    write it back under.

    Two kinds are known: a port of the top module, which keeps its name, and a
    word of registers, which was given one by the pass that proved it.
    """
    buses, render = dict(ins), {}
    for base, seats in ins.items():
        if base not in ports:
            del buses[base]
            continue
        for one in seats:
            render[one] = one
    seat = bus_seats(module, ports, [p for bits in ins.values() for p in bits])
    for name, bits in regs:
        got = [seat.get(b) for b in bits]
        if any(g is None for g in got) or len(set(got)) != len(got):
            continue
        buses[name] = got
        for i, one in enumerate(got):
            render[one] = "%s[%d]" % (name, i)
    return buses, render


def realign(module, ports, chains, states, banks, names, paths):
    """Renumbers a bank whose order a proved datapath disagrees with.

    Which flop of a bank is bit zero is not something a netlist records, and a
    bank is lifted in whatever order its flops came in. A datapath proved
    against that bank therefore comes back reading a shuffle of it, and the
    shuffle is the answer: it was proved, and it says which bit is which. So
    the bank is renumbered to agree and the shuffle becomes the bus it always
    was. Only banks are moved. A shift register's order is what it does, and a
    counter's is what it counts by; a bank alone has an order to spare.
    """
    where = {names[index]: index for index in banks}
    moved = {}
    for info in paths.values():
        for slot in ("a", "b"):
            got = [SLOT.match(one) for one in info.get(slot, ())]
            if not got or not all(got) or len({m.group(1) for m in got}) != 1:
                continue
            name = got[0].group(1)
            order = [int(m.group(2)) for m in got]
            if name in moved or name not in where:
                continue
            if sorted(order) != list(range(len(banks[where[name]]["flops"]))):
                continue
            moved[name] = order
            flops = banks[where[name]]["flops"]
            banks[where[name]]["flops"] = [flops[at] for at in order]
    if not moved:
        return {}
    for info in paths.values():
        for slot in ("a", "b"):
            got = [SLOT.match(one) for one in info.get(slot, ())]
            if got and all(got) and got[0].group(1) in moved:
                info[slot] = ["%s[%d]" % (got[0].group(1), i)
                              for i in range(len(got))]
    for name in sorted(moved):
        print("  bank %s renumbered to the order its datapath proved" % name)
    return resolve(module, ports, chains, states, banks, names)


def lift_datapaths(netlist, regions, workdir, done, regs=()):
    """An arithmetic form for every output bus that proves equivalent"""
    top = list(json.load(open(netlist))["modules"].values())[0]
    found = {}
    for index, region in enumerate(regions):
        if region["kind"] != "cone" or region["output"] in done:
            continue
        got = match.extract(netlist, region, index, workdir)
        if not got:
            continue
        path, name = got
        outs = port_buses(path, name, "output")
        ins = port_buses(path, name, "input")
        y = output_bus(region, outs)
        if y is None or not ins:
            continue
        target = shared_target(netlist, region)
        named, render = read_buses(top, top["ports"], ins, regs)
        hit = named and (
            lift_one_operand(path, name, named, ins, y, index, workdir) or
            lift_two_operand(path, name, named, ins, y, index, workdir))
        if hit:
            for slot in ("a", "b"):
                if slot in hit:
                    hit[slot] = [render[one] for one in hit[slot]]
        if hit and target:
            hit["target"] = target
        if hit and "b" in hit:
            print("  cone %-12s %s of %s and %s"
                  % (region["output"], hit["label"],
                     slice_of(hit["a"]), slice_of(hit["b"])))
        elif hit:
            print("  cone %-12s %-14s %s"
                  % (region["output"], hit["label"],
                     re.sub(r"\ba\b", slice_of(hit["a"]), hit["form"])))
        else:
            print("  cone %-12s no arithmetic form" % region["output"])
        if hit:
            found[region["output"]] = hit
    return found


def slice_of(bits):
    """Writes a list of port bits back as a slice where they form one"""
    parts = [match.bit_order(b) for b in bits]
    base = parts[0][0]
    # A port that never carried a bracket carries no position either, so it is
    # written as it stands rather than as bit minus one of itself.
    if len(parts) == 1 and parts[0][1] < 0:
        return base
    if all(p[0] == base for p in parts) and \
            [p[1] for p in parts] == list(range(parts[0][1], parts[-1][1] + 1)):
        if parts[0][1] == parts[-1][1]:
            return "%s[%d]" % (base, parts[0][1])
        return "%s[%d:%d]" % (base, parts[-1][1], parts[0][1])
    return "{%s}" % ", ".join(reversed(bits))


def numbered(role, letter):
    """Whether a role is one of a numbered family, and not merely spelt like it.

    The families are written a letter and an index, which a prefix alone does
    not tell apart from a role whose name happens to start with that letter:
    clk reads as the first of the c family and was being asked to name a
    control net, so every region clocked by anything but the top module's own
    clock port was refused after it had already proved.
    """
    return role[:1] == letter and role[1:].isdigit()


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
            if numbered(role, "q"):
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


def declare(lines, ports, rest=()):
    """Wires for every net a lifted block names that is not a port of the top.

    A region's clock, enable or load data can come from logic rather than a
    port, and that logic has not been lifted, so the net is named but never
    driven. Declaring it keeps the output legal Verilog and leaves the hole
    visible instead of silently absent. What the carried-over gates declare
    for themselves is left to them, so no net is declared twice.
    """
    known = set(ports)
    for name, spec in ports.items():
        known |= {"%s[%d]" % (name, i) for i in range(len(spec["bits"]))}
    known |= set(re.findall(r"\b(?:wire|reg) (n\d+)\s*[;=]", "\n".join(rest)))
    used = set(re.findall(r"\bn\d+\b", "\n".join(lines)))
    return ["  wire %s;" % n for n in sorted(used - known)]


def instance(hit, reg, role):
    """A library module wired up in place of the body it proved equal to.

    What a region proved equivalent to says more than any name the flow can
    invent for it, and says it with a proof behind it, so where there is one
    the module is named rather than written out. What the reference offers and
    the design never drove is tied off, exactly as it was tied off to prove it.
    """
    args = []
    for port in sorted(hit["wired"]):
        slot = hit["wired"][port]
        if slot == "":
            args.append(".%s()" % port)
        elif slot == "q":
            args.append(".%s(%s)" % (port, reg))
        elif slot.startswith("c[") and slot.endswith("]"):
            args.append(".%s(%s)" % (port, role.get("c" + slot[2:-1], "1'b1")))
        else:
            args.append(".%s(%s)" % (port, role.get(slot, slot)))
    head = "  %s #(.%s(%d)) u_%s (" % (hit["module"], hit["param"],
                                       hit["value"], reg)
    return ["  wire [%d:0] %s;" % (hit["width"] - 1, reg), head] + \
        ["      %s%s" % (a, "," if at < len(args) - 1 else "")
         for at, a in enumerate(args)] + ["  );"]


def proven_matches(netlist, outdir):
    """What a run of match.py proved this design's regions to be"""
    design = os.path.basename(netlist)
    for tail in ("_generic.json", "_faithful.json", ".json"):
        if design.endswith(tail):
            design = design[:-len(tail)]
            break
    path = "%s/%s_matches.json" % (outdir, design)
    return json.load(open(path)) if os.path.exists(path) else {}


def register_names(module, ports, chains, states, banks):
    """A name for every lifted register group, dropping those that cannot hold one.

    Naming happens before the cones are read rather than at the end, because a
    cone reading a register can only say so once the register has a name, and
    a register whose roles do not resolve is going back to gates: a form
    proved against it would name a bus that never gets written.
    """
    names = {}
    for slot, index in enumerate(sorted(chains)):
        names[index] = "r%d" % slot
    for slot, index in enumerate(sorted(states)):
        names[index] = "s%d" % slot
    for slot, index in enumerate(sorted(banks)):
        names[index] = "b%d" % slot
    roles = resolve(module, ports, chains, states, banks, names)
    for group in (chains, states, banks):
        for index in [i for i in group if i not in roles]:
            print("  region %d  cannot be named in the top module, "
                  "left as gates" % index)
            del group[index], names[index]
    return names, roles


def write_rtl(netlist, regions, chains, states, banks, cones, paths,
              names, roles, out):
    """Assembles the proven pieces into one readable module"""
    design = json.load(open(netlist))
    top = list(design["modules"])[0]
    module = design["modules"][top]
    ports = module["ports"]
    order = sorted(ports)
    head = ["module %s(%s);" % (top, ", ".join(order))]
    for name in order:
        spec = ports[name]
        span = "" if len(spec["bits"]) == 1 else "[%d:0] " % (len(spec["bits"]) - 1)
        head.append("  %s %s%s;" % (spec["direction"], span, name))

    matched = proven_matches(netlist, os.path.dirname(out) or ".")
    source = "%s/tmp/common_cells.v" % (os.path.dirname(out) or ".")
    if not os.path.exists(source):
        matched = {}
    library = []
    # Each recovered region is one piece, kept whole. Where the pieces go is
    # not settled here: they read gates that are written down further on, and
    # the arranger weighs putting them first against putting them among the
    # logic they read.
    proven = []
    for index, info in sorted(chains.items()):
        reg = names[index]
        piece = ["  reg [%d:0] %s;" % (info["width"] - 1, reg)]
        if info.get("form") == "load":
            piece += load_shift_body(info["width"], info["shift_on"], reg,
                                     roles[index], info["edges"])
        elif info.get("form") == "lfsr":
            piece += lfsr_body(info["width"], info["mask"], info["enable"],
                               info["clear"], reg, roles[index],
                               info["edges"])
        else:
            piece += shift_body(info["width"], info["enable"], info["clear"],
                                reg, roles[index], info["edges"])
        proven.append(piece)
    for index, info in sorted(states.items()):
        reg = names[index]
        hit = matched.get(str(index))
        if hit:
            proven.append(instance(hit, reg, roles[index]))
            library += match.needs(source, hit["module"])
            continue
        piece = ["  reg [%d:0] %s;" % (info["width"] - 1, reg)]
        piece += count_body(info["width"], info["clear"], info["en"],
                            info["updown"], info["step"], reg, roles[index],
                            info["edges"])
        proven.append(piece)
    for index, info in sorted(banks.items()):
        reg = names[index]
        role = dict(roles[index])
        role["d"] = "{%s}" % ", ".join(
            role["d%d" % i] for i in reversed(range(info["width"])))
        piece = ["  reg [%d:0] %s;" % (info["width"] - 1, reg)]
        piece += load_body(info["width"], info["enable"], info["clear"],
                           reg, role, info["edges"])
        proven.append(piece)
    for output, info in sorted(cones.items()):
        expr = info["form"].replace("y", output, 1) % (info["width"] + 1,
                                                       info["constant"])
        for slot, letter in enumerate("ab"):
            expr = re.sub(r"\b%s\b" % letter, names.get(slot, letter), expr)
        proven.append(["  " + expr])
    for output, info in sorted(paths.items()):
        proven.append(["  " + datapath_line(info)])
    proven += [[one] for one in output_wiring(ports, chains, names)]
    lines = [one for piece in proven for one in piece]
    rest, mods = leftover(netlist, regions, chains, states, banks, cones,
                          paths, names, lines, proven)
    lines = head + declare(lines, ports, rest) + rest
    tail = ["endmodule", ""]
    if mods:
        print("  %d sections stand as modules of their own" % len(mods))
        tail += ["// Sections of this design whose nets mostly stay inside",
                 "// them, written as modules so the hierarchy synthesis",
                 "// flattened reads as hierarchy again.",
                 ""] + [one for text in mods for one in text]
    if library:
        seen, keep = set(), []
        for body in library:
            name = re.match(r"module (\S+)", body).group(1)
            if name not in seen:
                seen.add(name)
                keep.append(body)
        tail += ["// Proved equivalent to the regions instantiated above, and",
                 "// carried here so the design still reads and still runs.",
                 ""] + "\n".join(keep).splitlines() + [""]
    open(out, "w").write("\n".join(lines + tail))
    return out


def driver_map(module):
    """Which cell drives each net bit of the top module"""
    out = {}
    for name, cell in module["cells"].items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in bits:
                    out[bit] = name
    return out


def enable_arms(module, flop, drivers):
    """The enable and the shifted-in value of a held register.

    A register that holds its value does it with a mux between itself and
    what comes next, so the select is the enable and the other arm is the
    data. Both are nets of the top module, which is what makes them safe to
    name: nothing has to be matched back from the extracted copy.
    """
    pins = module["cells"][flop]["connections"]
    src = drivers.get(pins["D"][0])
    if src is None or module["cells"][src]["type"] != "$_MUX_":
        return None
    mux = module["cells"][src]["connections"]
    own, a, b = pins["Q"][0], mux["A"][0], mux["B"][0]
    if a == own:
        return mux["S"][0], b
    return None


def port_alias(module):
    """Every input port bit under the name the top module gives it"""
    alias = {}
    for name, spec in module["ports"].items():
        if spec["direction"] != "input":
            continue
        for i, bit in enumerate(spec["bits"]):
            alias.setdefault(
                bit, name if len(spec["bits"]) == 1 else "%s[%d]" % (name, i))
    return alias


def resolve(module, ports, chains, states, banks, names):
    """Role names for every region that can be expressed, dropping the rest"""
    alias = port_alias(module)
    every = list(chains.items()) + list(states.items()) + list(banks.items())
    for index, info in every:
        for bit, flop in enumerate(info["flops"]):
            alias[module["cells"][flop]["connections"]["Q"][0]] = \
                "%s[%d]" % (names[index], bit)
    drivers = driver_map(module)
    out = {}
    for index, info in every:
        kind = ("chain" if index in chains
                else "state" if index in states else "bank")
        got = role_nets(module, info, alias, ports, kind, drivers)
        if got:
            out[index] = got
    return out


def top_bit(module, ports, name):
    """The net bit a region's port name stands for in the top module.

    A region is cut from a copy whose nets have been split to single bits, so
    its port carries either a name the top module already has or that name
    with an index after it. Only the bit behind the name is common to the two,
    and the name alone cannot be carried across: the recovered RTL calls a net
    after its number, so a region port called n20 is not the n20 that gets
    written out, and passing the spelling through would quietly join two
    different nets.
    """
    nets = module.get("netnames", {})
    for table in (ports, nets):
        if name in table and len(table[name]["bits"]) == 1:
            return table[name]["bits"][0]
    got = re.match(r"^(.*)\[(\d+)\]$", name or "")
    if not got:
        return None
    base, i = got.group(1), int(got.group(2))
    for table in (ports, nets):
        if base in table and i < len(table[base]["bits"]):
            return table[base]["bits"][i]
    return None


def role_nets(module, info, alias, ports, kind, drivers):
    """A lifted region's roles as they are named in the top module.

    Roles were derived inside the extracted region, whose port names belong to
    that region alone. Emitting them verbatim would name nets the top module
    does not have, so each one is resolved back to the pin it came from and
    the lift is refused when it cannot be.
    """
    def named(bit):
        return alias.get(bit, expr.net_name(bit))

    flops, conn = info["flops"], {}
    pins = module["cells"][flops[0]]["connections"]
    conn["clk"] = named(pins["C"][0])
    clear = info.get("clear")
    if clear and clear[3]:
        if "R" not in pins:
            return None
        conn["rst"] = named(pins["R"][0])

    def resolve_port(role):
        """A role's net under the name the recovered RTL gives it.

        A port of the top module keeps its own name; anything else is matched
        back to the bit it stands for and named from that, so a role driven by
        internal logic can be written out instead of costing the region a lift
        it had already proved. The region is still left as gates when the name
        answers to no net at all.
        """
        got = info["roles"].get(role)
        if got in ports:
            return got
        bit = top_bit(module, ports, got)
        return None if bit is None else named(bit)

    if clear and not clear[3]:
        conn["sr"] = resolve_port("sr")
        if conn["sr"] is None:
            return None
    if info["enable"]:
        conn["en"] = resolve_port("en")
        if conn["en"] is None:
            return None
    if kind == "state":
        for role in info["roles"]:
            if not numbered(role, "c"):
                continue
            conn[role] = resolve_port(role)
            if conn[role] is None:
                return None
        return conn
    if kind == "chain":
        if info.get("form") == "load":
            conn["ld"] = named(info["sel"])
            conn["d"] = named(pins["D"][0])
            for i, bit in enumerate(info["loads"], 1):
                conn["v%d" % i] = named(bit)
            return conn
        if info.get("form") == "lfsr":
            return conn
        if info["enable"]:
            got = enable_arms(module, info["flops"][0], drivers)
            if got is None:
                return None
            conn["en"], data = named(got[0]), named(got[1])
            conn["d"] = data
        else:
            conn["d"] = named(module["cells"][flops[0]]["connections"]["D"][0])
    else:
        for i, flop in enumerate(flops):
            conn["d%d" % i] = named(module["cells"][flop]["connections"]["D"][0])
    return conn


def exclusive(module, inside):
    """Cells of a region whose results nothing the region leaves behind reads.

    A template speaks for the cells it replaced, but only those: logic that
    also feeds elsewhere has to stay, or the rest of the module loses a
    driver and what gets written out is no longer the design.

    Which cells stay has to be settled together rather than one at a time. A
    cell kept because something outside reads it still reads its own inputs,
    and if one of those came from a cell dropped in the same pass, the text
    left behind reads a net nothing drives. So a cell is dropped only once
    every reader of its result has been dropped as well, which takes as many
    rounds as the chain is deep: present's ciphertext is sixty-four exclusive
    ors each fed by an inverter, the exclusive ors are read elsewhere and the
    inverters are not, and the first reading of this dropped all sixty-four
    inverters out from under the gates that were the only things reading them.
    """
    cells = module["cells"]
    readers = collections.defaultdict(set)
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "input":
                for bit in bits:
                    readers[bit].add(name)

    def held(name, drop):
        """Whether anything still standing reads what this cell puts out"""
        for port, bits in cells[name]["connections"].items():
            if cells[name]["port_directions"].get(port) != "output":
                continue
            if any(readers[bit] - drop for bit in bits):
                return True
        return False

    drop = set(inside)
    while True:
        keep = {name for name in drop if held(name, drop)}
        if not keep:
            return drop
        drop -= keep


def naming(module, regions, chains, states, banks, cones, paths, names):
    """What the transcription is told: which cells are done with, and by what
    name to call the nets that are left.

    Split out of the writing because it is wanted twice. Once at the end, to
    write the module; and once before the cones are read, because a datapath
    can only be written against a word after the word has been found, and it
    is this that finds them.
    """
    skip, alias = set(), {}
    for index, info in (list(chains.items()) + list(states.items())
                        + list(banks.items())):
        reg = names[index]
        skip |= set(regions[index]["registers"])
        skip |= exclusive(module, set(regions[index]["registers"])
                          | set(info.get("muxes", ()))
                          | set(info.get("inside", ())))
        for bit, flop in enumerate(info["flops"]):
            alias[module["cells"][flop]["connections"]["Q"][0]] = \
                "%s[%d]" % (reg, bit)
    for region in regions:
        if region["kind"] == "cone" and region["output"] in set(cones) | set(paths):
            skip |= exclusive(module, set(region["cells"]))
    for bit, name in port_alias(module).items():
        alias.setdefault(bit, name)
    label = {}
    for name, spec in module["ports"].items():
        if spec["direction"] == "input":
            continue
        for i, bit in enumerate(spec["bits"]):
            if bit not in alias and bit not in label:
                label[bit] = (name if len(spec["bits"]) == 1
                              else "%s_%d" % (name, i))
    return skip, alias, label


def leftover(netlist, regions, chains, states, banks, cones, paths,
             names, lines, proven=()):
    """Everything no template claimed, written out as plain expressions.

    A module is only worth proving as a whole once every output is driven, so
    what was recognised keeps its readable form and the remainder is carried
    over verbatim rather than dropped.
    """
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    ports = module["ports"]
    skip, alias, label = naming(module, regions, chains, states, banks,
                                cones, paths, names)
    wires, body, taken, mods = expr.transcribe(netlist, skip, alias, label, proven)
    if not body:
        return [], []
    driven = driven_names(lines)
    tail = []
    for name, spec in ports.items():
        if spec["direction"] == "input" or name in driven:
            continue
        for i, bit in enumerate(spec["bits"]):
            # A bit gathered into a word is driven there and wants no wiring
            # up: there is nothing left to drive it from.
            if bit in taken:
                continue
            one = name if len(spec["bits"]) == 1 else "%s[%d]" % (name, i)
            source = alias.get(bit, label.get(bit, expr.net_name(bit)))
            # A register carried over already took the output's own name, so
            # wiring it up again would assign the port to itself.
            if one not in driven and source != one:
                tail.append("  assign %s = %s;" % (one, source))
    return [""] + wires + [""] + body + tail, mods


def main(netlist, regions_path, outdir, out=None):
    workdir = os.path.join(outdir, "tmp")
    os.makedirs(workdir, exist_ok=True)
    regions = json.load(open(regions_path))
    print("chains")
    chains = lift_chains(netlist, regions, workdir)
    print("  %d of %d chains lifted"
          % (len(chains), sum(1 for r in regions if r["kind"] == "chain")))
    print("states")
    taken = {f for i in chains for f in chains[i]["flops"]}
    states = lift_states(netlist, regions, workdir, taken)
    print("  %d of %d state groups lifted"
          % (len(states), sum(1 for r in regions if r["kind"] == "state")))
    print("banks")
    held = taken | {f for i in states for f in states[i]["flops"]}
    banks, skipped = lift_banks(netlist, regions, workdir, held)
    print("  %d of %d banks lifted"
          % (len(banks),
             sum(1 for r in regions if r["kind"] == "bank") - skipped))
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    names, roles = register_names(module, module["ports"], chains, states,
                                  banks)
    regs = known_buses(netlist, regions, chains, states, banks, names)
    print("cones")
    if regs:
        show = ["%s[%d:0]" % (n, len(b) - 1) for n, b in regs[:12]]
        print("  %d words to read a cone against: %s%s"
              % (len(regs), ", ".join(show),
                 ", ..." if len(regs) > len(show) else ""))
    cones = lift_cones(netlist, regions, workdir)
    paths = lift_datapaths(netlist, regions, workdir, set(cones), regs)
    roles.update(realign(module, module["ports"], chains, states, banks,
                         names, paths))
    print("  %d of %d cones lifted"
          % (len(cones) + len(paths),
             sum(1 for r in regions if r["kind"] == "cone")))
    if out:
        write_rtl(netlist, regions, chains, states, banks, cones, paths,
                  names, roles, out)
        verdict = prove_candidate(open(out).read().replace(
            "module %s(" % list(json.load(open(netlist))["modules"])[0],
            "module cand("), netlist_as_gold(netlist, workdir), workdir, "rtl")
        print("rtl -> %s" % out)
        print("  whole module vs recovered netlist: %s" % verdict)
        # Giving up is not a disproof. A module too large to prove in the time
        # allowed still has to be compiled and simulated, and that simulation
        # is the only evidence it has left, so only an answer counts against
        # it: the puzzle proves nothing whole and was being denied the run.
        return 1 if verdict.startswith(("NOT EQUIVALENT", "no miter")) else 0


def netlist_as_gold(netlist, workdir):
    """The recovered netlist, renamed so it can stand as the reference"""
    top = list(json.load(open(netlist))["modules"])[0]
    match.yosys(["read_json %s" % netlist, "rename %s gold" % top,
                 "write_json %s/rtl_gold.json" % workdir],
                "%s/rtl_gold.ys" % workdir)
    return "%s/rtl_gold.json" % workdir


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
