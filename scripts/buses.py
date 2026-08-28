# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Groups a netlist's combinational nets into the words they are bits of"""

import collections
import json
import os
import subprocess
import sys

# How many bits a proposed word needs before it is taken for one. Two nets
# driven alike is the ordinary coincidence of a gate basis and says nothing;
# a design of two bit words would be seated almost entirely by chance.
FLOOR = int(os.environ.get("MINOS_WORD", "4"))

YOSYS = os.environ.get("YOSYS", "").split()

ROUNDS = 12


def real(bit):
    """Whether a bit is a net at all, rather than a constant or an undriven x"""
    return isinstance(bit, int) and bit > 1


def load(path):
    module = list(json.load(open(path))["modules"].values())[0]
    driver = {}
    for name, cell in module["cells"].items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                for bit in bits:
                    driver[bit] = name
    return module, driver


def named(module):
    """The plainest name each bit carries, a cut's name for it winning"""
    out = collections.defaultdict(list)
    for name, spec in module.get("netnames", {}).items():
        for index, bit in enumerate(spec["bits"]):
            out[bit].append(name if len(spec["bits"]) == 1
                            else "%s[%d]" % (name, index))
    return {bit: min(names, key=lambda n: ("." in n, len(n), n))
            for bit, names in out.items()}


