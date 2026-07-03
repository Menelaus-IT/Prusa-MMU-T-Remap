#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Prusa MMU T-Remap — Gcode post-processor for Prusa FDM printers with MMU2/3.
Remaps T commands, handles empty slot gaps, relocates the brim, and supports
automatic launch from PrusaSlicer with the gcode already loaded.
"""

import re, sys, json, subprocess, shutil
from pathlib import Path
from typing import Dict, List

# ── GUI imports (optional) ────────────────────────────────────────────────────
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        _DND = True
    except ImportError:
        _DND = False
    _BaseClass = (TkinterDnD.Tk if _DND else tk.Tk)
    _GUI_AVAILABLE = True
except ImportError:
    _GUI_AVAILABLE = False
    _DND = False

# ── OS theme detection (dark/light) ──────────────────────────────────────────
def _detect_dark() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        try:
            import subprocess as sp
            r = sp.run(['defaults','read','-g','AppleInterfaceStyle'],
                capture_output=True, text=True)
            return r.stdout.strip() == 'Dark'
        except Exception:
            return False

_IS_DARK = _detect_dark()

# ── Core gcode functions ──────────────────────────────────────────────────────

def parse_gcode_info(path: str):
    """Returns (colours, used_t, has_brim, brim_active_t, brim_width)."""
    lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    _st   = re.compile(r'^\s*T(\d+)\s*(?:;.*)?$')

    colours: List[str] = []
    brim_width = 0
    lc = 0
    for i, l in enumerate(lines):
        m = re.match(r';\s*extruder_colour\s*=\s*(.+)', l)
        if m:
            colours = [c.strip() for c in m.group(1).split(';')
                       if re.match(r'^#[0-9A-Fa-f]{6}$', c.strip())]
        bm = re.match(r';\s*brim_width\s*=\s*(\d+)', l)
        if bm:
            brim_width = int(bm.group(1))
        if ';LAYER_CHANGE' in l and lc == 0:
            lc = i

    used_t = sorted(set(int(m.group(1)) for l in lines if (m := _st.match(l))))

    brim_idx = next((i for i, l in enumerate(lines) if ';TYPE:Skirt/Brim' in l), None)
    has_brim = brim_idx is not None
    brim_t = None
    if has_brim:
        brim_t = next(
            (int(_st.match(lines[j]).group(1))
             for j in range(brim_idx, -1, -1)
             if _st.match(lines[j])),
            None)
    return colours, used_t, has_brim, brim_t, brim_width


def detect_brim(src: str):
    """Wrapper: returns (has_brim, active_t, brim_width)."""
    _, _, has_brim, brim_t, bw = parse_gcode_info(src)
    return has_brim, brim_t, bw


def detect_prior_remap(src: str) -> str:
    """Returns a short reason if the gcode looks ALREADY remapped, else ''.

    Guards against a double-remap. Detects two signatures:
      - a standalone M863 command  -> injected by older versions of this tool
      - a '; remap T' marker       -> written by this version on rewritten Ts
    A clean slice straight from PrusaSlicer contains neither.
    """
    for line in Path(src).read_text(encoding='utf-8', errors='replace').splitlines():
        if 'Prusa-MMU-T-Remap: remapped' in line:
            return "it was already processed by this tool"
        if line.lstrip().startswith('M863'):
            return "it contains an M863 remap block (from an older version)"
        if '; remap T' in line:
            return "it contains '; remap T' markers (already processed by this tool)"
    return ""


def _parse_print_time(s: str) -> str:
    """Converts '27m 13s' or '1h 5m 3s' to compact format '27m' or '1h5m'."""
    h = re.search(r'(\d+)h', s)
    m = re.search(r'(\d+)m', s)
    hours   = int(h.group(1)) if h else 0
    minutes = int(m.group(1)) if m else 0
    return f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"


def build_output_filename(src_path: str) -> str:
    """
    Suggests the output filename for the save dialog.
    Real file:      FileName_remapped.gcode
    Temp file (.pp): _0.15mm_PLA_MK4SMMU3_27m_remapped.gcode
    """
    src   = Path(src_path)
    lines = src.read_text(encoding='utf-8', errors='replace').splitlines()

    if src.suffix.lower() != '.pp':
        return src.stem + '_remapped.gcode'

    # Temp file: reconstruct name from gcode metadata
    vars = {}
    for l in lines:
        for key in ['layer_height', 'printer_model']:
            m = re.match(rf';\s*{key}\s*=\s*(.+)', l)
            if m: vars[key] = m.group(1).strip().split(',')[0]
        m = re.match(r';\s*estimated printing time \(normal mode\)\s*=\s*(.+)', l)
        if m: vars['print_time'] = _parse_print_time(m.group(1))
        m = re.match(r';\s*filament_type\s*=\s*(.+)', l)
        if m:
            types = list(dict.fromkeys(t.strip() for t in m.group(1).split(';') if t.strip()))
            vars['printing_filament_types'] = '_'.join(types)

    lh = vars.get('layer_height', '')
    ft = vars.get('printing_filament_types', '')
    pm = vars.get('printer_model', '')
    pt = vars.get('print_time', '')
    return f"_{lh}mm_{ft}_{pm}_{pt}_remapped.gcode"


def relocate_brim(output_lines: list, target_t: int) -> list:
    """
    Moves the BRIM to target_t color while keeping the SKIRT in the first color.

    PrusaSlicer emits skirt and brim together under a single ';TYPE:Skirt/Brim'
    block, skirt first then brim, separated by a retract+Z+prime transition.
    When both are present the block contains 2 such transitions ("primes"): the
    first ends the skirt, the second ends the brim and reconnects to the object.

      skirt  = block[:RS]    -> stays in T0 (first color), right after the purge
      brim   = block[RS:RB]  -> moved to target_t
      block[RB:]             -> brim->object transition, kept in T0 so the skirt
                               reconnects to the object perimeter.

    If the block has only one transition (brim-only, no skirt) the whole block is
    moved (legacy behaviour). If target_t is already active from the purge,
    nothing is moved.
    """
    lc    = next((i for i, l in enumerate(output_lines) if ';LAYER_CHANGE' in l), 0)
    ret   = re.compile(r'^G1\s+E-')
    prime = re.compile(r'^G1\s+E\.[0-9]')

    brim_idx = next((i for i, l in enumerate(output_lines) if ';TYPE:Skirt/Brim' in l), None)
    if brim_idx is None:
        return output_lines

    layer_changes = [i for i, l in enumerate(output_lines) if ';LAYER_CHANGE' in l]
    lc2 = layer_changes[1] if len(layer_changes) > 1 else len(output_lines)

    target_row = next(
        (i for i, l in enumerate(output_lines)
         if lc < i < lc2 and re.match(rf'^\s*T{target_t}\s*', l)),
        None)
    if target_row is None:
        return output_lines   # target already loaded from purge -> brim already correct

    brim_type_end = next(
        (i for i in range(brim_idx + 1, len(output_lines))
         if output_lines[i].startswith(';TYPE:') and 'Skirt/Brim' not in output_lines[i]),
        len(output_lines))

    block  = output_lines[brim_idx:brim_type_end]
    primes = [i for i, l in enumerate(block) if prime.match(l.strip())]

    if len(primes) >= 2:
        # ── skirt + brim present: keep the skirt, move only the brim ───────────
        p0, p1   = primes[0], primes[1]
        retracts = [i for i, l in enumerate(block) if ret.match(l.strip())]
        RS = max([r for r in retracts if r < p0],      default=0)
        RB = max([r for r in retracts if p0 < r < p1], default=p1)

        skirt_keep  = block[:RS]      # ;TYPE:Skirt/Brim + skirt extrusions
        brim_move   = block[RS:RB]    # brim entry transition + brim extrusions
        brim_to_obj = block[RB:]      # transition that reconnects skirt -> object

        new_lines    = (output_lines[:brim_idx] + skirt_keep + brim_to_obj
                        + output_lines[brim_type_end:])
        shift        = len(brim_move)
        brim_package = brim_move
    else:
        # ── brim-only (no skirt): move the whole block (legacy positioning) ────
        prep_start = brim_idx
        for j in range(brim_idx - 1, lc - 1, -1):
            l = output_lines[j].strip()
            if l.startswith(';TYPE:') or ';LAYER_CHANGE' in l or '; CP TOOLCHANGE' in l:
                break
            prep_start = j

        brim_content_end = brim_type_end
        for j in range(brim_type_end - 1, brim_idx, -1):
            if ret.match(output_lines[j].strip()):
                brim_content_end = j + 1
                break

        brim_package = output_lines[prep_start:brim_type_end]

        _has_xy = re.compile(r'G1\s+X[-\d.]+\s+Y[-\d.]+')
        prep_travel_offset = next(
            (j - prep_start for j in range(prep_start, brim_idx)
             if _has_xy.search(output_lines[j]) and 'E' not in output_lines[j]),
            None)
        perimeter_xy = None
        for i in range(brim_content_end, brim_type_end):
            m = re.search(r'(X[-\d.]+)\s+(Y[-\d.]+)', output_lines[i])
            if m and 'E' not in output_lines[i] and 'Z' not in output_lines[i] and 'G1' in output_lines[i]:
                perimeter_xy = (m.group(1), m.group(2)); break
        if perimeter_xy is None:
            for i in range(brim_type_end, min(len(output_lines), brim_type_end + 30)):
                m = re.search(r'(X[-\d.]+)\s+(Y[-\d.]+)', output_lines[i])
                if m and 'G1' in output_lines[i]:
                    perimeter_xy = (m.group(1), m.group(2)); break

        if prep_travel_offset is not None and perimeter_xy:
            modified_prep = list(output_lines[prep_start:brim_idx])
            orig = modified_prep[prep_travel_offset]
            mod  = re.sub(r'X[-\d.]+', perimeter_xy[0], orig, count=1)
            mod  = re.sub(r'Y[-\d.]+', perimeter_xy[1], mod,  count=1)
            modified_prep[prep_travel_offset] = mod
            new_lines = output_lines[:prep_start] + modified_prep + output_lines[brim_type_end:]
            shift = brim_type_end - brim_idx
        else:
            new_lines = output_lines[:prep_start] + output_lines[brim_type_end:]
            shift = brim_type_end - prep_start

    # ── insert the brim package after the target's CP TOOLCHANGE END ───────────
    target_row_nl = target_row - shift
    lc2_nl        = lc2        - shift
    tc_end = next(
        (i for i in range(target_row_nl, min(len(new_lines), target_row_nl + 300))
         if '; CP TOOLCHANGE END' in new_lines[i]),
        None)
    if tc_end is not None:
        insert_after = tc_end
    else:
        insert_after = next(
            (i - 1 for i in range(target_row_nl, lc2_nl)
             if new_lines[i].startswith(';TYPE:')
             and not any(x in new_lines[i] for x in ['Wipe', 'Skirt', 'Support', 'TOOLCHANGE'])),
            target_row_nl)

    return new_lines[:insert_after + 1] + brim_package + new_lines[insert_after + 1:]


def remap_gcode(src: str, dst: str,
                mapping: Dict[int, int],
                new_hex: Dict[int, str],
                brim_logical: int = None) -> dict:
    """Number-by-cassette remap for MK4S + MMU3.

    Each gcode color (logical tool) is renumbered to its PHYSICAL cassette:
    output tool number = cassette - 1.  The printer's native "filament mapping"
    screen then maps every tool to its cassette by identity (tool N -> cassette
    N+1), and this is what drives the very first filament load.  Cassettes 6-12
    (tool >= 5) pass through unchanged to the cjbaar 12-slot MMU.

    No M863 is injected: the gcode tool numbers ARE the cassettes, so the native
    screen is already correct, including the initial load.  This removes the
    conflict between M863 (which ignores the initial load) and the screen.
    """
    text  = Path(src).read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()

    out_of = dict(mapping)            # logical L -> cassette slot (0-indexed)

    # ---- rewrite standalone T commands -------------------------------------
    _st = re.compile(r'^\s*T(\d+)\s*(?:;.*)?$')
    counts = {L: 0 for L in mapping}
    output = []
    for line in lines:
        s = line
        m = _st.match(s)
        if m and not s.lstrip().startswith(';'):
            old = int(m.group(1))
            if old in out_of:
                new = out_of[old]
                s = re.sub(r'T\d+', f'T{new}', s, count=1)
                if new != old:
                    s = s.rstrip() + f' ; remap T{old}->T{new}'
                counts[old] += 1
        output.append(s)

    need = max(out_of.values(), default=0) + 1

    # ---- update extruder_colour --------------------------------------------
    out_hex = {out_of[L]: new_hex.get(L, '#000000') for L in mapping}
    updated = False
    for i, line in enumerate(output):
        mm = re.match(r'(;\s*extruder_colour\s*=\s*)(.+)', line)
        if mm:
            parts = mm.group(2).strip().split(';')
            while len(parts) < need:
                parts.append('#000000')
            for pos, hx in out_hex.items():
                if 0 <= pos < len(parts):
                    parts[pos] = hx
            output[i] = mm.group(1) + ';'.join(parts)
            updated = True

    # ---- update filament-used + fake-activate gaps -------------------------
    # Fake-activate ONLY between the first and last used tool. Positions BEFORE
    # the first used tool stay 0.00 so the native screen excludes those cassettes
    # (e.g. cassette 1) and the initial load starts from the first real cassette.
    used = sorted(out_of.values())
    first_u, last_u = (used[0], used[-1]) if used else (0, 0)
    _usage_keys = ('filament used [mm]', 'filament used [cm3]',
                   'filament used [g]', 'filament cost')
    for i, line in enumerate(output):
        for key in _usage_keys:
            mm = re.match(rf'(;\s*{re.escape(key)}\s*=\s*)(.+)', line, re.IGNORECASE)
            if mm:
                orig = [v.strip() for v in mm.group(2).split(',')]
                vals = ['0.00'] * max(need, len(orig))
                for L in mapping:
                    if L < len(orig):
                        vals[out_of[L]] = orig[L]
                try:
                    fl = [float(v) for v in vals]
                    for t in range(first_u + 1, last_u):
                        if fl[t] == 0.0:
                            fl[t] = 0.01
                    vals = [f'{v:.2f}' for v in fl]
                except Exception:
                    pass
                output[i] = mm.group(1) + ', '.join(vals)
                break

    # ---- brim relocation ----------------------------------------------------
    if brim_logical is not None and brim_logical in out_of:
        output = relocate_brim(output, out_of[brim_logical])

    # Stamp a header marker so this file is recognized as already remapped
    # (detect_prior_remap uses it to prevent an accidental double-remap).
    output.insert(0, '; Prusa-MMU-T-Remap: remapped — do not re-process this file')

    Path(dst).write_text('\n'.join(output) + '\n', encoding='utf-8')

    cass_map = {L: out_of[L] for L in sorted(mapping)}     # logical -> cassette slot (0-indexed)
    return {'total':          sum(counts.values()),
            'colors_updated':  updated,
            'cass_map':        cass_map}


# ── bgcode ────────────────────────────────────────────────────────────────────
def _find_prusaslicer() -> str:
    for p in [r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
              r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer.exe",
              "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
              "prusa-slicer", "prusa-slicer-console"]:
        if Path(p).exists() or shutil.which(p):
            return p
    return ""


def bgcode_to_gcode(bgcode_path: str) -> str:
    exe = _find_prusaslicer()
    if not exe:
        raise RuntimeError("PrusaSlicer not found — cannot convert bgcode")
    out = Path(bgcode_path).with_suffix('.gcode')
    kw = {}
    if sys.platform == 'win32':
        kw['creationflags'] = subprocess.CREATE_NO_WINDOW   # no console flash
    subprocess.run([exe, "--export-gcode", bgcode_path, "--output", str(out)],
                   check=True, capture_output=True, **kw)
    return str(out)


# ── GUI ───────────────────────────────────────────────────────────────────────
class RemapApp(_BaseClass if _GUI_AVAILABLE else object):

    def __init__(self):
        super().__init__()
        if not _GUI_AVAILABLE:
            return
        self.title("Prusa MMU T-Remap")
        self.minsize(600, 460)
        self.resizable(True, True)
        self._apply_theme()

        # State
        self.src_path  = ''
        self.gcolours  = []   # colors from gcode (extruder_colour)
        self.rows: List[dict] = []  # {t, slot_var, hex_var, swatch_lbl, frame}
        self.brim_detected  = False
        self.brim_orig_t    = None
        self.brim_t_var     = tk.StringVar()
        self._prior_remap   = ''    # non-empty if the loaded gcode looks already remapped

        self._build_ui()
        if _DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self._on_drop)

    # ── Theme ─────────────────────────────────────────────────────────────────
    def _apply_theme(self):
        try:
            import sv_ttk
            sv_ttk.use_dark_theme() if _IS_DARK else sv_ttk.use_light_theme()
            self._dark_bg = '#1c1c1c' if _IS_DARK else '#f0f0f0'
        except ImportError:
            try:
                style = ttk.Style()
                style.theme_use('clam')
                if _IS_DARK:
                    bg, fg, sel = '#2b2b2b', '#e0e0e0', '#3c3f41'
                    self._dark_bg = bg
                    style.configure('.', background=bg, foreground=fg,
                                    fieldbackground=sel, bordercolor='#555555')
                    style.configure('TLabelframe',       background=bg)
                    style.configure('TLabelframe.Label', background=bg, foreground=fg)
                    style.configure('TFrame',  background=bg)
                    style.configure('TLabel',  background=bg, foreground=fg)
                    style.configure('TButton', background='#3c3f41', foreground=fg)
                    style.configure('TEntry',  fieldbackground=sel, foreground=fg)
                    style.configure('TCombobox', fieldbackground=sel, foreground=fg)
                    style.map('TButton', background=[('active','#505355')])
                    self.configure(bg=bg)
                else:
                    self._dark_bg = '#f0f0f0'
            except Exception:
                self._dark_bg = '#f0f0f0'

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = dict(padx=8, pady=3)

        # File section
        frm_f = ttk.LabelFrame(self, text="Gcode file")
        frm_f.pack(fill='x', **pad)
        self._path_var = tk.StringVar()
        ttk.Entry(frm_f, textvariable=self._path_var, state='readonly'
                  ).pack(side='left', fill='x', expand=True, padx=4, pady=4)
        ttk.Button(frm_f, text="Browse…", command=self._browse).pack(side='left', padx=(0,4))
        self._dnd_lbl = ttk.Label(frm_f, text="Drag & drop enabled ✓" if _DND else "",
                                  foreground='green')
        self._dnd_lbl.pack(side='left')

        # Mapping table
        frm_map = ttk.LabelFrame(self, text="Logical T → Physical MMU slot")
        frm_map.pack(fill='both', expand=True, **pad)

        # Scrollable canvas — header and data rows share the same grid inside _inner
        bg = getattr(self, '_dark_bg', self.cget('bg'))
        self._canvas = tk.Canvas(frm_map, highlightthickness=0, bg=bg)
        sb = ttk.Scrollbar(frm_map, orient='vertical', command=self._canvas.yview)
        self._inner = tk.Frame(self._canvas, bg=bg)
        self._canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self._canvas.configure(yscrollcommand=sb.set)
        self._cwin = self._canvas.create_window((0,0), window=self._inner, anchor='nw')
        self._inner.bind('<Configure>',
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox('all')))
        self._canvas.bind('<Configure>',
            lambda e: self._canvas.itemconfig(self._cwin, width=e.width))
        self._canvas.bind_all('<MouseWheel>',
            lambda e: self._canvas.yview_scroll(int(-1*(e.delta/120)), 'units'))

        # Column widths — defined once, shared by header and all data rows
        self._inner.columnconfigure(0, minsize=80)   # Logical T
        self._inner.columnconfigure(1, minsize=52)   # Color swatch
        self._inner.columnconfigure(2, minsize=105)  # Slot [−][n][+]
        self._inner.columnconfigure(3, minsize=110)  # Hex entry

        # Header row (row 0)
        for col, txt in enumerate(["Logical T", "Color", "Slot", "Hex (editable)"]):
            ttk.Label(self._inner, text=txt, anchor='center',
                      font=('TkDefaultFont', 9, 'bold')).grid(
                          row=0, column=col, sticky='ew', padx=4, pady=(4,0))
        ttk.Separator(self._inner, orient='horizontal').grid(
            row=1, column=0, columnspan=4, sticky='ew', padx=4, pady=2)

        self._next_row = 2  # data rows start here

        # Brim selector (hidden until a brim is detected)
        self._frm_brim = ttk.Frame(self)
        ttk.Label(self._frm_brim, text="🎨 Brim color:").pack(side='left', padx=(8,4))
        self._brim_combo = ttk.Combobox(self._frm_brim, textvariable=self.brim_t_var,
                                         state='readonly', width=26)
        self._brim_combo.pack(side='left')
        ttk.Label(self._frm_brim,
                  text=" ← which color should print the brim",
                  foreground='gray').pack(side='left')

        # Buttons
        frm_btn = ttk.Frame(self)
        frm_btn.pack(fill='x', **pad)
        ttk.Button(frm_btn, text="💾 Save config", command=self._save_config).pack(side='left')
        ttk.Button(frm_btn, text="📂 Load config", command=self._load_config
                   ).pack(side='left', padx=4)
        ttk.Button(frm_btn, text="⚙  Process gcode",
                   command=self._process).pack(side='right')

        # Log
        frm_log = ttk.LabelFrame(self, text="Log")
        frm_log.pack(fill='x', **pad)
        log_bg = '#1e1e1e' if _IS_DARK else '#ffffff'
        log_fg = '#d4d4d4' if _IS_DARK else '#000000'
        self._log_txt = tk.Text(frm_log, height=5, state='disabled',
                                 wrap='word', font=('Consolas', 9),
                                 bg=log_bg, fg=log_fg, insertbackground=log_fg)
        self._log_txt.pack(fill='x', padx=4, pady=4)

    # ── Load ──────────────────────────────────────────────────────────────────
    def _browse(self):
        p = filedialog.askopenfilename(
            filetypes=[("Gcode / BGcode","*.gcode *.bgcode"),("All files","*.*")])
        if p: self._load(p)

    def _on_drop(self, event):
        p = event.data.strip().strip('{}')
        if p: self._load(p)

    def _load(self, path: str):
        if path.lower().endswith('.bgcode'):
            try:
                path = bgcode_to_gcode(path)
                self._log("BGcode converted to gcode")
            except Exception as ex:
                messagebox.showerror("Error", str(ex)); return

        self.src_path = path
        self._path_var.set(path)

        colours, t_used, has_brim, brim_t, brim_w = parse_gcode_info(path)
        self.gcolours       = colours
        self.brim_detected  = has_brim
        self.brim_orig_t    = brim_t
        self._prior_remap   = detect_prior_remap(path)

        # Clear previous rows
        for r in self.rows:
            for w in r['widgets']: w.destroy()
        self.rows.clear()
        self._next_row = 2

        # Create one row per used T
        for t in t_used:
            col = colours[t] if t < len(colours) else '#FF8000'
            self._add_row(t, col)

        # Brim selector — show whenever ;TYPE:Skirt/Brim is present in the gcode,
        # regardless of brim_width metadata (per-object brim sets brim_width = 0)
        if has_brim:
            opts = [f"T{t} — {colours[t] if t < len(colours) else '#FF8000'}" for t in t_used]
            self._brim_combo['values'] = opts
            # Default to the first color (T0): if the user forgets to pick a brim
            # color, the brim stays with the first printed color, not the last one.
            self.brim_t_var.set(opts[0] if opts else '')
            self._frm_brim.pack(fill='x', padx=8, pady=2)
        else:
            self._frm_brim.pack_forget()

        self._log(f"Loaded: {Path(path).name}")
        self._log(f"  {len(t_used)} extruders detected  |  T used: {t_used}")
        if colours: self._log(f"  Colors: {colours[:6]}{'…' if len(colours)>6 else ''}")
        if has_brim:
            brim_info = f"{brim_w}mm" if brim_w > 0 else "per-object"
            self._log(f"  ✓ Brim detected ({brim_info}) — T{brim_t} active in original brim")
        if self._prior_remap:
            self._log(f"  ⚠ WARNING: this gcode looks ALREADY REMAPPED — {self._prior_remap}.")
            self._log("    Remapping it again will produce a broken print. Use a fresh slice.")

    def _add_row(self, t: int, color_hex: str):
        bg  = getattr(self, '_dark_bg', '#f0f0f0')
        fg  = '#e0e0e0' if _IS_DARK else '#000000'
        row = self._next_row
        self._next_row += 1
        widgets = []  # track all widgets in this row for later cleanup

        # Col 0: Logical T label
        lbl = tk.Label(self._inner, text=f"T{t}", anchor='center',
                       bg=bg, fg=fg, font=('TkDefaultFont', 9, 'bold'))
        lbl.grid(row=row, column=0, sticky='ew', padx=4, pady=1)
        widgets.append(lbl)

        # Col 1: color swatch — click to open color picker
        swatch = tk.Label(self._inner, bg=color_hex, width=3,
                          relief='solid', bd=1, cursor='hand2')
        swatch.grid(row=row, column=1, padx=4, pady=1)
        widgets.append(swatch)

        # Col 2: slot [−] entry [+] buttons
        slot_var = tk.IntVar(value=t + 1)
        slot_frm = ttk.Frame(self._inner)
        slot_frm.grid(row=row, column=2, padx=4, pady=1)
        widgets.append(slot_frm)
        def _dec(v=slot_var): v.set(max(1,  v.get()-1))
        def _inc(v=slot_var): v.set(min(12, v.get()+1))
        ttk.Button(slot_frm, text="−", width=2, command=_dec).pack(side='left')
        ttk.Entry(slot_frm, textvariable=slot_var, width=3, justify='center'
                  ).pack(side='left', padx=1)
        ttk.Button(slot_frm, text="+", width=2, command=_inc).pack(side='left')

        # Col 3: hex entry
        hex_var = tk.StringVar(value=color_hex)
        hex_ent = ttk.Entry(self._inner, textvariable=hex_var, width=12)
        hex_ent.grid(row=row, column=3, padx=4, pady=1)
        widgets.append(hex_ent)

        def _upd_swatch(*_):
            h = hex_var.get().strip()
            if re.match(r'^#[0-9A-Fa-f]{6}$', h):
                try: swatch.configure(bg=h)
                except Exception: pass
        hex_var.trace_add('write', _upd_swatch)
        swatch.bind('<Button-1>', lambda e, hv=hex_var, sw=swatch: self._pick_color(hv, sw))

        self.rows.append({'t': t, 'slot_var': slot_var, 'hex_var': hex_var,
                          'swatch': swatch, 'widgets': widgets})

    @staticmethod
    def _pick_color(hex_var, swatch):
        from tkinter import colorchooser
        cur = hex_var.get()
        col = colorchooser.askcolor(color=cur)[1]
        if col:
            hex_var.set(col.upper())

    # ── Config ────────────────────────────────────────────────────────────────
    def _get_mapping(self):
        mapping, new_hex = {}, {}
        for r in self.rows:
            t = r['t']
            mapping[t] = r['slot_var'].get() - 1
            new_hex[t]  = r['hex_var'].get()
        return mapping, new_hex

    def _save_config(self):
        p = filedialog.asksaveasfilename(
            defaultextension='.json',
            initialfile='remap_config.json',
            filetypes=[("JSON","*.json")])
        if not p: return
        mapping, new_hex = self._get_mapping()
        data = [{'t': t, 'slot': mapping[t]+1, 'hex': new_hex[t]} for t in mapping]
        Path(p).write_text(json.dumps(data, indent=2), encoding='utf-8')
        self._log(f"✓ Config saved: {Path(p).name}")

    def _load_config(self):
        p = filedialog.askopenfilename(filetypes=[("JSON","*.json")])
        if not p: return
        data = json.loads(Path(p).read_text(encoding='utf-8'))
        cfg  = {e['t']: {'slot': e['slot'], 'hex': e['hex']} for e in data}
        for row in self.rows:
            t = row['t']
            if t in cfg:
                row['slot_var'].set(cfg[t]['slot'])
                row['hex_var'].set(cfg[t]['hex'])
        self._log(f"✓ Config loaded: {Path(p).name}")

    # ── Process ───────────────────────────────────────────────────────────────
    def _process(self):
        if not self.src_path:
            messagebox.showwarning('Warning', 'Please load a gcode file first')
            return

        # Guard against a double-remap: if the loaded gcode looks already
        # remapped, require an explicit confirmation before overwriting.
        if self._prior_remap:
            proceed = messagebox.askyesno(
                'Already remapped?',
                f"This gcode looks ALREADY REMAPPED — {self._prior_remap}.\n\n"
                "Remapping an already-remapped file produces a broken print: the "
                "old and new mappings collide and colors load from the wrong "
                "cassettes.\n\n"
                "Start from a fresh slice instead.\n\n"
                "Continue anyway?",
                default='no', icon='warning')
            if not proceed:
                self._log("✗ Aborted: gcode already remapped. Use a fresh slice.")
                return

        mapping, new_hex = self._get_mapping()

        # Save dialog with suggested filename
        suggested = build_output_filename(self.src_path)
        dst = filedialog.asksaveasfilename(
            initialfile=suggested,
            defaultextension='.gcode',
            filetypes=[("Gcode", "*.gcode"), ("All files", "*.*")],
            title="Save remapped gcode")
        if not dst:
            return  # user cancelled

        # Brim target (logical tool of the brim color)
        brim_log = None
        if self.brim_detected and self.brim_t_var.get():
            m = re.match(r'T(\d+)', self.brim_t_var.get())
            if m:
                brim_log = int(m.group(1))

        try:
            r  = remap_gcode(self.src_path, dst, mapping, new_hex,
                             brim_logical=brim_log)
            self._log(f"✓ Saved: {Path(dst).name}")
            cmap = ", ".join(f"T{L}→slot {s+1}" for L, s in r['cass_map'].items())
            self._log(f"  Cassette mapping: {cmap}")
            self._log(f"  {r['total']} T commands rewritten")
            if r['colors_updated']:
                self._log("  ✓ extruder_colour updated")
            if brim_log is not None:
                self._log(f"  ✓ Brim moved to T{brim_log}")
            messagebox.showinfo('Done',
                f"Saved:\n{dst}\n\n{r['total']} T commands rewritten")
        except Exception as ex:
            messagebox.showerror('Error', str(ex))
            self._log(f"✗ {ex}")

    def _log(self, msg: str):
        self._log_txt.configure(state='normal')
        self._log_txt.insert('end', msg + '\n')
        self._log_txt.see('end')
        self._log_txt.configure(state='disabled')


# ── Headless mode ─────────────────────────────────────────────────────────────
def _headless(gcode_path: str):
    script_dir = Path(__file__).parent
    log_path   = script_dir / 'remap_log.txt'
    phys_path  = script_dir / 'remap_physical.json'
    old_path   = script_dir / 'remap_config.json'

    def _log(msg):
        try: print(msg)
        except Exception: pass
        with open(log_path, 'a', encoding='utf-8') as f: f.write(msg + '\n')

    gcode = Path(gcode_path)
    if not gcode.exists():
        _log(f"[remap] ERROR: gcode not found: {gcode_path}"); sys.exit(1)

    # Convert bgcode if needed
    if gcode.suffix.lower() == '.bgcode':
        try:
            gcode = Path(bgcode_to_gcode(str(gcode)))
            _log("[remap] Converted bgcode → gcode")
        except Exception as ex:
            _log(f"[remap] ERROR converting bgcode: {ex}"); sys.exit(1)

    _log(f"[remap] Start: {gcode.name}")

    # Guard against a double-remap: refuse an already-remapped gcode.
    prior = detect_prior_remap(str(gcode))
    if prior:
        _log(f"[remap] ERROR: gcode already remapped — {prior}. "
             "Refusing to double-remap; use a fresh slice."); sys.exit(1)

    colours, t_used, _, _, _ = parse_gcode_info(str(gcode))
    gcode_colours = colours

    if phys_path.exists():
        physical = json.loads(phys_path.read_text(encoding='utf-8'))
        _log(f"[remap] Physical layout: {len(physical)} slots")
        mapping, new_hex = {}, {}
        for t, desired in enumerate(gcode_colours):
            phys_u = [c.upper() for c in physical]
            if desired.upper() in phys_u:
                slot = phys_u.index(desired.upper())
                mapping[t] = slot
                new_hex[t] = desired
            else:
                _log(f"[remap]   WARN T{t} ({desired}) not found in physical layout")
    elif old_path.exists():
        _log("[remap] Using remap_config.json")
        data = json.loads(old_path.read_text(encoding='utf-8'))
        mapping = {e['t']: e['slot']-1 for e in data}
        new_hex  = {e['t']: e['hex'] for e in data}
    else:
        _log(f"[remap] ERROR: config not found: {phys_path} or {old_path}"); sys.exit(1)

    dst = str(gcode.with_stem(gcode.stem + '_remapped'))
    r   = remap_gcode(str(gcode), dst, mapping, new_hex)

    _log(f"[remap] ✓ {gcode.name}")
    cmap = ", ".join(f"T{L}->slot {s+1}" for L, s in r['cass_map'].items())
    _log(f"[remap]   Cassette mapping: {cmap}")
    _log(f"[remap]   {r['total']} T commands rewritten")
    if r['colors_updated']:
        _log("[remap]   ✓ extruder_colour updated")


# ── Entry point ───────────────────────────────────────────────────────────────
def _hide_console_if_owned():
    """On Windows, hide the console window if the GUI ended up with one it owns
    alone — e.g. when the .pyw is (mis)associated with python.exe instead of
    pythonw.exe. Does nothing under pythonw.exe (there is no console) and never
    hides a shell the script was launched from (that console has >1 attached
    process, so it is left untouched)."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return                                     # pythonw.exe: no console
        buf = (ctypes.c_uint * 4)()
        if k32.GetConsoleProcessList(buf, 4) == 1:     # this process is the sole owner
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a]

    # --headless: silent mode (requires remap_config.json or remap_physical.json)
    # No flag: always opens the GUI, with gcode pre-loaded if passed as argument
    if '--headless' in args:
        gcode_arg = next((a for a in args if not a.startswith('-')), None)
        if gcode_arg:
            _headless(gcode_arg.strip('"'))
    else:
        if not _GUI_AVAILABLE:
            print("tkinter not available — GUI mode not possible")
            sys.exit(1)
        _hide_console_if_owned()
        app = RemapApp()
        gcode_arg = next((a for a in args if not a.startswith('-')), None)
        if gcode_arg:
            p = gcode_arg.strip('"')
            if Path(p).exists():
                app.after(200, lambda: app._load(p))
        app.mainloop()
