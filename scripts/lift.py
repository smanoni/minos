# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Proposes behavioural RTL for a region and keeps only what proves equivalent"""

import sys
import os
import itertools
import json
import re
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match
import expr
import structure
import buses


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


def guarded(width, clear, reg, role, form, guard, move, split=False):
    """A template's always block, with the reset arm only where there is one.

    Split, the value the register takes is given a name of its own and the
    block does nothing but take it, which is how the sources this corpus came
    from write a register. It is not only a convention: a next value with no
    name is a next value nothing can be proved about, and every attempt to say
    what a register takes has had to write the expression back inside the
    block that consumes it.

    A reset is not split out. It is not the register's next value, it is what
    happens instead of taking one.
    """
    lines = ["  always @(%s)" % sensitivity(role, clear, form)]
    if split:
        held_reg, want = "%s_q" % reg, move.split("<=", 1)[1].strip()
        lines = ["  assign %s_d = %s" % (reg, re.sub(
            r"\b%s\b" % re.escape(reg), held_reg, want))] + lines
        move = "%s <= %s_d;" % (held_reg, reg)
    else:
        held_reg = reg
    if clear:
        return lines + [held(role, clear, form, held_reg, width),
                        "    else %s%s" % (guard, move)]
    return lines + ["    %s%s" % (guard, move)]


def state_decl(width, reg):
    """A register declared the way the sources this corpus came from write one"""
    span = "[%d:0] " % (width - 1)
    return ["  wire %s%s_d;" % (span, reg), "  reg %s%s_q;" % (span, reg)]


def shift_body(width, enable, clear, reg="q", role=None, form=RISING, split=False):
    """A register whose data comes from the stage before it, in given names"""
    role = role or CANONICAL
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role["en"] if enable else "",
                   "%s <= {%s[%d:0], %s};" % (reg, reg, width - 2, role["d"]), split)


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


def load_shift_body(width, shift_on, reg="q", role=None, form=RISING,
                    split=False):
    """A shift register that takes a whole word when its select says so"""
    role = role or dict(CANONICAL, ld="ld",
                        **{"v%d" % i: "v[%d]" % i for i in range(1, width)})
    if split:
        return ["  assign %s_d = %s%s ? %s : {%s_q[%d:0], %s};"
                % (reg, "!" if shift_on else "", role["ld"],
                   value_of(width, role), reg, width - 2, role["d"]),
                "  always @(%s %s)" % (form[0], role["clk"]),
                "    %s_q <= %s_d;" % (reg, reg)]
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


def lfsr_body(width, mask, enable, clear, reg="q", role=None, form=RISING, split=False):
    """A chain whose serial input is the parity of some of its own stages"""
    role = role or CANONICAL
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role["en"] if enable else "",
                   "%s <= {%s[%d:0], ^(%s & %d'd%d)};"
                   % (reg, reg, width - 2, reg, width, mask), split)


def lfsr(width, mask, enable, clear, form=RISING):
    """The same shift register as a module with canonical port names"""
    body = ["module cand(clk, rst, sr, en, d, q);",
            "  input clk, rst, sr, en, d;",
            "  output reg [%d:0] q;" % (width - 1)]
    body += lfsr_body(width, mask, enable, clear, form=form)
    return "\n".join(body + ["endmodule", ""])


def load_body(width, enable, clear, reg="q", role=None, form=RISING, split=False):
    """A register loaded from somewhere that is not another register"""
    role = role or CANONICAL
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role["en"] if enable else "",
                   "%s <= %s;" % (reg, role["d"]), split)


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


def count_body(width, clear, en, updown, step, reg="q", role=None, form=RISING, split=False):
    """A register that walks its own value on, the shape of every counter"""
    role = dict(CANONICAL, **(role or {}))
    if updown is None:
        move = "%s <= %s %s %d'd1;" % (reg, reg, step, width)
    else:
        move = ("%s <= %s ? %s + %d'd1 : %s - %d'd1;"
                % (reg, role.get(updown, updown), reg, width, reg, width))
    return guarded(width, clear, reg, role, form,
                   "if (%s) " % role.get(en, en) if en else "", move, split)


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
                ("shift left", "<<"), ("shift right", ">>"),
                # One operand inverted, which is a form in its own right and
                # not a spelling of the three above. It is what the gate basis
                # itself offers, and a design that writes `a & ~b` comes back
                # as exactly that pair of cells: present's ready is a not and
                # an and, and hd_8b10b's output enable only ever lifted by
                # being called a comparison, which `~a | b` is not.
                ("bitwise and not", "& ~"), ("bitwise or not", "| ~"),
                ("bitwise xnor", "^ ~")]

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
    # A load cone's result is a set of internal nets, which the cut turns
    # into a port apiece under the name the netlist gave them. The region
    # wrote those names down in bit order, so the result is assembled from
    # them rather than looked for among the ports the cut exposed.
    want = region.get("outputs")
    if want:
        have = {port: bits for bits in outs.values() for port in bits}
        got = [port for port in want if port in have]
        return got if len(got) == len(want) else None
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


def leaves(module, drive, bit, cap=4):
    """The nets a bit's logic bottoms out on, where there are few enough.

    Stops where the cone does: at a register, at a port, at anything no gate
    in the design drives. More than a handful means the bit is not a bitwise
    function of anything and there is nothing here to say about it.
    """
    cells, seen, out, queue = module["cells"], set(), set(), [bit]
    while queue:
        one = queue.pop()
        if one in seen:
            continue
        seen.add(one)
        src = drive.get(one)
        if src is None or match.FLOP in cells[src]["type"]:
            out.add(one)
            if len(out) > cap:
                return None
            continue
        queue += [b for port, conn in cells[src]["connections"].items()
                  for b in conn
                  if cells[src]["port_directions"].get(port) == "input"]
    return sorted(out)


def bit_naming(module, regs, render, seat):
    """Every net the recovered RTL can name, as the name it will carry there"""
    out = {}
    for name, bits in regs:
        for i, bit in enumerate(bits):
            out[bit] = "%s[%d]" % (name, i)
    for one, name in render.items():
        for bit, at in seat.items():
            if at == one:
                out[bit] = name
    return out


