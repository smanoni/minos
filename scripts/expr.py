# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Rewrites the gates a template did not recognise as Verilog expressions"""

import os
import re
import json
import collections

FLOP = "DFF"
LATCH = "DLATCH"

# How tightly each form binds, so that folding one gate into another can tell
# when the result needs brackets round it. A gate's operands are ranked by the
# operator they belong to and not by the brackets already in the form: the ~()
# of a nand keeps the result together but does nothing for what is inside it,
# where a conditional would still be pulled apart by the &.
ATOM, NOT, AND, XOR, OR, MUX = 5, 4, 3, 2, 1, 0

GATES = {
    "$_AND_": ("%(A)s & %(B)s", AND, AND),
    "$_OR_": ("%(A)s | %(B)s", OR, OR),
    "$_XOR_": ("%(A)s ^ %(B)s", XOR, XOR),
    "$_XNOR_": ("~(%(A)s ^ %(B)s)", NOT, XOR),
    "$_NAND_": ("~(%(A)s & %(B)s)", NOT, AND),
    "$_NOR_": ("~(%(A)s | %(B)s)", NOT, OR),
    "$_ANDNOT_": ("%(A)s & ~%(B)s", AND, {"A": AND, "B": NOT}),
    "$_ORNOT_": ("%(A)s | ~%(B)s", OR, {"A": OR, "B": NOT}),
    "$_NOT_": ("~%(A)s", NOT, NOT),
    "$_MUX_": ("%(S)s ? %(B)s : %(A)s", MUX, OR),
    "$_NMUX_": ("~(%(S)s ? %(B)s : %(A)s)", NOT, OR),
}


def binding(need, port):
    """How tightly one operand of a form has to bind"""
    return need[port] if isinstance(need, dict) else need


TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\[\d+\])?")

# What a shape is called once a row of registers is found sharing it. Synthesis
# splits a word into bits and scatters them, so what was one line of the design
# comes back as a register apiece; a row that steps the same way is that word
# put back together, and the name says which of a few things it was doing.
IDIOMS = [
    ("%s | %s", "set"),
    ("%s ^ %s", "toggle"),
    ("%s ? %s : %s", "hold"),
    ("%s & %s", "mask"),
]

# What a row reads each of its operands for. A net carries no name of its own
# out of a netlist, but what a row does with it is one: the word a set of flags
# is set from, the word a row of toggles steps by. Naming them together also
# says they belong together, which a column of unrelated numbers does not.
ROLES = {
    "%s | %s": ["set"],
    "%s ^ %s": ["step"],
    "%s & %s": ["mask"],
    "%s ? %s : %s": ["en", "next"],
}

NUMBERED = re.compile(r"^n(\d+)$")
INDEXED = re.compile(r"^(\w+)\[(\d+)\]$")


def gathered(col):
    """A column written as the word it is, where it is one word in order"""
    got = [INDEXED.match(one) for one in col]
    if not all(got) or len({m.group(1) for m in got}) != 1:
        return None
    if [int(m.group(2)) for m in got] != list(range(len(col))):
        return None
    return got[0].group(1)


def outline(text):
    """An expression with its nets blanked, so two of a kind look alike"""
    return TERM.sub("%s", text)


def asked(shape):
    """How far into a shape a conditional reads its question.

    Everything up to the last question mark can be part of what one is asking,
    and a conditional takes a whole word as one answer: gathered bit by bit it
    would fire on any bit being set rather than on each in turn. The question
    is not always the first thing in the shape either, since a conditional can
    sit inside a larger term, so the count runs to the last one there is.
    """
    reach, slot = -1, -1
    for piece in re.finditer(r"%s|\?", shape):
        if piece.group(0) == "%s":
            slot += 1
        else:
            reach = slot
    return reach


def columns(shape, rows, names):
    """Each operand of a shared shape, as it reads for the row as a whole.

    An operand every register takes from the same net is that net; one each
    takes from itself is the row itself; anything else is the bits gathered
    into a word, in the order the row is in.
    """
    grid = [TERM.findall(text) for text in rows]
    if len({len(r) for r in grid}) != 1 or "1'b" in shape:
        return None
    reach, out, wide = asked(shape), [], len(rows)
    for slot, col in enumerate(zip(*grid)):
        if len(set(col)) == 1:
            # One net beside a word does not spread itself across it: Verilog
            # pads it with zeroes instead, which would leave every bit but the
            # lowest answering to nothing. So it is written out once per bit.
            # The question a conditional asks is the exception, being asked
            # once for the whole word rather than of each bit in turn.
            out.append(col[0] if slot <= reach
                       else "{%d{%s}}" % (wide, col[0]))
        elif slot <= reach:
            return None
        elif list(col) == names:
            out.append(None)
        else:
            whole = gathered(col)
            out.append(whole or "{%s}" % ", ".join(reversed(col)))
    return out


WIDTH = int(os.environ.get("MINOS_WIDTH", "88"))


def splits(text, op):
    """Where an operator falls in a term, outside every bracket of it"""
    depth, out = 0, []
    for at, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == op:
            out.append(at)
    return out


def conditional(text):
    """A term's question, its yes and its no, where the term is a conditional.

    The colon that answers a question is not the only one a term can hold: a
    bit range carries one too, and so does every conditional nested inside the
    yes arm of this one. Only the one that closes this question is the cut.
    """
    depth = nested = 0
    ask = None
    for at, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth:
            continue
        elif ch == "?":
            if ask is None:
                ask = at
            else:
                nested += 1
        elif ch == ":" and ask is not None:
            if nested:
                nested -= 1
            else:
                return (text[:ask].rstrip(), text[ask + 1:at].strip(),
                        text[at + 1:].strip())
    return None


def enclosed(text, open_, close):
    """Whether a term is one bracket round the whole of what it contains"""
    if text[:1] != open_ or text[-1:] != close:
        return False
    depth = 0
    for at, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return at == len(text) - 1
    return False


