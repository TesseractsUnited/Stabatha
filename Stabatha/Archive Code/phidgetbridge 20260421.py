"""
PhidgetBridge HMI  —  Recorder + Calibration + Viewer
=======================================================
Single-file Tkinter application with five tabs:

  🔴 Record      — live gauges, trigger config, capture log
  ⚖  Calibrate   — zero-point + 1-point calibration per channel (V/V → lbf)
  📋 Data Table  — browse CSV files, filter, export
  📊 Summary     — per-channel statistics (raw V/V and lbf)
  📈 Chart       — multi-channel line plot (requires matplotlib)

Calibration model (per channel)
────────────────────────────────
  lbf = (raw_V_per_V  −  zero_offset)  ×  scale_factor

  zero_offset  = average V/V reading when sensor carries zero load
  scale_factor = known_load_lbf / (cal_avg_V_per_V − zero_offset)

Calibration is saved to  calibration.json  in the working folder
and is automatically applied when recording and viewing CSV data.

Requirements
────────────
    pip install Phidget22
    pip install matplotlib          # optional — enables Chart tab

Usage
─────
    python phidgetbridge.py
    python phidgetbridge.py /path/to/save/folder
"""

import os
import sys
import csv
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    from Phidget22.Phidget import PhidgetException
    from Phidget22.Devices.VoltageRatioInput import VoltageRatioInput
    HAS_PHIDGET = True
except ImportError:
    HAS_PHIDGET = False

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ── Theme ─────────────────────────────────────────────────────────────────────
BG         = "#1e1e2e"
BG2        = "#2a2a3e"
BG3        = "#313145"
ACCENT     = "#7c6af7"
ACCENT2    = "#a78bfa"
TEXT       = "#e2e0f0"
TEXT_DIM   = "#8884aa"
GREEN      = "#4ade80"
YELLOW     = "#fbbf24"
RED        = "#f87171"
ORANGE     = "#fb923c"
CH_COLOURS = ["#7c6af7", "#4ade80", "#fbbf24", "#f87171"]

CSV_GLOB    = "bridge_data_*.csv"
CAL_FILE    = "calibration.json"
NUM_CH      = 4
CAL_SAMPLES = 50      # samples averaged per calibration step


# ═════════════════════════════════════════════════════════════════════════════
#  Calibration data model
# ═════════════════════════════════════════════════════════════════════════════

class CalibrationStore:
    """
    Holds zero_offset and scale_factor for each channel.
    Persisted to / loaded from calibration.json.

    Conversion:  lbf = (raw_V_per_V - zero_offset) * scale_factor
    """

    def __init__(self):
        self.channels: list[dict] = [self._blank() for _ in range(NUM_CH)]

    @staticmethod
    def _blank() -> dict:
        return {
            "zero_offset":   0.0,
            "scale_factor":  1.0,
            "cal_load_lbf":  0.0,
            "zero_raw":      None,
            "cal_raw":       None,
            "calibrated":    False,
            "timestamp":     "",
        }

    def to_lbf(self, ch: int, raw: float) -> float:
        c = self.channels[ch]
        return (raw - c["zero_offset"]) * c["scale_factor"]

    def save(self, folder: str):
        path = Path(folder) / CAL_FILE
        with open(path, "w") as f:
            json.dump({"channels": self.channels}, f, indent=2)

    def load(self, folder: str) -> bool:
        path = Path(folder) / CAL_FILE
        if not path.exists():
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            for i, ch in enumerate(data.get("channels", [])):
                if i < NUM_CH:
                    self.channels[i].update(ch)
            return True
        except Exception:
            return False

    def is_calibrated(self, ch: int) -> bool:
        return bool(self.channels[ch].get("calibrated", False))

    def set_zero(self, ch: int, raw_avg: float):
        self.channels[ch]["zero_offset"] = raw_avg
        self.channels[ch]["zero_raw"]    = raw_avg
        self.channels[ch]["calibrated"]  = False
        self.channels[ch]["timestamp"]   = datetime.now().isoformat(timespec="seconds")

    def set_cal_point(self, ch: int, raw_avg: float, load_lbf: float):
        zero = self.channels[ch]["zero_offset"]
        span = raw_avg - zero
        if abs(span) < 1e-12:
            raise ValueError("Cal point too close to zero — apply a larger load.")
        self.channels[ch]["cal_raw"]      = raw_avg
        self.channels[ch]["cal_load_lbf"] = load_lbf
        self.channels[ch]["scale_factor"] = load_lbf / span
        self.channels[ch]["calibrated"]   = True
        self.channels[ch]["timestamp"]    = datetime.now().isoformat(timespec="seconds")


# ═════════════════════════════════════════════════════════════════════════════
#  Recorder engine  (background thread)
# ═════════════════════════════════════════════════════════════════════════════