def paired(module, y, seat, spelling):
    """The two operands a bitwise cone is written against, read off the cone.

    A bitwise form is the one shape whose operands need no searching. Each
    output bit is a function of one bit of each operand, so walking back from
    that bit says which two nets they are, and the word each net belongs to
    says which operand is which. Searching instead asks the pairing to be
    guessed, and two words whose bit order was settled by different passes do
    not line up by luck. present's ciphertext is one 64 bit exclusive or of
    two recovered words and every bit of it takes a different position in
    each: `word0[64] ^ word1[12]`, then `word0[79] ^ word1[60]`. No whole bus
    offered either way round can be that, which is why the search refused a
    form the design plainly writes down.

    Membership is asked of the naming and not of the buses on offer, because a
    cone that reads two thirds of a word is reading that word: present's takes
    64 bits of an 80 bit key, and demanding the whole of it loses the operand
    altogether.
    """
    drive = driver_map(module)
    first, second, heads = [], [], None
    for bit in y:
        top = top_bit(module, module["ports"], bit)
        got = None if top is None else leaves(module, drive, top, 2)
        if not got or len(got) != 2:
            return None
        if any(one not in spelling or one not in seat for one in got):
            return None
        bases = [match.bit_order(spelling[one])[0] for one in got]
        if bases[0] == bases[1]:
            return None
        if heads is None:
            heads = tuple(sorted(bases))
        pick = dict(zip(bases, got))
        if set(pick) != set(heads):
            return None
        first.append(seat[pick[heads[0]]])
        second.append(seat[pick[heads[1]]])
    if len(set(first)) != len(first) or len(set(second)) != len(second):
        return None
    return (first, second) if first else None


def lift_two_operand(path, name, named, ins, y, index, workdir, read=None):
    """An arithmetic or comparing form written against two of a cone's buses"""
    pairs = [(a, b) for a, b in operand_pairs(named) if plausible(a, b, y)]
    pairs.sort(key=lambda ab: len(ab[0]) + len(ab[1]), reverse=True)
    pairs = ([read] if read else []) + pairs[:PAIR_CEILING]
    ready = []
    for at, (a, b) in enumerate(pairs):
        used = set(a) | set(b)
        rest = sorted(p for bits in ins.values() for p in bits
                      if p not in used)
        wrap = "%s/dp_%d_wrap.v" % (workdir, index)
        open(wrap, "w").write(datapath_wrapper("gold", name, a, b, rest, y))
        # Each pair keeps its own reference, since the comparisons are only
        # reached once every pair has been through the bitwise forms and a
        # shared file would by then hold the last pair's wrapper.
        gold = "%s/dp_%d_%d_gold.json" % (workdir, index, at)
        code, log = match.yosys(
            ["read_json %s" % path, "read_verilog %s" % wrap,
             "hierarchy -top gold", "flatten", "opt_clean",
             "write_json %s" % gold],
            "%s/dp_%d_wrap.ys" % (workdir, index))
        if code:
            continue
        for label, op in DATAPATH_OPS:
            verdict = prove_candidate(
                datapath_candidate(op, len(a), len(b), len(rest), len(y)),
                gold, workdir, "dp_%d" % index)
            if verdict == "PROVEN EQUIVALENT":
                return {"label": label, "op": op, "a": a, "b": b, "y": y}
        ready.append((a, b, rest, gold))

    # Comparisons last, and never between single bits. Every one of them is a
    # boolean form of one bit written as arithmetic: `a <= b` on one bit is
    # `~a | b`, and hd_8b10b's output enable was coming back as a comparison
    # of a reset against an input, which is not what the design says and not
    # what a reader would write. Tried after every pair has been offered the
    # bitwise forms, so a comparison is only reached where nothing plainer
    # holds.
    for a, b, rest, gold in ready:
        if len(y) != 1 or len(a) == 1:
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
    body = body.replace("~ ", "~")
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


def known_buses(netlist, regions, chains, states, banks, names, record=None):
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
                                {}, {}, {}, names)
    record = {} if record is None else record
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
    where = {"%s_q" % names[index]: index for index in banks}
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


# A cone's table doubles with every net it bottoms out on: sixteen of them is
# sixty-five thousand rows and instant, and a cone standing on more than that
# is a function of too much to be read as a selection. A control wider than
# four selects more arms than a design would ever write out.
TABLE_CAP, CONTROL_CAP = 16, 4


def row_axis(place, many):
    """The rows of a table of `many` nets in which one of them stands high"""
    period = 1 << (place + 1)
    block = ((1 << (1 << place)) - 1) << (1 << place)
    out = 0
    for step in range(1 << (many - place - 1)):
        out |= block << (step * period)
    return out


def gate_value(cell, val, full):
    """One gate's column of a table, worked out from its inputs' columns"""
    kind = cell["type"]
    if kind in expr.REDUCE:
        bits = [val[b] for b in cell["connections"]["A"]]
        if kind == "$reduce_and":
            out = full
            for one in bits:
                out &= one
            return out
        out = 0
        for one in bits:
            out = out ^ one if kind in ("$reduce_xor", "$reduce_xnor") \
                else out | one
        return full & ~out if kind == "$reduce_xnor" else out
    pin = cell["connections"]
    a = val[pin["A"][0]] if "A" in pin else 0
    b = val[pin["B"][0]] if "B" in pin else 0
    sel = val[pin["S"][0]] if "S" in pin else 0
    if kind == "$_AND_":
        return a & b
    if kind == "$_OR_":
        return a | b
    if kind == "$_XOR_":
        return a ^ b
    if kind == "$_XNOR_":
        return full & ~(a ^ b)
    if kind == "$_NAND_":
        return full & ~(a & b)
    if kind == "$_NOR_":
        return full & ~(a | b)
    if kind == "$_ANDNOT_":
        return a & (full & ~b)
    if kind == "$_ORNOT_":
        return a | (full & ~b)
    if kind == "$_NOT_":
        return full & ~a
    if kind == "$_MUX_":
        return (sel & b) | ((full & ~sel) & a)
    if kind == "$_NMUX_":
        return full & ~((sel & b) | ((full & ~sel) & a))
    return None


