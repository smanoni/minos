# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Asks a model what a register looks like it is for, and says it was asked"""

import sys
import os
import re
import json
import collections

try:
    from urllib.request import urlopen, Request
except ImportError:
    from urllib2 import urlopen, Request

MODEL = os.environ.get("MINOS_MODEL", "")
HOST = os.environ.get("MINOS_MODEL_HOST", "http://127.0.0.1:11434")
THREADS = int(os.environ.get("MINOS_MODEL_THREADS", "32"))
BUDGET = int(os.environ.get("MINOS_SLICE", "70"))

# How many of a design's combinational nets to ask about. Naming all of them
# is most of a day's work for a model and most of it wasted: a net defined
# three lines above its only reader costs nothing to hold, and a name would
# only lengthen the line. What costs a reader is distance, so the ones asked
# about are the ones held longest.
WIDEST = int(os.environ.get("MINOS_NAMES", "24"))

# Whether to put a question a second time showing what a net feeds, where
# asking what it is built from gave no answer. Off, because it was measured:
# over the whole corpus the second pass turned one unknown into a name, out of
# some four hundred and forty it was asked, for twice the running time. It is
# left here rather than deleted because that is one model's answer on one
# corpus, and the flag costs nothing to try.
AHEAD = os.environ.get("MINOS_AHEAD", "") not in ("", "0")

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
TOP = re.compile(r"^(?:  )?(\S)")

# The nets worth asking about: the ones nothing else has a name for. A name a
# run earned by watching, or a row earned by the shape it steps in, is
# evidence and outranks anything a model has to offer, so it is left alone.
ANON = re.compile(r"^(?:n|b|r|s|word)\d+$")

# What a model may answer. Measured on the registers a run has already named,
# a model reads the shapes it can see off the logic and invents freely when
# asked for prose: a shift register came back as an accumulator, and a
# multiply-accumulate whose module name contains MAC came back holding an
# Ethernet address. Held to a list it cannot do either, and the list is kept
# clear of every word the rest of the flow uses so an answer stays telling.
#
# Four more words were tried and taken out again: carry, parity, address and
# select. Across the whole corpus not one register came back as any of them,
# and an option a model never picks is dead weight in front of the ones it
# does. They earn their place on the list for combinational nets below, where
# they are answers a net can actually have.
CHOICES = [
    ("unknown", "none of the below, or not enough here to tell"),
    ("shifter", "its bits move along by one place, taking one bit in"),
    ("counter", "it steps up or down by one"),
    ("accumulator", "it adds or combines a value into what it already holds"),
    ("capture", "it takes a value straight off the input ports"),
    ("state", "it encodes which step of a sequence the design is in"),
    ("control", "it decides whether other registers move"),
    ("data", "it holds a value with nothing evidently done to it"),
]

# What a combinational net may be answered with. A different list, because
# the two are different questions: a wire is never a shifter and a register is
# never a clock, and offering either the other's words spends the model's
# attention on answers it should not give. The two that matter most are the
# ones a netlist destroys and nothing else recovers: a gated clock and a
# derived reset read as ordinary gates, and a reader meeting one has no way to
# know the design's timing turns on it.
#
# There is no enable on the list, though it was the obvious word to want.
# Offered it, a model answered enable for fourteen of one design's sixteen
# named wires, every one of them plainly a multiplexer of the form en ? a : b;
# it was reading the port called en sitting in the line rather than the shape
# of the line. Taken off the list all sixteen came back select, which is what
# they are.
WIRES = [
    ("unknown", "none of the below, or not enough here to tell"),
    ("clock", "registers are clocked on it, so it is a clock or a gated one"),
    ("reset", "it forces registers to their reset value"),
    ("select", "it picks which of several values something downstream takes"),
    ("carry", "it is a bit carried out of one sum into the next"),
    ("sum", "it is one bit of an addition, before any carry"),
    ("zero", "it says a whole word is all zeros, or all ones"),
    ("match", "it says two values are equal"),
    ("parity", "it is the exclusive-or of several bits at once"),
]
SAYS = dict(CHOICES)
SAYS.update(WIRES)


def items(text):
    """The module split into its top level pieces, each with its own lines.

    A piece begins where the indentation returns to two spaces or less, since
    a term broken over several lines is written under the one it belongs to.
    """
    out, cur = [], []
    for line in text.splitlines():
        if TOP.match(line) or not line.strip():
            if cur:
                out.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
        else:
            cur = [line]
    if cur:
        out.append(cur)
    return out


