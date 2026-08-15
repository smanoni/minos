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
- Gates are folded into whoever reads them. A net one gate reads is written
  where it is read rather than on a line of its own, so what comes out is an
  expression per register instead of a line per gate, and a design shows its
  shape: a register holding, stepping and loading on three control lines reads
  as one term where it used to be spread over a dozen. Brackets are placed
  from how tightly each form binds, counted on the operator an operand belongs
  to rather than on the brackets a form already carries. Across the corpus this
  is half the lines and a third of the assignments, with nothing else changed.
  A net read twice keeps a wire, since folding it would write the same term
  out twice over and lose that the two are one net.
- A register clocked from anywhere but a port keeps its data on a wire. Such a
  clock is built by the design out of its own state, so it arrives a moment
  after that state moved rather than once everything has settled, and then it
  matters whether a value is read off a wire or worked out again in place. The
  netlist being compared against reads every register's data off a wire, and a
  counter on a divided clock came back differing in most of its cycles until
  this one did too. No proof can see the difference, which is what simulating
  the recovered RTL is there for.
- A run says how much of the design it stirred, counting the registers that
  ever moved rather than only how often the ports did. Agreeing on the ports
  says little when the state behind them sat still, and there was no way to
  tell the two apart: a ripple counter was reported as matching over two
  thousand cycles while every one of its registers held no value throughout
  and every reported change came from a combinational path beside it. Across
  the corpus a run reaches 402 of 884 registers, with an I2C controller and a
  memory game lowest at a tenth and a quarter of theirs.
- Both modules start their registers from zero. A design with no reset to
  release holds no value from the outset and keeps it, and two modules that
  both hold nothing agree about nothing, so what looked like a match was a
  run over a design that had never started.
- Registers stepping the same way on the same clock are put back into the word
  they came from. Synthesis splits a word into bits and scatters them, so what
  was one line of the design comes back as a register apiece; a row of them
  sharing a shape is that word again, and the shape says what it was for, so
  the row is named for what it does rather than numbered. Each operand of the
  shape is written as the row reads it: a net every register takes from the
  same place stays that net, one each takes from itself is the row, and the
  rest are the bits gathered into a word of their own and named for the part
  they play in the shape. The puzzle comes back with forty-five always blocks
  where it had ninety-two.
- The registers carrying an output are written as the word they are. Split
  into a register apiece an output reads as eight unrelated blocks that happen
  to share a clock; put back together, a byte that shifts is visibly a byte
  that shifts, and an encryption core comes back as the one line of feedback
  it is rather than as eight. The bits have to be neighbours and to agree on
  clock and reset for one block to speak for them, but not on what they reset
  to, which is a constant per bit and is written out as one word. Where the
  registers are the whole port they are the port and take its name; where they
  are part of it the word stands beside the port and is wired to the bits it
  speaks for, since a net cannot be a register on some bits and a gate's
  output on others. Across the corpus fifty-nine output bits become nine
  words, leaving three standing alone; a multiply-accumulate goes from
  seventeen blocks to three, a ripple counter from five to two.
- A row of registers is split by the condition it asks rather than refused for
  not agreeing on one. A conditional asks its question of the whole word, so
  registers stepping alike on different conditions are not one word however
  alike they step; asked who shares a condition, they come apart into the
  words they are. An I2C controller keeps eight registers loaded together and
  was coming back as sixty-eight loaded apart, because the whole family had
  been turned away for disagreeing instead of grouped by what it disagreed on.
- A register that can hold its value is written as the enable it has, not as
  the choice it is made of. A stage that keeps its value does it by choosing
  between what comes next and its own output, and read as data that choice
  makes the stage look like it depends on whatever drives the enable: a row of
  them stops looking like a chain at all, which is how an enabled shift
  register came to be left as loose gates. Read as a condition, the enable is
  visible in both places, and the register is only spoken of where it moves.
- A net is written where it is defined, once, and the lines are put in the
  order they build on each other. A declaration in one place and an assignment
  in another is two lines saying what one says, and half the recovered text
  was the first kind. What is left can be read downwards, each term resting on
  what is above it.