def cone_table(module, drive, bit, support, full):
    """What a bit does, as one answer for every value its nets can take.

    Carried as one integer of 2**len(support) bits rather than a list, so a
    whole column is a machine word and holding a net still is a mask.

    Walked with a stack rather than by recursion: a cone here is thousands of
    gates deep and Python would give up long before the cone did.
    """
    cells = module["cells"]
    val = {net: row_axis(at, len(support)) for at, net in enumerate(support)}
    stack = [bit]
    while stack:
        net = stack[-1]
        if net in val:
            stack.pop()
            continue
        if not isinstance(net, int):
            val[net] = full if str(net) == "1" else 0
            stack.pop()
            continue
        src = drive.get(net)
        if src is None:
            val[net] = 0
            stack.pop()
            continue
        want = [b for port, conn in cells[src]["connections"].items()
                for b in conn
                if cells[src]["port_directions"].get(port) == "input"
                and b not in val]
        if want:
            stack += want
            continue
        got = gate_value(cells[src], val, full)
        if got is None:
            return None
        val[net] = got
        stack.pop()
    return val[bit]


def true_nets(table, support, full):
    """The nets a table turns on, which is fewer than it was built over"""
    out = []
    for at, net in enumerate(support):
        high = row_axis(at, len(support))
        if ((table & high) >> (1 << at)) != (table & (full & ~high)):
            out.append(net)
    return out


def branch_reading(table, support, full, control):
    """What each arm of a case over these nets says, or None if it says nothing.

    An arm is worth writing only where holding the control still leaves the
    bit a function of one net or of none: a constant, a net, or a net
    inverted. Two nets left over is not a selection, it is the logic the
    selection was supposed to stand in for.
    """
    place = {net: at for at, net in enumerate(support)}
    arms = []
    for value in range(1 << len(control)):
        keep = full
        for at, net in enumerate(control):
            high = row_axis(place[net], len(support))
            keep &= high if (value >> at) & 1 else (full & ~high)
        free = [net for net in support if net not in control
                and ((table ^ (table >> (1 << place[net])))
                     & keep & (full & ~row_axis(place[net], len(support))))]
        if len(free) > 1:
            return None
        if not free:
            held = table & keep
            if held == 0:
                arms.append(("0", False))
            elif held == keep:
                arms.append(("1", False))
            else:
                return None
            continue
        net = free[0]
        high = row_axis(place[net], len(support))
        on, off = table & keep & high, table & keep & (full & ~high)
        if on == (keep & high) and off == 0:
            arms.append((net, False))
        elif on == 0 and off == (keep & (full & ~high)):
            arms.append((net, True))
        else:
            return None
    return arms


def select_control(bits, shared):
    """The fewest nets this cone is a case over, where it is a case at all.

    Each bit is asked in its own nets and not in the cone's together. Eight
    bits standing on three shared nets and four of their own apiece is
    thirty-five nets between them and a table too wide to build, where each
    bit on its own is seven and instant.
    """
    for many in range(1, min(CONTROL_CAP, len(shared)) + 1):
        for control in itertools.combinations(shared, many):
            read = [branch_reading(table, support, full, control)
                    for table, support, full in bits]
            if all(arm is not None for arm in read):
                return control, read
    return None, None


def select_wrapper(name, inner, ins, y):
    """Names every input the region takes as one bus, and its result as another.

    A cone is read here as a whole rather than as two operands, so there is
    nothing to tell apart and nothing to leave over: what a case is proven
    against is the region taking everything it takes.
    """
    conn = {}
    for at, port in enumerate(ins):
        conn[port] = "x[%d]" % at
    for at, port in enumerate(y):
        conn[port] = "y[%d]" % at
    return "\n".join([
        "module %s(x, y);" % name,
        "  input [%d:0] x;" % (len(ins) - 1),
        "  output [%d:0] y;" % (len(y) - 1),
        "  %s i_dut (%s);" % (inner, ", ".join(
            ".%s(%s)" % (match.escape(port), wire)
            for port, wire in sorted(conn.items()))),
        "endmodule", ""])


def arm_terms(arms, spell):
    """One arm of a case, as the word the cone puts out under that control"""
    out = []
    for net, flipped in arms:
        if net in ("0", "1"):
            out.append("1'b%s" % net)
        else:
            out.append("%s%s" % ("~" if flipped else "", spell(net)))
    return out[0] if len(out) == 1 else "{%s}" % ", ".join(reversed(out))


def case_lines(control, arms, spell, head):
    """A case over the nets a cone selects on, one arm per value they take"""
    out = ["  always @* case ({%s})" % ", ".join(
        spell(net) for net in reversed(control))]
    for value in range(1 << len(control)):
        out.append("    %d'd%d: %s = %s;"
                   % (len(control), value, head,
                      arm_terms([one[value] for one in arms], spell)))
    return out + ["  endcase"]


def select_block(info):
    """A proven cone written back as the case it was proved to be"""
    target = info.get("target") or slice_of(info["y"])
    head = "%s_sel" % re.sub(r"\W", "_", target)
    wide = len(info["y"])
    spell = lambda net: info["speak"][info["taken"][info["seats"][net]]]
    out = ["  reg %s%s;" % ("" if wide == 1 else "[%d:0] " % (wide - 1), head)]
    out += case_lines(info["control"], info["arms"], spell, head)
    return out + ["  assign %s = %s;" % (target, head)]


def select_candidate(control, arms, seats, width, many):
    """The case a cone was read as, written against the bus it is proved on"""
    body = ["module cand(x, y);",
            "  input [%d:0] x;" % (many - 1),
            "  output reg [%d:0] y;" % (width - 1)]
    body += case_lines(control, arms, lambda net: "x[%d]" % seats[net], "y")
    return "\n".join(body + ["endmodule", ""])