def packed(head, pad, parts, tail):
    """A list written out as many to a line as will fit on one"""
    out, line, alone = [], head, True
    for at, piece in enumerate(parts):
        end = tail if at == len(parts) - 1 else ", "
        if not alone and len(line) + len(piece) + len(end) > WIDTH:
            out.append(line.rstrip())
            line, alone = pad, True
        line += piece + end
        alone = False
    return out + [line.rstrip()]


def wrap(head, text, tail=""):
    """A term written over as many lines as it takes to be read.

    A term half a screen wide says nothing the same term broken at its loosest
    operator does not, and folding gates into their readers makes plenty of
    them. The break goes where the term binds least tightly, so each piece is
    one whole operand, and the operator stays at the front of the line where
    it can be seen against the ones above it.
    """
    if len(head) + len(text) + len(tail) <= WIDTH:
        return [head + text + tail]
    pad = " " * len(head)
    # A term wrapped in brackets holds nothing to break at from outside them,
    # so the brackets are opened and what is inside is broken instead.
    for lead in ("~(", "("):
        if text[:len(lead)] == lead and enclosed(text[len(lead) - 1:], "(", ")"):
            return wrap(head + lead, text[len(lead):-1], ")" + tail)
    if enclosed(text, "{", "}"):
        cuts = splits(text[1:-1], ",")
        if cuts:
            inner = text[1:-1]
            parts = [inner[:cuts[0]].strip()]
            parts += [inner[a + 1:b].strip()
                      for a, b in zip(cuts, cuts[1:] + [len(inner)])]
            return packed(head + "{", pad + " ", parts, "}" + tail)
    got = conditional(text)
    if got:
        ask, yes, no = got
        return (wrap(head, ask) + wrap(pad + "? ", yes)
                + wrap(pad + ": ", no, tail))
    for op in "|^&":
        cuts = splits(text, op)
        if not cuts:
            continue
        pieces = [text[:cuts[0]].rstrip()]
        pieces += [text[a:b].rstrip()
                   for a, b in zip(cuts, cuts[1:] + [len(text)])]
        out = []
        for at, piece in enumerate(pieces):
            last = tail if at == len(pieces) - 1 else ""
            if at == 0:
                out += wrap(head, piece, last)
            else:
                out += wrap(pad + piece[0] + " ", piece[1:].strip(), last)
        return out
    return [head + text + tail]


def holding(target, data):
    """The enable a register holds itself on, and what it takes otherwise.

    A register that can keep its value does it by choosing between what comes
    next and its own output, and written that way the choice reads as data.
    Written as the condition it is, the enable is visible and the register is
    only spoken of where it actually moves.
    """
    got = conditional(data)
    if not got:
        return None
    ask, yes, no = got
    if no == target:
        return ask, yes
    if yes == target:
        return ("!" + ask if outline(ask) == "%s" else "!(%s)" % ask), no
    return None


def moves(pad, target, data):
    """What one register takes, written as the enable and the value it is"""
    got = holding(target, data)
    if got is None:
        return wrap("%s%s <= " % (pad, target), data, ";")
    ask, value = got
    return (wrap("%sif (" % pad, ask, ")")
            + wrap("%s  %s <= " % (pad, target), value, ";"))


def flop_kind(kind):
    """Reads a generic flop name as edge, reset polarity and reset value.

    The names run $_DFF_<edge>_ and $_DFF_<edge><reset><value>_, so the letters
    after the prefix say everything the always block needs.
    """
    tag = kind[len("$_DFF_"):].rstrip("_")
    edge = "posedge" if tag[:1] == "P" else "negedge"
    if len(tag) < 3:
        return edge, None, None
    return edge, "negedge" if tag[1] == "N" else "posedge", tag[2]


def latch_kind(kind):
    """Reads a generic latch name as the level its enable lets data through on

    The names run $_DLATCH_<level>_, and the variants that also carry a set or
    a reset are not read here: nothing that writes them has come up, and a
    latch written wrong is worse than a latch refused.
    """
    tag = kind[len("$_DLATCH_"):].rstrip("_")
    return "" if tag == "P" else "!" if tag == "N" else None


def load(path):
    module = list(json.load(open(path))["modules"].values())[0]
    cells = module["cells"]
    driver, fanout = {}, collections.Counter()
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            for bit in bits:
                if cell["port_directions"].get(port) == "output":
                    driver[bit] = (name, port)
                else:
                    fanout[bit] += 1
    return module, cells, driver, fanout


def net_name(bit):
    return "n%s" % bit


BASE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

# How many items a net may touch and still say something about which of them
# belong together. A clock, a reset or a global enable reaches most of a
# design and would pull the whole of it into one group.
SPREAD = int(os.environ.get("MINOS_SPREAD", "12"))


def sections(items, rounds=30):
    """Which group each item belongs to, by what it has to do with the rest.

    A design flattened by synthesis is one long module, but it is not one long
    thought: most of its nets are read only by their neighbours. Each item
    joins whichever group it already shares most nets with, over and over
    until nothing moves, which finds those neighbourhoods without being told
    how many to look for. A net touching a large part of the design says
    nothing about who belongs with whom and is not allowed to vote.

    Laid out a group at a time, a net read only inside one group is carried
    only while that group is being read, and can be forgotten at its end.
    About two thirds of them are, which is the whole of why this is worth
    doing.
    """
    touch = {}
    for at, (lines, head, reads) in enumerate(items):
        for name in reads | ({head} if head is not None else set()):
            touch.setdefault(name, set()).add(at)
    label = list(range(len(items)))
    for _ in range(rounds):
        moved = 0
        for at, (lines, head, reads) in enumerate(items):
            votes = {}
            for name in reads:
                if name not in touch or len(touch[name]) > SPREAD:
                    continue
                for other in touch[name]:
                    if other != at:
                        votes[label[other]] = votes.get(label[other], 0) + 1
            if votes:
                best = max(sorted(votes), key=lambda g: votes[g])
                if best != label[at]:
                    label[at] = best
                    moved += 1
        if not moved:
            break
    return label


