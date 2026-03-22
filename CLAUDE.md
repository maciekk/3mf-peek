# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A single-file Python CLI tool that analyzes Bambu Lab 3MF files (ZIP archives containing G-code and slicer metadata). It parses `Metadata/plate_1.gcode` to produce filament usage reports, cost estimates (CAD), and matplotlib visualizations of print paths colored by volumetric flow or speed.

## Running

```bash
python 3mf-peek.py example.gcode.3mf                    # default: flow mode, 5 layers
python 3mf-peek.py example.gcode.3mf --mode speed       # speed visualization
python 3mf-peek.py example.gcode.3mf --layers 10        # visualize more layers
```

Dependencies: `numpy`, `matplotlib` (no requirements file — install manually).

## Architecture

Everything lives in `3mf-peek.py` inside the `BambuMaster` class:

- **`_parse_metadata(zf)`** — Reads `.config`/`.xml` files from the 3MF ZIP for slicer settings (wall_loops, sparse_infill_density).
- **`process(mode, max_layers)`** — Line-by-line G-code parser. Tracks tool changes (`T` commands), layer changes, bed temp (`M140`), fan activation (`M106`), and `G1` moves. Computes extrusion deltas, filament usage per AMS tool, and flow/speed metrics for visualization segments.
- **`visualize(mode)`** — Renders XY print paths as a matplotlib `LineCollection` with colormap. Bed bounds are hardcoded to 256×256mm (P2S).
- **`print_report()`** — Prints settings and per-tool cost breakdown.

## Key Constants

- Filament diameter: 1.75mm, density: 1.24 g/cm³ (PLA)
- Pricing: Bambu $0.026/g, Sunlu $0.022/g (2026 CAD)
- G-code coordinate regex: `[XYEF]-?\d*\.?\d+` (must handle values like `E.01755` with no leading zero and negative values)
