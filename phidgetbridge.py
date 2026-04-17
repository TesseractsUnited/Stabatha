"""
PhidgetBridge HMI  —  Recorder + Viewer
========================================
A single-file Tkinter application that combines live recording from a
PhidgetBridge 4-Input device with a full CSV file browser and analyser.

Tabs
────
  🔴 Record   — device config, live channel gauges, trigger setup, capture log
  📋 Data Table — browse recorded CSV files; filter / export rows
  📊 Summary  — per-channel min / max / mean / std for the selected file
  📈 Chart    — multi-channel line plot  (requires matplotlib)

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
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path

# ── Optional Phidget import ───────────────────────────────────────────────────
try:
    from Phidget22.Phidget import PhidgetException
    from Phidget22.Devices.VoltageRatioInput import VoltageRatioInput
    HAS_PHIDGET = True
except ImportError:
    HAS_PHIDGET = False

# ── Optional matplotlib import ────────────────────────────────────────────────
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
CH_COLOURS = ["#7c6af7", "#4ade80", "#fbbf24", "#f87171"]

CSV_GLOB   = "bridge_data_*.csv"
NUM_CH     = 4


# ═════════════════════════════════════════════════════════════════════════════
#  Recorder engine  (runs entirely on a background thread)
# ═════════════════════════════════════════════════════════════════════════════

class RecorderEngine:
    """
    Wraps Phidget channel management and the capture state-machine.
    All public attributes are written from the Phidget callback thread;
    the GUI polls them via after().
    """

    # States
    IDLE       = "idle"
    CONNECTING = "connecting"
    WAITING    = "waiting"
    RECORDING  = "recording"
    DONE       = "done"
    ERROR      = "error"

    def __init__(self):
        self.state          = self.IDLE
        self.latest         = [None] * NUM_CH      # live channel values
        self.captured       = []                   # list of (ts, [v0..v3])
        self.capture_target = 100
        self.error_msg      = ""
        self._channels      = []
        self._stop_evt      = threading.Event()
        self._thread        = None

        # config (set before start())
        self.serial_number    = None
        self.data_interval_ms = 50
        self.trigger_channel  = 0
        self.trigger_threshold = 0.01
        self.trigger_direction = "either"   # "rising" | "falling" | "either"
        self.num_points        = 100
        self.save_folder       = "."

        self._triggered = False
        self.saved_path = ""

    # ── Public API ────────────────────────────────────────────────────────────

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

    # ── Background thread ─────────────────────────────────────────────────────

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

    # ── Phidget callbacks ─────────────────────────────────────────────────────

    def _on_attach(self, ch):
        ch.setDataInterval(self.data_interval_ms)

    def _on_detach(self, ch):
        pass

    def _on_error(self, ch, code, desc):
        self.error_msg = f"CH{ch.getChannel()} error [{code}]: {desc}"

    def _on_value_change(self, ch, value):
        idx = ch.getChannel()
        self.latest[idx] = value

        if self.state == self.WAITING and not self._triggered:
            if idx == self.trigger_channel:
                fired = False
                if self.trigger_direction in ("rising",  "either") and value >  self.trigger_threshold:
                    fired = True
                if self.trigger_direction in ("falling", "either") and value < -self.trigger_threshold:
                    fired = True
                if fired:
                    self._triggered = True

    # ── Capture helpers ───────────────────────────────────────────────────────

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
        headers = ["timestamp"] + [f"ch{i}_V_per_V" for i in range(NUM_CH)]
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for ts, vals in self.captured:
                w.writerow([ts] + [f"{v:.8f}" if v is not None else "" for v in vals])
        self.saved_path = path


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
        self.geometry("1180x740")
        self.minsize(900, 560)
        self.configure(bg=BG)

        self.folder          = tk.StringVar(value=str(Path(start_folder).resolve()))
        self.csv_files:      list[dict]  = []
        self.selected_path:  str | None  = None
        self._data_headers:  list[str]   = []
        self._data_rows:     list[list]  = []

        self._engine = RecorderEngine()
        self._engine.save_folder = str(Path(start_folder).resolve())

        self._apply_style()
        self._build_ui()
        self.refresh_file_list()
        self._poll_recorder()

    # ── Style ─────────────────────────────────────────────────────────────────

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",              background=BG,  foreground=TEXT, font=("Segoe UI", 10))
        s.configure("TFrame",         background=BG)
        s.configure("TLabel",         background=BG,  foreground=TEXT)
        s.configure("TButton",        background=BG3, foreground=TEXT, relief="flat", padding=(10, 5))
        s.map("TButton",
              background=[("active", ACCENT),  ("pressed", ACCENT2)],
              foreground=[("active", "#ffffff")])
        s.configure("Accent.TButton", background=ACCENT,  foreground="#ffffff")
        s.map("Accent.TButton",
              background=[("active", ACCENT2), ("pressed", BG3)])
        s.configure("Red.TButton",    background="#7f1d1d", foreground="#fca5a5")
        s.map("Red.TButton",
              background=[("active", RED),     ("pressed", BG3)])
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
        s.configure("Files.Treeview.Heading", background=BG3, foreground=ACCENT2, relief="flat", padding=(6, 6))
        s.map("Files.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
        s.configure("Data.Treeview",     background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=24, font=("Consolas", 9), borderwidth=0)
        s.configure("Data.Treeview.Heading", background=BG3, foreground=ACCENT2, relief="flat", padding=(4, 5))
        s.map("Data.Treeview",
              background=[("selected", BG3)],
              foreground=[("selected", ACCENT2)])
        s.configure("TScrollbar",        background=BG3, troughcolor=BG, arrowcolor=TEXT_DIM, relief="flat")
        s.configure("TProgressbar",      troughcolor=BG3, background=ACCENT, thickness=10)
        s.configure("TCheckbutton",      background=BG,  foreground=TEXT)
        s.map("TCheckbutton",            background=[("active", BG)])
        s.configure("TSeparator",        background=BG3)

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self, padding=(12, 8))
        top.pack(fill="x")

        ttk.Label(top, text="📁 Save / Browse Folder:", foreground=TEXT_DIM).pack(side="left")
        ttk.Entry(top, textvariable=self.folder, width=52).pack(side="left", padx=6)
        ttk.Button(top, text="Browse…", command=self._browse_folder).pack(side="left", padx=2)
        ttk.Button(top, text="⟳ Refresh", command=self.refresh_file_list,
                   style="Accent.TButton").pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status_var, foreground=TEXT_DIM).pack(side="right", padx=8)

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # ── Main paned layout ─────────────────────────────────────────────────
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
        for col, label, w, anchor in [
            ("filename", "File",     160, "w"),
            ("rows",     "Rows",      55, "center"),
            ("size_kb",  "KB",        55, "center"),
            ("modified", "Modified", 145, "center"),
        ]:
            self.file_tree.heading(col, text=label)
            self.file_tree.column(col, width=w, anchor=anchor)

        sb = ttk.Scrollbar(parent, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=sb.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.file_tree.bind("<<TreeviewSelect>>", self._on_file_select)

    # ── Right panel (notebook) ────────────────────────────────────────────────

    def _build_right_panel(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True)

        # Tab order: Record first, then viewer tabs
        rec_tab = ttk.Frame(self.notebook)
        self.notebook.add(rec_tab, text="  🔴 Record  ")
        self._build_record_tab(rec_tab)

        data_tab = ttk.Frame(self.notebook)
        self.notebook.add(data_tab, text="  📋 Data Table  ")
        self._build_data_table(data_tab)

        stats_tab = ttk.Frame(self.notebook)
        self.notebook.add(stats_tab, text="  📊 Summary  ")
        self._build_stats_panel(stats_tab)

        if HAS_MPL:
            chart_tab = ttk.Frame(self.notebook)
            self.notebook.add(chart_tab, text="  📈 Chart  ")
            self._build_chart_panel(chart_tab)

        # Placeholder shown when no file selected in viewer tabs
        self.placeholder = ttk.Label(
            parent,
            text="← Select a file to view its contents",
            foreground=TEXT_DIM, font=("Segoe UI", 12),
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  RECORD TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_record_tab(self, parent):
        # ── Two-column layout: config left, live display right ────────────────
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

        # ── Device ───────────────────────────────────────────────────────────
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

        # ── Trigger ───────────────────────────────────────────────────────────
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

        # ── Capture ───────────────────────────────────────────────────────────
        cap = ttk.LabelFrame(cfg, text="Capture", padding=10)
        cap.pack(fill="x", pady=(0, 8))

        ttk.Label(cap, text="Number of points", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_npoints = tk.IntVar(value=100)
        ttk.Spinbox(cap, from_=10, to=10000, increment=10,
                    textvariable=self._r_npoints, width=8).pack(anchor="w", pady=(2, 0))

        # ── Control buttons ───────────────────────────────────────────────────
        btn_row = ttk.Frame(cfg)
        btn_row.pack(fill="x", pady=(4, 0))

        self._btn_start = ttk.Button(btn_row, text="▶  Start",
                                      style="Accent.TButton", command=self._rec_start)
        self._btn_start.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self._btn_abort = ttk.Button(btn_row, text="■  Abort",
                                      style="Red.TButton", command=self._rec_abort,
                                      state="disabled")
        self._btn_abort.pack(side="left", expand=True, fill="x")

        # Phidget warning
        if not HAS_PHIDGET:
            warn = ttk.Label(cfg,
                text="⚠  Phidget22 not installed.\nRecording unavailable.",
                foreground=YELLOW, font=("Segoe UI", 9), justify="center")
            warn.pack(pady=(10, 0))

    def _build_live_panel(self, parent):
        live = ttk.Frame(parent)
        live.grid(row=0, column=1, sticky="nsew")

        # ── State banner ──────────────────────────────────────────────────────
        banner = ttk.Frame(live, padding=(0, 0, 0, 6))
        banner.pack(fill="x")

        self._state_var  = tk.StringVar(value="IDLE")
        self._state_colour = tk.StringVar(value=TEXT_DIM)
        self._state_lbl  = tk.Label(banner, textvariable=self._state_var,
                                     fg=TEXT_DIM, bg=BG,
                                     font=("Segoe UI", 13, "bold"))
        self._state_lbl.pack(side="left")

        # Progress bar + counter
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

        # ── Channel gauges ────────────────────────────────────────────────────
        gauge_frame = ttk.LabelFrame(live, text="Live Channel Values  (V/V)", padding=10)
        gauge_frame.pack(fill="x", pady=(0, 8))

        self._gauge_vars  = []
        self._gauge_bars  = []
        self._gauge_lbls  = []

        for i in range(NUM_CH):
            row = ttk.Frame(gauge_frame)
            row.pack(fill="x", pady=3)

            # Channel label
            tk.Label(row, text=f"CH {i}", fg=CH_COLOURS[i], bg=BG,
                     font=("Consolas", 10, "bold"), width=5).pack(side="left")

            # Bipolar bar canvas
            cv = tk.Canvas(row, height=16, bg=BG2, highlightthickness=0)
            cv.pack(side="left", fill="x", expand=True, padx=6)
            self._gauge_bars.append(cv)

            # Value label
            var = tk.StringVar(value="---")
            tk.Label(row, textvariable=var, fg=CH_COLOURS[i], bg=BG,
                     font=("Consolas", 10), width=14, anchor="e").pack(side="right")
            self._gauge_vars.append(var)

        # ── Capture log ───────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(live, text="Capture Log", padding=(6, 4))
        log_frame.pack(fill="both", expand=True)

        self._log = tk.Text(log_frame, bg=BG2, fg=TEXT, font=("Consolas", 9),
                            relief="flat", state="disabled", wrap="none",
                            insertbackground=TEXT, height=8)
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=log_sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        self._log.tag_configure("info",    foreground=TEXT_DIM)
        self._log.tag_configure("ok",      foreground=GREEN)
        self._log.tag_configure("trigger", foreground=YELLOW)
        self._log.tag_configure("error",   foreground=RED)
        self._log.tag_configure("done",    foreground=ACCENT2)

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
        ttk.Label(toolbar, textvariable=self.row_count_var, foreground=TEXT_DIM).pack(side="right", padx=8)
        ttk.Button(toolbar, text="Export copy…", command=self._export_filtered).pack(side="right", padx=4)

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

        for tag, colour in [("header", ACCENT2), ("label", TEXT_DIM), ("value", GREEN),
                             ("ch0", CH_COLOURS[0]), ("ch1", CH_COLOURS[1]),
                             ("ch2", CH_COLOURS[2]), ("ch3", CH_COLOURS[3])]:
            kw = {"foreground": colour}
            if tag == "header":
                kw["font"] = ("Consolas", 10, "bold")
            self.stats_text.tag_configure(tag, **kw)

    # ══════════════════════════════════════════════════════════════════════════
    #  CHART TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_chart_panel(self, parent):
        self._fig = Figure(figsize=(6, 4), dpi=100, facecolor=BG)
        self._ax  = self._fig.add_subplot(111, facecolor=BG2)
        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)
        tf = ttk.Frame(parent)
        tf.pack(fill="x")
        NavigationToolbar2Tk(self._canvas, tf)

    # ══════════════════════════════════════════════════════════════════════════
    #  Recorder control
    # ══════════════════════════════════════════════════════════════════════════

    def _rec_start(self):
        if not HAS_PHIDGET:
            messagebox.showerror("Phidget22 not found",
                                  "Install Phidget22 first:\n  pip install Phidget22")
            return

        e = self._engine
        e.save_folder      = self.folder.get()
        serial             = self._r_serial.get().strip()
        e.serial_number    = serial if serial else None
        e.data_interval_ms = self._r_interval.get()
        e.trigger_channel  = self._r_trg_ch.get()
        e.trigger_threshold = float(self._r_threshold.get())
        e.trigger_direction = self._r_direction.get()
        e.num_points        = self._r_npoints.get()

        self._prog_bar.configure(maximum=e.num_points)
        self._log_clear()
        self._log_append(f"Connecting to PhidgetBridge"
                         + (f" (serial {serial})" if serial else " (auto-detect)") + " …", "info")

        self._btn_start.configure(state="disabled")
        self._btn_abort.configure(state="normal")
        e.start()

    def _rec_abort(self):
        self._engine.abort()
        self._log_append("Abort requested …", "error")
        self._btn_abort.configure(state="disabled")

    # ── Log helpers ───────────────────────────────────────────────────────────

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

    # ── Gauge drawing ─────────────────────────────────────────────────────────

    def _draw_gauge(self, idx: int, value: float | None):
        cv = self._gauge_bars[idx]
        cv.delete("all")
        W = cv.winfo_width() or 200
        H = cv.winfo_height() or 16
        mid = W // 2

        # Track
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

        # Centre tick
        cv.create_line(mid, 2, mid, H - 2, fill=TEXT_DIM, width=1)

    # ══════════════════════════════════════════════════════════════════════════
    #  Polling loop  (runs on main thread via after())
    # ══════════════════════════════════════════════════════════════════════════

    _prev_state      = None
    _prev_count      = -1
    _logged_connected = False
    _logged_trigger   = False

    def _poll_recorder(self):
        e     = self._engine
        state = e.state

        # ── Update gauges ─────────────────────────────────────────────────────
        for i in range(NUM_CH):
            v = e.latest[i]
            self._gauge_vars[i].set(f"{v:+.6f}" if v is not None else "---")
            self._draw_gauge(i, v)

        # ── State machine transitions ─────────────────────────────────────────
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

        # ── Progress bar while recording ──────────────────────────────────────
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
        folder = filedialog.askdirectory(initialdir=self.folder.get(),
                                          title="Select folder")
        if folder:
            self.folder.set(folder)
            self._engine.save_folder = folder
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

    # ── Data table population ─────────────────────────────────────────────────

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

    # ── Stats population ──────────────────────────────────────────────────────

    def _populate_stats(self, headers, rows):
        t = self.stats_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not rows:
            t.insert("end", "No data.\n")
            t.configure(state="disabled")
            return

        ch_cols = [h for h in headers if h.startswith("ch")]
        t.insert("end", f"  File: ", "label");  t.insert("end", f"{Path(self.selected_path).name}\n", "value")
        t.insert("end", f"  Rows: ", "label");  t.insert("end", f"{len(rows)}\n\n", "value")
        t.insert("end", "  CHANNEL STATISTICS\n", "header")
        t.insert("end", "  " + "─" * 58 + "\n", "label")

        col_w = 14
        t.insert("end", "  " + f"{'Metric':<12}" + "".join(f"{c:>{col_w}}" for c in ch_cols) + "\n", "header")
        t.insert("end", "  " + "─" * 58 + "\n", "label")

        data: dict[str, list[float]] = {c: [] for c in ch_cols}
        for row in rows:
            for c in ch_cols:
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
            ("Std",   lambda xs: (sum((x - sum(xs)/len(xs))**2 for x in xs) / len(xs)) ** 0.5),
        ]:
            t.insert("end", f"  {metric:<12}", "label")
            for i, c in enumerate(ch_cols):
                vals = data[c]
                tag  = f"ch{i}" if i < 4 else "value"
                t.insert("end", f"{(fmt(fn(vals)) if vals else 'N/A'):>{col_w}}", tag)
            t.insert("end", "\n")

        t.insert("end", "  " + "─" * 58 + "\n\n", "label")
        if headers:
            t.insert("end", "  CAPTURE INFO\n", "header")
            t.insert("end", f"  {'Start':<12}", "label"); t.insert("end", f"{rows[0][0]}\n",  "value")
            t.insert("end", f"  {'End':<12}",   "label"); t.insert("end", f"{rows[-1][0]}\n", "value")
        t.configure(state="disabled")

    # ── Chart population ──────────────────────────────────────────────────────

    def _populate_chart(self, headers, rows):
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG2)
        self._fig.patch.set_facecolor(BG)

        ch_cols = [h for h in headers if h.startswith("ch")]
        x = list(range(len(rows)))
        for i, col in enumerate(ch_cols):
            idx  = headers.index(col)
            vals = []
            for row in rows:
                try:    vals.append(float(row[idx]))
                except: vals.append(None)
            cx = [xi for xi, v in zip(x, vals) if v is not None]
            cy = [v  for v in vals if v is not None]
            ax.plot(cx, cy, color=CH_COLOURS[i % 4], linewidth=1.4, label=col, alpha=0.9)

        ax.set_xlabel("Sample Index", color=TEXT_DIM, fontsize=9)
        ax.set_ylabel("V / V",        color=TEXT_DIM, fontsize=9)
        ax.set_title(Path(self.selected_path).name, color=TEXT, fontsize=10)
        ax.tick_params(colors=TEXT_DIM)
        for spine in ax.spines.values():
            spine.set_edgecolor(BG3)
        ax.legend(facecolor=BG3, edgecolor=BG3, labelcolor=TEXT, fontsize=9)
        ax.grid(True, color=BG3, linewidth=0.6, linestyle="--")
        self._canvas.draw()

    # ── Cleanup on close ──────────────────────────────────────────────────────

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