def lift_selects(netlist, regions, workdir, done, regs=()):
    """A case for every cone that turns out to be a selection over a few nets.

    A cone whose bits all stand on the same handful of nets, and which each
    become a net or a constant once those are held, is a case over them. The
    control is read off the cone rather than guessed at, and so is every arm:
    what is searched for is only how many nets the control takes.
    """
    top = list(json.load(open(netlist))["modules"].values())[0]
    found = {}
    for index, region in enumerate(regions):
        if region["kind"] != "cone" or region["output"] in done:
            continue
        got = match.extract(netlist, region, index, workdir)
        if not got:
            continue
        path, name = got
        inner = json.load(open(path))["modules"][name]
        outs = port_buses(path, name, "output")
        ins = port_buses(path, name, "input")
        y = output_bus(region, outs)
        if y is None or len(y) < 2 or not ins:
            continue
        taken = [port for base in sorted(ins) for port in ins[base]]
        seats = {inner["ports"][port]["bits"][0]: at
                 for at, port in enumerate(taken)}
        drive = driver_map(inner)
        bits, shared = [], None
        for port in y:
            bit = inner["ports"][port]["bits"][0]
            support = leaves(inner, drive, bit, TABLE_CAP)
            if not support or any(net not in seats for net in support):
                bits = None
                break
            full = (1 << (1 << len(support))) - 1
            table = cone_table(inner, drive, bit, support, full)
            if table is None:
                bits = None
                break
            bits.append((table, support, full))
            stands = set(true_nets(table, support, full))
            shared = stands if shared is None else shared & stands
        if not bits or not shared:
            continue
        control, arms = select_control(bits, sorted(shared))
        if control is None:
            print("  cone %-12s no selecting form" % region["output"])
            continue
        # A leaf may be a register, and a register is not called in the
        # recovered RTL what it is called in the netlist: `leaves` stops at
        # one and hands back `n280`, which the design writes as a bit of the
        # word it belongs to. Spelt raw it names nothing and comes back
        # undriven, so a cone naming anything the RTL will not carry is left
        # alone rather than written against a name that is not there.
        named, render = read_buses(top, top["ports"], ins, regs)
        seat = bus_seats(top, top["ports"],
                         [p for bits in ins.values() for p in bits])
        spelling = bit_naming(top, regs, render, seat)
        # Only a net that has a name of its own counts. Falling back to what
        # the region called it puts `n795` in an arm, which is the netlist's
        # word for a net the recovered RTL never declares.
        speak = dict(render)
        for bit, one in seat.items():
            if bit in spelling:
                speak.setdefault(one, spelling[bit])
        # A net no word claims is still written down: the transcription calls
        # an internal net after the bit it is, and that name is as real as any
        # other. Refusing it cost hd_8b10b both its encoder tables, which are
        # a lookup and have no regularity for a word to be recovered from —
        # ten per cent of its nets are seated, the corpus's lowest. Offered
        # last, so a word's name always wins where there is one.
        for bit, one in seat.items():
            speak.setdefault(one, expr.net_name(bit))
        wanted = set(control) | {net for one in arms for net, _ in one
                                 if net not in ("0", "1")}
        if any(taken[seats[net]] not in speak for net in wanted):
            print("  cone %-12s selects on nets the RTL does not name"
                  % region["output"])
            continue
        text = select_candidate(control, arms, seats, len(y), len(taken))
        wrap = "%s/sel_%d_wrap.v" % (workdir, index)
        open(wrap, "w").write(select_wrapper("gold", name, taken, y))
        gold = "%s/sel_%d_gold.json" % (workdir, index)
        code, log = match.yosys(
            ["read_json %s" % path, "read_verilog %s" % wrap,
             "hierarchy -top gold", "flatten", "opt_clean",
             "write_json %s" % gold],
            "%s/sel_%d_wrap.ys" % (workdir, index))
        if code:
            continue
        verdict = prove_candidate(text, gold, workdir, "sel_%d" % index)
        print("  cone %-12s case over %d net%s  %s"
              % (region["output"], len(control),
                 "" if len(control) == 1 else "s", verdict))
        if verdict == "PROVEN EQUIVALENT":
            found[region["output"]] = {
                "label": "case", "control": list(control), "arms": arms,
                "seats": seats, "y": y, "taken": taken,
                "speak": speak, "text": text}
    return found


def port_bits(module, spelt):
    """The netlist bit each of a region's own bit names stands for.

    A load cone's result is internal nets and not ports, so looking only at
    the ports resolves nothing and the cone is passed over in silence: that
    is every cone drawn at a register's input, which is most of them.
    """
    where = dict(module.get("ports", {}))
    for name, spec in module.get("netnames", {}).items():
        where.setdefault(name, spec)
    out = []
    for one in spelt:
        got = re.match(r"^(.*?)(?:\[(\d+)\])?$", one)
        base, at = got.group(1), int(got.group(2) or 0)
        if base in where and at < len(where[base]["bits"]):
            out.append(where[base]["bits"][at])
    return out if len(out) == len(spelt) else []


def cut_forms(netlist, region, index, workdir, words, cells, drive, ports,
              module):
    """A refusing cone tried again, cut at a word it computes for itself.

    This is the wall M5 stands at: a cone reads a word the design works out
    inside the same cone, and no form can be written against a net that has
    no name beyond it. `db_MAC` adds an accumulator to a product, `open8`
    muxes its sources and then adds. Cutting at the word turns it into a
    boundary — it crosses as an input, so it can be named — and the form is
    asked of what is left.
    """
    ybits = port_bits(module, region.get("bits", []))
    if not ybits:
        return None
    for at, (name, bits) in enumerate(sorted(words.items())):
        cut = structure.cut_at(cells, drive, ports, region["output"], ybits, bits)
        if not cut:
            continue
        got = match.extract(netlist, cut, 9000 + index * 40 + at, workdir)
        if not got:
            continue
        path, inner = got
        ins = port_buses(path, inner, "input")
        outs = port_buses(path, inner, "output")
        have = {port for one in ins.values() for port in one}
        y = output_bus(cut, outs)
        want = ["minos_net_%d" % slot for slot in cut["operand_slots"]]
        if y is None or not set(want) <= have:
            continue
        yield name, path, inner, ins, y, want