def knots(items, label):
    """The same grouping, with any groups that wait on each other folded in.

    Nearness is not order: two groups can each read something the other
    defines. Laid out one whole group at a time there is then no order that
    puts every net before the lines reading it, and the reader meets a number
    with nothing said about it yet. Folded together they are one section and
    an order inside it exists again, at the price of a section large enough to
    hold both. Which of those two prices is lower is not the same design to
    design, so both groupings are laid out and the cheaper is kept.
    """
    where = {}
    for at, (lines, head, reads) in enumerate(items):
        if head is not None:
            where[head] = label[at]
    edge = {}
    for one in set(label):
        edge[one] = set()
    for at, (lines, head, reads) in enumerate(items):
        for name in reads:
            if name in where and where[name] != label[at]:
                edge[label[at]].add(where[name])
    # Tarjan, kept to a stack of its own so a deep chain of sections cannot
    # run the interpreter out of frames.
    index, low, stack, on, comp = {}, {}, [], set(), {}
    for root in sorted(edge):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on.add(root)
        work = [(root, iter(sorted(edge[root])))]
        while work:
            node, kids = work[-1]
            for kid in kids:
                if kid not in index:
                    index[kid] = low[kid] = len(index)
                    stack.append(kid)
                    on.add(kid)
                    work.append((kid, iter(sorted(edge[kid]))))
                    break
                if kid in on:
                    low[node] = min(low[node], index[kid])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    while True:
                        one = stack.pop()
                        on.discard(one)
                        comp[one] = node
                        if one == node:
                            break
    return [comp[one] for one in label]


def carried(order):
    """What an order asks of a reader, per line of the module.

    A net written as a number means nothing on its own, so a reader meeting
    one holds it until the last place it is read. Held from where it is
    defined they at least know what they are holding; met before that, they
    are carrying a token nothing has yet attached a meaning to, which is
    worse, and counts twice. This is the whole of what the layout is trying to
    make small, and it is here so that a run can say what it achieved rather
    than the claim having to be taken on trust.
    """
    at, first, last, made = 0, {}, {}, {}
    for lines, head, reads in order:
        for name in reads | ({head} if head is not None else set()):
            if NUMBERED.match(name):
                first.setdefault(name, at)
                last[name] = at
        if head is not None:
            made.setdefault(head, at)
        at += len(lines)
    if not at:
        return 0.0
    total = 0
    for name, start in first.items():
        told = made.get(name, start)
        total += max(0, last[name] - max(told, start))
        total += 2 * max(0, min(told, last[name]) - start)
    return total / float(at)


def closing(items, waiting, done):
    """The items in the order that asks least of a reader, greedily.

    Take whatever is ready and closes the most nets while opening the fewest,
    a net being closed when nothing further is waiting on it. Only the nets
    written as a number count: a reader carries n565 until they are told what
    reads it, and carries word1_in0 not at all, its name being what it is for.
    A design with a combinational loop in it has nothing ready at some point,
    and is given whatever is left rather than refused an order.
    """
    def gain(item):
        lines, head, reads = item
        closes = sum(1 for n in reads if n in waiting and n != head
                     and waiting[n] == 1 and NUMBERED.match(n))
        opens = 1 if head is not None and NUMBERED.match(head) else 0
        return (closes - opens, -len(lines))

    left, out = list(items), []
    while left:
        ready = [it for it in left
                 if all(n in done or n not in waiting or n == it[1]
                        for n in it[2])]
        pick = max(ready or left, key=gain)
        for name in pick[2]:
            if name in waiting and name != pick[1]:
                waiting[name] -= 1
        if pick[1] is not None:
            done.add(pick[1])
        out.append(pick)
        left.remove(pick)
    return out


def laid(items, label, waiting):
    """Every item, a group at a time, groups in the order they depend on

    A group is ready once every group defining what it reads is written down.
    Where they wait on each other, the one waiting on least goes, and what it
    reads and has not been told is met early.
    """
    groups = {}
    for at, one in enumerate(label):
        groups.setdefault(one, []).append(items[at])
    where = {}
    for one, members in groups.items():
        for lines, head, reads in members:
            if head is not None:
                where[head] = one
    needs = dict((one, set(where[n] for lines, head, reads in members
                           for n in reads
                           if n in where and where[n] != one))
                 for one, members in groups.items())
    out, done, gone = [], set(), set()
    while len(gone) < len(groups):
        ready = [one for one in sorted(groups)
                 if one not in gone and not (needs[one] - gone)]
        if not ready:
            ready = [min((one for one in sorted(groups) if one not in gone),
                         key=lambda one: len(needs[one] - gone))]
        pick = min(ready, key=lambda one: (len(groups[one]), one))
        out += closing(groups[pick], waiting, done)
        gone.add(pick)
    return out


def demand(items):
    """How many items are still waiting to read each net an item defines"""
    counts = {}
    for lines, head, reads in items:
        if head is not None:
            counts.setdefault(head, 0)
    for lines, head, reads in items:
        for name in reads:
            if name in counts and name != head:
                counts[name] += 1
    return counts


# What makes a group of lines worth a module of its own. Calibrated against
# the corpus's own sources rather than picked: the humans' modules run to a
# median of eighteen lines behind six ports, and the widest interface among
# the large ones is twenty. A group with more ports than that has explained
# nothing by being separate, and one shorter than this has moved lines rather
# than found a structure.
PIECE, PINS, PER_PIN = 8, 24, 2.0

DECL = re.compile(r"^\s*(?:wire|reg)\s*(?:\[(\d+):(\d+)\])?\s*(\w+)\s*[;=]")
DRIVEN = re.compile(r"^\s*(\w+)\s*<=")


def spans(module, wires, proven):
    """How each name was declared: the span to repeat, and how many bits.

    The span is carried as it was written rather than worked out again from
    the count. A one bit region is declared [0:0] and indexed [0], and given
    back as a plain scalar it cannot be indexed at all.
    """
    wide = {}
    for name, spec in module.get("ports", {}).items():
        many = len(spec["bits"])
        wide[name] = ("" if many == 1 else "[%d:0] " % (many - 1), many)
    for line in list(wires) + [one for piece in proven for one in piece]:
        got = DECL.match(line)
        if got:
            hi, lo, name = got.groups()
            wide[name] = (("" if hi is None else "[%s:%s] " % (hi, lo)),
                          1 if hi is None else abs(int(hi) - int(lo)) + 1)
    return wide


