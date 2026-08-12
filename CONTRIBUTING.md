# Contributing

Contributions are welcome. Please keep to the conventions already in the tree.

- Every source file carries the SPDX header and an author line.
- Python scripts live in `scripts/` and take their paths as arguments, so the
  `Makefile` owns all layout decisions.
- Every stage checks itself. If you add one, add the check with it: the warmup
  design ships ground truth for the whole flow, and there is no reason to
  guess whether a stage works.
- Dependencies are submodules under `deps/`. Generated and downloaded content
  goes in `work/`, `pdk/` and `klayout/`, all of which are ignored.
