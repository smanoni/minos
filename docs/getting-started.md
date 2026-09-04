# Getting started

## Requirements

Versions minos is developed and tested against:

| Tool                                | Version  |
| ----------------------------------- | -------- |
| `yosys`                             | 0.33     |
| `iverilog`                          | 12.0     |
| Python                              | 3.12     |
| [`klayout`](../requirements.txt)    | 0.30.10  |

Other versions are likely to work; these are the ones every result reported
here was produced with.

```bash
pip install -r requirements.txt
```

The remaining dependencies are fetched by `make deps`.

```bash
make deps
make warmup
make puzzle
make corpus
```

* `make deps` fetches submodules, layouts, and the PDK.
* `make warmup` prepares the warmup design and its ground truth.
* `make puzzle` runs the puzzle design.
* `make corpus` prepares the nineteen Tiny Tapeout designs.

Use `DESIGN=<name>` to select a design. `MINOS_TIMEOUT` sets the solver timeout.

```bash
make run DESIGN=<name>
```

This runs the flow on a layout already present in `gds/`.

## Flow

Each stage reads the output of the previous stage. Intermediate files are written to `work/` and can be inspected or used to run individual stages.

### 1. GDS to DEF

`scripts/gds2def.py` extracts placement and connectivity from the GDS layout.

It reads cell dimensions and pin information from the PDK's LEF files, and uses KLayout's `LayoutToNetlist` to resolve the wiring. Standard-cell instances are identified from the GDS hierarchy. Instance names are generated in placement order.

The resulting DEF contains placement and connectivity, with routing resolved into nets. If a reference DEF is available, the script compares placement exactly and connectivity up to net renaming.

### 2. DEF to gates

`scripts/def2v.py` converts the DEF into a gate-level Verilog netlist.

From this stage onward, the physical geometry is no longer used.

### 3. Gates to a common basis

`generic.ys` reads the netlist and the PDK Liberty files, then maps the design to technology-independent gates.

A design that already has a netlist can enter the flow here with:

```bash
make netlist
```

Run:

```bash
make lec
```

This checks equivalence between the extracted netlist and the generic netlist.

### 4. Structure recovery

`scripts/structure.py` identifies candidate regions in the gate-level netlist, including shift chains, register banks, counters, and state registers.

`scripts/buses.py` groups individual bits into buses and extracts arithmetic structures such as full adders.

### 5. Behaviour recovery

`scripts/match.py` compares regions against reference modules from `common_cells`.

`scripts/lift.py` and `scripts/expr.py` recover higher-level behaviour, such as increments, shifts, holds, multiplexers, and case statements.

Each proposed representation is checked for equivalence. Proven regions are emitted as RTL. Regions that cannot be proven remain at a lower level.

### 6. Names

Layout files do not contain the original RTL signal names.

`scripts/observe.py` can identify registers from their observed behaviour. Other names are inferred from the recovered structure.

### 7. Verification

| Command           | Check                                        |
| ----------------- | -------------------------------------------- |
| `make lec`        | Extracted netlist equals the generic netlist |
| `make lec-rtl`    | Lifted RTL matches the reference             |
| `make lec-lifted` | Recovered RTL equals the original netlist    |

`lec-lifted` first attempts temporal induction, then bounded checks at 40, 20, and 10 cycles.

A failed or incomplete proof is reported as such. It is not treated as a successful verification.

`scripts/cosim.py` also simulates the recovered RTL alongside the netlist.

### 8. Evaluation

```bash
make score
make sources
```

`make sources` fetches the reference sources at pinned commits.

`make score` compares recovered structure against the original source where available.

The corpus contains nineteen Tiny Tapeout designs, plus the puzzle and warmup designs.

## Solving the puzzle

```bash
make jsc-puzzle
```

`scripts/itersat.py` searches for an input that makes the design assert `success`.

The design is unrolled for a variable number of cycles. Reset is released, the design is enabled, and `success` is constrained to `1`.

The search doubles the depth until a solution is found, then bisects the interval to find the shortest solution.

## Repository layout

| Path                   | Description                      |
| ---------------------- | -------------------------------- |
| `scripts/gds2def.py`   | GDS and PDK to DEF               |
| `scripts/def2v.py`     | DEF to gate-level Verilog        |
| `scripts/generic.ys`   | Technology-independent synthesis |
| `scripts/structure.py` | Candidate regions                |
| `scripts/buses.py`     | Bus and arithmetic recovery      |
| `scripts/match.py`     | Region equivalence checking      |
| `scripts/lift.py`      | Behavioural RTL recovery         |
| `scripts/expr.py`      | RTL expression recovery          |
| `scripts/emit.py`      | Hierarchy reconstruction         |
| `scripts/cosim.py`     | RTL/netlist simulation           |
| `scripts/observe.py`   | Behaviour-based naming           |
| `scripts/itersat.py`   | Puzzle input search              |
| `scripts/score.py`     | Structure evaluation             |
| `gds/`                 | Input layouts                    |
| `deps/`                | Submodules                       |
| `work/`                | Generated files                  |

## Limitations

Control-flow recovery is currently limited.

The matcher can identify operands that are nets, but state-machine branches often contain expressions rather than direct nets. As a result, only a small number of `case` statements are currently recovered.