def adders(netlist, workdir):
    """The words an adder's carry chain spells out, in the order it spells them.

    A rule keyed on what a net is built from cuts a bus in half, because
    synthesis pushes an inverter wherever it is locally cheaper and the bits
    of one word stop looking alike. A carry chain does not have that problem:
    it is a chain because each stage hands the next its carry, and that hand
    is the bit order itself, least significant first. Nothing about it depends
    on how the stage was mapped.

    Read off a copy of the design with adders extracted, and carried back to
    the design itself by net name, since the extraction renumbers the bits but
    keeps what they are called.
    """
    if not YOSYS:
        return {}
    out = "%s/fa.json" % workdir
    run = subprocess.run(
        YOSYS + ["-qp", "read_json %s; simplemap; extract_fa; write_json %s"
                 % (netlist, out)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if run.returncode or not os.path.exists(out):
        return {}
    module = list(json.load(open(out))["modules"].values())[0]
    home = named(module)
    # Extraction renumbers the bits and keeps what they are called, so the
    # chain is read there and carried back here by name.
    seats = {name: bit for bit, name
             in named(list(json.load(open(netlist))["modules"].values())[0]).items()}
    cells = {name: cell for name, cell in module["cells"].items()
             if cell["type"] == "$fa"}

    # The carry does not always arrive on the carry pin, and does not always
    # arrive uninverted: the three inputs of a full adder are symmetric, so a
    # mapper is free to put the carry on any of them, and ABC builds adders
    # with inverted carries. Followed the strict way the chains come out three
    # wide; followed this way open8's runs to fourteen.
    skip = {name: cell["connections"]["A"][0]
            for name, cell in module["cells"].items()
            if cell["type"] == "$_NOT_"}
    takes = collections.defaultdict(list)
    for name, cell in cells.items():
        for port in ("A", "B", "C"):
            takes[cell["connections"][port][0]].append((name, port))
    after = {}
    for name, cell in cells.items():
        out_bit = cell["connections"]["X"][0]
        reach = list(takes.get(out_bit, []))
        for gate, source in skip.items():
            if source == out_bit:
                reach += takes.get(
                    module["cells"][gate]["connections"]["Y"][0], [])
        reach = [one for one in reach if one[0] != name]
        if len(reach) == 1:
            after[name] = reach[0]

    words, taken = {}, set()
    heads = [name for name in cells
             if name not in {one for one, _ in after.values()}]
    for head in sorted(heads):
        chain, carry, step = [], [], head
        while step and step not in taken:
            taken.add(step)
            chain.append(step)
            got = after.get(step)
            if not got:
                break
            step, port = got
            carry.append(port)
        if len(chain) < FLOOR:
            continue
        # Y is the sum whichever input carried, since A ^ B ^ C does not care
        # which is which. The two operands do, so they are only claimed where
        # every stage agrees on which pin each of them came in on.
        rest = [[p for p in "ABC" if p != one] for one in carry] or None
        spans = [("sum", ["Y"] * len(chain))]
        if rest and len({tuple(one) for one in rest}) == 1:
            first, second = rest[0]
            spans += [("one", [first] * len(chain)),
                      ("two", [second] * len(chain))]
        for role, ports in spans:
            bits = [cells[one]["connections"][port][0]
                    for one, port in zip(chain, ports)]
            spelt = [home.get(bit) for bit in bits]
            if any(one is None for one in spelt) or len(set(spelt)) != len(spelt):
                continue
            back = [seats.get(one) for one in spelt]
            if any(one is None for one in back) or len(set(back)) != len(back):
                continue
            words["add%d_%s" % (len(words), role)] = back
    return words


def seeds(module, regions):
    """The words already known, which is what the propagation starts from.

    Three kinds are known before anything combinational is: a port of the top
    module, which the layout itself declared wide; a region's registers, which
    an earlier pass proved belong together and put in order; and any net the
    netlist still carries a multi-bit name for. Nothing here is proposed — it
    is read off work already done.
    """
    words = {}
    for name, spec in module["ports"].items():
        bits = [b for b in spec["bits"] if real(b)]
        if len(bits) > 1:
            words[name] = bits
    for index, region in enumerate(regions):
        # Only the kinds whose registers are a word. A cone's registers are
        # what it reads, which for open8 is a hundred and forty five flops
        # spread over every word the design has.
        if region["kind"] not in ("chain", "bank", "state"):
            continue
        bits = []
        for flop in region.get("registers", []):
            cell = module["cells"].get(flop)
            out = cell and cell["connections"].get("Q")
            if out and real(out[0]):
                bits.append(out[0])
        if len(bits) > 1:
            words.setdefault("%s%d" % (region["kind"], index), bits)
    for name, spec in module.get("netnames", {}).items():
        bits = [b for b in spec["bits"] if real(b)]
        if len(bits) > 1:
            words.setdefault(name, bits)
    return words


def seating(words):
    seat = {}
    for name, bits in words.items():
        for index, bit in enumerate(bits):
            seat.setdefault(bit, (name, index))
    return seat


def signatures(module, driver, seat):
    """Every unseated net, filed under what would make it a bit of a word.

    A net is a bit of a word when the net beside it is the same function of
    the net beside its operand: bit three of a sum is an adder reading bit
    three of each operand, and so is bit four. So a net is described by the
    gate that drives it and the words that gate reads, and two nets sharing
    that description differ only in which bit of those words they took. What
    the gate also reads and we cannot name is counted but not identified,
    since a word is not disqualified by being masked or enabled.
    """
    cells = module["cells"]
    filed = collections.defaultdict(dict)
    for bit, name in driver.items():
        if bit in seat or not real(bit):
            continue
        cell = cells[name]
        reads = [b for port, bits in sorted(cell["connections"].items())
                 for b in bits
                 if cell["port_directions"].get(port) == "input" and real(b)]
        known = [seat[b] for b in reads if b in seat]
        if not known:
            continue
        places = {index for _, index in known}
        if len(places) != 1:
            continue
        key = (cell["type"], tuple(sorted(w for w, _ in known)),
               len(reads) - len(known))
        filed[key].setdefault(places.pop(), bit)
    return filed


def propagate(module, driver, words):
    """Words grown from the known ones until no new one can be read off.

    A word found this round is a seed for the next, which is what carries the
    grouping down a datapath rather than one gate into it. Bit order is not
    guessed: a net inherits the seat of the net it was made from, so a word
    comes out ordered by the word it came from. The seats it takes need not
    be all of them or even run consecutively — a cipher's permutation layer
    deliberately scatters them — so the order is kept and the gaps closed.
    """
    seat = seating(words)
    found = 0
    for _ in range(ROUNDS):
        fresh = 0
        for key, places in sorted(signatures(module, driver, seat).items(),
                                  key=lambda kv: -len(kv[1])):
            bits = [places[at] for at in sorted(places)]
            if len(bits) < FLOOR or any(b in seat for b in bits):
                continue
            name = "bus%d" % found
            words[name] = bits
            for index, bit in enumerate(bits):
                seat[bit] = (name, index)
            found += 1
            fresh += len(bits)
        if not fresh:
            break
    return words, seat


def main(netlist, regions_path=None, out=None):
    module, driver = load(netlist)
    regions = json.load(open(regions_path)) if regions_path else []
    known = seeds(module, regions)
    words, seat = propagate(module, driver, dict(known))
    nets = {bit for bit in driver if real(bit)}
    made = {name: bits for name, bits in words.items() if name not in known}

    print("%s" % netlist)
    print("  %d driven nets, %d of them already in a word"
          % (len(nets), len(nets & set(seating(known)))))
    widths = collections.Counter(len(bits) for bits in made.values())
    print("  %d words recovered, %d nets seated (%d%% of the netlist)"
          % (len(made), len(nets & set(seat)),
             100 * len(nets & set(seat)) // max(len(nets), 1)))
    for width, count in sorted(widths.items(), reverse=True):
        print("    %3d wide  %d" % (width, count))

    if out:
        json.dump({name: bits for name, bits in words.items()},
                  open(out, "w"), indent=1)
        print("  %d words -> %s" % (len(words), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