def held(lines):
    """The register a block drives, which is what that block defines"""
    for line in lines:
        got = DRIVEN.match(line)
        if got:
            return got.group(1)
    return None


def split(defs, blocks, proven, keep, wires, wide, top, ports=()):
    """The groups worth standing on their own, pulled out into modules.

    Synthesis flattens a design and the hierarchy is the first thing a reader
    misses: the corpus's own sources carry a hundred and five modules where
    this writes twenty-one. Sectioning already finds the neighbourhoods, most
    of whose nets never leave them, and a neighbourhood whose nets mostly stay
    inside is exactly what a module is.

    Not every group is one. A group is taken only if it is long enough to be
    worth naming and narrow enough at its interface to have said something by
    being separate. What it cannot take goes on without it: a net driven from
    two groups at once, or a port it drives only part of, stays where it was
    rather than costing the whole group its module.
    """
    kinds, items, heads = [], [], []
    for lines, name in defs:
        kinds.append(0); items.append(lines)
        heads.append(BASE.match(name).group(0))
    for lines in blocks:
        kinds.append(1); items.append(lines); heads.append(held(lines))
    for lines in proven:
        kinds.append(2); items.append(lines); heads.append(made(lines))
    if not items:
        return defs, blocks, proven, [], wires

    def touches(lines):
        return set(BASE.findall("\n".join(lines)))

    reads = [touches(lines) for lines in items]
    label = sections([(items[at], heads[at], reads[at])
                      for at in range(len(items))])

    where, shared = {}, set()
    for at, name in enumerate(heads):
        if name is None:
            continue
        if name in where and where[name] != label[at]:
            shared.add(name)
        where[name] = label[at]

    members = {}
    for at, one in enumerate(label):
        members.setdefault(one, []).append(at)

    # A module's own port can be driven from inside a section, but only if the
    # section drives the whole of it: exporting a port while driving four of
    # its eight bits leaves the other four with two drivers.
    port = set(ports)
    reach = {}
    for at, name in enumerate(heads):
        if name in port:
            reach.setdefault(name, []).append(at)
    entire = {}
    for name, at_list in reach.items():
        if len(set(label[at] for at in at_list)) != 1:
            continue
        bits = set()
        for at in at_list:
            for line in items[at]:
                bits |= {int(b) for b in
                         re.findall(r"\b%s\[(\d+)\]" % re.escape(name), line)}
                if re.search(r"assign\s+%s\s*=" % re.escape(name), line):
                    bits |= set(range(wide.get(name, ("", 1))[1]))
        if bits == set(range(wide.get(name, ("", 1))[1])):
            entire[name] = label[at_list[0]]

    mods, taken, out, sends = [], set(), [], set()
    for one in sorted(members):
        group = [at for at in members[one]
                 if heads[at] not in shared
                 and (heads[at] not in port or entire.get(heads[at]) == one)]
        if not group:
            continue
        drives = set(heads[at] for at in group if heads[at])
        length = sum(len(items[at]) for at in group)
        if length < PIECE:
            continue
        home = set(group)
        elsewhere = set()
        for at in range(len(items)):
            if at not in home:
                elsewhere |= reads[at]
        outs = sorted(n for n in drives if n in elsewhere or n in keep)
        ins = sorted(n for at in group for n in reads[at]
                     if n not in drives and n not in KEYWORD
                     and (n in where or n in wide))
        ins = sorted(set(ins))
        pins = len(ins) + len(outs)
        if pins > PINS or pins == 0 or length < PER_PIN * pins:
            continue

        # What a name was declared as out here it stays inside: a register
        # that leaves the section is its output register, a wire its output.
        was = {}
        for line in list(wires) + [l for at in group for l in items[at]]:
            got = DECL.match(line)
            if got and got.group(3) in drives:
                was[got.group(3)] = "reg" if line.strip().startswith("reg") \
                                    else "wire"

        name = "%s_part%d" % (top, len(mods))
        text = ["module %s(%s);" % (name, ", ".join(ins + outs))]
        for pin in ins:
            text.append("  input %s%s;" % (wide.get(pin, ("", 1))[0], pin))
        for pin in outs:
            text.append("  %s %s%s;"
                        % ("output reg" if was.get(pin) == "reg" else "output",
                           wide.get(pin, ("", 1))[0], pin))
        text.append("")
        for line in wires:
            got = DECL.match(line)
            if got and got.group(3) in drives and got.group(3) not in outs:
                text.append(line)
        for at in group:
            for line in items[at]:
                got = DECL.match(line)
                if got and got.group(3) in outs and "=" not in line:
                    continue
                text.append(line)
        text += ["endmodule", ""]
        mods.append(text)
        sends |= set(outs)
        taken |= home
        out.append(["  %s u_part%d(%s);"
                    % (name, len(mods) - 1,
                       ", ".join(".%s(%s)" % (p, p) for p in ins + outs))])

    if not mods:
        return defs, blocks, proven, [], wires

    gone = set()
    for at in taken:
        for line in items[at]:
            got = DECL.match(line)
            if got and got.group(3) not in sends:
                gone.add(got.group(3))
    left = []
    for line in wires:
        got = DECL.match(line)
        if got and got.group(3) in gone:
            continue
        # A register an instance now drives is a wire out here, and only one
        # an instance drives: a name it merely reads keeps the kind it had.
        if got and got.group(3) in sends and line.strip().startswith("reg"):
            line = line.replace("reg", "wire", 1)
        left.append(line)
    for pin in sorted(sends):
        if not any(DECL.match(l) and DECL.match(l).group(3) == pin
                   for l in left) and pin not in set(ports):
            left.append("  wire %s%s;" % (wide.get(pin, ("", 1))[0], pin))

    kept = {0: [], 1: [], 2: []}
    for at in range(len(items)):
        if at in taken:
            continue
        kept[kinds[at]].append(at)
    return ([defs[at] for at in kept[0]],
            [items[at] for at in kept[1]] + out,
            [items[at] for at in kept[2]], mods, left)


