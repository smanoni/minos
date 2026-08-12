# minos

GDS to RTL with open tools.

**Under active development. Not ready for use.**

| path | what |
| --- | --- |
| `scripts/gds2def.py` | GDS + PDK to DEF: placement and connectivity |
| `scripts/def2v.py` | DEF to gate-level Verilog |
| `scripts/generic.ys` | PDK netlist to technology-independent gates |
| `scripts/gen_layer_props.py` | KLayout layer properties |
| `scripts/structure.py` | candidate regions in a recovered netlist |
| `scripts/cc_lib.ys` | common_cells reference to the same gate basis |
| `gds/` | input layouts, populated by `make gds` |
| `work/` | generated output |