class RecorderEngine:
    IDLE       = "idle"
    CONNECTING = "connecting"
    WAITING    = "waiting"
    RECORDING  = "recording"
    DONE       = "done"
    ERROR      = "error"

    def __init__(self):
        self.state          = self.IDLE
        self.latest         = [None] * NUM_CH
        self.captured       = []
        self.capture_target = 100
        self.error_msg      = ""
        self._channels      = []
        self._stop_evt      = threading.Event()
        self._thread        = None

        self.serial_number     = None
        self.data_interval_ms  = 50
        self.trigger_channel   = 0
        self.trigger_threshold = 0.01
        self.trigger_direction = "either"
        self.num_points        = 100
        self.save_folder       = "."
        self.calibration: CalibrationStore | None = None

        self._triggered = False
        self.saved_path = ""

    def start(self):
        if self.state not in (self.IDLE, self.DONE, self.ERROR):
            return
        self._stop_evt.clear()
        self._triggered  = False
        self.captured    = []
        self.saved_path  = ""
        self.error_msg   = ""
        self.capture_target = self.num_points
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def abort(self):
        self._stop_evt.set()

    @property
    def capture_count(self):
        return len(self.captured)

    def _run(self):
        try:
            self.state = self.CONNECTING
            self._open_channels()
            self.state = self.WAITING
            self._wait_for_trigger()
            if self._stop_evt.is_set():
                self.state = self.IDLE
                return
            self.state = self.RECORDING
            self._record()
            self._save_csv()
            self.state = self.DONE
        except Exception as exc:
            self.error_msg = str(exc)
            self.state     = self.ERROR
        finally:
            self._close_channels()

    def _open_channels(self):
        for i in range(NUM_CH):
            ch = VoltageRatioInput()
            ch.setChannel(i)
            if self.serial_number:
                ch.setDeviceSerialNumber(int(self.serial_number))
            ch.setOnAttachHandler(self._on_attach)
            ch.setOnDetachHandler(self._on_detach)
            ch.setOnErrorHandler(self._on_error)
            ch.setOnVoltageRatioChangeHandler(self._on_value_change)
            ch.openWaitForAttachment(5000)
            self._channels.append(ch)

    def _close_channels(self):
        for ch in self._channels:
            try:
                ch.close()
            except Exception:
                pass
        self._channels.clear()

    def _on_attach(self, ch):
        ch.setDataInterval(self.data_interval_ms)

    def _on_detach(self, ch):
        pass

    def _on_error(self, ch, code, desc):
        self.error_msg = f"CH{ch.getChannel()} error [{code}]: {desc}"

    def _on_value_change(self, ch, value):
        idx = ch.getChannel()
        self.latest[idx] = value
        if self.state == self.WAITING and not self._triggered and idx == self.trigger_channel:
            fired = False
            if self.trigger_direction in ("rising",  "either") and value >  self.trigger_threshold:
                fired = True
            if self.trigger_direction in ("falling", "either") and value < -self.trigger_threshold:
                fired = True
            if fired:
                self._triggered = True

    def _wait_for_trigger(self):
        while not self._triggered and not self._stop_evt.is_set():
            time.sleep(0.05)

    def _record(self):
        interval = self.data_interval_ms / 1000.0
        for _ in range(self.num_points):
            if self._stop_evt.is_set():
                break
            snap = list(self.latest)
            ts   = datetime.now().isoformat(timespec="milliseconds")
            self.captured.append((ts, snap))
            time.sleep(interval)

    def _save_csv(self):
        if not self.captured:
            return
        fname = f"bridge_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path  = str(Path(self.save_folder) / fname)
        cal   = self.calibration

        raw_hdrs = ["timestamp"] + [f"ch{i}_V_per_V" for i in range(NUM_CH)]
        lbf_hdrs = [f"ch{i}_lbf" for i in range(NUM_CH)] if cal else []
        headers  = raw_hdrs + lbf_hdrs

        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for ts, vals in self.captured:
                raw_row = [ts] + [f"{v:.8f}" if v is not None else "" for v in vals]
                lbf_row = []
                if cal:
                    for i, v in enumerate(vals):
                        if v is not None and cal.is_calibrated(i):
                            lbf_row.append(f"{cal.to_lbf(i, v):.6f}")
                        else:
                            lbf_row.append("")
                w.writerow(raw_row + lbf_row)

        self.saved_path = path


# ═════════════════════════════════════════════════════════════════════════════
#  Calibration sampler  (background thread)
# ═════════════════════════════════════════════════════════════════════════════

class CalSampler:
    """Average N live samples from the engine across all channels."""
    IDLE    = "idle"
    RUNNING = "running"
    DONE    = "done"
    ERROR   = "error"

    def __init__(self, engine: RecorderEngine):
        self._engine   = engine
        self.state     = self.IDLE
        self.progress  = 0
        self.result:   list[float | None] = [None] * NUM_CH
        self.error_msg = ""

    def start(self, n: int = CAL_SAMPLES):
        self._n    = n
        self.state = self.RUNNING
        self.progress = 0
        self.result   = [None] * NUM_CH
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            accum  = [0.0] * NUM_CH
            counts = [0]   * NUM_CH
            for step in range(self._n):
                for i in range(NUM_CH):
                    v = self._engine.latest[i]
                    if v is not None:
                        accum[i]  += v
                        counts[i] += 1
                self.progress = int((step + 1) / self._n * 100)
                time.sleep(0.05)
            self.result = [
                accum[i] / counts[i] if counts[i] > 0 else None
                for i in range(NUM_CH)
            ]
            self.state = self.DONE
        except Exception as exc:
            self.error_msg = str(exc)
            self.state     = self.ERROR


# ═════════════════════════════════════════════════════════════════════════════
#  CSV helpers
# ═════════════════════════════════════════════════════════════════════════════

def find_csv_files(folder: str) -> list[dict]:
    folder = Path(folder)
    files  = []
    for p in sorted(folder.glob(CSV_GLOB), key=os.path.getmtime, reverse=True):
        stat = p.stat()
        files.append({
            "path":     str(p),
            "filename": p.name,
            "size_kb":  f"{stat.st_size / 1024:.1f}",
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "rows":     _count_rows(p),
        })
    return files


def _count_rows(path: Path) -> int:
    try:
        with open(path, newline="") as f:
            return sum(1 for _ in f) - 1
    except Exception:
        return 0


def load_csv(path: str) -> tuple[list[str], list[list]]:
    headers, rows = [], []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0:
                headers = row
            else:
                rows.append(row)
    return headers, rows


# ═════════════════════════════════════════════════════════════════════════════
#  Main application
# ═════════════════════════════════════════════════════════════════════════════

