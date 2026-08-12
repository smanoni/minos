# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

"""Extends the PDK layer properties with the layers a puzzle GDS adds"""

import sys

EXTRA = [("PUZZLE internal-blackbox - 200/0", "200/0", "#ff0000", "I5", "true"),
         ("cell outline - 236/0", "236/0", "#808080", "I1", "false"),
         ("cell name text - 83/44", "83/44", "#ffffff", "I1", "false")]


def entry(name, layer, color, dither, visible):
    """One layer-properties record"""
    return """ <properties>
  <frame-color>%s</frame-color>
  <fill-color>%s</fill-color>
  <frame-brightness>0</frame-brightness>
  <fill-brightness>0</fill-brightness>
  <dither-pattern>%s</dither-pattern>
  <line-style/>
  <valid>true</valid>
  <visible>%s</visible>
  <transparent>false</transparent>
  <width>1</width>
  <marked>false</marked>
  <xfill>false</xfill>
  <animation>0</animation>
  <name>%s</name>
  <source>%s@1</source>
 </properties>
""" % (color, color, dither, visible, name, layer)


def main(src, out):
    text = open(src).read()
    if "</layer-properties>" not in text:
        raise SystemExit("%s is not a layer properties file" % src)
    added = "".join(entry(n, l, c, d, v) for n, l, c, d, v in EXTRA)
    open(out, "w").write(text.replace("</layer-properties>",
                                      added + "</layer-properties>"))
    print("%s -> %s (+%d layers)" % (src, out, len(EXTRA)))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
