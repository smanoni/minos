# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Fetches the RTL a shuttle's authors wrote, to measure a recovery against"""

import json
import os
import subprocess
import sys
try:
    from urllib.request import urlopen, Request
except ImportError:
    from urllib2 import urlopen, Request

INDEX = "https://index.tinytapeout.com/%s.json"


def index(shuttle, out):
    """The shuttle's own list of what was taped out, and where each came from.

    Every project carries the repository it was built from and the commit that
    was built, so the sources a recovery is scored against are the ones that
    became the layout and not whatever the author has pushed since.
    """
    path = "%s/%s_index.json" % (out, shuttle)
    if not os.path.exists(path):
        # Named, because the index refuses the one urllib sends by default
        # and answers 403 to it.
        ask = Request(INDEX % shuttle, headers={"User-Agent": "minos"})
        open(path, "wb").write(urlopen(ask).read())
    got = json.load(open(path))
    return got["projects"] if isinstance(got, dict) else got


def fetch(project, out):
    """One project's repository at the commit the shuttle pinned"""
    where = "%s/%s" % (out, project["macro"])
    if os.path.isdir(where):
        return True
    if subprocess.call(["git", "clone", "-q", project["repo"], where]):
        return False
    return not subprocess.call(["git", "-C", where, "checkout", "-q",
                                project["commit"]])


def main(shuttle, out, *want):
    os.makedirs(out, exist_ok=True)
    by = {one["macro"]: one for one in index(shuttle, out)}
    names = list(want) or sorted(by)
    got = 0
    for name in names:
        if name not in by:
            print("  %s is not in the %s index" % (name, shuttle))
            continue
        if fetch(by[name], out):
            got += 1
        else:
            print("  %s did not clone from %s" % (name, by[name]["repo"]))
    print("%d of %d sources in %s" % (got, len(names), out))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: sources.py <shuttle> <outdir> [macro ...]")
    sys.exit(main(*sys.argv[1:]))
