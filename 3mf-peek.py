import argparse
import zipfile
import re
import math
import json
import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

class BambuMaster:
    def __init__(self, file_path):
        self.file_path = file_path
        self.filament_area = math.pi * (1.75 / 2)**2
        self.filament_density = 1.24  # PLA default, overridden from metadata
        # 2026 CAD Pricing
        self.prices = {"bambu": 0.026, "sunlu": 0.022}  # Cost per gram (approx)
        self.filament_vendors = {}  # Tool ID -> vendor name

        self.settings = {
            'walls': 'N/A', 'infill': 'N/A', 'no_fan_layers': 0,
            'temp_init': None, 'temp_rest': None,
            # Print basics
            'layer_height': 'N/A', 'nozzle_diameter': 'N/A',
            'filament_type': 'N/A', 'object_name': 'N/A',
            'print_time': 'N/A', 'total_weight': 'N/A',
            # Speed & flow
            'outer_wall_speed': 'N/A', 'inner_wall_speed': 'N/A',
            'sparse_infill_speed': 'N/A', 'travel_speed': 'N/A',
            'max_volumetric_speed': 'N/A',
            # Adhesion & support
            'enable_support': 'N/A', 'support_type': 'N/A',
            'brim_type': 'N/A', 'brim_width': 'N/A', 'bed_type': 'N/A',
        }
        self.ams_mapping = {}  # Tool ID -> {"type": str, "usage": float(g)}
        self.segments = []
        self.metrics = []  # Can store speed or flow depending on arg

    @staticmethod
    def _get_val(data, key):
        """Get a value from config data, handling both scalars and arrays."""
        v = data.get(key)
        if v is None: return None
        if isinstance(v, list): return v[0] if v else None
        return v

    def _parse_metadata(self, zf):
        """Extracts slicer settings from metadata files within the 3MF."""
        self._parse_project_settings(zf)
        self._parse_plate_json(zf)
        self._parse_slice_info(zf)

    def _parse_project_settings(self, zf):
        try:
            data = json.loads(zf.read('Metadata/project_settings.config'))
        except (KeyError, json.JSONDecodeError):
            return
        g = lambda k: self._get_val(data, k)

        # Existing settings
        if g('wall_loops'): self.settings['walls'] = str(g('wall_loops'))
        infill = g('sparse_infill_density')
        if infill: self.settings['infill'] = str(infill).rstrip('%')
        fan = g('close_fan_the_first_x_layers')
        if fan: self.settings['no_fan_layers'] = fan

        # Print basics
        if g('layer_height'): self.settings['layer_height'] = g('layer_height')
        if g('nozzle_diameter'): self.settings['nozzle_diameter'] = g('nozzle_diameter')
        if g('filament_type'): self.settings['filament_type'] = g('filament_type')

        # Speed & flow
        for key in ['outer_wall_speed', 'inner_wall_speed', 'sparse_infill_speed', 'travel_speed']:
            if g(key): self.settings[key] = g(key)
        if g('filament_max_volumetric_speed'):
            self.settings['max_volumetric_speed'] = g('filament_max_volumetric_speed')

        # Adhesion & support
        if g('enable_support') is not None: self.settings['enable_support'] = str(g('enable_support'))
        if g('support_type'): self.settings['support_type'] = g('support_type')
        if g('brim_type'): self.settings['brim_type'] = g('brim_type')
        if g('brim_width'): self.settings['brim_width'] = g('brim_width')

        # Filament properties
        density = g('filament_density')
        if density:
            try: self.filament_density = float(density)
            except ValueError: pass
        vendor = data.get('filament_vendor')
        if isinstance(vendor, list):
            for i, v in enumerate(vendor):
                self.filament_vendors[f"T{i}"] = v

    def _parse_plate_json(self, zf):
        try:
            data = json.loads(zf.read('Metadata/plate_1.json'))
        except (KeyError, json.JSONDecodeError):
            return
        try:
            self.settings['object_name'] = data['bbox_objects'][0]['name']
        except (KeyError, IndexError):
            pass
        if data.get('bed_type'): self.settings['bed_type'] = data['bed_type']
        if self.settings['nozzle_diameter'] == 'N/A' and data.get('nozzle_diameter'):
            self.settings['nozzle_diameter'] = str(round(data['nozzle_diameter'], 1))

    def _parse_slice_info(self, zf):
        try:
            content = zf.read('Metadata/slice_info.config').decode('utf-8', errors='ignore')
        except KeyError:
            return
        m = re.search(r'key="prediction"\s+value="(\d+)"', content)
        if m:
            secs = int(m.group(1))
            h, rem = divmod(secs, 3600)
            mins, s = divmod(rem, 60)
            parts = []
            if h: parts.append(f"{h}h")
            if mins: parts.append(f"{mins}m")
            if s or not parts: parts.append(f"{s}s")
            self.settings['print_time'] = ' '.join(parts)
        m = re.search(r'key="weight"\s+value="([\d.]+)"', content)
        if m: self.settings['total_weight'] = m.group(1)

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
                            usage_g = (de * self.filament_area * self.filament_density) / 1000
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

    def _price_for_tool(self, tool):
        vendor = self.filament_vendors.get(tool, '').lower()
        for name, price in self.prices.items():
            if name in vendor:
                return price
        return self.prices['bambu']

    def dump_gcode(self, n=100):
        """Print first N lines of G-code with human-readable annotations in Markdown."""
        annotations = {
            'G28': 'Home all axes',
            'G90': 'Set absolute positioning',
            'G91': 'Set relative positioning',
            'G92': 'Set position (reset coordinate)',
            'M82': 'Set extruder to absolute mode',
            'M83': 'Set extruder to relative mode',
            'M104': 'Set hotend temperature (no wait)',
            'M109': 'Set hotend temperature and wait',
            'M140': 'Set bed temperature (no wait)',
            'M190': 'Set bed temperature and wait',
            'M106': 'Set part cooling fan speed',
            'M107': 'Turn off part cooling fan',
            'M204': 'Set acceleration',
            'M205': 'Set jerk / advanced settings',
            'M220': 'Set feedrate override percentage',
            'M221': 'Set flow rate override percentage',
            'M400': 'Wait for moves to finish',
            'M900': 'Set linear advance (pressure advance)',
            'M1002': 'Bambu: judge last / gcode claim speed',
            'M620': 'Bambu: AMS filament change start',
            'M621': 'Bambu: AMS filament change end',
            'M622': 'Bambu: conditional block start',
            'M623': 'Bambu: conditional block end',
            'M625': 'Bambu: conditional block (else)',
            'M73': 'Set print progress',
            'M201': 'Set max acceleration per axis',
            'M203': 'Set max feedrate per axis',
            'M993': 'Bambu: nozzle cam detection setting',
            'M960': 'Bambu: LED/lighting control',
            'M412': 'Filament runout detection',
            'M142': 'Bambu: set chamber/aux heater',
            'G29': 'Auto bed leveling',
            'G0': 'Rapid move (travel, no extrusion)',
            'G1': 'Linear move',
            'G2': 'Clockwise arc move',
            'G3': 'Counter-clockwise arc move',
            'G4': 'Dwell (pause)',
        }

        with zipfile.ZipFile(self.file_path, 'r') as z:
            with z.open('Metadata/plate_1.gcode') as f:
                lines = []
                for raw in f:
                    lines.append(raw.decode('utf-8').rstrip('\n\r'))
                    if len(lines) >= n:
                        break

        print(f"## G-code dump — first {len(lines)} lines\n")
        print("| Line | G-code | Annotation |")
        print("|-----:|--------|------------|")

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            display = stripped.replace('|', '\\|')  # escape pipes for Markdown

            if not stripped:
                print(f"| {i} | | |")
                continue
            elif stripped.startswith(';'):
                comment_text = stripped.lstrip('; ').strip()
                annotation = f'&nbsp;&nbsp;; *{comment_text}*'
            else:
                annotation = self._annotate_gcode_line(stripped, annotations)

            print(f"| {i} | `{display}` | {annotation} |")

    @staticmethod
    def _annotate_gcode_line(line, annotations):
        """Produce a human-readable annotation for a single G-code line."""
        # Strip inline comment
        code_part = line.split(';')[0].strip()
        inline_comment = line.split(';', 1)[1].strip() if ';' in line else None

        # Match command word
        cmd_match = re.match(r'([GMT]\d+)', code_part)
        if not cmd_match:
            # Could be a bare tool change like T0, T1
            t_match = re.match(r'T(\d+)', code_part)
            if t_match:
                desc = f'Switch to tool/extruder {t_match.group(1)} (AMS slot)'
            else:
                desc = '*(unknown)*'
            if inline_comment:
                desc += f' — *{inline_comment}*'
            return desc

        cmd = cmd_match.group(1)
        base = annotations.get(cmd, f'{cmd} command')

        # Add parameter details for common commands
        params = code_part[len(cmd):].strip()
        detail_parts = []

        if cmd in ('G0', 'G1'):
            coord_map = {'X': 'X', 'Y': 'Y', 'Z': 'Z', 'E': 'extrude', 'F': 'feedrate'}
            for m in re.finditer(r'([XYZEF])(-?\d*\.?\d+)', params):
                letter, val = m.group(1), m.group(2)
                name = coord_map.get(letter, letter)
                if letter == 'F':
                    detail_parts.append(f'{name}={val} mm/min')
                elif letter == 'E':
                    detail_parts.append(f'{name}={val}')
                else:
                    detail_parts.append(f'{name}={val}mm')
        elif cmd in ('M104', 'M109', 'M140', 'M190'):
            s = re.search(r'S(\d+)', params)
            if s:
                detail_parts.append(f'{s.group(1)}°C')
        elif cmd == 'M106':
            s = re.search(r'S(\d+)', params)
            p = re.search(r'P(\d+)', params)
            if p:
                detail_parts.append(f'fan {p.group(1)}')
            if s:
                pct = round(int(s.group(1)) / 255 * 100)
                detail_parts.append(f'{pct}%')
        elif cmd == 'G92':
            for m in re.finditer(r'([XYZE])(-?\d*\.?\d+)', params):
                detail_parts.append(f'{m.group(1)}={m.group(2)}')
        elif cmd == 'M204':
            s = re.search(r'S(\d+)', params)
            if s:
                detail_parts.append(f'{s.group(1)} mm/s²')
        elif cmd == 'G4':
            s = re.search(r'[SP](\d+)', params)
            if s:
                detail_parts.append(f'{s.group(1)}ms')

        desc = base
        if detail_parts:
            desc += f' ({", ".join(detail_parts)})'
        if inline_comment:
            desc += f' — *{inline_comment}*'
        return desc

    def print_report(self):
        s = self.settings
        print(f"\n--- 🖨️  PRINT BASICS ---")
        print(f"Object: {s['object_name']}")
        print(f"Layer Height: {s['layer_height']}mm | Nozzle: {s['nozzle_diameter']}mm")
        print(f"Filament: {s['filament_type']} | Bed: {s['bed_type']}")
        print(f"Est. Time: {s['print_time']} | Weight: {s['total_weight']}g")

        print(f"\n--- ⚙️  SLICER SETTINGS ---")
        print(f"Walls: {s['walls']} | Infill: {s['infill']}%")
        print(f"Bed Temp: {s['temp_init']}°C | Fan-Off: {s['no_fan_layers']} layers")

        print(f"\n--- 🚀 SPEED & FLOW ---")
        print(f"Outer Wall: {s['outer_wall_speed']}mm/s | Inner Wall: {s['inner_wall_speed']}mm/s")
        print(f"Infill: {s['sparse_infill_speed']}mm/s | Travel: {s['travel_speed']}mm/s")
        print(f"Max Vol. Flow: {s['max_volumetric_speed']}mm³/s")

        print(f"\n--- 🧱 ADHESION & SUPPORT ---")
        if s['enable_support'] == '0':
            print(f"Support: None")
        else:
            print(f"Support: {s['support_type']}")
        print(f"Brim: {s['brim_type']} ({s['brim_width']}mm)")

        print(f"\n--- 🧩 AMS & COST (CAD) ---")
        total_cost = 0
        for t, data in self.ams_mapping.items():
            price = self._price_for_tool(t)
            cost = data['usage'] * price
            total_cost += cost
            vendor = self.filament_vendors.get(t, 'N/A')
            print(f"{t}: {data['usage']:.2f}g ({vendor}) ~${cost:.2f} CAD")
        print(f"TOTAL ESTIMATED COST: ${total_cost:.2f} CAD\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Path to .3mf")
    parser.add_argument("--mode", choices=['flow', 'speed'], default='flow')
    parser.add_argument("--layers", type=int, default=5)
    parser.add_argument("--dump-gcode", type=int, nargs='?', const=100, default=None,
                        metavar='N', help="Dump first N lines of G-code with annotations (default: 100)")
    args = parser.parse_args()

    bm = BambuMaster(args.file)

    if args.dump_gcode is not None:
        bm.dump_gcode(n=args.dump_gcode)
    else:
        bm.process(mode=args.mode, max_layers=args.layers)
        bm.print_report()
        bm.visualize(mode=args.mode)
