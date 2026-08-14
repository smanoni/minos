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
- A loadable shift register template, matched from the mux each stage uses to
  choose between its neighbour and a value of its own, and extracted with
  those muxes alone so the shifted-in bit stays a port of the region.
- A state group is the largest set of registers closed under itself, rather
  than a whole control group that happens to be closed. One register watching
  something outside the group used to hide the state machine the rest of them
  form, which is why the puzzle came back with no state group at all and now
  comes back with eighty registers.
- Synchronous reset, recognised rather than pattern-matched: a net is a reset
  when holding it settles every register's data pin at once, whatever the rest
  of the design is doing, and the word it settles them to is the value the
  template resets to. Three quarters of the registers in the corpus have no
  reset pin, so this is the shape most of them are in. A region drawn round
  its registers leaves the reset gates outside, so it is offered a wider cut
  that takes in the gates the reset settles, and falls back to the plain cut
  when the wider one will not take roles.
- `scripts/cosim.py`: simulates the recovered RTL beside the netlist it was
  lifted from, driving both from one clock and one stimulus. It says how often
  the netlist's own outputs moved, so a design that sat still is reported as
  such rather than as a match. `make lift` runs it wherever there is a
  simulator to run it with.

### Fixed

- Port labels are read from every conductor label layer, not met3 alone, so
  layouts that place their pins on another layer no longer come back portless.
- Regions are split to single bits before extraction, so a role is derived per
  bit rather than collapsing a whole bus onto one.
- No two region ports may share a role. Sharing one wired them to the same net
  and proved the region with its inputs tied together, which passed templates
  the region does not implement; a region with more inputs than roles is now
  refused instead.
- A lifted block and the gates carried over beside it no longer declare the
  same net twice.
- An output driven by a register no template claimed is left to the register,
  which already carries the output's name, instead of being wired to itself.
- Templates take their clock edge, reset polarity and reset value from the
  registers they speak for instead of assuming a rising edge and a zero. The
  proof has no model of a clock net and so could never have caught the
  difference: a bank of falling-edge registers was being written out, and
  passing as proven, as `always @(posedge clk)`.
- A template is only offered a reset arm when the registers it covers agree on
  one, and is refused outright when they do not share a clock edge, since one
  always block cannot speak for registers that clock differently.
- Recovered chains no longer overlap. Where one stage fed two, the walk back
  from each arrived at the same registers and returned them as two chains,
  either of which could then be lifted as if the shared registers were its
  own and drive them twice over.
- A chain's registers are exposed before it is proved. Only the last stage of
  a chain is read from outside it, so the region came out with fewer outputs
  than registers and the template was compared against bits nothing drove; a
  chain whose stages do not all reach an output is now refused outright.
- A chain can be cut to the cells carrying one stage to the next, leaving what
  computes its serial input outside. Cut around everything feeding it, that
  logic arrives as several ports of its own and the region has more inputs
  than a shift register has roles.