def made(lines):
    """The net a recovered region drives, which is what it defines"""
    for line in lines:
        got = DRIVEN.match(line) or re.match(r"^\s*assign\s+(\w+)", line)
        if got:
            return got.group(1)
    for line in lines:
        got = DECL.match(line)
        if got:
            return got.group(3)
    return None


KEYWORD = set("wire reg assign always posedge negedge if else begin end "
              "module endmodule input output inout".split())


def prune(defs, blocks, proven, keep):
    """Definitions nothing goes on to read, dropped.

    A region lifted into a template leaves the gates that used to feed its
    registers with nothing reading them. The template was matched against the
    netlist and wires itself to the netlist's own nets, so the muxes it stands
    in for are still written out and then never mentioned again: sixteen of
    warmup's seventeen net definitions are that, and the file reads as three
    lines of design followed by sixteen of debris.

    A port is kept however little reads it, being what the module is for.
    Dropping one definition can orphan the one above it, so this runs until
    nothing more falls.
    """
    def touches(lines):
        return set(BASE.findall("\n".join(lines)))

    outer = set(keep)
    for lines in list(blocks) + list(proven):
        outer |= touches(lines)

    kept = list(defs)
    while True:
        live = set(outer)
        for lines, name in kept:
            live |= touches(lines) - {BASE.match(name).group(0)}
        left = [(l, n) for l, n in kept if BASE.match(n).group(0) in live]
        if len(left) == len(kept):
            return kept
        kept = left


# The three shapes a register block is written in here: what it takes, what
# it takes under an enable, and either of those behind a reset.
HEAD = re.compile(r"^(\s*)always @\((.*)\)\s*$")
TAKES = re.compile(r"^(\s*)([A-Za-z_][\w$]*)\s*<=\s")
ASKS = re.compile(r"^(\s*)if \((.*)\)\s*$")
CLEARS = re.compile(r"^(\s*)if \((.*?)\)\s+([A-Za-z_][\w$]*)\s*<=\s*(.*;)\s*$")
OTHERWISE = re.compile(r"^(\s*)else\s*$")


def reading(lines, at):
    """One always block, read into the parts that can be shared with another.

    Only the shapes this file writes are read. A block a template wrote has a
    body of its own and is left alone rather than guessed at.
    """
    got = HEAD.match(lines[at])
    if not got:
        return None
    pad, sens = got.groups()
    # The block is what stands further in than its own header. A blank line
    # is not the boundary: the wires after a block are not always separated
    # from it, and taken in they would be carried inside the begin.
    end = at + 1
    while end < len(lines) and lines[end].strip() and \
            len(lines[end]) - len(lines[end].lstrip()) > len(pad):
        end += 1
    body = lines[at + 1:end]
    if not body:
        return None
    clear, rest = None, body
    got = CLEARS.match(body[0])
    if got:
        if len(body) < 3 or not OTHERWISE.match(body[1]):
            return None
        clear = (got.group(2), "%s <= %s" % (got.group(3), got.group(4)))
        rest = body[2:]
    ask = None
    got = ASKS.match(rest[0]) if rest else None
    if got:
        ask, rest = got.group(2), rest[1:]
    if not rest or not TAKES.match(rest[0]):
        return None
    for line in rest[1:]:
        if TAKES.match(line) or ASKS.match(line) or OTHERWISE.match(line):
            return None
    return end, pad, sens, clear, ask, rest


def folded(lines):
    """Register blocks that say the same thing about when, written once.

    A flattened netlist gives every register a block of its own, so a design
    with seventy of them on one clock reads as seventy always blocks with the
    same header. The sources these came from write one: the header, the reset
    and the enable are said once and the registers that share them are listed
    under it. Grouped by exactly those three, this corpus goes from 297 blocks
    to 126, against the 74 its authors wrote.

    A block is moved to where the last of its group stood, never earlier, so
    nothing it reads is met before it is written.
    """
    read, order = {}, []
    at = 0
    while at < len(lines):
        got = reading(lines, at)
        if got is None:
            at += 1
            continue
        end, pad, sens, clear, ask, rest = got
        key = (pad, sens, clear[0] if clear else None, ask)
        if key not in read:
            read[key] = []
            order.append(key)
        read[key].append((at, end, clear, rest))
        at = end

    out, done = [], {}
    for key in order:
        if len(read[key]) > 1:
            done[read[key][-1][0]] = key
    if not done:
        return lines

    skip = set()
    for key, members in read.items():
        if len(members) > 1:
            for at, end, _, _ in members:
                skip |= set(range(at, end))

    at = 0
    while at < len(lines):
        if at in done:
            out += rewritten(done[at], read[done[at]])
            at += 1
            continue
        if at in skip:
            at += 1
            continue
        out.append(lines[at])
        at += 1
    return out


def rewritten(key, members):
    """One block for the registers that share a header, a reset and an enable"""
    pad, sens, _, ask = key
    out = ["%salways @(%s)" % (pad, sens)]
    step = pad + "  "
    clears = [clear[1] for _, _, clear, _ in members if clear]
    if clears:
        out.append("%sif (%s) begin" % (step, key[2]))
        out += ["%s  %s" % (step, one) for one in clears]
        out.append("%send else%s begin" % (step, " if (%s)" % ask if ask else ""))
    elif ask:
        out.append("%sif (%s) begin" % (step, ask))
    else:
        out[-1] += " begin"
    for _, _, _, rest in members:
        out += rest
    out.append("%send" % (step if clears or ask else pad))
    return out


