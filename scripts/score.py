# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Scores a recovered grouping of registers against the one its author wrote

Reproduces the benchmark half of the grouping table: `present` 1.000 and
`sha-3` 0.575 exactly, `des` 0.023. `open8` comes back 0.675 against a
recorded 0.778 and says why itself — 143 of its 208 flops carry a name this
can parse, so it is scoring a smaller set than the record was taken over.
"""

import collections
import json
import re
import sys

# How a synthesised netlist spells a register bit: the name its author gave
# the word, `_reg` where the tool marked it one, and the bit's own index with
# the brackets turned into underscores. A register one bit wide has no index
# at all, which is not a name that failed to parse but a word of one — a
# third of open8's flops are those, and demanding an index scored it over
# two thirds of itself.
# A register file carries two of those, an entry and a bit — `Regfile_reg_2__2_`
# — and the word it belongs to is the file, which is how M16 declares it.
BIT = re.compile(r"^(.*?)_reg(?:_reg)?((?:_\d+_)*)$")
INSTANCE = re.compile(r"^\s*(\S+)\s+(\S+)\s*\(", re.M)
FLOP = "DFF"


def author(netlist):
    """Which word each flop belongs to, read off the name the netlist kept.

    Design Compiler leaves the register's own name on every bit it made, so a
    benchmark carries its own answer and nothing has to be inferred. A bit
    whose name does not parse is its own word, which is what an unplaced bit
    would be scored as anyway.
    """
    out = {}
    for kind, name in INSTANCE.findall(open(netlist).read()):
        got = BIT.match(name)
        if got and got.group(1):
            out[name] = got.group(1)
    return out


def recovered(words, cells):
    """Which word each flop belongs to as the recovery has it.

    Read from what the lift wrote down rather than from the regions it drew:
    a region is where a proof was attempted, and the word a register ends up
    in is settled later by several passes together. Scored on regions,
    `present` reads as 148 words where the RTL declares two.

    A flop no word claimed is a word of one. Counting them is the honest
    comparison where a tool leaves flops out: placing every bit and placing
    half of them are not the same achievement, and a metric that ignores the
    ones left behind rewards the tool that gives up more often.
    """
    out, at = {}, 0
    for flop, where in words.items():
        out[flop] = where.split("[")[0]
    for flop in cells:
        if flop not in out:
            out[flop] = "alone%d" % at
            at += 1
    return out


def rand(first, second):
    """Adjusted Rand Index over the items both groupings speak about.

    Chance corrected, so a grouping that splits everything and one that merges
    everything both score zero rather than looking respectable. Purity is not
    used anywhere here: it rewards splitting, and would call a design of
    nothing but one bit registers perfectly recovered.
    """
    shared = sorted(set(first) & set(second))
    if len(shared) < 2:
        return None
    table = collections.Counter((first[one], second[one]) for one in shared)
    rows = collections.Counter(first[one] for one in shared)
    cols = collections.Counter(second[one] for one in shared)
    pair = lambda n: n * (n - 1) // 2
    total = pair(len(shared))
    inside = sum(pair(n) for n in table.values())
    across = sum(pair(n) for n in rows.values())
    down = sum(pair(n) for n in cols.values())
    want = across * down / total
    most = (across + down) / 2
    return None if most == want else (inside - want) / (most - want)


def pair_names(netlist, generic):
    """The author's name for each flop of the generic netlist, by its Q net.

    Synthesis renames every cell, so the two sides cannot be matched by cell
    name. They can be matched by the net a flop drives: the layout read back
    carries the original instance's own output pin as an alias of that net,
    `regKey_reg_reg_62_.Q` beside `kupd[42]`, so the author's name for a bit
    is sitting on the bit itself.
    """
    module = list(json.load(open(generic))["modules"].values())[0]
    spell = {}
    for name, spec in module.get("netnames", {}).items():
        for index, bit in enumerate(spec["bits"]):
            spell.setdefault(bit, []).append(
                name if len(spec["bits"]) == 1 else "%s[%d]" % (name, index))
    out = {}
    for name, cell in module["cells"].items():
        if FLOP not in cell["type"]:
            continue
        for one in spell.get(cell["connections"]["Q"][0], []):
            head = one.split(".")[0] if "." in one else one
            out.setdefault(name, []).append(head)
    return out


def main(netlist, generic, words_path):
    module = list(json.load(open(generic))["modules"].values())[0]
    flops = [n for n, c in module["cells"].items() if FLOP in c["type"]]
    theirs = author(netlist)
    named = pair_names(netlist, generic)

    # A flop of the generic netlist is the author's register bit whose name
    # one of its nets still carries.
    mine = recovered(json.load(open(words_path)), flops)
    first, second = {}, {}
    for flop in flops:
        for one in named.get(flop, []):
            if one in theirs:
                first[flop] = theirs[one]
                second[flop] = mine[flop]
                break
    got = rand(first, second)
    print("%s: %d flops, %d matched to a name the author wrote"
          % (words_path, len(flops), len(first)))
    print("  author %d words, recovered %d words, ARI %s"
          % (len(set(first.values())), len(set(second.values())),
             "n/a" if got is None else "%.3f" % got))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: score.py <netlist.v> <generic.json> <words.json>")
    sys.exit(main(*sys.argv[1:]))
