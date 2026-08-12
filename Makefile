# Copyright 2026 Simone Manoni.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Simone Manoni <simone.manoni2@gmail.com>

PYTHON    ?= python3
YOSYS     ?= yosys
IVERILOG  ?= iverilog
VVP       ?= vvp
KLAYOUT   ?= klayout

SCRIPTS   ?= scripts
DEPS      ?= deps
WORKDIR   ?= work
GDSDIR    ?= gds
PDK_ROOT  ?= pdk
PDK       ?= sky130A

PUZZLE    := $(DEPS)/asic-puzzle-2026

# Layouts to reverse engineer, as name:path pairs. `make gds` links each one
# into $(GDSDIR) as <name>.gds. Append to this to pull in layouts from a new
# dependency; drop your own .gds straight into $(GDSDIR) to skip it entirely.
GDS_SOURCES ?= warmup:$(PUZZLE)/warmup/04_final.gds \
               puzzle:$(PUZZLE)/puzzle.gds
STDCELLS  := $(PDK_ROOT)/$(PDK)/libs.ref/sky130_fd_sc_hd
LIBERTY   := $(STDCELLS)/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
MODELS    := $(STDCELLS)/verilog/primitives.v $(STDCELLS)/verilog/sky130_fd_sc_hd.v
SIMFLAGS  := -g2012 -DFUNCTIONAL -DUNIT_DELAY=\#1

PDK_TAG   := sky130-ff08c23db8359afce3f134c454e7930586d0641c
PDK_URL   := https://github.com/fossi-foundation/ciel-releases/releases/download/$(PDK_TAG)
PDK_PARTS := common sky130_fd_pr sky130_fd_pr_reram sky130_fd_io sky130_ml_xx_hd \
             sky130_sram_macros sky130_fd_sc_hd sky130_fd_sc_hdll sky130_fd_sc_hs \
             sky130_fd_sc_hvl sky130_fd_sc_lp sky130_fd_sc_ls sky130_fd_sc_ms

.PHONY: all gds warmup puzzle check sim lyp pdk clean distclean

all: warmup puzzle

$(WORKDIR):
	mkdir -p $@

gds:
	@mkdir -p $(GDSDIR)
	@for s in $(GDS_SOURCES); do \
		name=$${s%%:*}; path=$${s#*:}; \
		if [ ! -e "$$path" ]; then \
			echo "missing $$path (git submodule update --init --recursive?)"; \
			exit 1; \
		fi; \
		ln -sfn ../$$path $(GDSDIR)/$$name.gds; \
		echo "  $(GDSDIR)/$$name.gds -> $$path"; \
	done

.PHONY: warmup
warmup: gds | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/gds2def.py $(GDSDIR)/warmup.gds $(PDK_ROOT)/$(PDK) \
		$(WORKDIR)/warmup.def $(PUZZLE)/warmup/03_post_place_and_route.def
	$(PYTHON) $(SCRIPTS)/def2v.py $(WORKDIR)/warmup.def $(WORKDIR)/warmup.v \
		$(PUZZLE)/warmup/01_netlist.v
	$(MAKE) generic DESIGN=warmup TOP=adder_demo

.PHONY: puzzle
puzzle: gds | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/gds2def.py $(GDSDIR)/puzzle.gds $(PDK_ROOT)/$(PDK) \
		$(WORKDIR)/puzzle.def
	$(PYTHON) $(SCRIPTS)/def2v.py $(WORKDIR)/puzzle.def $(WORKDIR)/puzzle.v
	$(MAKE) generic DESIGN=puzzle TOP=puzzle

.PHONY: generic
generic:
	sed -e 's|LIBERTY|$(LIBERTY)|' \
	    -e 's|IN_V|$(WORKDIR)/$(DESIGN).v|' \
	    -e 's|TOP|$(TOP)|' \
	    -e 's|LOG|$(WORKDIR)/$(DESIGN)_generic.log|' \
	    -e 's|OUT_FAITHFUL_JSON|$(WORKDIR)/$(DESIGN)_faithful.json|' \
	    -e 's|OUT_FAITHFUL_V|$(WORKDIR)/$(DESIGN)_faithful.v|' \
	    -e 's|OUT_GENERIC_JSON|$(WORKDIR)/$(DESIGN)_generic.json|' \
	    -e 's|OUT_GENERIC_V|$(WORKDIR)/$(DESIGN)_generic.v|' \
	    $(SCRIPTS)/generic.ys > $(WORKDIR)/$(DESIGN)_generic.ys
	$(YOSYS) -q -s $(WORKDIR)/$(DESIGN)_generic.ys

check: warmup

sim: | $(WORKDIR)
	$(IVERILOG) $(SIMFLAGS) -o $(WORKDIR)/warmup_sim.vvp \
		$(MODELS) $(WORKDIR)/warmup.v test/warmup_tb.v
	$(VVP) $(WORKDIR)/warmup_sim.vvp
	$(IVERILOG) -g2012 -o $(WORKDIR)/warmup_generic_sim.vvp \
		$(WORKDIR)/warmup_generic.v test/warmup_tb.v
	$(VVP) $(WORKDIR)/warmup_generic_sim.vvp

lyp: | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/gen_layer_props.py \
		$(PDK_ROOT)/$(PDK)/libs.tech/klayout/tech/$(PDK).lyp $(WORKDIR)/minos_$(PDK).lyp

view: lyp gds
	$(KLAYOUT) -l $(WORKDIR)/minos_$(PDK).lyp $(GDSDIR)/puzzle.gds

pdk:
	mkdir -p $(PDK_ROOT)/.download
	for p in $(PDK_PARTS); do \
		test -s $(PDK_ROOT)/.download/$$p.tar.zst || \
			curl -fsSL -o $(PDK_ROOT)/.download/$$p.tar.zst $(PDK_URL)/$$p.tar.zst; \
		tar --use-compress-program=unzstd -xf $(PDK_ROOT)/.download/$$p.tar.zst -C $(PDK_ROOT); \
	done

clean:
	rm -rf $(WORKDIR)
	find $(GDSDIR) -type l -delete 2>/dev/null || true

distclean: clean
	rm -rf $(PDK_ROOT)
