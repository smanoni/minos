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
- `scripts/match.py`: extracts a region, derives its port roles from its own
  structure, and proves equivalence by temporal induction.
- `scripts/lift.py`: proposes behavioural RTL for a region and keeps only what
  proves equivalent, so the readable form is derived rather than written.
- `scripts/emit.py`: rebuilds the hierarchy synthesis flattened, instantiating
  one shared module wherever regions prove equivalent to each other.
- `make cc`, elaborating common_cells modules through sv2v onto the same gate
  basis a recovered netlist uses, parameterised in place with no wrappers.
- `gds/` input directory, populated by `make gds` from a `name:source` list so
  new dependencies contribute layouts with a single line.
- `make deps` as the one entry point a fresh clone needs, and `gds.lock` to pin
  downloaded layouts by checksum the way a submodule pins a commit.
- `make run DESIGN=<name>` for any layout in `gds/`, and `make corpus` to run
  the flow over hardened macros from a Tiny Tapeout shuttle.

### Fixed

- Port labels are read from every conductor label layer, not met3 alone, so
  layouts that place their pins on another layer no longer come back portless.
- Regions are split to single bits before extraction, so a role is derived per
  bit rather than collapsing a whole bus onto one.