def lift_datapaths(netlist, regions, workdir, done, regs=(), cut_words=None):
    """An arithmetic form for every output bus that proves equivalent"""
    top = list(json.load(open(netlist))["modules"].values())[0]
    cells_, drive_, _, ports_ = structure.load(netlist)
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
        seat = bus_seats(top, top["ports"],
                         [p for bits in ins.values() for p in bits])
        spelling = bit_naming(top, regs, render, seat)
        read = paired(top, y, seat, spelling)
        speak = dict(render)
        for bit, one in seat.items():
            speak.setdefault(one, spelling.get(bit, one))
        hit = (named or read) and (
            lift_one_operand(path, name, named, ins, y, index, workdir) or
            lift_two_operand(path, name, named, ins, y, index, workdir, read))
        if hit:
            for slot in ("a", "b"):
                if slot in hit:
                    hit[slot] = [speak[one] for one in hit[slot]]
        if hit and target:
            hit["target"] = target
        if hit and "b" in hit:
            print("  cone %-12s %s of %s and %s"
                  % (region["output"], hit["label"],
                     slice_of(hit["a"]), slice_of(hit["b"])))
        elif hit and hit.get("cut"):
            pass
        elif hit:
            print("  cone %-12s %-14s %s"
                  % (region["output"], hit["label"],
                     re.sub(r"\ba\b", slice_of(hit["a"]), hit["form"])))
        # Refused against every operand it could name, the cone is tried once
        # more cut at a word it computes for itself. What it could not say as
        # one form it may say as a form of two.
        if not hit and cut_words:
            for name, path2, inner, ins2, y2, want in cut_forms(
                    netlist, region, index, workdir, cut_words, cells_, drive_,
                    ports_, top):
                rest2 = sorted(p for one in ins2.values() for p in one
                               if p not in set(want))
                for other in sorted(ins2):
                    a = ins2[other]
                    # The same width rule the uncut path uses. Demanding the
                    # operand be as wide as the result refuses a product
                    # outright, whose halves are half of it — which is the
                    # shape a multiply and accumulate is made of.
                    if set(a) & set(want) or not plausible(a, want, y2):
                        continue
                    wrap = "%s/cut_%d_wrap.v" % (workdir, index)
                    open(wrap, "w").write(
                        datapath_wrapper("gold", inner, a, want,
                                         sorted(set(rest2) - set(a)), y2))
                    gold = "%s/cut_%d_gold.json" % (workdir, index)
                    code, log = match.yosys(
                        ["read_json %s" % path2, "read_verilog %s" % wrap,
                         "hierarchy -top gold", "flatten", "opt_clean",
                         "write_json %s" % gold],
                        "%s/cut_%d.ys" % (workdir, index))
                    if code:
                        continue
                    for label, op in DATAPATH_OPS:
                        if prove_candidate(
                                datapath_candidate(op, len(a), len(want),
                                                   len(rest2), len(y2)),
                                gold, workdir, "cut_%d" % index) \
                                == "PROVEN EQUIVALENT":
                            hit = {"label": "%s with %s" % (label, name),
                                   "op": op, "a": a, "b": want, "y": y2,
                                   "cut": name}
                            break
                    if hit:
                        break
                if hit:
                    print("  cone %-12s %s, cut at %s"
                          % (region["output"], hit["label"], name))
                    break
        if not hit:
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


def output_wiring(ports, chains, names, seat=()):
    """Drives each output bit from a lifted register, a constant or an input"""
    driven, lines = {}, []
    for slot, (index, info) in enumerate(sorted(chains.items())):
        for role, port in info["roles"].items():
            if not numbered(role, "q"):
                continue
            driven[port] = ("%sq[%s][%d]" % (seat[index][0], role[1:],
                                             seat[index][1])
                            if index in seat
                            else "%s_q[%s]" % (names[index], role[1:]))
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


def hoist(lines, ports):
    """Declarations for the nets a line reads above the line that declares them.

    A net written where it is first computed reads better than one declared
    far from the value it carries, so that is how a section comes out. The
    sections are ordered by what the design does, though, not by what each
    net needs, so a handful of uses land above their declaration, and a port
    is defined a second time by the section that drives it. Verilog reads
    such a use as a net of its own and then refuses the declaration below as
    a second one. Those few are lifted to the block at the top: a definition
    leaves its value behind as an assignment where it stood, and a bare
    declaration moves whole, having nothing to leave.
    """
    define = re.compile(r"^(\s*)wire (\w+) = ")
    bare = re.compile(r"^\s*(?:wire|reg|integer)\s+(?:\[[^\]]*\]\s*)?"
                      r"(\w+)\s*(?:\[[^\]]*\]\s*)*;\s*$")
    valued, plain = {}, {}
    for index, line in enumerate(lines):
        hit = define.match(line)
        if hit:
            valued.setdefault(hit.group(2), index)
            continue
        hit = bare.match(line)
        if hit:
            plain.setdefault(hit.group(1), index)
    early = {name for name in valued if name in ports}
    moved = set()
    for index, line in enumerate(lines):
        for name in set(re.findall(r"\b\w+\b", line)):
            if index < valued.get(name, index):
                early.add(name)
            if index < plain.get(name, index):
                moved.add(name)
    for name in early:
        lines[valued[name]] = define.sub(r"\1assign %s = " % name,
                                         lines[valued[name]], 1)
    out = ["  wire %s;" % name for name in sorted(early) if name not in ports]
    out += ["  " + lines[plain[name]].strip()
            for name in sorted(moved) if name not in ports]
    for name in moved:
        lines[plain[name]] = None
    lines[:] = [line for line in lines if line is not None]
    return out


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


