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
| `scripts/match.py` | proves a region equivalent to a reference |
| `scripts/cc_lib.ys` | common_cells reference to the same gate basis |
| `scripts/lift.py` | behavioural RTL for a region, kept only if proven |
| `scripts/emit.py` | rebuilds the hierarchy synthesis flattened |
| `scripts/cosim.py` | simulates the recovered RTL beside the netlist |
| `scripts/observe.py` | names a register from what it is watched doing |
| `gds/` | input layouts, populated by `make deps` |
| `gds.lock` | checksums pinning the downloaded layouts |
| `work/` | generated output |
