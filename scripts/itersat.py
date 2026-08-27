# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""The shortest input a recovered netlist needs to raise one of its flags"""

import os
import subprocess
import sys

YOSYS = os.environ.get("YOSYS", "yosys").split()

# A netlist read back from a layout keeps its asynchronous resets, and the
# solver has no model for those, so they are rewritten as logic first. Holding
# reset released and the design enabled at every step asks the one question
# worth asking: what does the design have to be fed, from a standing start, to
# arrive at the flag.
ASK = """
read_verilog %s
prep -top %s -flatten
async2sync
sat -seq %d -set-init-zero -set rst_n 1 -set enable 1 -set-at %d %s 1 -show %s
"""

RESET = 3


def ask(netlist, top, depth, flag, drive):
    """The input the flag needs within this many cycles, or None if unreachable"""
    run = subprocess.run(
        YOSYS + ["-p", ASK % (netlist, top, depth, depth, flag, drive)],
        capture_output=True, text=True)
    bits = [line.split()[-1] for line in run.stdout.splitlines()
            if line.split()[1:2] == ["\\" + drive]]
    return bits or None


def shortest(netlist, top, flag, drive, cap=4096):
    """The fewest cycles the flag can be reached in, and the input reaching it.

    An input that works in N cycles still works in N + 1, so as the depth grows
    the answer turns from no to yes exactly once. That is what makes the depth
    worth searching for rather than guessing: double the depth until the solver
    answers at all, then halve the gap left behind until the two meet.
    """
    depth = 1
    while depth <= cap:
        found = ask(netlist, top, depth, flag, drive)
        print("  %4d cycles: %s" % (depth, "reachable" if found else "no"))
        if found:
            break
        depth *= 2
    else:
        return None, None
    low, high = depth // 2, depth
    while low + 1 < high:
        mid = (low + high) // 2
        hit = ask(netlist, top, mid, flag, drive)
        print("  %4d cycles: %s" % (mid, "reachable" if hit else "no"))
        if hit:
            high, found = mid, hit
        else:
            low = mid
    return high, found


def table(bits, out):
    """Every driven signal, one row per cycle, as the design is to be fed it.

    The solver answers for a design already released and enabled, so the reset
    it assumed is written out ahead of the input rather than left for a reader
    to remember. Three bits a row is what $readmemb wants and the cycle number
    rides along in a comment, so the one file is both replayable and readable.
    """
    rows = ["000  // %d" % (n + 1) for n in range(RESET)]
    rows += ["11%s  // %d" % (bit, RESET + n + 1) for n, bit in enumerate(bits)]
    open(out, "w").write("// rst_n enable I, one row per clock\n" +
                         "\n".join(rows) + "\n")


def main(netlist, top, out, flag="success", drive="I"):
    depth, bits = shortest(netlist, top, flag, drive)
    if not bits:
        raise SystemExit("%s is unreachable in %s" % (flag, netlist))
    table(bits, out)
    print("%s: %d cycles of reset, then %d of input" % (out, RESET, depth))


if __name__ == "__main__":
    if not 4 <= len(sys.argv) <= 6:
        raise SystemExit("usage: itersat.py <netlist.v> <top> <out> [flag] [input]")
    main(*sys.argv[1:])
