"""
PhidgetBridge HMI  —  Recorder + Calibration + Viewer  (Channel 0)
===================================================================
Monitors and records Channel 0 of a PhidgetBridge 4-Input device.

Tabs
────
  🔴 Record     — live gauge, trigger config, capture log
  ⚖  Calibrate  — zero-point + 1-point calibration  (V/V → lbf)
  📋 Data Table — browse CSV files, filter, export
  📊 Summary    — statistics for the selected file
  📈 Chart      — line plot  (requires matplotlib)

Calibration model
─────────────────
  lbf = (raw_V_per_V − zero_offset) × scale_factor

  zero_offset  = avg V/V at zero load
  scale_factor = known_load_lbf / (cal_avg − zero_offset)

Calibration is saved to  calibration.json  in the working folder.

Requirements
────────────
    pip install Phidget22
    pip install matplotlib     # optional — enables Chart tab

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
BG       = "#1e1e2e"
BG2      = "#2a2a3e"
BG3      = "#313145"
ACCENT   = "#7c6af7"
ACCENT2  = "#a78bfa"
TEXT     = "#e2e0f0"
TEXT_DIM = "#8884aa"
GREEN    = "#4ade80"
YELLOW   = "#fbbf24"
RED      = "#f87171"
ORANGE   = "#fb923c"
CH_COL   = "#7c6af7"          # colour for channel 0

CSV_GLOB    = "bridge_data_*.csv"
CAL_FILE    = "calibration.json"
CAL_SAMPLES = 50              # samples averaged per calibration step


# ═════════════════════════════════════════════════════════════════════════════
#  Calibration model
# ═════════════════════════════════════════════════════════════════════════════

class CalibrationStore:
    """
    Single-channel calibration.
    lbf = (raw_V_per_V − zero_offset) × scale_factor
    """

    def __init__(self):
        self._d = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {
            "zero_offset":  0.0,
            "scale_factor": 1.0,
            "cal_load_lbf": 0.0,
            "zero_raw":     None,
            "cal_raw":      None,
            "calibrated":   False,
            "timestamp":    "",
        }

    # ── Conversion ────────────────────────────────────────────────────────────

    def to_lbf(self, raw: float) -> float:
        return (raw - self._d["zero_offset"]) * self._d["scale_factor"]

    @property
    def is_calibrated(self) -> bool:
        return bool(self._d.get("calibrated", False))

    # ── Setters ───────────────────────────────────────────────────────────────

    def set_zero(self, raw_avg: float):
        self._d["zero_offset"] = raw_avg
        self._d["zero_raw"]    = raw_avg
        self._d["calibrated"]  = False
        self._d["timestamp"]   = datetime.now().isoformat(timespec="seconds")

    def set_cal_point(self, raw_avg: float, load_lbf: float):
        span = raw_avg - self._d["zero_offset"]
        if abs(span) < 1e-12:
            raise ValueError("Cal point too close to zero — apply a larger load.")
        self._d["cal_raw"]      = raw_avg
        self._d["cal_load_lbf"] = load_lbf
        self._d["scale_factor"] = load_lbf / span
        self._d["calibrated"]   = True
        self._d["timestamp"]    = datetime.now().isoformat(timespec="seconds")

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, folder: str):
        with open(Path(folder) / CAL_FILE, "w") as f:
            json.dump(self._d, f, indent=2)

    def load(self, folder: str) -> bool:
        p = Path(folder) / CAL_FILE
        if not p.exists():
            return False
        try:
            with open(p) as f:
                data = json.load(f)
            # support both old multi-channel format and new single format
            if "channels" in data:
                data = data["channels"][0]
            self._d.update(data)
            return True
        except Exception:
            return False

    def reset(self):
        self._d = self._blank()

    # ── Read-only properties for display ─────────────────────────────────────

    @property
    def zero_raw(self):
        return self._d["zero_raw"]

    @property
    def cal_raw(self):
        return self._d["cal_raw"]

    @property
    def cal_load_lbf(self):
        return self._d["cal_load_lbf"]

    @property
    def scale_factor(self):
        return self._d["scale_factor"]

    @property
    def zero_offset(self):
        return self._d["zero_offset"]

    @property
    def timestamp(self):
        return self._d["timestamp"]


# ═════════════════════════════════════════════════════════════════════════════
#  Recorder engine  (background thread, channel 0 only)
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
        self.latest: float | None = None    # most recent CH0 reading
        self.captured       = []            # list of (timestamp_str, raw_float)
        self.capture_target = 100
        self.error_msg      = ""
        self._ch            = None          # single VoltageRatioInput handle
        self._stop_evt      = threading.Event()
        self._thread        = None

        self.serial_number     = None
        self.data_interval_ms  = 50
        self.trigger_threshold = 0.01
        self.trigger_direction = "either"   # "rising" | "falling" | "either"
        self.num_points        = 100
        self.save_folder       = "."
        self.calibration: CalibrationStore | None = None

        self._triggered = False
        self.saved_path = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if self.state not in (self.IDLE, self.DONE, self.ERROR):
            return
        self._stop_evt.clear()
        self._triggered     = False
        self.captured       = []
        self.saved_path     = ""
        self.error_msg      = ""
        self.capture_target = self.num_points
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def abort(self):
        self._stop_evt.set()

    @property
    def capture_count(self):
        return len(self.captured)

    # ── Background thread ─────────────────────────────────────────────────────

    def _run(self):
        try:
            self.state = self.CONNECTING
            self._open_channel()
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
            self._close_channel()

    def _open_channel(self):
        ch = VoltageRatioInput()
        ch.setChannel(0)
        if self.serial_number:
            ch.setDeviceSerialNumber(int(self.serial_number))
        ch.setOnAttachHandler(self._on_attach)
        ch.setOnDetachHandler(lambda c: None)
        ch.setOnErrorHandler(self._on_error)
        ch.setOnVoltageRatioChangeHandler(self._on_value_change)
        ch.openWaitForAttachment(5000)
        self._ch = ch

    def _close_channel(self):
        if self._ch is not None:
            try:
                self._ch.close()
            except Exception:
                pass
            self._ch = None

    def _on_attach(self, ch):
        ch.setDataInterval(self.data_interval_ms)

    def _on_error(self, ch, code, desc):
        self.error_msg = f"CH0 error [{code}]: {desc}"

    def _on_value_change(self, ch, value):
        self.latest = value
        if self.state == self.WAITING and not self._triggered:
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
            ts  = datetime.now().isoformat(timespec="milliseconds")
            val = self.latest
            self.captured.append((ts, val))
            time.sleep(interval)

    def _save_csv(self):
        if not self.captured:
            return
        fname = f"bridge_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path  = str(Path(self.save_folder) / fname)
        cal   = self.calibration
        headers = ["timestamp", "ch0_V_per_V"] + (["ch0_lbf"] if cal and cal.is_calibrated else [])

        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for ts, v in self.captured:
                raw_str = f"{v:.8f}" if v is not None else ""
                row = [ts, raw_str]
                if cal and cal.is_calibrated:
                    row.append(f"{cal.to_lbf(v):.6f}" if v is not None else "")
                w.writerow(row)

        self.saved_path = path


# ═════════════════════════════════════════════════════════════════════════════
#  Calibration sampler  (background thread)
# ═════════════════════════════════════════════════════════════════════════════

class CalSampler:
    """Average N live readings from channel 0."""
    IDLE    = "idle"
    RUNNING = "running"
    DONE    = "done"
    ERROR   = "error"

    def __init__(self, engine: RecorderEngine):
        self._engine   = engine
        self.state     = self.IDLE
        self.result:   float | None = None
        self.error_msg = ""

    def start(self, n: int = CAL_SAMPLES):
        self._n    = n
        self.state = self.RUNNING
        self.result = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            accum = 0.0
            count = 0
            for _ in range(self._n):
                v = self._engine.latest
                if v is not None:
                    accum += v
                    count += 1
                time.sleep(0.05)
            self.result = accum / count if count > 0 else None
            self.state  = self.DONE
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
        self.title("PhidgetBridge HMI  —  Channel 0")
        self.geometry("1100x720")
        self.minsize(860, 540)
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

        # Cal sampler pending action: "zero" | "point" | None
        self._cal_pending: str | None = None

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
        s.configure("TLabelframe",       background=BG,  foreground=ACCENT2, relief="flat")
        s.configure("TLabelframe.Label", background=BG,  foreground=ACCENT2, font=("Segoe UI", 9, "bold"))
        s.configure("Files.Treeview",    background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=28, borderwidth=0)
        s.configure("Files.Treeview.Heading", background=BG3, foreground=ACCENT2,
                    relief="flat", padding=(6, 6))
        s.map("Files.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
        s.configure("Data.Treeview",     background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=24, font=("Consolas", 9), borderwidth=0)
        s.configure("Data.Treeview.Heading", background=BG3, foreground=ACCENT2,
                    relief="flat", padding=(4, 5))
        s.map("Data.Treeview",
              background=[("selected", BG3)],
              foreground=[("selected", ACCENT2)])
        s.configure("TScrollbar",        background=BG3, troughcolor=BG, arrowcolor=TEXT_DIM, relief="flat")
        s.configure("TSeparator",        background=BG3)

    # ══════════════════════════════════════════════════════════════════════════
    #  Top bar + layout
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        top = ttk.Frame(self, padding=(12, 8))
        top.pack(fill="x")
        ttk.Label(top, text="📁 Save / Browse Folder:", foreground=TEXT_DIM).pack(side="left")
        ttk.Entry(top, textvariable=self.folder, width=52).pack(side="left", padx=6)
        ttk.Button(top, text="Browse…",   command=self._browse_folder).pack(side="left", padx=2)
        ttk.Button(top, text="⟳ Refresh", command=self.refresh_file_list,
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

    # ── File list ─────────────────────────────────────────────────────────────

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

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right_panel(self, parent):
        # ── Initialise tk variables used by multiple tabs BEFORE building tabs ─
        self._cal_status_var = tk.StringVar(value="")
        self._cal_prog_lbl   = tk.StringVar(value="")

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
        cols.columnconfigure(0, weight=0, minsize=260)
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
        trg = ttk.LabelFrame(cfg, text="Trigger  (Channel 0)", padding=10)
        trg.pack(fill="x", pady=(0, 8))
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

        # State banner + counter
        banner = ttk.Frame(live, padding=(0, 0, 0, 6))
        banner.pack(fill="x")
        self._state_var = tk.StringVar(value="IDLE")
        self._state_lbl = tk.Label(banner, textvariable=self._state_var,
                                    fg=TEXT_DIM, bg=BG, font=("Segoe UI", 13, "bold"))
        self._state_lbl.pack(side="left")
        self._prog_lbl = tk.StringVar(value="")
        tk.Label(banner, textvariable=self._prog_lbl,
                 fg=TEXT_DIM, bg=BG, font=("Consolas", 11)).pack(side="right", padx=12)

        ttk.Separator(live, orient="horizontal").pack(fill="x", pady=(0, 8))

        # Channel 0 gauge
        gauge_frame = ttk.LabelFrame(live, text="Channel 0  —  Live Value  (V/V)", padding=12)
        gauge_frame.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(gauge_frame)
        row.pack(fill="x")
        tk.Label(row, text="CH 0", fg=CH_COL, bg=BG,
                 font=("Consolas", 11, "bold"), width=5).pack(side="left")
        self._gauge_cv = tk.Canvas(row, height=20, bg=BG2, highlightthickness=0)
        self._gauge_cv.pack(side="left", fill="x", expand=True, padx=8)
        self._gauge_var = tk.StringVar(value="---")
        tk.Label(row, textvariable=self._gauge_var, fg=CH_COL, bg=BG,
                 font=("Consolas", 11), width=16, anchor="e").pack(side="right")

        # Calibrated lbf readout (shown only when calibrated)
        self._live_lbf_var = tk.StringVar(value="")
        self._live_lbf_lbl = tk.Label(gauge_frame, textvariable=self._live_lbf_var,
                                       fg=ORANGE, bg=BG, font=("Consolas", 11))
        self._live_lbf_lbl.pack(anchor="e", pady=(4, 0))

        # Capture log
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
        # Header
        hdr = ttk.Frame(parent, padding=(12, 10, 12, 4))
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚖  Channel 0 Calibration  —  V/V  →  lbf",
                 fg=ACCENT2, bg=BG, font=("Segoe UI", 12, "bold")).pack(side="left")
        # Status text (right-aligned in header)
        tk.Label(hdr, textvariable=self._cal_prog_lbl,
                 fg=GREEN, bg=BG, font=("Consolas", 9)).pack(side="right", padx=6)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8)

        # Instructions
        note_frame = ttk.Frame(parent, padding=(14, 8, 14, 4))
        note_frame.pack(fill="x")
        note = (
            "Step 1 — Remove all load, then click  \"⊙ Capture Zero\".\n"
            "Step 2 — Apply a known reference load (lbf), enter the value, "
            "then click  \"⊙ Capture Cal Point\".\n"
            f"Each step averages  {CAL_SAMPLES}  live samples (~2.5 s).  "
            "Click  \"💾 Save\"  when done."
        )
        tk.Label(note_frame, text=note, fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9), justify="left").pack(anchor="w")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8, pady=(4, 0))

        # ── Main calibration area ─────────────────────────────────────────────
        body = ttk.Frame(parent, padding=(16, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        # Live readout card
        live_lf = ttk.LabelFrame(body, text="Live Reading  —  Channel 0", padding=12)
        live_lf.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        live_inner = ttk.Frame(live_lf)
        live_inner.pack()
        tk.Label(live_inner, text="Raw:", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._cal_raw_var = tk.StringVar(value="---")
        tk.Label(live_inner, textvariable=self._cal_raw_var, fg=CH_COL, bg=BG,
                 font=("Consolas", 12, "bold"), width=16).grid(row=0, column=1, sticky="w")
        tk.Label(live_inner, text="V/V", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=2, sticky="w", padx=(2, 24))

        tk.Label(live_inner, text="Calibrated:", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=3, sticky="w", padx=(0, 6))
        self._cal_lbf_live_var = tk.StringVar(value="—")
        tk.Label(live_inner, textvariable=self._cal_lbf_live_var, fg=ORANGE, bg=BG,
                 font=("Consolas", 12, "bold"), width=14).grid(row=0, column=4, sticky="w")
        tk.Label(live_inner, text="lbf", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=5, sticky="w", padx=(2, 0))

        # Step 1 — Zero
        zero_lf = ttk.LabelFrame(body, text="Step 1 — Zero Point  (no load on sensor)", padding=12)
        zero_lf.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        self._zero_result_var = tk.StringVar(value="Not captured")
        tk.Label(zero_lf, textvariable=self._zero_result_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 8))
        ttk.Button(zero_lf, text="⊙  Capture Zero",
                   style="Green.TButton",
                   command=self._cal_capture_zero).pack(fill="x")

        # Step 2 — Cal point
        cal_lf = ttk.LabelFrame(body, text="Step 2 — Cal Point  (known load applied)", padding=12)
        cal_lf.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        load_row = ttk.Frame(cal_lf)
        load_row.pack(fill="x", pady=(0, 8))
        tk.Label(load_row, text="Known load:", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left")
        self._cal_load_var = tk.DoubleVar(value=10.0)
        ttk.Entry(load_row, textvariable=self._cal_load_var, width=10).pack(side="left", padx=6)
        tk.Label(load_row, text="lbf", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left")

        self._cal_result_var = tk.StringVar(value="Not captured")
        tk.Label(cal_lf, textvariable=self._cal_result_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 8))
        ttk.Button(cal_lf, text="⊙  Capture Cal Point",
                   style="Orange.TButton",
                   command=self._cal_capture_point).pack(fill="x")

        # Equation + status
        info_lf = ttk.LabelFrame(body, text="Calibration Status", padding=12)
        info_lf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self._cal_eq_var = tk.StringVar(value="")
        tk.Label(info_lf, textvariable=self._cal_eq_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 4))
        self._cal_badge_var = tk.StringVar(value="⬤  NOT CALIBRATED")
        self._cal_badge_lbl = tk.Label(info_lf, textvariable=self._cal_badge_var,
                                        fg=RED, bg=BG, font=("Segoe UI", 10, "bold"))
        self._cal_badge_lbl.pack(anchor="w")

        # Bottom bar
        bot = ttk.Frame(parent, padding=(12, 6))
        bot.pack(fill="x", side="bottom")
        ttk.Button(bot, text="💾  Save Calibration",
                   style="Accent.TButton", command=self._cal_save).pack(side="left", padx=(0, 8))
        ttk.Button(bot, text="Reset",
                   style="Red.TButton", command=self._cal_reset).pack(side="left")
        tk.Label(bot, textvariable=self._cal_status_var,
                 fg=GREEN, bg=BG, font=("Segoe UI", 9)).pack(side="right", padx=8)

        # Store button refs for enable/disable
        self._btn_zero = self.nametowidget(
            zero_lf.winfo_children()[-1].winfo_pathname(
                zero_lf.winfo_children()[-1].winfo_id()))
        self._btn_cal_pt = self.nametowidget(
            cal_lf.winfo_children()[-1].winfo_pathname(
                cal_lf.winfo_children()[-1].winfo_id()))

        # Refresh display from any loaded calibration
        self._cal_refresh()

    # ── Cal helpers ───────────────────────────────────────────────────────────

    def _cal_refresh(self):
        cal = self._cal
        if cal.zero_raw is not None:
            self._zero_result_var.set(f"avg = {cal.zero_raw:+.6f} V/V")
        else:
            self._zero_result_var.set("Not captured")

        if cal.cal_raw is not None:
            self._cal_result_var.set(
                f"avg = {cal.cal_raw:+.6f} V/V  @  {cal.cal_load_lbf:.4f} lbf")
        else:
            self._cal_result_var.set("Not captured")

        if cal.is_calibrated:
            self._cal_eq_var.set(
                f"lbf = (raw − {cal.zero_offset:+.5f}) × {cal.scale_factor:.4f}"
                + (f"   [saved: {cal.timestamp[11:19]}]" if cal.timestamp else ""))
            self._cal_badge_var.set("⬤  CALIBRATED")
            self._cal_badge_lbl.configure(fg=GREEN)
        else:
            self._cal_eq_var.set("")
            self._cal_badge_var.set("⬤  NOT CALIBRATED")
            self._cal_badge_lbl.configure(fg=RED)

    def _cal_capture_zero(self):
        if not self._sampler_ready():
            return
        self._cal_pending = "zero"
        self._cal_set_buttons("disabled")
        self._cal_prog_lbl.set(f"Sampling zero  ({CAL_SAMPLES} pts) …")
        self._sampler.start()

    def _cal_capture_point(self):
        if not self._sampler_ready():
            return
        if self._cal.zero_raw is None:
            messagebox.showwarning("Zero not set", "Capture the zero point first.")
            return
        self._cal_pending = "point"
        self._cal_set_buttons("disabled")
        self._cal_prog_lbl.set(f"Sampling cal point  ({CAL_SAMPLES} pts) …")
        self._sampler.start()

    def _sampler_ready(self) -> bool:
        if self._sampler.state == CalSampler.RUNNING:
            messagebox.showinfo("Busy", "A calibration sample is already in progress.")
            return False
        if not HAS_PHIDGET:
            messagebox.showerror("No device", "Install Phidget22 first:\n  pip install Phidget22")
            return False
        if self._engine.latest is None:
            messagebox.showwarning("No live data",
                                    "No readings yet — connect the device via the Record tab first.")
            return False
        return True

    def _cal_set_buttons(self, state: str):
        for w in self.winfo_children():
            pass  # buttons stored directly — use widget references
        try:
            self._btn_zero.configure(state=state)
            self._btn_cal_pt.configure(state=state)
        except Exception:
            pass

    def _cal_save(self):
        try:
            self._cal.save(self.folder.get())
            self._cal_status_var.set(
                f"✓ Saved  ({datetime.now().strftime('%H:%M:%S')})")
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))

    def _cal_reset(self):
        if not messagebox.askyesno("Reset", "Clear calibration data for Channel 0?"):
            return
        self._cal.reset()
        self._cal_refresh()
        self._cal_status_var.set("Calibration reset.")

    # ── Cal sampler polling ───────────────────────────────────────────────────

    def _poll_cal_sampler(self):
        s   = self._sampler
        cal = self._cal
        v   = self._engine.latest

        # Update live readouts
        if v is None:
            self._cal_raw_var.set("---")
            self._cal_lbf_live_var.set("—")
        else:
            self._cal_raw_var.set(f"{v:+.7f}")
            self._cal_lbf_live_var.set(f"{cal.to_lbf(v):+.4f}" if cal.is_calibrated else "—")

        if s.state == CalSampler.DONE:
            avg = s.result
            if avg is None:
                messagebox.showerror("No data", "Channel 0 returned no readings — check connection.")
            else:
                try:
                    if self._cal_pending == "zero":
                        cal.set_zero(avg)
                    elif self._cal_pending == "point":
                        cal.set_cal_point(avg, self._cal_load_var.get())
                except ValueError as exc:
                    messagebox.showerror("Calibration error", str(exc))
                self._cal_refresh()

            self._cal_pending = None
            self._cal_prog_lbl.set("Done")
            self._cal_set_buttons("normal")
            s.state = CalSampler.IDLE

        elif s.state == CalSampler.ERROR:
            messagebox.showerror("Sampler error", s.error_msg)
            self._cal_pending = None
            self._cal_prog_lbl.set("")
            self._cal_set_buttons("normal")
            s.state = CalSampler.IDLE

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
            ("header", ACCENT2, True),
            ("label",  TEXT_DIM, False),
            ("value",  GREEN,    False),
            ("raw",    CH_COL,   False),
            ("lbf",    ORANGE,   False),
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
                     values=["V/V  (raw)", "lbf  (if calibrated)"],
                     state="readonly", width=24).pack(side="left", padx=6)
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
        e.trigger_threshold = float(self._r_threshold.get())
        e.trigger_direction = self._r_direction.get()
        e.num_points        = self._r_npoints.get()
        e.calibration       = self._cal

        self._log_clear()
        self._log_append(
            "Connecting to PhidgetBridge  CH0"
            + (f"  (serial {serial})" if serial else "  (auto-detect)") + " …", "info")
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

    def _draw_gauge(self, value: float | None):
        cv = self._gauge_cv
        cv.delete("all")
        W   = cv.winfo_width() or 300
        H   = cv.winfo_height() or 20
        mid = W // 2
        cv.create_rectangle(2, H // 2 - 4, W - 2, H // 2 + 4, fill=BG3, outline="")
        if value is not None:
            clamp = max(-1.0, min(1.0, value))
            if clamp >= 0:
                x1, x2 = mid, mid + int(clamp * (W // 2 - 4))
            else:
                x1, x2 = mid + int(clamp * (W // 2 - 4)), mid
            if x1 != x2:
                cv.create_rectangle(x1, H // 2 - 4, x2, H // 2 + 4, fill=CH_COL, outline="")
        cv.create_line(mid, 2, mid, H - 2, fill=TEXT_DIM, width=1)

    # ══════════════════════════════════════════════════════════════════════════
    #  Recorder polling
    # ══════════════════════════════════════════════════════════════════════════

    _prev_state       = None
    _prev_count       = -1
    _logged_connected = False
    _logged_trigger   = False

    def _poll_recorder(self):
        e     = self._engine
        state = e.state
        v     = e.latest

        # Update gauge
        self._gauge_var.set(f"{v:+.6f} V/V" if v is not None else "---")
        self._draw_gauge(v)
        if v is not None and self._cal.is_calibrated:
            self._live_lbf_var.set(f"{self._cal.to_lbf(v):+.4f}  lbf")
        else:
            self._live_lbf_var.set("")

        # State transitions
        if state != self._prev_state:
            self._prev_state = state

            if state == RecorderEngine.CONNECTING:
                self._set_banner("CONNECTING …", YELLOW)

            elif state == RecorderEngine.WAITING:
                if not self._logged_connected:
                    self._logged_connected = True
                    self._log_append("Channel 0 attached.", "ok")
                    dir_txt = "|value|" if e.trigger_direction == "either" else e.trigger_direction
                    self._log_append(
                        f"Waiting for trigger  ({dir_txt} > {e.trigger_threshold:.4f} V/V) …", "info")
                self._set_banner("WAITING FOR TRIGGER", YELLOW)
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
            self._cal_refresh()
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
            tree.column(col, width=max(120, len(col) * 11), anchor="center", minwidth=80)
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

        t.insert("end", f"  File: ", "label")
        t.insert("end", f"{Path(self.selected_path).name}\n", "value")
        t.insert("end", f"  Rows: ", "label")
        t.insert("end", f"{len(rows)}\n\n", "value")

        def stat_block(col: str, tag: str, unit: str):
            if col not in headers:
                return
            idx = headers.index(col)
            vals = []
            for row in rows:
                try:
                    vals.append(float(row[idx]))
                except (ValueError, IndexError):
                    pass
            if not vals:
                return
            col_w = 16
            t.insert("end", f"  {col}  ({unit})\n", "header")
            t.insert("end", "  " + "─" * 40 + "\n", "label")
            fmt = lambda v: f"{v:+.6f}"
            for metric, fn in [
                ("Min",   min),
                ("Max",   max),
                ("Mean",  lambda xs: sum(xs) / len(xs)),
                ("Range", lambda xs: max(xs) - min(xs)),
                ("Std",   lambda xs: (sum((x - sum(xs)/len(xs))**2 for x in xs)/len(xs))**0.5),
            ]:
                t.insert("end", f"  {metric:<10}", "label")
                t.insert("end", f"{fmt(fn(vals)):>{col_w}}\n", tag)
            t.insert("end", "\n")

        stat_block("ch0_V_per_V", "raw", "V/V")
        stat_block("ch0_lbf",     "lbf", "lbf")

        if headers:
            t.insert("end", "  CAPTURE INFO\n", "header")
            t.insert("end", f"  {'Start':<10}", "label")
            t.insert("end", f"{rows[0][0]}\n",  "value")
            t.insert("end", f"  {'End':<10}",   "label")
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
        col     = "ch0_lbf" if use_lbf else "ch0_V_per_V"
        colour  = ORANGE    if use_lbf else CH_COL
        ylabel  = "lbf"     if use_lbf else "V / V"

        if col not in headers:
            ax.set_title("No data for selected column", color=TEXT_DIM, fontsize=10)
            self._canvas.draw()
            return

        idx  = headers.index(col)
        vals = []
        for row in rows:
            try:    vals.append(float(row[idx]))
            except: vals.append(None)

        x  = list(range(len(vals)))
        cx = [xi for xi, v in zip(x, vals) if v is not None]
        cy = [v  for v in vals if v is not None]
        ax.plot(cx, cy, color=colour, linewidth=1.6, label=col, alpha=0.9)

        ax.set_xlabel("Sample Index", color=TEXT_DIM, fontsize=9)
        ax.set_ylabel(ylabel,         color=TEXT_DIM, fontsize=9)
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
