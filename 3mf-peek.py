import argparse
import zipfile
import re
import math
import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

class BambuMaster:
    def __init__(self, file_path):
        self.file_path = file_path
        self.filament_area = math.pi * (1.75 / 2)**2
        # 2026 CAD Pricing
        self.prices = {"Bambu": 0.026, "Sunlu": 0.022} # Cost per gram (approx)

        self.settings = {
            'walls': 'N/A', 'infill': 'N/A', 'no_fan_layers': 0,
            'temp_init': None, 'temp_rest': None
        }
        self.ams_mapping = {}  # Tool ID -> {"type": str, "usage": float(g)}
        self.segments = []
        self.metrics = [] # Can store speed or flow depending on arg

    def _parse_metadata(self, zf):
        """Extracts slicer settings from XML configs within the 3MF."""
        for name in zf.namelist():
            if name.endswith(('.config', '.xml')) and 'Metadata' in name:
                with zf.open(name) as f:
                    content = f.read().decode('utf-8', errors='ignore')
                    m = re.search(r'"wall_loops"\s*:\s*"(\d+)"', content)
                    if m: self.settings['walls'] = m.group(1)
                    m = re.search(r'"sparse_infill_density"\s*:\s*"(\d+)', content)
                    if m: self.settings['infill'] = m.group(1)
                    m = re.search(r'"close_fan_the_first_x_layers"\s*:\s*\[\s*"(\d+)"', content)
                    if m: self.settings['no_fan_layers'] = m.group(1)

    def process(self, mode='flow', max_layers=10):
        with zipfile.ZipFile(self.file_path, 'r') as z:
            self._parse_metadata(z)
            with z.open('Metadata/plate_1.gcode') as f:
                x, y, e, f_speed = 0.0, 0.0, 0.0, 0.0
                layer_count = 0
                current_tool = "T0"

                for line in f:
                    l = line.decode('utf-8').strip()
                    if not l: continue

                    # 1. AMS & Layer Logic
                    if l.startswith("T"):
                        t_match = re.match(r'T(\d+)', l)
                        if t_match: current_tool = f"T{t_match.group(1)}"
                    if ";LAYER_CHANGE" in l: layer_count += 1

                    # 2. Temperature Logic (skip comment lines)
                    if l.startswith("M140") and not self.settings['temp_init']:
                        s = re.search(r'S(\d+)', l)
                        if s: self.settings['temp_init'] = s.group(1)

                    # 3. Path & Math
                    if l.startswith("G1"):
                        coords = {m.group(0)[0]: float(m.group(0)[1:]) for m in re.finditer(r'[XYEF]-?\d*\.?\d+', l)}
                        nx, ny, ne, nf = coords.get('X', x), coords.get('Y', y), coords.get('E', e), coords.get('F', f_speed)

                        dist = math.sqrt((nx-x)**2 + (ny-y)**2)
                        de = ne - e

                        if dist > 0 and de > 0:
                            # Tracking AMS Usage (1.24 g/cm3 for PLA)
                            usage_g = (de * self.filament_area * 1.24) / 1000
                            self.ams_mapping.setdefault(current_tool, {"usage": 0})["usage"] += usage_g

                            # Visualization Data
                            if layer_count <= max_layers:
                                self.segments.append(((x, y), (nx, ny)))
                                if mode == 'flow':
                                    flow = (de * self.filament_area) / (dist / (nf / 60))
                                    self.metrics.append(flow)
                                else:
                                    self.metrics.append(nf / 60)

                        x, y, e, f_speed = nx, ny, ne, nf

    def visualize(self, mode='flow'):
        if not self.segments: return
        cmap = 'magma' if mode == 'flow' else 'viridis'
        label = 'Volumetric Flow (mm³/s)' if mode == 'flow' else 'Speed (mm/s)'
        vmax = 30 if mode == 'flow' else 300

        lc = LineCollection(self.segments, cmap=cmap, norm=plt.Normalize(0, vmax))
        lc.set_array(np.array(self.metrics))

        fig, ax = plt.subplots(figsize=(10, 8))
        line = ax.add_collection(lc)
        ax.set_xlim(0, 256); ax.set_ylim(0, 256)
        ax.set_title(f"Bambu P2S Diagnostic: {mode.capitalize()}")
        plt.show()

    def print_report(self):
        print(f"\n--- 🖨️  BAMBU P2S PRINT MANIFEST ---")
        print(f"Walls: {self.settings['walls']} | Infill: {self.settings['infill']}%")
        print(f"Bed Temp: {self.settings['temp_init']}°C | Fan-Off: {self.settings['no_fan_layers']} layers")
        print(f"\n--- 🧩 AMS & COST (CAD) ---")
        total_cost = 0
        for t, data in self.ams_mapping.items():
            cost = data['usage'] * self.prices['Bambu']
            total_cost += cost
            print(f"{t}: {data['usage']:.2f}g (~${cost:.2f} CAD)")
        print(f"TOTAL ESTIMATED COST: ${total_cost:.2f} CAD\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Path to .3mf")
    parser.add_argument("--mode", choices=['flow', 'speed'], default='flow')
    parser.add_argument("--layers", type=int, default=5)
    args = parser.parse_args()

    bm = BambuMaster(args.file)
    bm.process(mode=args.mode, max_layers=args.layers)
    bm.print_report()
    bm.visualize(mode=args.mode)
