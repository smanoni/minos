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
| `scripts/infer.py` | asks a model what a net looks like it is for |
| `scripts/nameval.py` | scores a model against the names a run earned |
| `gds/` | input layouts, populated by `make deps` |
| `gds.lock` | checksums pinning the downloaded layouts |
| `work/` | generated output |

## Naming, and the optional model

A layout carries no names out of the foundry, so every name in the recovered
RTL is one the flow worked out. Three things give one, in order of what they
are worth: a port, which came in with the layout; a run, which watched the
design and can say a register counts or shifts or only ever gains bits; and
the shape a row of registers steps in, which says what it was for.

Whatever is still a number after that can be guessed at by a local model, off
by default because the flow has to work without one:

    make lift DESIGN=<name> MINOS_MODEL=qwen2.5-coder:7b-instruct-q4_K_M

It answers from a fixed list of words and never writes prose, it is shown the
logic with the design's own name held back, and it is offered only the nets
nothing else has named. Every name it gives is marked
`// inferred, unverified` where the net is declared and counted at the top of
the module, because a guess that reads like a measurement is worse than a
number. Point `MINOS_MODEL_HOST` at anything speaking the ollama API.

Registers and combinational nets get separate lists, being separate questions:
a wire is never a shifter and a register is never a clock. A design has around
ten times as many nameless wires as registers and asking about all of them
would take longer than the rest of the flow, so the ones asked about are the
ones a reader carries furthest — `MINOS_NAMES` sets how many, 24 by default.
A wire defined three lines above its only reader costs nothing to hold, and a
name would only lengthen the line.

Both lists are short, and shorter than they were: every word on them earned
its place by being an answer some net actually came back as. `MINOS_AHEAD=1`
puts a question a second time showing what a net feeds where asking what it is
built from gave nothing; it is off because on this corpus it turned one
unknown into a name out of some four hundred and forty asked, for twice the
running time.

`scripts/nameval.py` is why 7B and not smaller: it hides the names a run
earned, asks the model for them back, and scores what comes out.

    MINOS_MODEL=<model> python3 scripts/nameval.py work
