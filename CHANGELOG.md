# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Added

- `scripts/gds2def.py`: recovers placement and connectivity from a GDS as DEF,
  with a self-check against a reference DEF.
- `scripts/def2v.py`: rewrites a DEF as a gate-level Verilog netlist, with a
  self-check against a reference netlist.
- `scripts/generic.ys`: maps a PDK netlist to technology-independent gates,
  emitting a faithful and an optimised form.
- `scripts/gen_layer_props.py`: extends the PDK layer properties with the
  layers a puzzle GDS adds.
- `scripts/structure.py`: recovers register groups, shift-register chains and
  output cones from a netlist, independently of the gate basis.
- `make cc`, elaborating common_cells modules through sv2v onto the same gate
  basis a recovered netlist uses, parameterised in place with no wrappers.
- `gds/` input directory, populated by `make gds` from a `name:path` list so
  new dependencies contribute layouts with a single line.