def chain_words(chains, roles, names):
    """Chains alike enough to be read across the datapath instead of along it.

    A pipeline is one register per stage, as wide as the data. Synthesis
    leaves it as one chain per bit, and reading those along the datapath gives
    a register apiece: des comes back as 512 two deep shift registers where
    the design has two registers 512 bits wide. Where a family of chains
    shares its clock and its depth and has neither enable nor reset, the stage
    is the word and the chain is one bit of it, which is the same registers
    said the way they were written.

    Only where a family has more chains than it has stages. Read across, a
    family costs a declaration and a line per stage where along it costs one
    per chain, so eight chains fifteen deep are better left as they are.
    """
    family = collections.defaultdict(list)
    shape = {}
    for index, info in sorted(chains.items()):
        if info.get("form") not in (None, "shift") or info["enable"]:
            continue
        role = roles.get(index)
        if role is None or info["width"] < 2:
            continue
        clear = info["clear"]
        rst = role.get(clear[0]) if clear else None
        if clear and rst is None:
            continue
        key = (info["width"], info["edges"], role["clk"], str(clear), rst)
        family[key].append(index)
        shape[key] = (info["edges"], clear, role)
    out, seat = {}, {}
    for at, key in enumerate(sorted(family, key=str)):
        members = family[key]
        if len(members) <= key[0]:
            continue
        for slot, index in enumerate(members):
            seat[index] = ("pipe%d_" % at, slot)
        out["pipe%d_" % at] = (key[0], len(members), shape[key], members)
    return out, seat


def word_shift_body(prefix, width, count, shape, feeds):
    """A family of chains as an array of stages, written with the loop it is.

    A pipeline is one register per stage and every stage but the first takes
    the stage before it, which is a copy of one body as many times as the
    pipeline is deep. Declared as an array the stages are indexed rather than
    numbered into names, and then the copies are a loop: des's key schedule is
    a fifty-six bit word shifted sixteen times, and reads as that instead of
    as sixteen blocks that differ only in a digit.

    The whole family is one piece. An array cannot cross a module boundary,
    so nothing can pull half of it into a section and leave the rest named at
    the top and declared nowhere, which is what writing a stage apiece was
    guarding against.

    The reset arm composes because a stage resets uniformly across the word: a
    register with a reset pin holds one value in every bit, and a region held
    at a constant holds that constant's bit for the stage, the same bit for
    every chain in the family. Where the stages hold different bits the loop
    is worth nothing over them and they are written out one apiece instead,
    and the same for a family too shallow for a loop to say anything: two
    stages written as a loop of one turn is a longer way to say one line.
    """
    form, clear, role = shape
    span = "[%d:0] " % (count - 1)
    out = ["  wire %s%sd;" % (span, prefix),
           "  reg %s%sq [0:%d];" % (span, prefix, width - 1)]
    out += expr.packed("  assign %sd = {" % prefix, "      ",
                       list(reversed(feeds)), "};")
    out.append("  always @(%s) begin" % sensitivity(role, clear, form))
    step = "    "
    if clear:
        start = [form[2] if clear[2] is None else clear[2][at]
                 for at in range(width)]
        ask = "%s%s" % ("!" if clear[1] else "", role[clear[0]])
        if len(set(start)) == 1 and width > 2:
            out += ["    if (%s)" % ask,
                    "      %s" % loop(prefix, 0, width),
                    "        %sq[%si] <= {%d{1'b%s}};"
                    % (prefix, prefix, count, start[0]),
                    "    else begin"]
        else:
            out += ["    if (%s) begin" % ask]
            out += ["      %sq[%d] <= {%d{1'b%s}};"
                    % (prefix, at, count, start[at]) for at in range(width)]
            out += ["    end else begin"]
        step = "      "
    out.append("%s%sq[0] <= %sd;" % (step, prefix, prefix))
    if width > 2:
        out += ["%s%s" % (step, loop(prefix, 1, width)),
                "%s  %sq[%si] <= %sq[%si-1];"
                % (step, prefix, prefix, prefix, prefix)]
    else:
        out += ["%s%sq[%d] <= %sq[%d];" % (step, prefix, at, prefix, at - 1)
                for at in range(1, width)]
    out += (["    end"] if clear else []) + ["  end"]
    if any(one.lstrip().startswith("for (") for one in out):
        out.insert(2, "  integer %si;" % prefix)
    return out


def loop(prefix, first, width):
    """The head of the loop a family's stages are written under"""
    return ("for (%si = %d; %si < %d; %si = %si + 1)"
            % (prefix, first, prefix, width, prefix, prefix))


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


def loaded_with(held, index):
    """The form a bank's own load cone was proved to be, where one was.

    A bank writes what it is given and nothing else, so a proof about what it
    is given is a proof about the bank: where the cone driving its D inputs
    was shown to be a sum, the register loads that sum. Only banks, since a
    chain and a counter compute their own next state and a form proved of
    their inputs would be claimed twice.
    """
    hit = held.get("bank%d_d" % index)
    if not hit or "y" not in hit:
        return None
    return datapath_form(hit)


