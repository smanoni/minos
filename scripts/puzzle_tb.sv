// Copyright 2026 Simone Manoni.
// Licensed under the Apache License, Version 2.0, see LICENSE for details.
// SPDX-License-Identifier: Apache-2.0
//
// Simone Manoni <simone.manoni2@gmail.com>

// Replays a solved key through a recovered netlist. Every driven signal is
// read from the key file rather than set here, so the run repeats what the
// solver was asked and assumes nothing it did not.
module tb;
  reg clk = 0;
  reg rst_n, enable, I;
  wire [7:0] O;
  wire success;

  reg [2:0] key [0:4095];
  integer i, n;
  reg [8*256-1:0] path;

  puzzle dut(.I(I), .O(O), .clk(clk), .enable(enable), .rst_n(rst_n),
             .success(success));

  // What the design says once it has been answered correctly is the answer to
  // the puzzle, and it says it one character at a time after the flag is up.
  task report;
    begin
      $write("message:");
      if (O >= 32 && O < 127) $write("%c", O);
      for (n = 0; n < 64; n = n + 1) begin
        #5 clk = 1; #5 clk = 0;
        if (O >= 32 && O < 127) $write("%c", O);
      end
      $write("\n");
    end
  endtask

  initial begin
    if (!$value$plusargs("key=%s", path)) path = "work/puzzle_key.txt";
    $readmemb(path, key);
    for (i = 0; i < 4096; i = i + 1) begin
      if (key[i] === 3'bx) begin
        $display("no success in %0d cycles", i);
        $fatal(1);
      end
      {rst_n, enable, I} = key[i];
      #5 clk = 1; #5 clk = 0;
      if (success) begin
        $display("success on cycle %0d", i + 1);
        report;
        $finish;
      end
    end
  end
endmodule
