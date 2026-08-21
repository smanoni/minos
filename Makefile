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
CURL      ?= curl -fsSL

SCRIPTS   ?= scripts
DEPS      ?= deps
WORKDIR   ?= work
TMPDIR    := $(WORKDIR)/tmp
GDSDIR    ?= gds
GDSLOCK   ?= gds.lock
PDK_ROOT  ?= pdk
PDK       ?= sky130A

PUZZLE    := $(DEPS)/asic-puzzle-2026

# Hardened macros from a Tiny Tapeout shuttle, used as a corpus of layouts
# from a flow that is not ours. Every project on a shuttle is listed in its
# index; `make corpus` runs the whole flow over each one named here.
TT_SHUTTLE  ?= tt09
TT_ASSETS   := https://shuttle-assets.tinytapeout.com/$(TT_SHUTTLE)
TT_INDEX    := https://index.tinytapeout.com/$(TT_SHUTTLE).json
TT_PROJECTS ?= tt_um_wokwi_413387352465821697 \
               tt_um_lfsr_stevej \
               tt_um_LFSR_Encrypt \
               tt_um_shifter \
               tt_um__kwr_lfsr__top \
               tt_um_claudiotalarico_counter \
               tt_um_wokwi_413386973689694209 \
               tt_um_wokwi_413919500942601217 \
               tt_um_wokwi_411783629732984833 \
               tt_um_4x4multiplier \
               tt_um_B_14_array_multiplier \
               tt_um_db_MAC \
               tt_um_urish_simon \
               tt_um_I2C \
               tt_um_anas_7193 \
               tt_um_MichaelBell_hd_8b10b \
               tt_um_perceptron_mtchun \
               tt_um_mroblesh \
               tt_um_juarez_jimenez

# Layouts to reverse engineer, as name:source pairs, source being a path or a
# URL. `make gds` links or downloads each into $(GDSDIR) as <name>.gds; append
# to add one of your own. $(GDSDIR) is disposable, `make clean` empties it.
GDS_SOURCES ?= warmup:$(PUZZLE)/warmup/04_final.gds \
               puzzle:$(PUZZLE)/puzzle.gds \
               $(foreach p,$(TT_PROJECTS),$(p):$(TT_ASSETS)/$(p)/$(p).gds)
STDCELLS  := $(PDK_ROOT)/$(PDK)/libs.ref/sky130_fd_sc_hd
LIBERTY   := $(STDCELLS)/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
MODELS    := $(STDCELLS)/verilog/primitives.v $(STDCELLS)/verilog/sky130_fd_sc_hd.v
SIMFLAGS  := -g2012 -DFUNCTIONAL -DUNIT_DELAY=\#1

# What tells the synthesiser what the cells in a netlist do. A layout is read
# against the PDK's Liberty; a netlist that arrives in some other technology
# brings its own, and `make netlist` overrides this with Verilog models.
READ_CELLS ?= read_liberty -ignore_miss_func -ignore_miss_dir $(LIBERTY)

# Reference modules to recognise in a recovered netlist, as module:param=value
# pairs taken straight from common_cells. `make cc` elaborates each onto the
# gate basis the recovered netlists use; omitted parameters keep their default.
SV2V      ?= sv2v
CC        := $(DEPS)/common_cells
SV2VFLAGS := -DSYNTHESIS -DASSERTS_OFF -I$(CC)/include

PDK_TAG   := sky130-ff08c23db8359afce3f134c454e7930586d0641c
PDK_URL   := https://github.com/fossi-foundation/ciel-releases/releases/download/$(PDK_TAG)
PDK_PARTS := common sky130_fd_pr sky130_fd_pr_reram sky130_fd_io sky130_ml_xx_hd \
             sky130_sram_macros sky130_fd_sc_hd sky130_fd_sc_hdll sky130_fd_sc_hs \
             sky130_fd_sc_hvl sky130_fd_sc_lp sky130_fd_sc_ls sky130_fd_sc_ms

.PHONY: all deps gds warmup puzzle check sim lyp pdk clean distclean

all: warmup puzzle

$(WORKDIR) $(TMPDIR):
	mkdir -p $@

# Everything a fresh clone needs before any flow will run: submodules for the
# sources, $(GDSDIR) for the layouts. Downloads have no commit to pin them to,
# so their checksum is recorded in $(GDSLOCK) on first fetch and enforced after.
deps:
	@git submodule update --init --recursive
	@$(MAKE) --no-print-directory gds