def write_rtl(netlist, regions, chains, states, banks, cones, paths, selects,
              names, roles, across, seat, out, held=None, words=None):
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
    for prefix, (width, count, shape, members) in sorted(across.items()):
        proven.append(word_shift_body(
            prefix, width, count, shape,
            [roles[index]["d"] for index in members]))
    for index, info in sorted(chains.items()):
        if index in seat:
            continue
        reg = names[index]
        piece = state_decl(info["width"], reg)
        if info.get("form") == "load":
            piece += load_shift_body(info["width"], info["shift_on"], reg,
                                     roles[index], info["edges"], True)
        elif info.get("form") == "lfsr":
            piece += lfsr_body(info["width"], info["mask"], info["enable"],
                               info["clear"], reg, roles[index],
                               info["edges"], True)
        else:
            piece += shift_body(info["width"], info["enable"], info["clear"],
                                reg, roles[index], info["edges"], True)
        proven.append(piece)
    for index, info in sorted(states.items()):
        reg = names[index]
        hit = matched.get(str(index))
        if hit:
            proven.append(instance(hit, reg, roles[index]))
            library += match.needs(source, hit["module"])
            continue
        piece = state_decl(info["width"], reg)
        piece += count_body(info["width"], info["clear"], info["en"],
                            info["updown"], info["step"], reg, roles[index],
                            info["edges"], True)
        proven.append(piece)
    for index, info in sorted(banks.items()):
        reg = names[index]
        role = dict(roles[index])
        role["d"] = loaded_with(held or {}, index) or "{%s}" % ", ".join(
            role["d%d" % i] for i in reversed(range(info["width"])))
        piece = state_decl(info["width"], reg)
        piece += load_body(info["width"], info["enable"], info["clear"],
                           reg, role, info["edges"], True)
        proven.append(piece)
    for output, info in sorted(cones.items()):
        form = info["form"].replace("y", output, 1) % (info["width"] + 1,
                                                       info["constant"])
        for slot, letter in enumerate("ab"):
            form = re.sub(r"\b%s\b" % letter,
                          "%s_q" % names[slot] if slot in names else letter,
                          form)
        proven.append(["  " + form])
    for output, info in sorted(paths.items()):
        proven.append(["  " + datapath_line(info)])
    for output, info in sorted(selects.items()):
        proven.append(select_block(info))
    proven += [[one] for one in output_wiring(ports, chains, names, seat)]
    lines = [one for piece in proven for one in piece]
    rest, mods = leftover(netlist, regions, chains, states, banks, cones,
                          paths, selects, names, lines, proven, seat, words)
    forward = hoist(rest, ports)
    lines = head + forward + declare(lines, ports, rest + forward) + rest
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
                "%s_q[%d]" % (names[index], bit)
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


def naming(module, regions, chains, states, banks, cones, paths, selects,
           names, seat=()):
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
            # A chain read across the datapath is named for the stage it is in
            # and the bit of that stage it carries, which is the other way
            # round from a chain that stands on its own.
            alias[module["cells"][flop]["connections"]["Q"][0]] = (
                "%sq[%d][%d]" % (seat[index][0], bit, seat[index][1])
                if index in seat else "%s_q[%d]" % (reg, bit))
    for region in regions:
        if region["kind"] == "cone" and region["output"] in (
                set(cones) | set(paths) | set(selects)):
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


def word_labels(module, skip, label, alias, words, lines=()):
    """Names every bit of a recovered word after the word, and says how wide.

    A bit already spoken for keeps the name it has and the rest of the word is
    still a word: demanding every bit be free loses almost all of them, since
    any design of size has a proven region touching most words somewhere. Only
    a net a gate drives can be renamed — one off a register is that register,
    and one out of a replaced region belongs to the template speaking for it.
    """
    cells = module["cells"]
    drive = driver_map(module)
    # Only a net that more than one line reads. A net read once is folded into
    # the line that reads it and costs nothing; naming it keeps it as a wire
    # of its own and buys a word at the price of a line, which present paid
    # 578 to 715 times over before this was seen.
    reads = collections.Counter(
        bit for cell in cells.values()
        for port, bits in cell["connections"].items()
        for bit in bits
        if cell["port_directions"].get(port) == "input")
    # A net a proven piece already wrote down keeps that spelling. The pieces
    # are emitted before this runs and are not revisited, so renaming one now
    # leaves a template reading a wire nothing declares.
    spoken = set(re.findall(r"\b\w+\b", "\n".join(lines)))
    out = {}
    for name in sorted(words or {}):
        back = [bit for bit in words[name]
                if isinstance(bit, int) and bit not in label
                and bit not in alias and bit in drive
                and reads[bit] > 1
                and expr.net_name(bit) not in spoken
                and drive[bit] not in skip
                and match.FLOP not in cells[drive[bit]]["type"]]
        if len(back) < 2:
            continue
        out[expr.plainly(name)] = len(back)
        for slot, bit in enumerate(back):
            label[bit] = "%s[%d]" % (expr.plainly(name), slot)
    return out


