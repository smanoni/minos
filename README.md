<div align="center">

  <img src="docs/img/minos_logo.png" alt="minos logo" width="280">

# minos

**A silicon decompiler**

</div>

minos reconstructs RTL from the physical layout of a digital circuit. It can be useful for understanding existing silicon, recovering RTL when the original source is unavailable, and checking recovered designs against the gates they came from.

Given a GDS layout and the PDK used to build it, minos extracts the circuit, identifies its structure, and attempts to recover an equivalent SystemVerilog description. The output is intended to describe the design at the RTL level: registers, buses, arithmetic, and control flow, rather than a gate-level netlist.

Every transformation is checked for equivalence against the extracted circuit. If minos cannot prove that a proposed RTL representation implements the same logic, it does not emit that representation.

The project started with the [Jane Street ASIC puzzle 2026](https://github.com/janestreet/asic-puzzle-2026), which provides a layout and asks the solver to determine what it computes.

<div align="center">

[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

[Getting started](docs/getting-started.md) •
[Against a C decompiler](#against-a-c-decompiler) •
[License](#license)

</div>

> **Under active development.** Interfaces, supported designs, and output quality are still changing.

## Against a C decompiler

A C decompiler reconstructs a higher-level program from a compiled binary. minos performs a similar reconstruction on a digital circuit, but the available information and the guarantees are different.

|                  | C decompiler                       | minos                                                |
| ---------------- | ---------------------------------- | ---------------------------------------------------- |
| Input            | Stripped binary                    | GDS layout and PDK                                   |
| Information lost | Symbols, types, source structure   | Names, hierarchy, RTL structure                      |
| Recovered        | Functions, variables, control flow | Modules, registers, buses, arithmetic, control flow  |
| Names            | Usually invented                   | Invented, or recovered from observed behavior        |
| Correctness      | Best effort                        | Proposed representations are checked for equivalence |

The distinction is the last row. A software decompiler generally has to choose a plausible interpretation of the binary and leave validation to the user. minos can instead check each proposed representation against the finite-state circuit it came from.

This does not make the reconstruction problem easy. It makes it possible to separate what has been recovered from what has merely been guessed.

When minos cannot prove a higher-level representation, it leaves the corresponding logic at a lower level. In particular, recovering control flow remains one of the more difficult parts of the process.

## Getting started

See [docs/getting-started.md](docs/getting-started.md) for the complete workflow, including the individual stages, how to run them, and the layout of the repository.

To fetch the dependencies and prepare the example designs:

```bash
make deps
make warmup
```

The warmup design includes a ground-truth implementation that can be used to compare the recovered RTL against the original.

## License

minos is licensed under the Apache License 2.0. See [LICENSE](LICENSE).

The layouts under `gds/` and the submodules under `deps/` retain the licenses of their respective projects.