gds:
	@mkdir -p $(GDSDIR); touch $(GDSLOCK)
	@for s in $(GDS_SOURCES); do \
		name=$${s%%:*}; src=$${s#*:}; out=$(GDSDIR)/$$name.gds; \
		case "$$src" in \
		*://*) \
			test -s $$out || $(CURL) -o $$out $$src || exit 1; \
			want=`sed -n "s|^$$name  *||p" $(GDSLOCK)`; \
			got=`sha256sum $$out | cut -d' ' -f1`; \
			if [ -z "$$want" ]; then \
				echo "$$name $$got" >> $(GDSLOCK); \
				echo "  pinned $$name $$got"; \
			elif [ "$$want" != "$$got" ]; then \
				echo "$$out does not match $(GDSLOCK), refusing to use it"; \
				exit 1; \
			fi;; \
		*) \
			if [ ! -e "$$src" ]; then \
				echo "missing $$src (make deps?)"; exit 1; \
			fi; \
			ln -sfn ../$$src $$out;; \
		esac; \
	done
	@echo "  $(words $(GDS_SOURCES)) layouts in $(GDSDIR)"

.PHONY: warmup
warmup: deps | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/gds2def.py $(GDSDIR)/warmup.gds $(PDK_ROOT)/$(PDK) \
		$(WORKDIR)/warmup.def $(PUZZLE)/warmup/03_post_place_and_route.def
	$(PYTHON) $(SCRIPTS)/def2v.py $(WORKDIR)/warmup.def $(WORKDIR)/warmup.v \
		$(PUZZLE)/warmup/01_netlist.v
	$(MAKE) generic DESIGN=warmup TOP=adder_demo
	$(MAKE) structure DESIGN=warmup
	$(MAKE) match DESIGN=warmup
	$(MAKE) lift DESIGN=warmup
	-$(MAKE) emit DESIGN=warmup

.PHONY: puzzle
puzzle: deps | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/gds2def.py $(GDSDIR)/puzzle.gds $(PDK_ROOT)/$(PDK) \
		$(WORKDIR)/puzzle.def
	$(PYTHON) $(SCRIPTS)/def2v.py $(WORKDIR)/puzzle.def $(WORKDIR)/puzzle.v
	$(MAKE) generic DESIGN=puzzle TOP=puzzle
	$(MAKE) structure DESIGN=puzzle
	$(MAKE) match DESIGN=puzzle
	$(MAKE) lift DESIGN=puzzle
	-$(MAKE) emit DESIGN=puzzle

.PHONY: generic
generic: | $(TMPDIR)
	sed -e 's|READ_CELLS|$(READ_CELLS)|' \
	    -e 's|IN_V|$(WORKDIR)/$(DESIGN).v|' \
	    -e 's|TOP|$(TOP)|' \
	    -e 's|LOG|$(TMPDIR)/$(DESIGN)_generic.log|' \
	    -e 's|OUT_FAITHFUL_JSON|$(WORKDIR)/$(DESIGN)_faithful.json|' \
	    -e 's|OUT_FAITHFUL_V|$(WORKDIR)/$(DESIGN)_faithful.v|' \
	    -e 's|OUT_GENERIC_JSON|$(WORKDIR)/$(DESIGN)_generic.json|' \
	    -e 's|OUT_GENERIC_V|$(WORKDIR)/$(DESIGN)_generic.v|' \
	    $(SCRIPTS)/generic.ys > $(TMPDIR)/$(DESIGN)_generic.ys
	$(YOSYS) -q -s $(TMPDIR)/$(DESIGN)_generic.ys

# A design that came as a gate netlist rather than as a layout. Reading a
# layout is what the first two steps of the flow are for, so a netlist joins
# the flow where they leave off, and everything actually in question is asked
# of it exactly as it is asked of a layout: which flops belong together, what
# the logic between them says, where the hierarchy went. What the cells do
# still has to come from somewhere, and where there is no Liberty file for
# them the gate library's own description is turned into models first.
.PHONY: netlist
netlist: | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/hgl2v.py $(HGL) $(WORKDIR)/$(DESIGN)_cells.v
	cp $(NETLIST) $(WORKDIR)/$(DESIGN).v
	$(MAKE) generic DESIGN=$(DESIGN) TOP=$(TOP) \
		READ_CELLS="read_verilog $(WORKDIR)/$(DESIGN)_cells.v"
	$(MAKE) structure DESIGN=$(DESIGN)
	$(MAKE) lift DESIGN=$(DESIGN)

.PHONY: structure
structure:
	$(PYTHON) $(SCRIPTS)/structure.py \
		$(WORKDIR)/$(DESIGN)_generic.json $(WORKDIR)/$(DESIGN)_regions.json