def arrange(defs, blocks, proven=(), keep=()):
    """The lines put in the order that asks least of a reader.

    A reader meeting n565 has to carry it until the last place it is read, and
    what a module costs to read is how many such numbers it makes them carry
    at once. Written out in the order the nets happened to be found, and with
    every register left to the end, a design of six hundred lines asks for
    forty at a time; written out in groups of what belongs together, each line
    coming as late as it can, the same design asks for twenty.

    What was recovered whole and proven equivalent has always gone first, on
    the grounds that it is the part worth reading; but it reads gates that
    have not been written down yet, and on one design that alone was four
    fifths of everything the reader was carrying. Whether it is better at the
    front or in among the logic it reads is not the same answer for every
    design, so both are laid out, along with both ways of grouping, and the
    one that asks least is kept.
    """
    def touches(lines):
        return set(BASE.findall("\n".join(lines)))

    defs = prune(defs, blocks, proven, keep)
    items = [(lines, BASE.match(name).group(0), touches(lines))
             for lines, name in defs]
    items += [(lines, None, touches(lines)) for lines in blocks]
    stock = [(lines, None, touches(lines)) for lines in proven]

    tries = []
    label = sections(items)
    for choice in (label, knots(items, label)):
        tries.append(stock + laid(items, choice, demand(items)))
    if stock:
        whole = items + stock
        label = sections(whole)
        for choice in (label, knots(whole, label)):
            tries.append(laid(whole, choice, demand(whole)))
    out = []
    for lines, head, reads in min(tries, key=carried):
        # A register standing among the nets that feed it wants a line's space
        # around it, or the block runs on from the wire above and the eye has
        # nothing to catch.
        if head is None and out:
            out += [""]
        out += lines
    return out


