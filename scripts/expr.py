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


def transcribe(path, skip, alias, label=None):
    """Wires, assignments and always blocks for every cell not skipped.

    A net that more than one gate reads becomes a wire of its own, so the
    output stays a readable set of assignments rather than one nested term.
    An alias says a net is already driven elsewhere and its gate is dropped;
    a label only gives the net a better name than its number. The output bits
    taken over as words come back too, since those are driven here and need no
    wiring up afterwards.
    """
    module, cells, driver, fanout = load(path)
    wires, assigns, always = [], [], []
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
        rows.append((word, key, members))

    # Each operand a row reads takes the row's name and the part it plays, so
    # a column of numbers becomes a word with a name. Only a column that is
    # nets all the way across is named: one carrying worked-out terms has no
    # nets to put the name on, and one the whole row shares is a single net
    # that already answers to itself.
    buses = collections.OrderedDict()
    for word, key, members in rows:
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
        assigns += wrap(head, expand(written[target])[0], ";")

    for word, (port, run, parts, entire) in sorted(ports_of.items()):
        edge, clock, redge, rst = sensitivity(parts[0])[:4]
        wide = len(parts)
        data = "{%s}" % ", ".join(build(c["connections"]["D"][0], MUX)
                                  for c in reversed(parts))
        wires.append("  reg [%d:0] %s;" % (wide - 1, word))
        if not entire:
            assigns.append("  assign %s[%d:%d] = %s;"
                           % (port, run[-1], run[0], word))
        if redge is None:
            always += (["  always @(%s %s)" % (edge, clock)]
                       + moves("    ", word, data))
            continue
        # Each bit resets to a value of its own, so the word resets to those
        # values written out as one, rather than to a word of the same digit.
        start = "".join(flop_kind(c["type"])[2] for c in reversed(parts))
        always += ["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                   "    if (%s%s) %s <= %d'b%s;"
                   % ("!" if redge == "negedge" else "", rst, word, wide,
                      start),
                   "    else"] + moves("      ", word, data)

    for word, key, members in rows:
        edge, clock, redge, rst, value = key[:5]
        wide = len(members)
        cols = columns(key[-1], [build(c["connections"]["D"][0], MUX)
                                 for _, c in members],
                       ["%s[%d]" % (word, i) for i in range(wide)])
        data = key[-1] % tuple(word if c is None else c for c in cols)
        wires.append("  reg [%d:0] %s;" % (wide - 1, word))
        if redge is None:
            always += (["  always @(%s %s)" % (edge, clock)]
                       + moves("    ", word, data))
            continue
        always += ["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                   "    if (%s%s) %s <= {%d{1'b%s}};"
                   % ("!" if redge == "negedge" else "", rst, word, wide,
                      value),
                   "    else"] + moves("      ", word, data)

    grouped = {c["connections"]["Q"][0] for _, _, ms in rows for _, c in ms}
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
            always += (["  always @(%s %s)" % (edge, clock)]
                       + moves("    ", reg, data))
            continue
        rst = build(cell["connections"]["R"][0], ATOM)
        always += ["  always @(%s %s or %s %s)" % (edge, clock, redge, rst),
                   "    if (%s%s) %s <= 1'b%s;"
                   % ("!" if redge == "negedge" else "", rst, reg, value),
                   "    else"] + moves("      ", reg, data)
    return wires, assigns, always, taken


def net_of(path, name):
    """The bit a named port carries, so a caller can alias it"""
    module = list(json.load(open(path))["modules"].values())[0]
    return module["ports"][name]["bits"]