class BridgeHMI(tk.Tk):

    def __init__(self, start_folder: str = "."):
        super().__init__()
        self.title("PhidgetBridge HMI")
        self.geometry("1200x760")
        self.minsize(960, 580)
        self.configure(bg=BG)

        self.folder         = tk.StringVar(value=str(Path(start_folder).resolve()))
        self.csv_files:     list[dict] = []
        self.selected_path: str | None = None
        self._data_headers: list[str]  = []
        self._data_rows:    list[list] = []

        self._cal     = CalibrationStore()
        self._engine  = RecorderEngine()
        self._sampler = CalSampler(self._engine)

        self._engine.save_folder = str(Path(start_folder).resolve())
        self._engine.calibration = self._cal
        self._cal.load(self._engine.save_folder)

        self._apply_style()
        self._build_ui()
        self.refresh_file_list()
        self._poll_recorder()
        self._poll_cal_sampler()

    # ══════════════════════════════════════════════════════════════════════════
    #  Styles
    # ══════════════════════════════════════════════════════════════════════════

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",              background=BG,  foreground=TEXT, font=("Segoe UI", 10))
        s.configure("TFrame",         background=BG)
        s.configure("TLabel",         background=BG,  foreground=TEXT)
        s.configure("TButton",        background=BG3, foreground=TEXT, relief="flat", padding=(10, 5))
        s.map("TButton",
              background=[("active", ACCENT),   ("pressed", ACCENT2)],
              foreground=[("active", "#ffffff")])
        s.configure("Accent.TButton", background=ACCENT,   foreground="#ffffff")
        s.map("Accent.TButton",
              background=[("active", ACCENT2),  ("pressed", BG3)])
        s.configure("Green.TButton",  background="#14532d", foreground="#bbf7d0")
        s.map("Green.TButton",
              background=[("active", GREEN),    ("pressed", BG3)],
              foreground=[("active", "#000000")])
        s.configure("Orange.TButton", background="#7c2d12", foreground="#fed7aa")
        s.map("Orange.TButton",
              background=[("active", ORANGE),   ("pressed", BG3)],
              foreground=[("active", "#000000")])
        s.configure("Red.TButton",    background="#7f1d1d", foreground="#fca5a5")
        s.map("Red.TButton",
              background=[("active", RED),      ("pressed", BG3)])
        s.configure("TEntry",         fieldbackground=BG3, foreground=TEXT, insertcolor=TEXT, relief="flat")
        s.configure("TCombobox",      fieldbackground=BG3, foreground=TEXT, selectbackground=ACCENT)
        s.map("TCombobox",            fieldbackground=[("readonly", BG3)])
        s.configure("TSpinbox",       fieldbackground=BG3, foreground=TEXT, insertcolor=TEXT)
        s.configure("TNotebook",      background=BG,  tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab",  background=BG3, foreground=TEXT_DIM, padding=[14, 6])
        s.map("TNotebook.Tab",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
        s.configure("TLabelframe",        background=BG,  foreground=ACCENT2, relief="flat")
        s.configure("TLabelframe.Label",  background=BG,  foreground=ACCENT2, font=("Segoe UI", 9, "bold"))
        s.configure("Files.Treeview",     background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=28, borderwidth=0)
        s.configure("Files.Treeview.Heading", background=BG3, foreground=ACCENT2, relief="flat", padding=(6, 6))
        s.map("Files.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
        s.configure("Data.Treeview",      background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=24, font=("Consolas", 9), borderwidth=0)
        s.configure("Data.Treeview.Heading", background=BG3, foreground=ACCENT2, relief="flat", padding=(4, 5))
        s.map("Data.Treeview",
              background=[("selected", BG3)],
              foreground=[("selected", ACCENT2)])
        s.configure("TScrollbar",         background=BG3, troughcolor=BG, arrowcolor=TEXT_DIM, relief="flat")
        s.configure("TProgressbar",       troughcolor=BG3, background=ACCENT, thickness=10)
        s.configure("Cal.TProgressbar",   troughcolor=BG3, background=GREEN,  thickness=8)
        s.configure("TCheckbutton",       background=BG,  foreground=TEXT)
        s.map("TCheckbutton",             background=[("active", BG)])
        s.configure("TSeparator",         background=BG3)

    # ══════════════════════════════════════════════════════════════════════════
    #  Top bar + paned layout
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        top = ttk.Frame(self, padding=(12, 8))
        top.pack(fill="x")
        ttk.Label(top, text="📁 Save / Browse Folder:", foreground=TEXT_DIM).pack(side="left")
        ttk.Entry(top, textvariable=self.folder, width=52).pack(side="left", padx=6)
        ttk.Button(top, text="Browse…",    command=self._browse_folder).pack(side="left", padx=2)
        ttk.Button(top, text="⟳ Refresh",  command=self.refresh_file_list,
                   style="Accent.TButton").pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status_var, foreground=TEXT_DIM).pack(side="right", padx=8)

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane, padding=(8, 8, 4, 8))
        pane.add(left, weight=1)
        self._build_file_list(left)

        right = ttk.Frame(pane, padding=(4, 8, 8, 8))
        pane.add(right, weight=3)
        self._build_right_panel(right)

    # ── File list (left panel) ────────────────────────────────────────────────

    def _build_file_list(self, parent):
        ttk.Label(parent, text="CSV FILES", foreground=ACCENT2,
                  font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 6))

        cols = ("filename", "rows", "size_kb", "modified")
        self.file_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                       style="Files.Treeview", selectmode="browse")
        for col, label, w, anc in [
            ("filename", "File",     160, "w"),
            ("rows",     "Rows",      55, "center"),
            ("size_kb",  "KB",        55, "center"),
            ("modified", "Modified", 145, "center"),
        ]:
            self.file_tree.heading(col, text=label)
            self.file_tree.column(col, width=w, anchor=anc)

        sb = ttk.Scrollbar(parent, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=sb.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.file_tree.bind("<<TreeviewSelect>>", self._on_file_select)

    # ── Right panel (notebook) ────────────────────────────────────────────────

    def _build_right_panel(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True)

        for text, builder in [
            ("  🔴 Record  ",      self._build_record_tab),
            ("  ⚖  Calibrate  ",  self._build_cal_tab),
            ("  📋 Data Table  ", self._build_data_table),
            ("  📊 Summary  ",    self._build_stats_panel),
        ]:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=text)
            builder(tab)

        if HAS_MPL:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text="  📈 Chart  ")
            self._build_chart_panel(tab)

        self.placeholder = ttk.Label(
            parent, text="← Select a file to view its contents",
            foreground=TEXT_DIM, font=("Segoe UI", 12))

    # ══════════════════════════════════════════════════════════════════════════
    #  RECORD TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_record_tab(self, parent):
        cols = ttk.Frame(parent)
        cols.pack(fill="both", expand=True, padx=8, pady=8)
        cols.columnconfigure(0, weight=0, minsize=280)
        cols.columnconfigure(1, weight=1)
        cols.rowconfigure(0, weight=1)
        self._build_config_panel(cols)
        self._build_live_panel(cols)

    def _build_config_panel(self, parent):
        cfg = ttk.Frame(parent, padding=(0, 0, 12, 0))
        cfg.grid(row=0, column=0, sticky="nsew")

        # Device
        dev = ttk.LabelFrame(cfg, text="Device", padding=10)
        dev.pack(fill="x", pady=(0, 8))
        ttk.Label(dev, text="Serial number  (blank = auto)", foreground=TEXT_DIM,
                  font=("Segoe UI", 8)).pack(anchor="w")
        self._r_serial = tk.StringVar()
        ttk.Entry(dev, textvariable=self._r_serial, width=18).pack(anchor="w", pady=(2, 6))
        ttk.Label(dev, text="Data interval (ms)", foreground=TEXT_DIM,
                  font=("Segoe UI", 8)).pack(anchor="w")
        self._r_interval = tk.IntVar(value=50)
        ttk.Spinbox(dev, from_=10, to=1000, increment=10,
                    textvariable=self._r_interval, width=8).pack(anchor="w", pady=(2, 0))

        # Trigger
        trg = ttk.LabelFrame(cfg, text="Trigger", padding=10)
        trg.pack(fill="x", pady=(0, 8))
        ttk.Label(trg, text="Watch channel", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_trg_ch = tk.IntVar(value=0)
        ttk.Combobox(trg, textvariable=self._r_trg_ch, values=[0, 1, 2, 3],
                     state="readonly", width=6).pack(anchor="w", pady=(2, 6))
        ttk.Label(trg, text="Threshold  (V/V)", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_threshold = tk.DoubleVar(value=0.01)
        ttk.Entry(trg, textvariable=self._r_threshold, width=12).pack(anchor="w", pady=(2, 6))
        ttk.Label(trg, text="Direction", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_direction = tk.StringVar(value="either")
        ttk.Combobox(trg, textvariable=self._r_direction,
                     values=["either", "rising", "falling"],
                     state="readonly", width=10).pack(anchor="w", pady=(2, 0))

        # Capture
        cap = ttk.LabelFrame(cfg, text="Capture", padding=10)
        cap.pack(fill="x", pady=(0, 8))
        ttk.Label(cap, text="Number of points", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_npoints = tk.IntVar(value=100)
        ttk.Spinbox(cap, from_=10, to=10000, increment=10,
                    textvariable=self._r_npoints, width=8).pack(anchor="w", pady=(2, 0))

        # Buttons
        btn_row = ttk.Frame(cfg)
        btn_row.pack(fill="x", pady=(4, 0))
        self._btn_start = ttk.Button(btn_row, text="▶  Start",
                                      style="Accent.TButton", command=self._rec_start)
        self._btn_start.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn_abort = ttk.Button(btn_row, text="■  Abort",
                                      style="Red.TButton", command=self._rec_abort,
                                      state="disabled")
        self._btn_abort.pack(side="left", expand=True, fill="x")

        if not HAS_PHIDGET:
            ttk.Label(cfg, text="⚠  Phidget22 not installed.\nRecording unavailable.",
                      foreground=YELLOW, font=("Segoe UI", 9), justify="center").pack(pady=(10, 0))

    def _build_live_panel(self, parent):
        live = ttk.Frame(parent)
        live.grid(row=0, column=1, sticky="nsew")

        banner = ttk.Frame(live, padding=(0, 0, 0, 6))
        banner.pack(fill="x")
        self._state_var = tk.StringVar(value="IDLE")
        self._state_lbl = tk.Label(banner, textvariable=self._state_var,
                                    fg=TEXT_DIM, bg=BG, font=("Segoe UI", 13, "bold"))
        self._state_lbl.pack(side="left")

        prog_frame = ttk.Frame(banner)
        prog_frame.pack(side="right", fill="x", expand=True, padx=(16, 0))
        self._prog_var = tk.IntVar(value=0)
        self._prog_bar = ttk.Progressbar(prog_frame, variable=self._prog_var,
                                          maximum=100, style="TProgressbar")
        self._prog_bar.pack(fill="x", pady=(6, 2))
        self._prog_lbl = tk.StringVar(value="0 / 0")
        ttk.Label(prog_frame, textvariable=self._prog_lbl,
                  foreground=TEXT_DIM, font=("Consolas", 9)).pack(anchor="e")

        ttk.Separator(live, orient="horizontal").pack(fill="x", pady=(0, 8))

        gauge_frame = ttk.LabelFrame(live, text="Live Channel Values  (V/V)", padding=10)
        gauge_frame.pack(fill="x", pady=(0, 8))
        self._gauge_vars = []
        self._gauge_bars = []
        for i in range(NUM_CH):
            row = ttk.Frame(gauge_frame)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"CH {i}", fg=CH_COLOURS[i], bg=BG,
                     font=("Consolas", 10, "bold"), width=5).pack(side="left")
            cv = tk.Canvas(row, height=16, bg=BG2, highlightthickness=0)
            cv.pack(side="left", fill="x", expand=True, padx=6)
            self._gauge_bars.append(cv)
            var = tk.StringVar(value="---")
            tk.Label(row, textvariable=var, fg=CH_COLOURS[i], bg=BG,
                     font=("Consolas", 10), width=14, anchor="e").pack(side="right")
            self._gauge_vars.append(var)

        log_frame = ttk.LabelFrame(live, text="Capture Log", padding=(6, 4))
        log_frame.pack(fill="both", expand=True)
        self._log = tk.Text(log_frame, bg=BG2, fg=TEXT, font=("Consolas", 9),
                            relief="flat", state="disabled", wrap="none",
                            insertbackground=TEXT, height=8)
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=log_sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")
        for tag, colour in [("info", TEXT_DIM), ("ok", GREEN), ("trigger", YELLOW),
                             ("error", RED), ("done", ACCENT2)]:
            self._log.tag_configure(tag, foreground=colour)

    # ══════════════════════════════════════════════════════════════════════════
    #  CALIBRATION TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_cal_tab(self, parent):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(parent, padding=(12, 10, 12, 4))
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚖  Sensor Calibration  —  V/V  →  lbf",
                 fg=ACCENT2, bg=BG, font=("Segoe UI", 12, "bold")).pack(side="left")

        # Sampling progress (shared)
        self._cal_prog_var = tk.IntVar(value=0)
        self._cal_prog_bar = ttk.Progressbar(hdr, variable=self._cal_prog_var,
                                              maximum=100, style="Cal.TProgressbar", length=180)
        self._cal_prog_bar.pack(side="right", padx=(12, 0))
        self._cal_prog_lbl = tk.StringVar(value="")
        ttk.Label(hdr, textvariable=self._cal_prog_lbl,
                  foreground=TEXT_DIM, font=("Consolas", 9)).pack(side="right", padx=6)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8)

        # ── Instructions ──────────────────────────────────────────────────────
        note_frame = ttk.Frame(parent, padding=(14, 8, 14, 4))
        note_frame.pack(fill="x")
        note = (
            "Step 1 — Remove all load from the sensor, then click  \"⊙ Capture Zero\"  for each channel.\n"
            "Step 2 — Apply a known reference load (lbf), enter the value, then click  \"⊙ Capture Cal Point\".\n"
            f"Each step averages  {CAL_SAMPLES} live samples  (~2.5 s).  "
            "Click  \"💾 Save Calibration\"  when finished."
        )
        tk.Label(note_frame, text=note, fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9), justify="left", anchor="w").pack(anchor="w")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8, pady=(4, 0))

        # ── 4 channel cards in a 2 × 2 grid ──────────────────────────────────
        grid = ttk.Frame(parent, padding=(10, 8))
        grid.pack(fill="both", expand=True)
        grid.columnconfigure((0, 1), weight=1)
        grid.rowconfigure((0, 1), weight=1)

        self._cal_cards: list[dict] = []
        for i in range(NUM_CH):
            card = self._build_cal_card(grid, i)
            card["frame"].grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
            self._cal_cards.append(card)

        # ── Bottom action bar ─────────────────────────────────────────────────
        bot = ttk.Frame(parent, padding=(12, 6))
        bot.pack(fill="x", side="bottom")
        ttk.Button(bot, text="💾  Save Calibration",
                   style="Accent.TButton", command=self._cal_save).pack(side="left", padx=(0, 8))
        ttk.Button(bot, text="Reset All Channels",
                   style="Red.TButton",    command=self._cal_reset_all).pack(side="left")
        self._cal_status_var = tk.StringVar(value="")
        tk.Label(bot, textvariable=self._cal_status_var,
                 fg=GREEN, bg=BG, font=("Segoe UI", 9)).pack(side="right", padx=8)

        # Load any saved calibration into the cards
        self._cal_refresh_all_cards()

    def _build_cal_card(self, parent, ch: int) -> dict:
        colour = CH_COLOURS[ch]
        frame  = ttk.LabelFrame(parent, text=f"  Channel {ch}", padding=10)

        # Live readings row
        live_row = ttk.Frame(frame)
        live_row.pack(fill="x", pady=(0, 4))
        tk.Label(live_row, text="Live raw:", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")
        live_raw_var = tk.StringVar(value="---")
        tk.Label(live_row, textvariable=live_raw_var, fg=colour, bg=BG,
                 font=("Consolas", 10, "bold"), width=13).pack(side="left", padx=(4, 0))
        tk.Label(live_row, text="V/V", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(2, 12))
        tk.Label(live_row, text="lbf:", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")
        live_lbf_var = tk.StringVar(value="—")
        tk.Label(live_row, textvariable=live_lbf_var, fg=ORANGE, bg=BG,
                 font=("Consolas", 10, "bold"), width=11).pack(side="left", padx=(4, 0))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(4, 8))

        # ── Step 1: Zero point ────────────────────────────────────────────────
        zero_lf = ttk.LabelFrame(frame, text="Step 1 — Zero Point  (no load)", padding=8)
        zero_lf.pack(fill="x", pady=(0, 6))

        zero_result_var = tk.StringVar(value="Not captured")
        tk.Label(zero_lf, textvariable=zero_result_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 4))

        btn_zero = ttk.Button(zero_lf, text="⊙  Capture Zero",
                               style="Green.TButton",
                               command=lambda c=ch: self._cal_capture_zero(c))
        btn_zero.pack(fill="x")

        # ── Step 2: Calibration point ─────────────────────────────────────────
        cal_lf = ttk.LabelFrame(frame, text="Step 2 — Cal Point  (known load applied)", padding=8)
        cal_lf.pack(fill="x", pady=(0, 6))

        load_row = ttk.Frame(cal_lf)
        load_row.pack(fill="x", pady=(0, 6))
        tk.Label(load_row, text="Known load:", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")
        load_var = tk.DoubleVar(value=10.0)
        ttk.Entry(load_row, textvariable=load_var, width=10).pack(side="left", padx=6)
        tk.Label(load_row, text="lbf", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")

        cal_result_var = tk.StringVar(value="Not captured")
        tk.Label(cal_lf, textvariable=cal_result_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 4))

        btn_cal = ttk.Button(cal_lf, text="⊙  Capture Cal Point",
                              style="Orange.TButton",
                              command=lambda c=ch: self._cal_capture_point(c))
        btn_cal.pack(fill="x")

        # ── Calibration equation display ──────────────────────────────────────
        eq_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=eq_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 8), anchor="w").pack(fill="x", pady=(4, 0))

        # ── Status badge ──────────────────────────────────────────────────────
        status_var = tk.StringVar(value="⬤  NOT CALIBRATED")
        status_lbl = tk.Label(frame, textvariable=status_var, fg=RED, bg=BG,
                               font=("Segoe UI", 9, "bold"))
        status_lbl.pack(anchor="w", pady=(4, 0))

        return {
            "frame":           frame,
            "live_raw_var":    live_raw_var,
            "live_lbf_var":    live_lbf_var,
            "zero_result_var": zero_result_var,
            "cal_result_var":  cal_result_var,
            "eq_var":          eq_var,
            "load_var":        load_var,
            "status_var":      status_var,
            "status_lbl":      status_lbl,
            "btn_zero":        btn_zero,
            "btn_cal":         btn_cal,
            "pending":         None,   # "zero" | "point" | None
        }

    # ── Calibration card refresh ──────────────────────────────────────────────

    def _cal_refresh_card(self, ch: int):
        card = self._cal_cards[ch]
        c    = self._cal.channels[ch]

        if c["zero_raw"] is not None:
            card["zero_result_var"].set(f"avg = {c['zero_raw']:+.6f} V/V")
        else:
            card["zero_result_var"].set("Not captured")

        if c["cal_raw"] is not None:
            card["cal_result_var"].set(
                f"avg = {c['cal_raw']:+.6f} V/V  @  {c['cal_load_lbf']:.4f} lbf")
        else:
            card["cal_result_var"].set("Not captured")

        if c["calibrated"]:
            z   = c["zero_offset"]
            sf  = c["scale_factor"]
            sign = "+" if -z * sf >= 0 else ""
            card["eq_var"].set(
                f"lbf = (raw − {c['zero_offset']:+.5f}) × {sf:.4f}   "
                f"[ts: {c['timestamp'][11:19]}]")
            card["status_var"].set("⬤  CALIBRATED")
            card["status_lbl"].configure(fg=GREEN)
        else:
            card["eq_var"].set("")
            card["status_var"].set("⬤  NOT CALIBRATED")
            card["status_lbl"].configure(fg=RED)

    def _cal_refresh_all_cards(self):
        for ch in range(NUM_CH):
            self._cal_refresh_card(ch)

    # ── Calibration capture actions ───────────────────────────────────────────

    def _cal_capture_zero(self, ch: int):
        if not self._sampler_ready():
            return
        # Mark only this channel as pending
        for card in self._cal_cards:
            card["pending"] = None
        self._cal_cards[ch]["pending"] = "zero"
        self._cal_disable_buttons()
        self._cal_prog_lbl.set(f"Sampling CH{ch} zero  ({CAL_SAMPLES} pts) …")
        self._sampler.start()

    def _cal_capture_point(self, ch: int):
        if not self._sampler_ready():
            return
        if self._cal.channels[ch]["zero_raw"] is None:
            messagebox.showwarning("Zero not set",
                                    f"Capture the zero point for Channel {ch} first.")
            return
        for card in self._cal_cards:
            card["pending"] = None
        self._cal_cards[ch]["pending"] = "point"
        self._cal_disable_buttons()
        self._cal_prog_lbl.set(f"Sampling CH{ch} cal point  ({CAL_SAMPLES} pts) …")
        self._sampler.start()

    def _sampler_ready(self) -> bool:
        if self._sampler.state == CalSampler.RUNNING:
            messagebox.showinfo("Busy", "A calibration sample is already in progress.")
            return False
        if not HAS_PHIDGET:
            messagebox.showerror("No device",
                                  "Phidget22 is not installed.\n"
                                  "Install it with:  pip install Phidget22")
            return False
        if all(v is None for v in self._engine.latest):
            messagebox.showwarning("No live data",
                                    "No live readings — connect the device via the Record tab first.")
            return False
        return True

    def _cal_disable_buttons(self):
        for card in self._cal_cards:
            card["btn_zero"].configure(state="disabled")
            card["btn_cal"].configure(state="disabled")

    def _cal_enable_buttons(self):
        for card in self._cal_cards:
            card["btn_zero"].configure(state="normal")
            card["btn_cal"].configure(state="normal")

    def _cal_save(self):
        try:
            self._cal.save(self.folder.get())
            self._cal_status_var.set(
                f"✓ Saved  calibration.json  ({datetime.now().strftime('%H:%M:%S')})")
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))

    def _cal_reset_all(self):
        if not messagebox.askyesno("Reset calibration",
                                    "Clear all calibration data for all channels?"):
            return
        self._cal.channels = [CalibrationStore._blank() for _ in range(NUM_CH)]
        self._cal_refresh_all_cards()
        self._cal_status_var.set("All channels reset.")

    # ── Cal sampler polling loop ──────────────────────────────────────────────

    def _poll_cal_sampler(self):
        s = self._sampler

        # Always update live lbf readouts on cal cards
        for i in range(NUM_CH):
            v    = self._engine.latest[i]
            card = self._cal_cards[i]
            if v is None:
                card["live_raw_var"].set("---")
                card["live_lbf_var"].set("—")
            else:
                card["live_raw_var"].set(f"{v:+.7f}")
                if self._cal.is_calibrated(i):
                    card["live_lbf_var"].set(f"{self._cal.to_lbf(i, v):+.4f}")
                else:
                    card["live_lbf_var"].set("—")

        if s.state == CalSampler.RUNNING:
            self._cal_prog_var.set(s.progress)

        elif s.state == CalSampler.DONE:
            self._cal_prog_var.set(100)

            # Find the channel that triggered this sample
            for ch, card in enumerate(self._cal_cards):
                if card["pending"] in ("zero", "point"):
                    avg = s.result[ch]
                    if avg is None:
                        messagebox.showerror("No data",
                                              f"CH{ch} returned no readings — check connection.")
                    else:
                        try:
                            if card["pending"] == "zero":
                                self._cal.set_zero(ch, avg)
                            else:
                                load = card["load_var"].get()
                                self._cal.set_cal_point(ch, avg, load)
                        except ValueError as exc:
                            messagebox.showerror("Calibration error", str(exc))
                        self._cal_refresh_card(ch)
                    card["pending"] = None
                    break

            self._cal_enable_buttons()
            self._cal_prog_lbl.set("Done")
            s.state = CalSampler.IDLE
            self._cal_prog_var.set(0)

        elif s.state == CalSampler.ERROR:
            messagebox.showerror("Sampler error", s.error_msg)
            self._cal_enable_buttons()
            s.state = CalSampler.IDLE
            self._cal_prog_var.set(0)
            self._cal_prog_lbl.set("")
            for card in self._cal_cards:
                card["pending"] = None

        self.after(100, self._poll_cal_sampler)

    # ══════════════════════════════════════════════════════════════════════════
    #  DATA TABLE TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_data_table(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(toolbar, text="Filter:", foreground=TEXT_DIM).pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(toolbar, textvariable=self.filter_var, width=22).pack(side="left", padx=6)
        self.row_count_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.row_count_var,
                  foreground=TEXT_DIM).pack(side="right", padx=8)
        ttk.Button(toolbar, text="Export copy…",
                   command=self._export_filtered).pack(side="right", padx=4)

        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=4, pady=4)
        self.data_tree = ttk.Treeview(frame, show="headings",
                                       style="Data.Treeview", selectmode="browse")
        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.data_tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.data_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.data_tree.tag_configure("odd",  background=BG2)
        self.data_tree.tag_configure("even", background="#252536")

    # ══════════════════════════════════════════════════════════════════════════
    #  SUMMARY TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_stats_panel(self, parent):
        self.stats_text = tk.Text(parent, bg=BG2, fg=TEXT, font=("Consolas", 10),
                                   relief="flat", wrap="none", state="disabled",
                                   insertbackground=TEXT, selectbackground=ACCENT)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.stats_text.yview)
        self.stats_text.configure(yscrollcommand=sb.set)
        self.stats_text.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        sb.pack(side="right", fill="y", pady=4)
        for tag, colour, bold in [
            ("header", ACCENT2,      True),
            ("label",  TEXT_DIM,     False),
            ("value",  GREEN,        False),
            ("ch0",    CH_COLOURS[0],False),
            ("ch1",    CH_COLOURS[1],False),
            ("ch2",    CH_COLOURS[2],False),
            ("ch3",    CH_COLOURS[3],False),
            ("lbf",    ORANGE,       False),
        ]:
            kw: dict = {"foreground": colour}
            if bold:
                kw["font"] = ("Consolas", 10, "bold")
            self.stats_text.tag_configure(tag, **kw)

    # ══════════════════════════════════════════════════════════════════════════
    #  CHART TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_chart_panel(self, parent):
        ctrl = ttk.Frame(parent, padding=(8, 6))
        ctrl.pack(fill="x")
        ttk.Label(ctrl, text="Show:", foreground=TEXT_DIM).pack(side="left")
        self._chart_mode = tk.StringVar(value="V/V  (raw)")
        ttk.Combobox(ctrl, textvariable=self._chart_mode,
                     values=["V/V  (raw)", "lbf  (calibrated channels only)"],
                     state="readonly", width=30).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Redraw", command=self._redraw_chart).pack(side="left")

        self._fig = Figure(figsize=(6, 4), dpi=100, facecolor=BG)
        self._ax  = self._fig.add_subplot(111, facecolor=BG2)
        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)
        tf = ttk.Frame(parent)
        tf.pack(fill="x")
        NavigationToolbar2Tk(self._canvas, tf)

    def _redraw_chart(self):
        if self._data_headers:
            self._populate_chart(self._data_headers, self._data_rows)

    # ══════════════════════════════════════════════════════════════════════════
    #  Recorder control
    # ══════════════════════════════════════════════════════════════════════════

    def _rec_start(self):
        if not HAS_PHIDGET:
            messagebox.showerror("Phidget22 not found",
                                  "Install with:  pip install Phidget22")
            return
        e = self._engine
        e.save_folder       = self.folder.get()
        serial              = self._r_serial.get().strip()
        e.serial_number     = serial if serial else None
        e.data_interval_ms  = self._r_interval.get()
        e.trigger_channel   = self._r_trg_ch.get()
        e.trigger_threshold = float(self._r_threshold.get())
        e.trigger_direction = self._r_direction.get()
        e.num_points        = self._r_npoints.get()
        e.calibration       = self._cal

        self._prog_bar.configure(maximum=e.num_points)
        self._log_clear()
        self._log_append(
            "Connecting to PhidgetBridge"
            + (f" (serial {serial})" if serial else " (auto-detect)") + " …", "info")
        self._btn_start.configure(state="disabled")
        self._btn_abort.configure(state="normal")
        e.start()

    def _rec_abort(self):
        self._engine.abort()
        self._log_append("Abort requested …", "error")
        self._btn_abort.configure(state="disabled")

    def _log_append(self, msg: str, tag: str = "info"):
        self._log.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._log.insert("end", f"[{ts}]  {msg}\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _draw_gauge(self, idx: int, value: float | None):
        cv  = self._gauge_bars[idx]
        cv.delete("all")
        W   = cv.winfo_width() or 200
        H   = cv.winfo_height() or 16
        mid = W // 2
        cv.create_rectangle(2, H // 2 - 3, W - 2, H // 2 + 3, fill=BG3, outline="")
        if value is not None:
            clamp = max(-1.0, min(1.0, value))
            colour = CH_COLOURS[idx]
            if clamp >= 0:
                x1, x2 = mid, mid + int(clamp * (W // 2 - 4))
            else:
                x1, x2 = mid + int(clamp * (W // 2 - 4)), mid
            if x1 != x2:
                cv.create_rectangle(x1, H // 2 - 3, x2, H // 2 + 3, fill=colour, outline="")
        cv.create_line(mid, 2, mid, H - 2, fill=TEXT_DIM, width=1)

    # ══════════════════════════════════════════════════════════════════════════
    #  Recorder polling loop
    # ══════════════════════════════════════════════════════════════════════════

    _prev_state       = None
    _prev_count       = -1
    _logged_connected = False
    _logged_trigger   = False

    def _poll_recorder(self):
        e     = self._engine
        state = e.state

        for i in range(NUM_CH):
            v = e.latest[i]
            self._gauge_vars[i].set(f"{v:+.6f}" if v is not None else "---")
            self._draw_gauge(i, v)

        if state != self._prev_state:
            self._prev_state = state
            if state == RecorderEngine.CONNECTING:
                self._set_banner("CONNECTING …", YELLOW)
            elif state == RecorderEngine.WAITING:
                if not self._logged_connected:
                    self._logged_connected = True
                    self._log_append("All 4 channels attached.", "ok")
                    self._log_append(
                        f"Waiting for trigger on CH{e.trigger_channel}  "
                        f"({'|value|' if e.trigger_direction == 'either' else e.trigger_direction} "
                        f"> {e.trigger_threshold:.4f} V/V) …", "info")
                self._set_banner("WAITING FOR TRIGGER", YELLOW)
                self._prog_var.set(0)
                self._prog_lbl.set(f"0 / {e.capture_target}")
            elif state == RecorderEngine.RECORDING:
                if not self._logged_trigger:
                    self._logged_trigger = True
                    self._log_append("⚡ Trigger fired — recording started.", "trigger")
                self._set_banner("RECORDING", GREEN)
            elif state == RecorderEngine.DONE:
                self._set_banner("DONE", ACCENT2)
                self._log_append(
                    f"✓ Captured {e.capture_count} points → {Path(e.saved_path).name}", "done")
                self._btn_start.configure(state="normal")
                self._btn_abort.configure(state="disabled")
                self._logged_connected = False
                self._logged_trigger   = False
                self.refresh_file_list()
            elif state == RecorderEngine.ERROR:
                self._set_banner("ERROR", RED)
                self._log_append(f"✗ {e.error_msg}", "error")
                self._btn_start.configure(state="normal")
                self._btn_abort.configure(state="disabled")
                self._logged_connected = False
                self._logged_trigger   = False
            elif state == RecorderEngine.IDLE:
                self._set_banner("IDLE", TEXT_DIM)

        if state == RecorderEngine.RECORDING:
            cnt = e.capture_count
            if cnt != self._prev_count:
                self._prev_count = cnt
                self._prog_var.set(cnt)
                self._prog_lbl.set(f"{cnt} / {e.capture_target}")

        self.after(80, self._poll_recorder)

    def _set_banner(self, text: str, colour: str):
        self._state_var.set(text)
        self._state_lbl.configure(fg=colour)

    # ══════════════════════════════════════════════════════════════════════════
    #  Folder / file helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder.get(), title="Select folder")
        if folder:
            self.folder.set(folder)
            self._engine.save_folder = folder
            self._cal.load(folder)
            self._cal_refresh_all_cards()
            self.refresh_file_list()

    def refresh_file_list(self):
        folder = self.folder.get()
        if not Path(folder).is_dir():
            self.status_var.set("Invalid folder")
            return
        self.csv_files = find_csv_files(folder)
        self.file_tree.delete(*self.file_tree.get_children())
        for meta in self.csv_files:
            self.file_tree.insert("", "end",
                values=(meta["filename"], meta["rows"], meta["size_kb"], meta["modified"]),
                tags=(meta["path"],))
        count = len(self.csv_files)
        self.status_var.set(f"{count} file{'s' if count != 1 else ''} found")

    def _on_file_select(self, _event=None):
        sel = self.file_tree.selection()
        if not sel:
            return
        path = self.file_tree.item(sel[0], "tags")[0]
        self.selected_path = path
        try:
            self.placeholder.place_forget()
        except Exception:
            pass
        try:
            headers, rows = load_csv(path)
        except Exception as exc:
            messagebox.showerror("Read error", str(exc))
            return
        self._data_headers = headers
        self._data_rows    = rows
        self.filter_var.set("")
        self._populate_data_table(headers, rows)
        self._populate_stats(headers, rows)
        if HAS_MPL:
            self._populate_chart(headers, rows)

    # ── Data table ────────────────────────────────────────────────────────────

    def _populate_data_table(self, headers, rows):
        tree = self.data_tree
        tree.delete(*tree.get_children())
        tree["columns"] = headers
        for col in headers:
            tree.heading(col, text=col, anchor="center")
            tree.column(col, width=max(100, len(col) * 11), anchor="center", minwidth=70)
        for i, row in enumerate(rows):
            tree.insert("", "end", values=row, tags=("odd" if i % 2 else "even",))
        self.row_count_var.set(f"{len(rows)} rows")

    def _apply_filter(self):
        term = self.filter_var.get().lower()
        rows = [r for r in self._data_rows
                if not term or any(term in str(c).lower() for c in r)]
        self._populate_data_table(self._data_headers, rows)

    def _export_filtered(self):
        if not self._data_headers:
            return
        term = self.filter_var.get().lower()
        rows = [r for r in self._data_rows
                if not term or any(term in str(c).lower() for c in r)]
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             filetypes=[("CSV", "*.csv")],
                                             title="Save filtered data")
        if path:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerows([self._data_headers] + rows)
            messagebox.showinfo("Exported", f"Saved {len(rows)} rows to:\n{path}")

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _populate_stats(self, headers, rows):
        t = self.stats_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not rows:
            t.insert("end", "No data.\n")
            t.configure(state="disabled")
            return

        raw_cols = [h for h in headers if h.startswith("ch") and "lbf" not in h]
        lbf_cols = [h for h in headers if h.endswith("_lbf")]

        def stat_block(cols: list[str], unit_tag: str):
            col_w = 14
            t.insert("end",
                     "  " + f"{'Metric':<12}" + "".join(f"{c:>{col_w}}" for c in cols) + "\n",
                     "header")
            t.insert("end", "  " + "─" * (12 + col_w * len(cols)) + "\n", "label")

            data: dict[str, list[float]] = {c: [] for c in cols}
            for row in rows:
                for c in cols:
                    try:
                        data[c].append(float(row[headers.index(c)]))
                    except (ValueError, IndexError):
                        pass

            fmt = lambda v: f"{v:+.6f}"
            for metric, fn in [
                ("Min",   min),
                ("Max",   max),
                ("Mean",  lambda xs: sum(xs) / len(xs)),
                ("Range", lambda xs: max(xs) - min(xs)),
                ("Std",   lambda xs: (sum((x - sum(xs)/len(xs))**2 for x in xs)/len(xs))**0.5),
            ]:
                t.insert("end", f"  {metric:<12}", "label")
                for i, c in enumerate(cols):
                    vals = data[c]
                    tag  = unit_tag if unit_tag == "lbf" else (f"ch{i}" if i < 4 else "value")
                    t.insert("end", f"{(fmt(fn(vals)) if vals else 'N/A'):>{col_w}}", tag)
                t.insert("end", "\n")
            t.insert("end", "\n")

        t.insert("end", f"  File: ", "label")
        t.insert("end", f"{Path(self.selected_path).name}\n", "value")
        t.insert("end", f"  Rows: ", "label")
        t.insert("end", f"{len(rows)}\n\n", "value")
        t.insert("end", "  RAW  (V/V)\n", "header")
        stat_block(raw_cols, "ch")
        if lbf_cols:
            t.insert("end", "  CALIBRATED  (lbf)\n", "header")
            stat_block(lbf_cols, "lbf")
        if headers:
            t.insert("end", "  CAPTURE INFO\n", "header")
            t.insert("end", f"  {'Start':<12}", "label")
            t.insert("end", f"{rows[0][0]}\n",  "value")
            t.insert("end", f"  {'End':<12}",   "label")
            t.insert("end", f"{rows[-1][0]}\n", "value")
        t.configure(state="disabled")

    # ── Chart ─────────────────────────────────────────────────────────────────

    def _populate_chart(self, headers, rows):
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG2)
        self._fig.patch.set_facecolor(BG)

        mode    = self._chart_mode.get() if hasattr(self, "_chart_mode") else "V/V"
        use_lbf = "lbf" in mode

        if use_lbf:
            plot_cols = [h for h in headers if h.endswith("_lbf")]
            y_label   = "lbf"
        else:
            plot_cols = [h for h in headers if h.startswith("ch") and "lbf" not in h]
            y_label   = "V / V"

        x = list(range(len(rows)))
        for i, col in enumerate(plot_cols):
            idx  = headers.index(col)
            vals = []
            for row in rows:
                try:    vals.append(float(row[idx]))
                except: vals.append(None)
            cx = [xi for xi, v in zip(x, vals) if v is not None]
            cy = [v  for v in vals if v is not None]
            colour = ORANGE if use_lbf else CH_COLOURS[i % 4]
            ax.plot(cx, cy, color=colour, linewidth=1.4, label=col, alpha=0.9)

        ax.set_xlabel("Sample Index", color=TEXT_DIM, fontsize=9)
        ax.set_ylabel(y_label,        color=TEXT_DIM, fontsize=9)
        ax.set_title(Path(self.selected_path).name if self.selected_path else "",
                     color=TEXT, fontsize=10)
        ax.tick_params(colors=TEXT_DIM)
        for spine in ax.spines.values():
            spine.set_edgecolor(BG3)
        ax.legend(facecolor=BG3, edgecolor=BG3, labelcolor=TEXT, fontsize=9)
        ax.grid(True, color=BG3, linewidth=0.6, linestyle="--")
        self._canvas.draw()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def destroy(self):
        self._engine.abort()
        super().destroy()


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "."
    app = BridgeHMI(start_folder=start)
    app.mainloop()
