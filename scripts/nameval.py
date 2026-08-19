# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Scores a model against the registers a run has already named"""

import sys
import os
import re
import glob
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import infer
import observe

KIND = re.compile(r"^([a-z]+)\d+$")

# What a run can tell about a register, put as a question a model can answer.
# The wording is the one observe.py works to, so the two are being asked the
# same thing and their answers can be set side by side.
CHOICES = ["count", "down", "shift", "flags", "gray", "latched", "onehot"]
GLOSS = dict(observe.VERBS)

# Which of those a model could know without running anything. A shift or a
# count is in the logic and can be read off it; that a word only ever gains
# bits, or settles once and stays, or gets around one bit at a time, is a
# claim about which states are reached and is not in the logic at all. Scoring
# the two together hides which of them a model can do.
STRUCTURAL = ("count", "down", "shift")

# A name a model has already given is not evidence either, and a run of the
# flow with a model on leaves them lying about the slice. They are the words
# infer.py may answer with, followed by a number.
GUESSED = re.compile(r"^(?:%s)\d+$"
                     % "|".join(w for w, _ in infer.CHOICES + infer.WIRES))


def redact(lines, target):
    """The slice with every name a run has already earned taken back out.

    The question is whether a model can say what a register does from what the
    circuit looks like, so a name that already says it is not evidence. The
    module's own name goes too, being the one token that carries a claim about
    the whole design and colours every answer under it.
    """
    text = re.sub(r"\bmodule\s+\S+\(", "module top(", "\n".join(lines))
    others = sorted({w for w in infer.WORD.findall(text)
                     if (observe.EARNED.match(w) or GUESSED.match(w))
                     and w != target})
    table = {target: "TARGET"}
    for at, name in enumerate(others):
        table[name] = "reg_%s" % chr(ord("a") + at % 26)
    for old, new in table.items():
        text = re.sub(r"(?<![\w'])%s(?=\b|_)" % re.escape(old), new, text)
    return text


def question(text):
    out = ["Verilog recovered from a chip layout by reverse engineering.",
           "Every name in it is meaningless: wires are called n<number> and",
           "the register in question is called TARGET. Only the module's",
           "ports carry names that mean anything.", "", text, "",
           "What does the register TARGET do? Choose one:"]
    for word in CHOICES:
        out.append("  %-8s %s" % (word, GLOSS[word]))
    out += ["  other    none of the above", "",
            "Answer with one word from that list and nothing else."]
    return "\n".join(out)


def cases(workdir):
    """Every register a run named, with the name hidden and the answer kept"""
    out = []
    for path in sorted(glob.glob("%s/*_lifted.sv" % workdir)):
        design = os.path.basename(path)[:-len("_lifted.sv")]
        text = open(path).read()
        parsed = infer.module(text)
        reads = infer.readers(parsed)
        for name in sorted(parsed[1]):
            if observe.EARNED.match(name):
                out.append((design, parsed, reads, name,
                            KIND.match(name).group(1)))
    return out


def main(workdir="work"):
    if not infer.MODEL:
        print("  set MINOS_MODEL to the model to score")
        return 1
    got = cases(workdir)
    if not got:
        print("  no run has named a register to score a model against")
        return 1
    right, kinds = collections.Counter(), collections.Counter()
    for design, parsed, reads, name, truth in got:
        lines = infer.slice_for(parsed, name, reads)
        if lines is None:
            continue
        said = infer.ask(question(redact(lines, name)),
                         CHOICES + ["other"], "other")
        if said == "other" and infer.AHEAD:
            # The same second pass the flow makes, so what is scored here is
            # what the flow actually asks and not a simpler question.
            lines = infer.slice_for(parsed, name, reads, True)
            said = infer.ask(question(redact(lines, name)),
                             CHOICES + ["other"], "other")
        if said is None:
            return 1
        kinds[truth] += 1
        right[truth] += said == truth
        print("  %-32s %-9s said %-9s %s"
              % (design, name, said, "ok" if said == truth else "<- " + truth))
    # A total over both halves says less than either half does, so both are
    # given: what a model could have read off the logic, and what it could
    # only have got by running the design, which it cannot do.
    halves = [("reads off the logic", [k for k in kinds if k in STRUCTURAL]),
              ("only a run can see", [k for k in kinds if k not in STRUCTURAL])]
    for half, pick in halves:
        total = sum(kinds[k] for k in pick)
        if total:
            print("  %-22s %d of %d"
                  % (half, sum(right[k] for k in pick), total))
    common = max(kinds.values())
    print("  %s: %d of %d  (1 in 8 by chance, %d always answering the "
          "commonest)" % (infer.MODEL, sum(right.values()), sum(kinds.values()),
                          common))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