def leftover(netlist, regions, chains, states, banks, cones, paths, selects,
             names, lines, proven=(), seat=(), words=None):
    """Everything no template claimed, written out as plain expressions.

    A module is only worth proving as a whole once every output is driven, so
    what was recognised keeps its readable form and the remainder is carried
    over verbatim rather than dropped.
    """
    design = json.load(open(netlist))
    module = list(design["modules"].values())[0]
    ports = module["ports"]
    skip, alias, label = naming(module, regions, chains, states, banks,
                                cones, paths, selects, names, seat)
    # Settled here rather than inside the transcription, which is not the only
    # thing that names a net: a register's role and an output's wiring name
    # them too, and a word named in one place and not the others leaves a
    # concatenation reading a wire nothing declares.
    wide = word_labels(module, skip, label, alias, words, lines)
    wires, body, taken, mods = expr.transcribe(netlist, skip, alias, label,
                                               proven, recovered=wide)
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
    # Settled before anything names a net, since a name made under one
    # spelling and read back under another is two names for one net.
    expr.NUMBER[0] = expr.numbering(
        list(json.load(open(netlist))["modules"].values())[0])
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
    across, seat = chain_words(chains, roles, names)
    if across:
        print("  %d families read across the datapath: %s"
              % (len(across), ", ".join(
                  "%d chains %d deep" % (count, width)
                  for width, count, _, _ in sorted(across.values(), key=str))))
    spoken = {}
    regs = known_buses(netlist, regions, chains, states, banks, names, spoken)
    # The register words tell the combinational nets what they are bits of,
    # and a cone reads far more of the second than the first. Grown here
    # rather than in the pass of their own so a cone is offered both at once.
    module_, driver_ = buses.load(netlist)
    start = dict(regs)
    start.update(buses.seeds(module_, regions))
    start.update(buses.adders(netlist, workdir))
    grown, _ = buses.propagate(module_, driver_, start)
    have = {name for name, _ in regs}
    # Only the words this pass made, and only where they are its own: a bit
    # a seed word already holds belongs to the port or register it came
    # from, and naming it twice is what a word must never do.
    seeded = {bit for name, bits in grown.items()
              if not name.startswith(("bus", "add")) for bit in bits}
    made = {}
    for name, bits in grown.items():
        if not name.startswith(("bus", "add")):
            continue
        keep = [bit for bit in bits if bit not in seeded]
        if len(keep) > 1:
            made[name] = keep
    regs = regs + [(name, bits) for name, bits in sorted(made.items())
                   if name not in have]
    # The grouping this run recovered, written down so it can be scored.
    # It exists nowhere else: the words are built here out of what several
    # passes proved and are spent immediately on the text, so a reader of the
    # output can see them and a scorer cannot.
    if out:
        # Every bit the transcription named, not only the ones that became a
        # bus: an array is named `mem0[3][2]` and belongs to no bus at all, so
        # reading the buses alone left open8's sixty-four register file bits
        # out and scored them as words of one apiece.
        seen = dict(spoken)
        for word, bits in regs:
            for index, bit in enumerate(bits):
                seen[bit] = "%s[%d]" % (word, index)
        # A family read across the datapath is declared as an array and every
        # chain in it is one bit of a stage, so the word a register belongs to
        # is the stage and not the chain it was found in. Without this des
        # records 48 chains of 16 under names its own RTL never uses, and is
        # scored on a grouping it did not emit.
        for index, (prefix, slot) in seat.items():
            for depth, flop in enumerate(chains[index]["flops"]):
                cell = module["cells"].get(flop)
                if cell and match.FLOP in cell["type"]:
                    seen[cell["connections"]["Q"][0]] = \
                        "%sq[%d][%d]" % (prefix, depth, slot)
        holds = {}
        for name, cell in module["cells"].items():
            if match.FLOP not in cell["type"]:
                continue
            where = seen.get(cell["connections"]["Q"][0])
            if where:
                holds[name] = where
        json.dump(holds, open(out.replace(".sv", "_words.json"), "w"), indent=1)
    print("cones")
    if regs:
        show = ["%s[%d:0]" % (n, len(b) - 1) for n, b in regs[:12]]
        print("  %d words to read a cone against: %s%s"
              % (len(regs), ", ".join(show),
                 ", ..." if len(regs) > len(show) else ""))
    # A chain and a counter were proved against a template that already says
    # what they load: a shift chain takes {serial in, q}, a counter takes
    # q + step. Asking a cone the same question again answers it a second
    # time at the price of proving it, and eighty seven of the corpus's
    # hundred and eleven load cones were that. A bank is the exception and
    # the reason the cones are drawn: its template says only q <= d, and what
    # d is is exactly what is not yet known.
    said = {"chain%d_d" % index for index in chains}
    said |= {"state%d_d" % index for index in states}
    cones = lift_cones(netlist, regions, workdir)
    paths = lift_datapaths(netlist, regions, workdir, set(cones) | said, regs,
                           {n: b for n, b in made.items()
                            if n.startswith("bus")})
    selects = lift_selects(netlist, regions, workdir,
                           set(cones) | set(paths) | said, regs)
    # A load is written back now, which it was not: the counter broke when
    # one was, and that was the same folded-net fault that stopped
    # hd_8b10b's tables — a net the block named was folded into the line
    # reading it and the block then named nothing.
    #
    # A bank is the exception and stays held. Its template already writes
    # `q <= d` and wires every bit of `d` itself, so a form written beside
    # it drives those nets twice: mroblesh comes back NOT EQUIVALENT, which
    # is a disproof and not a timeout. A chain or a counter that reached
    # here did not lift at all, so nothing else is speaking for its nets.
    loads = {r["output"] for r in regions if r.get("outputs")}
    banks_ = {name for name in loads if name.startswith("bank")}
    held = {name: hit for name, hit in
            list(paths.items()) + list(selects.items()) if name in banks_}
    paths = {n: h for n, h in paths.items() if n not in banks_}
    selects = {n: h for n, h in selects.items() if n not in banks_}
    if held:
        print("  %d bank loads proved and not written back: %s"
              % (len(held), ", ".join(sorted(held))))
    got = sorted((set(paths) | set(selects)) & loads)
    if got:
        print("  %d loads written back: %s" % (len(got), ", ".join(got)))
    roles.update(realign(module, module["ports"], chains, states, banks,
                         names, paths))
    print("  %d of %d cones lifted, %d of them as a case"
          % (len(cones) + len(paths) + len(selects),
             sum(1 for r in regions if r["kind"] == "cone"), len(selects)))
    if out:
        write_rtl(netlist, regions, chains, states, banks, cones, paths, selects,
                  names, roles, across, seat, out, held, made)
        gold = netlist_as_gold(netlist, workdir)
        verdict = prove_candidate(open(out).read().replace(
            "module %s(" % list(json.load(open(netlist))["modules"])[0],
            "module cand("), gold, workdir, "rtl")
        print("rtl -> %s" % out)
        print("  whole module vs recovered netlist: %s" % verdict)
        # Giving up is not a disproof, and it is not a pass either. Running a
        # design for two thousand cycles and seeing it agree says nothing
        # about the cycle after: db_MAC and kwr_lfsr once agreed for every one
        # of them and were disproved outright the moment they were asked. So
        # a module that does not prove is reported unverified rather than
        # counted, and only an answer fails the run.
        if verdict.startswith(("NOT EQUIVALENT", "no miter")):
            return 1
        if not verdict.startswith("PROVEN"):
            # Induction did not converge, which is not an answer. Ask the
            # smaller question it will answer rather than record nothing.
            bounded = match.to_depth(gold, "%s/rtl.json" % workdir,
                                     workdir, "rtl")
            if bounded and bounded.startswith("NOT"):
                print("  whole module: %s" % bounded)
                return 1
            print("  whole module: %s"
                  % (bounded or "unverified, and a simulation would not be one"))
        return 0


def netlist_as_gold(netlist, workdir):
    """The recovered netlist, renamed so it can stand as the reference"""
    top = list(json.load(open(netlist))["modules"])[0]
    match.yosys(["read_json %s" % netlist, "rename %s gold" % top,
                 "write_json %s/rtl_gold.json" % workdir],
                "%s/rtl_gold.ys" % workdir)
    return "%s/rtl_gold.json" % workdir


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