def module(text):
    """A lifted design read as its ports, declarations, blocks and definitions"""
    head, decl, defines, blocks = [], {}, {}, {}
    for piece in items(text):
        line = piece[0]
        if line.startswith("module") or re.match(r"^\s*(?:input|output)\s", line):
            head.append(line)
            continue
        got = re.match(r"^  reg\s+(?:\[[^\]]*\]\s*)?(\S+?);", line)
        if got:
            decl[got.group(1)] = line
            continue
        got = re.match(r"^  (?:wire|assign)\s+(?:\[[^\]]*\]\s*)?"
                       r"([A-Za-z_][A-Za-z0-9_$]*)(?:\[[^\]]*\])?\s*[=;]", line)
        if got:
            defines.setdefault(got.group(1), []).append(piece)
            continue
        if line.strip().startswith("always"):
            # An always block written after the declarations stands on its
            # own, so what it speaks for is read off the targets it assigns.
            for name in set(re.findall(r"([A-Za-z_][A-Za-z0-9_$]*)"
                                       r"(?:\[[^\]]*\])?\s*<=",
                                       "\n".join(piece))):
                blocks.setdefault(name, []).append(piece)
    return head, decl, defines, blocks


def readers(parsed):
    """Which pieces read each name, so a register can be followed forwards"""
    head, decl, defines, blocks = parsed
    out = {}
    for owner, pieces in list(defines.items()) + list(blocks.items()):
        for piece in pieces:
            for name in set(WORD.findall("\n".join(piece))):
                if name != owner:
                    out.setdefault(name, []).append((owner, piece))
    return out


def behind(defines, start, budget, skip=None):
    """The definitions of what a piece reads, and of what those read in turn"""
    seen, queue, out = set([skip]), [], []

    def push(chunk):
        for name in WORD.findall("\n".join(chunk)):
            if name in defines and name not in seen:
                seen.add(name)
                queue.append(name)

    push(start)
    while queue and len(out) < budget:
        name = queue.pop(0)
        for piece in defines[name]:
            out += [l for l in piece if l.strip()]
        push(defines[name][0])
    return out


def ahead(reads, start, budget):
    """The pieces that read a name, then what those go on to feed"""
    seen, queue, taken, out = set([start]), [start], set(), []
    while queue and len(out) < budget:
        name = queue.pop(0)
        for owner, piece in reads.get(name, ()):
            if id(piece) not in taken:
                taken.add(id(piece))
                out += [l for l in piece if l.strip()]
            if owner is not None and owner not in seen:
                seen.add(owner)
                queue.append(owner)
    return out


def slice_for(parsed, target, reads=None, forward=False):
    """What a reader would look at to say what one net is for.

    What drives it, then whatever that reads, then whatever those read, until
    the budget runs out. Breadth first, so the nets nearest the target are the
    ones that fit, and only what the target itself reaches is chased: starting
    from the ports drags in every cone in the design and buries the one piece
    the question is about.

    What it feeds is held back until asked for. A net is as often known by
    what it decides as by what is put into it, and one gating every register's
    enable says so nowhere in its own fan-in — but shown both at once a model
    reads neither well. Measured on the registers a run had already named,
    adding the forward half to every question turned two plain shift registers
    into no answer at all: fourteen clear lines with forty-three of downstream
    logic after them stopped looking like a shift. So the question is put
    backwards first, and only where that gives nothing is it put again with
    what the net goes on to feed, which cannot cost an answer that was already
    there.
    """
    head, decl, defines, blocks = parsed
    if target in blocks:
        own = ([decl[target]] if target in decl else []) \
            + [l for body in blocks[target] for l in body]
    elif target in defines:
        own = [l for piece in defines[target] for l in piece]
    else:
        return None
    feeds = ahead(readers(parsed) if reads is None else reads,
                  target, BUDGET // 2) if forward else []
    back = behind(defines, own, BUDGET - len(feeds), target)
    out = list(head) + [""] + own + [""]
    if back:
        out += ["// what %s is built from:" % target] + back
    if feeds:
        out += ["", "// what %s goes on to feed:" % target] + feeds
    return out


def question(lines, target, choices=None, kind="register"):
    """The slice put as a question, with the design's own name kept back.

    A module's name is the one token carrying a claim about the whole design,
    and a wrong claim there colours every answer under it: read with its name
    showing, a multiply-accumulate called db_MAC was named for an Ethernet
    address, and the same register read without it was not.
    """
    choices = CHOICES if choices is None else choices
    text = re.sub(r"\bmodule\s+\S+\(", "module top(", "\n".join(lines))
    text = re.sub(r"(?<![\w'])%s(?=\b|_)" % re.escape(target), "TARGET", text)
    out = ["Verilog recovered from a chip layout by reverse engineering.",
           "Every name in it is meaningless: wires are called n<number> and",
           "the %s in question is called TARGET. Only the module's" % kind,
           "ports carry names that mean anything.", "", text, "",
           "What does the %s TARGET look like it is for? Choose one:" % kind]
    for word, gloss in choices:
        out.append("  %-12s %s" % (word, gloss))
    out += ["", "Answer unknown unless the logic above plainly shows one of",
            "the others. Answer with one word from that list and nothing else."]
    return "\n".join(out)


def ask(prompt, allowed=None, otherwise="unknown"):
    """One answer from the model, or nothing if it will not give one.

    What it may answer is passed in rather than fixed here, since the same
    question put to score a model has a different list to the one put to name
    a register with it. Anything else it says is read as the fallback: a model
    that will not choose from the list has not chosen.
    """
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0, "num_predict": 6,
                                   "seed": 1, "num_thread": THREADS,
                                   "num_ctx": 8192}}).encode()
    req = Request(HOST + "/api/generate", body,
                  {"Content-Type": "application/json"})
    try:
        got = json.loads(urlopen(req, timeout=600).read().decode())["response"]
    except Exception as why:
        print("  no model to ask: %s" % why)
        return None
    said = (WORD.findall(got.strip().lower()) or [""])[0]
    return said if said in (SAYS if allowed is None else allowed) else otherwise