- A term too wide to read is broken where it binds least tightly, with the
  operator at the front of each line where it can be seen against the ones
  above it. Folding gates into their readers makes plenty of terms half a
  screen wide, and a term that wide says nothing the broken one does not.
- `scripts/observe.py`: names a register from what it is watched doing. A
  layout carries no names out of the foundry: across the corpus not one net
  is called anything but a number, and the only words in the whole input are
  the ports. So a name cannot be recovered and would have to be invented,
  which is worse than a number, since it asserts something about a circuit
  nobody has ground truth for. What can be had honestly is what the design is
  seen to do: a word that only ever gains bits is flags, one that steps by one
  is a count, one whose bits move over by a place is a shift, one that changes
  a bit at a time is gray, and a bit that goes up once and stays is latched.
  Each is a claim about a whole run, dropped the moment one step contradicts
  it, and kept only where three runs from different starts saw the same thing.
  What no run settles keeps its number. A register carrying an output keeps
  the port's name, but what it was seen doing is said anyway, since that is
  usually the one register in a design a reader wants to know about. Renaming
  cannot change behaviour and the simulation that follows says so either way.
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
- A region proved and then thrown away is kept. Control roles are written a
  letter and an index, and the letter alone does not tell one from a role that
  merely starts the same way: clk read as the first of the c family and was
  asked to name a control net, so every region clocked by anything but the
  module's own clock port was refused after it had already proved. A ripple
  counter came back with one of its eight stages recovered and now comes back
  with all eight.
- A role driven by internal logic is named instead of costing the region a
  lift it had already proved. The name a region gives its port belongs to a
  copy whose nets have been split, and the two modules number their nets
  differently, so the name cannot be carried across as it stands: a region
  port called n20 is not the n20 that gets written out. It is matched back to
  the bit behind it and named from that.
- A row of registers no longer spreads one net across a word it does not fit.
  A net beside a word is not repeated across it in Verilog but padded with
  zeroes, which left every bit but the lowest answering to nothing; it is now
  written out once per bit. The question a conditional asks is the exception,
  being asked once of the whole word rather than of each bit in turn, and a
  question is not always the first thing in a term either, since a conditional
  can sit inside a larger one. No proof caught either: both are differences a
  miter sees as the same function of the same nets.
- A register carrying an output keeps the output's name rather than being put
  in a row, which says more about it than any row could and is what the rest
  of the module calls it by.
- A cone is left alone where the design offers more operands than an
  arithmetic form takes, instead of being wired to one that does not exist.
- A word shifted up is recognised as one. The test asked whether the bits kept
  were those of the shifted operand rather than of the operand itself, so a
  register shifting towards the top could never match and no left-shifting
  design was ever named for what it plainly was.
- Looking at a design twice reads the first look as work already done. The
  names this pass gives were being counted as names to number around, so a
  second run turned flags0 into flags2; and every name is now put aside before
  any is put back, since one register can be taking the name another gives up.
- A register whose name Verilog cannot spell plainly is started like any
  other. A name can be written with a backslash and closed by a space, and a
  netlist written back out is full of them: uo_out_reg[0] is one name and not
  an index into anything. Three of an I2C controller's registers were being
  passed over, so the netlist it was compared against ran holding no value in
  them and the outputs they carry were never compared at all. A run that still
  cannot start a module now says so rather than reporting what it found.
- Giving up on a proof is no longer treated as a disproof. A module too large
  to prove in the time allowed still has to be compiled and simulated, and
  that simulation is the only evidence it has left; the puzzle proves nothing
  whole and was being denied the run that speaks for it.
- A net written where it is defined counts as declared. What a lifted block
  names and does not drive is declared beside it, and the check for whether it
  is declared already was looking for a declaration standing on its own.
- A chain can be cut to the cells carrying one stage to the next, leaving what
  computes its serial input outside. Cut around everything feeding it, that
  logic arrives as several ports of its own and the region has more inputs
  than a shift register has roles.