# The whole flow on any layout already sitting in $(GDSDIR), with the top cell
# read back from the recovered netlist rather than given.
.PHONY: run
run: | $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/gds2def.py $(GDSDIR)/$(DESIGN).gds $(PDK_ROOT)/$(PDK) \
		$(WORKDIR)/$(DESIGN).def
	$(PYTHON) $(SCRIPTS)/def2v.py $(WORKDIR)/$(DESIGN).def $(WORKDIR)/$(DESIGN).v
	$(MAKE) generic DESIGN=$(DESIGN) \
		TOP=`sed -n 's/^module \([A-Za-z_][A-Za-z0-9_]*\).*/\1/p' \
			$(WORKDIR)/$(DESIGN).v | head -1`
	$(MAKE) structure DESIGN=$(DESIGN)
	$(MAKE) lift DESIGN=$(DESIGN)

.PHONY: corpus
corpus: deps
	@rc=0; for p in $(TT_PROJECTS); do \
		echo "======== $$p"; \
		$(MAKE) --no-print-directory run DESIGN=$$p || rc=1; \
	done; exit $$rc

# Recovered RTL is meant to be read and simulated, and a proof reads it
# through yosys alone, so it is compiled as well before it counts as done.
# The proof also has no model of a clock net, which leaves the edge a design
# runs on unchecked by it; simulating the recovered RTL beside the netlist
# closes that, so it is run wherever there is a simulator to run it with.
# The same simulator is what names a register, since the layout carries no
# name out of the foundry and watching one is the only honest way to say what
# it is for. Naming comes before the comparison so the comparison covers it.
#
# A model is asked last and only when one is named, since it is the one step
# here that guesses. What it says about a net is worth having where a run
# never reached one, and worth less than a run wherever a run did, so it is
# offered only the nets nothing else has named. Setting MINOS_MODEL turns it
# on; without it the flow runs as it always has, which is the point.
.PHONY: lift
lift:
	YOSYS="$(YOSYS)" $(PYTHON) $(SCRIPTS)/lift.py \
		$(WORKDIR)/$(DESIGN)_generic.json $(WORKDIR)/$(DESIGN)_regions.json \
		$(WORKDIR) $(WORKDIR)/$(DESIGN)_lifted.sv
	@command -v $(firstword $(IVERILOG)) >/dev/null || exit 0; \
	$(IVERILOG) -g2012 -o /dev/null $(WORKDIR)/$(DESIGN)_lifted.sv
	@command -v $(firstword $(VVP)) >/dev/null || exit 0; \
	IVERILOG="$(IVERILOG)" VVP="$(VVP)" \
		$(PYTHON) $(SCRIPTS)/observe.py $(WORKDIR)/$(DESIGN)_generic.json \
		$(WORKDIR)/$(DESIGN)_lifted.sv $(WORKDIR)
	@[ -n "$(MINOS_MODEL)" ] || exit 0; \
	MINOS_MODEL="$(MINOS_MODEL)" \
		$(PYTHON) $(SCRIPTS)/infer.py $(WORKDIR)/$(DESIGN)_lifted.sv
	@YOSYS="$(YOSYS)" IVERILOG="$(IVERILOG)" VVP="$(VVP)" \
		$(PYTHON) $(SCRIPTS)/cosim.py $(WORKDIR)/$(DESIGN)_generic.json \
		$(WORKDIR)/$(DESIGN)_lifted.sv $(WORKDIR)

.PHONY: emit
emit:
	YOSYS="$(YOSYS)" $(PYTHON) $(SCRIPTS)/emit.py \
		$(WORKDIR)/$(DESIGN)_generic.json $(WORKDIR)/$(DESIGN)_regions.json \
		$(WORKDIR) $(WORKDIR)/$(DESIGN)_rtl.sv

.PHONY: match
match: cc
	YOSYS="$(YOSYS)" $(PYTHON) $(SCRIPTS)/match.py \
		$(WORKDIR)/$(DESIGN)_generic.json $(WORKDIR)/$(DESIGN)_regions.json \
		$(WORKDIR)/lib $(WORKDIR)

# The library is not built to a fixed size any more. A design's counter is as
# wide as it is, and a reference built at one width has nothing to offer any
# design that does not happen to share it: the corpus holds a 26 bit counter
# that a library built at 8 never matched. What is needed is elaborated when a
# region asks for it, by scripts/match.py, and kept in work/lib once built.
.PHONY: cc
cc: $(TMPDIR)/common_cells.v
	@mkdir -p $(WORKDIR)/lib $(TMPDIR)
	@echo "  common_cells ready to elaborate on demand"

$(TMPDIR)/common_cells.v: | $(WORKDIR)
	$(SV2V) $(SV2VFLAGS) $(CC)/src/*.sv > $@ 2> $(TMPDIR)/common_cells_sv2v.log

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
	rm -rf $(WORKDIR) $(GDSDIR)

distclean: clean
	rm -rf $(PDK_ROOT)