def rename(text, table):
    """The recovered text under the names a model guessed at.

    Every name is put aside before any is put back, since one register can be
    taking the name another is giving up and a rename done in order would
    lose one of them.
    """
    # A name is only a name where a name can stand. Verilog writes the width
    # of a constant before its base and its digits after, so b0 is a register
    # in one place and the whole of 1'b0 in another, and a rename blind to the
    # difference turns a reset value into a reference to what it resets.
    aside = {}
    for at, old in enumerate(table):
        aside[old] = "minos$%d" % at
        text = re.sub(r"(?<![\w'])%s(?=\b|_)" % re.escape(old), aside[old], text)
    for old, new in table.items():
        text = text.replace(aside[old], new)
    return text


def marked(text, table):
    """The text with every guessed name owned up to where it is declared.

    A name a model guessed reads exactly like one the design was watched
    earning, and the difference is the whole of what a reader needs to know
    about it, so it is said twice: once at the top of the module where the
    guesses can be counted, and once against each declaration where it cannot
    be missed while reading the line it belongs to.
    """
    for name in sorted(set(table.values())):
        text = re.sub(r"^(  (?:reg|wire)\s+(?:\[[^\]]*\]\s*)?%s\s*[;=].*)$"
                      % re.escape(name),
                      r"\1  // inferred, unverified", text, flags=re.M)
    note = ["// %d of this module's nets are named by a model reading the"
            % len(table),
            "// logic, not by anything that checked. Those names are marked",
            "// where they are declared and say what the logic looks like, not",
            "// what the design was seen to do or what it was ever called.",
            "// Asked of %s." % MODEL]
    return "\n".join(note) + "\n" + text


def furthest(text, names, many):
    """The nets a reader carries longest, worst first.

    A net is on a reader's hands from the first place it is mentioned to the
    last place it is read. Most are met and done with inside a few lines and a
    name would buy nothing; a handful are held across most of a module, and
    those are the ones worth spending a model on.
    """
    first, last = {}, {}
    for at, line in enumerate(text.splitlines()):
        for name in set(WORD.findall(line)):
            if name in names:
                first.setdefault(name, at)
                last[name] = at
    held = [n for n in names if n in first]
    held.sort(key=lambda n: (first[n] - last[n], n))
    return held[:many]


def guess(parsed, reads, targets, choices, kind, taken, table, kinds, nextat):
    """One answer per target, added to the renaming under a free name"""
    for name in targets:
        got = slice_for(parsed, name, reads)
        if got is None:
            continue
        words = [word for word, gloss in choices]
        said = ask(question(got, name, choices, kind), words)
        if said == "unknown" and AHEAD:
            # Nothing behind it says what it is, so ask again with what it
            # goes on to feed. Only the questions that came back empty are put
            # twice, so the second pass can add answers but never take one.
            said = ask(question(slice_for(parsed, name, reads, True),
                                name, choices, kind), words)
        if said is None:
            return False
        if said == "unknown":
            continue
        index = nextat.get(said, 0)
        while "%s%d" % (said, index) in taken:
            index += 1
        nextat[said] = index + 1
        kinds[said] += 1
        table[name] = "%s%d" % (said, index)
    return True


def main(lifted):
    if not MODEL:
        return 0
    text = open(lifted).read()
    parsed = module(text)
    regs = sorted(name for name in parsed[1] if ANON.match(name))
    # A design has ten times as many nameless wires as registers, and asking
    # about all of them would take a model longer than the whole rest of the
    # flow. The ones a reader carries furthest are asked about; the rest are
    # short-lived and read perfectly well as a number.
    wires = furthest(text, [name for name in parsed[2] if ANON.match(name)],
                     WIDEST)
    if not regs and not wires:
        print("  no nets left for a model to guess at")
        return 0
    reads = readers(parsed)
    taken = {w for w in WORD.findall(text)}
    table, kinds, nextat = {}, collections.Counter(), {}
    for targets, choices, kind in ((regs, CHOICES, "register"),
                                   (wires, WIRES, "wire")):
        if not guess(parsed, reads, targets, choices, kind,
                     taken, table, kinds, nextat):
            return 0
    if table:
        open(lifted, "w").write(marked(rename(text, table), table))
    print("  %d of %d registers and %d of %d long-carried wires guessed at "
          "by %s%s"
          % (sum(1 for n in table if n in parsed[1]), len(regs),
             sum(1 for n in table if n in parsed[2]), len(wires), MODEL,
             ": " + ", ".join("%d %s" % (n, k)
                              for k, n in sorted(kinds.items())) if table else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