def transcribe(path, skip, alias, label=None, proven=()):
    """Wires, assignments and always blocks for every cell not skipped.

    A net that more than one gate reads becomes a wire of its own, so the
    output stays a readable set of assignments rather than one nested term.
    An alias says a net is already driven elsewhere and its gate is dropped;
    a label only gives the net a better name than its number. The output bits
    taken over as words come back too, since those are driven here and need no
    wiring up afterwards.
    """
    module, cells, driver, fanout = load(path)
    wires, defs, blocks = [], [], []
    named, label = dict(alias), dict(label or {})

    def show(bit):
        return label.get(bit, net_name(bit))

    # What has to keep a name of its own. A net two gates read would otherwise
    # be written out twice over, which loses the fact that it is one net; a
    # port has to stay addressable; a clock or a reset is read by an event list
    # that cannot hold an expression; and whatever a replaced region reads is
    # left named for its template to wire itself to, since the template was
    # matched against the netlist's own nets rather than against this text.
    pinned = set(named) | set(label)
    outside = set()
    for spec in module.get("ports", {}).values():
        pinned |= set(spec["bits"])
        if spec["direction"] == "input":
            outside |= set(spec["bits"])
    for name, cell in cells.items():
        for port, bits in cell["connections"].items():
            if cell["port_directions"].get(port) == "output":
                continue
            if name in skip or (FLOP in cell["type"] and port in ("C", "R")):
                pinned |= set(bits)
    # A register clocked from anywhere but a port of the module keeps its data
    # on a wire. Such a clock is made by the design out of its own state, so it
    # arrives a moment after that state moved rather than once everything has
    # settled, and then it matters whether a value is read from a wire or
    # worked out again where it is used. The netlist being compared against
    # reads every register's data off a wire, so this reads it off one too.
    for name, cell in cells.items():
        if name in skip or FLOP not in cell["type"]:
            continue
        if cell["connections"]["C"][0] not in outside:
            pinned |= set(cell["connections"]["D"])

    def folds(bit):
        """The gate to write in place of a net, where it is right to fold one"""
        src = driver.get(bit)
        if src is None or bit in pinned or fanout[bit] != 1:
            return None
        if src[0] in skip or cells[src[0]]["type"] not in GATES:
            return None
        return src[0]

    def expand(name):
        """A gate as an expression, with whatever folds into it folded in"""
        cell = cells[name]
        form, prec, need = GATES[cell["type"]]
        args = {p: build(bits[0], binding(need, p))
                for p, bits in cell["connections"].items()
                if cell["port_directions"].get(p) == "input"}
        return form % args, prec

    def term(bit):
        if bit in named:
            return named[bit], ATOM
        if bit in label:
            return label[bit], ATOM
        if bit in ("0", "1"):
            return "1'b%s" % bit, ATOM
        if bit in ("x", "z"):
            return "1'b0", ATOM
        got = folds(bit)
        return expand(got) if got else (net_name(bit), ATOM)

    def build(bit, want):
        text, prec = term(bit)
        return "(%s)" % text if prec < want else text

    def sensitivity(cell):
        """The clock, reset and held value one register runs on"""
        edge, redge, value = flop_kind(cell["type"])
        rst = (build(cell["connections"]["R"][0], ATOM)
               if redge is not None else None)
        return edge, build(cell["connections"]["C"][0], ATOM), redge, rst, value

    # An output whose every bit is a register is one word of the design, and
    # the port already says what that word is called. Split into a register
    # apiece it reads as eight unrelated blocks that happen to share a clock;
    # put back together, a byte that shifts is visibly a byte that shifts. The
    # bits have to agree on clock and reset for one block to speak for them,
    # but not on what they reset to, which is a constant per bit.
    holder = {}
    for bit, (name, port) in driver.items():
        if name not in skip and FLOP in cells[name]["type"] and port == "Q":
            holder[bit] = name
    def stretch(bits):
        """The longest run of neighbouring bits one always block can speak for"""
        runs, cur, was = [], [], None
        for at, bit in enumerate(bits):
            here = (sensitivity(cells[holder[bit]])[:4]
                    if bit in holder and bit not in named else None)
            if here is None:
                cur, was = [], None
            elif cur and here == was:
                cur.append(at)
            else:
                cur, was = [at], here
                runs.append(cur)
        return max(runs, key=len) if runs else []

    ports_of, taken = {}, set()
    for name, spec in module.get("ports", {}).items():
        bits = spec["bits"]
        if spec["direction"] != "output" or len(bits) < 2:
            continue
        run = stretch(bits)
        if len(run) < 2:
            continue
        # Where the registers are the whole port they are the port, and the
        # design says so by name. Where they are part of it the word stands
        # beside the port and is wired to the bits it speaks for, since a net
        # cannot be a register on some bits and a gate's output on others.
        entire = len(run) == len(bits)
        word = name if entire else "%s_held" % name
        ports_of[word] = (name, run, [cells[holder[bits[i]]] for i in run],
                          entire)
        for slot, at in enumerate(run):
            label[bits[at]] = "%s[%d]" % (word, slot)
            taken.add(bits[at])
    worn = {holder[bit] for bit in taken}

    # A register carrying an output already answers to that output's name,
    # which says more about it than any row it could be put in and is what the
    # rest of the module calls it by, so it is left to stand on its own.
    stock = [(name, cell) for name, cell in cells.items()
             if name not in skip and FLOP in cell["type"]
             and cell["connections"]["Q"][0] not in named
             and cell["connections"]["Q"][0] not in label]
    # Registers stepping the same way on the same clock are one word of the
    # design that synthesis split into bits, so they are put back together and
    # named for what the row does. Reading the shapes needs the expressions,
    # and writing them needs the names, so the expressions are built once to
    # be grouped and again once every member knows what it is called.
    def question(cell, shape):
        """The operands a row has to hold in common to be one row.

        A conditional asks its question of the whole word, so registers that
        step alike but on different conditions are not one word however alike
        they step: they are that many words of one bit. Asking who shares a
        condition splits them into the words they are — an I2C controller
        keeps eight registers loaded together and was coming back as sixty-
        eight loaded apart, because one family had been refused for not
        agreeing rather than split by what it disagreed on.
        """
        reach = asked(shape)
        if reach < 0:
            return ()
        return tuple(TERM.findall(
            build(cell["connections"]["D"][0], MUX))[:reach + 1])

    families = collections.defaultdict(list)
    for name, cell in stock:
        shape = outline(build(cell["connections"]["D"][0], MUX))
        families[sensitivity(cell)
                 + (question(cell, shape), shape)].append((name, cell))
    words, rows = dict(IDIOMS), []
    for key, members in sorted(families.items(),
                               key=lambda f: (-len(f[1]), str(f[0]))):
        if len(members) < 2:
            continue
        bits = [c["connections"]["D"][0] for _, c in members]
        outs = [show(c["connections"]["Q"][0]) for _, c in members]
        if columns(key[-1], [build(b, MUX) for b in bits], outs) is None:
            continue
        word = "%s%d" % (words.get(key[-1], "word"), len(rows))
        for slot, (_, cell) in enumerate(members):
            named[cell["connections"]["Q"][0]] = "%s[%d]" % (word, slot)
        rows.append((word, key, members, False))

    # A register left standing alone joins the word that shares its clock, its
    # reset and the condition it moves on, even though what it takes is not
    # shaped like the rest. Measured against the sources these came from, that
    # is where our grouping was weakest: I2C declares nineteen registers of one
    # bit and we were declaring fifty-nine, urish_simon seven against fifty-
    # eight. The word is then written bit by bit rather than as one row, since
    # its members no longer say the same thing in different places.
    home = collections.defaultdict(list)
    for at, (word, key, members, _) in enumerate(rows):
        home[key[:-1]].append(at)
    for key, members in sorted(families.items(), key=lambda f: str(f[0])):
        if len(members) != 1:
            continue
        cell = members[0][1]
        if cell["connections"]["Q"][0] in named:
            continue
        where = home.get(key[:-1])
        if not where:
            continue
        at = min(where, key=lambda i: len(rows[i][2]))
        word, rkey, held, _ = rows[at]
        named[cell["connections"]["Q"][0]] = "%s[%d]" % (word, len(held))
        rows[at] = (word, rkey, held + list(members), True)

    # Each operand a row reads takes the row's name and the part it plays, so
    # a column of numbers becomes a word with a name. Only a column that is
    # nets all the way across is named: one carrying worked-out terms has no
    # nets to put the name on, and one the whole row shares is a single net
    # that already answers to itself.
    buses = collections.OrderedDict()
    for word, key, members, mixed in rows:
        if mixed:
            continue
        grid = [TERM.findall(build(c["connections"]["D"][0], MUX))
                for _, c in members]
        mine = [show(c["connections"]["Q"][0]) for _, c in members]
        roles, spare, reach = ROLES.get(key[-1], []), 0, asked(key[-1])
        for slot, col in enumerate(zip(*grid)):
            if slot <= reach or len(set(col)) == 1 or list(col) == mine:
                continue
            got = [NUMBERED.match(one) for one in col]
            if not all(got):
                continue
            # A net is written out under its number, so the number is what
            # leads back to the net itself; it is the net as the netlist counts
            # them, not as the text spells them.
            back = [int(m.group(1)) for m in got]
            # Only a net a gate drives can be renamed into a word. One coming
            # off a register is that register, which is declared and assigned
            # under its own name elsewhere, and one coming out of a replaced
            # region belongs to the template that speaks for it.
            if not all(b in driver and driver[b][0] not in skip
                       and FLOP not in cells[driver[b][0]]["type"]
                       for b in back):
                continue
            role = roles[spare] if spare < len(roles) else "in%d" % spare
            spare += 1
            if any(b in label or b in named for b in back):
                continue
            buses["%s_%s" % (word, role)] = len(members)
            for slot2, b in enumerate(back):
                label[b] = "%s_%s[%d]" % (word, role, slot2)
    for name, wide in buses.items():
        wires.append("  wire [%d:0] %s;" % (wide - 1, name))

    written, order = {}, []
    for name, cell in cells.items():
        if name in skip or FLOP in cell["type"] \
                or cell["type"] not in GATES:
            continue
        out = [b for p, bits in cell["connections"].items() for b in bits
               if cell["port_directions"].get(p) == "output"]
        if not out or out[0] in named or out[0] in written:
            continue
        if folds(out[0]):
            continue
        written[out[0]] = name
        order.append(out[0])

    def reads(name):
        """The nets one line reads, looking through whatever folded into it"""
        out, queue = set(), [name]
        while queue:
            cell = cells[queue.pop()]
            for port, bits in cell["connections"].items():
                if cell["port_directions"].get(port) == "output":
                    continue
                for bit in bits:
                    got = folds(bit)
                    if got:
                        queue.append(got)
                    elif bit in written:
                        out.add(bit)
        return out

    # Each net is written where it is defined rather than declared in one place
    # and assigned in another, and the lines are put in the order they build on
    # each other, so a term can be read by reading upwards. A design with a
    # combinational loop in it has no such order, and keeps the one it had.
    def below(bit):
        return iter(sorted(reads(written[bit]), key=str))

    done, active, laid = set(), set(), []
    for start in order:
        if start in done:
            continue
        active.add(start)
        stack = [(start, below(start))]
        while stack:
            bit, kids = stack[-1]
            nxt = next(kids, None)
            if nxt is None:
                stack.pop()
                active.discard(bit)
                done.add(bit)
                laid.append(bit)
            elif nxt not in done and nxt not in active:
                active.add(nxt)
                stack.append((nxt, below(nxt)))

    for target in laid:
        # A bit of a named word is declared with the word, not on its own.
        head = ("  assign %s = " if INDEXED.match(show(target))
                else "  wire %s = ") % show(target)
        defs.append((wrap(head, expand(written[target])[0], ";"), show(target)))

    for word, (port, run, parts, entire) in sorted(ports_of.items()):
        edge, clock, redge, rst = sensitivity(parts[0])[:4]
        wide = len(parts)
        data = "{%s}" % ", ".join(build(c["connections"]["D"][0], MUX)
                                  for c in reversed(parts))
        wires.append("  reg [%d:0] %s;" % (wide - 1, word))
        if not entire:
            defs.append((["  assign %s[%d:%d] = %s;"
                          % (port, run[-1], run[0], word)], port))
        if redge is None:
            blocks.append(["  always @(%s %s)" % (edge, clock)]
                          + moves("    ", word, data))
            continue
        # Each bit resets to a value of its own, so the word resets to those
        # values written out as one, rather than to a word of the same digit.
        start = "".join(flop_kind(c["type"])[2] for c in reversed(parts))
        blocks.append(["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                       "    if (%s%s) %s <= %d'b%s;"
                       % ("!" if redge == "negedge" else "", rst, word, wide,
                          start),
                       "    else"] + moves("      ", word, data))

    for word, key, members, mixed in rows:
        edge, clock, redge, rst, value = key[:5]
        wide = len(members)
        if mixed:
            data = "{%s}" % ", ".join(build(c["connections"]["D"][0], MUX)
                                      for _, c in reversed(members))
        else:
            cols = columns(key[-1], [build(c["connections"]["D"][0], MUX)
                                     for _, c in members],
                           ["%s[%d]" % (word, i) for i in range(wide)])
            data = key[-1] % tuple(word if c is None else c for c in cols)
        wires.append("  reg [%d:0] %s;" % (wide - 1, word))
        if redge is None:
            blocks.append(["  always @(%s %s)" % (edge, clock)]
                          + moves("    ", word, data))
            continue
        blocks.append(["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                       "    if (%s%s) %s <= {%d{1'b%s}};"
                       % ("!" if redge == "negedge" else "", rst, word, wide,
                          value),
                       "    else"] + moves("      ", word, data))

    grouped = {c["connections"]["Q"][0] for _, _, ms, _ in rows for _, c in ms}
    for name, cell in cells.items():
        if name in skip or FLOP not in cell["type"] or name in worn:
            continue
        q = cell["connections"]["Q"][0]
        if q in named and q not in grouped:
            continue
        if q in grouped:
            continue
        edge, redge, value = flop_kind(cell["type"])
        reg = show(q)
        wires.append("  reg %s;" % reg)
        clock = build(cell["connections"]["C"][0], ATOM)
        data = build(cell["connections"]["D"][0], MUX)
        if redge is None:
            blocks.append(["  always @(%s %s)" % (edge, clock)]
                          + moves("    ", reg, data))
            continue
        rst = build(cell["connections"]["R"][0], ATOM)
        blocks.append(["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                       "    if (%s%s) %s <= 1'b%s;"
                       % ("!" if redge == "negedge" else "", rst, reg, value),
                       "    else"] + moves("      ", reg, data))
    # A latch, which is not a register and belongs to no region, but which a
    # synthesiser puts in all the same: a clock gate is a latch and an and
    # gate. Nothing here tries to read one as anything more than it is.
    for name, cell in cells.items():
        if name in skip or LATCH not in cell["type"]:
            continue
        level = latch_kind(cell["type"])
        q = cell["connections"]["Q"][0]
        if level is None or q in named or q in written:
            continue
        held = show(q)
        wires.append("  reg %s;" % held)
        blocks.append(["  always @(*)",
                       "    if (%s%s) %s <= %s;"
                       % (level, build(cell["connections"]["E"][0], ATOM),
                          held, build(cell["connections"]["D"][0], MUX))])

    # What the caller will wire the module's ports up to, once this returns.
    # Those lines are added after this text is laid out, so nothing here sees
    # them read anything, and a net driving an output would be pruned as dead
    # and then referred to by a wire that no longer exists.
    keep = set(module.get("ports", {}))
    keep |= {BASE.match(str(n)).group(0) for n in named.values()}
    keep |= {BASE.match(str(n)).group(0) for n in label.values()}
    for spec in module.get("ports", {}).values():
        for bit in spec["bits"]:
            keep.add(BASE.match(show(bit)).group(0))
    defs = prune(defs, blocks, proven, keep)
    top = list(json.load(open(path))["modules"])[0]
    defs, blocks, proven, mods, wires = split(
        defs, blocks, proven, keep, wires,
        spans(module, wires, proven), top, set(module.get("ports", {})))
    return (wires, folded(arrange(defs, blocks, proven, keep)), taken,
            [folded(text) for text in mods])


def net_of(path, name):
    """The bit a named port carries, so a caller can alias it"""
    module = list(json.load(open(path))["modules"].values())[0]
    return module["ports"][name]["bits"]
