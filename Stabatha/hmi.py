"""Tkinter HMI: tabs, dialogs, charts, and data views."""

import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from pathlib import Path

from constants import (
    ACCENT, BG, BG2, CAL_SAMPLES, PHIDGET_BRIDGE_MAX_DATA_RATE_HZ, TEXT, GREEN, ORANGE, RED,
    YELLOW, ensure_data_dir,
)
from calibration import CalibrationStore
from file_access import (
    find_strike_files,
    load_strike,
    strike_to_tabular,
    update_strike_feedback,
    update_strike_metadata,
)
from strike_data import StrikeData
from sensor_input import CalSampler, HAS_PHIDGET, RecorderEngine
from osk_linux import bind_osk_tree

try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

KINGDOMS = (
    "NA", "None", "Æthelmearc", "An Tir", "Ansteorra", "Artemisia", "Atenveldt",
    "Atlantia", "Avacal", "Caid", "Calontir", "Drachenwald", "Ealdormere",
    "East Kingdom", "Gleann Abhann", "Lochac", "Meridies", "Middle Kingdom",
    "Northshield", "Outlands", "Trimaris", "West Kingdom",
)


class StrikeFeedbackDialog(tk.Toplevel):
    """Non-modal post-strike calibration feedback (slider 0-5)."""

    def __init__(self, parent, strike_path: str, on_slider_moved=None, on_written=None,
                 on_close=None):
        super().__init__(parent)
        self.title("Strike Feedback")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)

        self._strike_path = strike_path
        self._on_slider_moved = on_slider_moved
        self._on_written = on_written
        self._on_close = on_close
        self._closed = False
        self.slider_moved = False
        self.protocol("WM_DELETE_WINDOW", self._handle_close)

        tk.Label(self, text="How was your calibration?", fg=TEXT, bg=BG,
                 font=("Segoe UI", 11)).pack(pady=(16, 8), padx=16)

        row = ttk.Frame(self, padding=(16, 4))
        row.pack(fill="x")
        self._value_var = tk.DoubleVar(value=0.0)
        scale = tk.Scale(
            row, from_=0, to=5, orient="horizontal",
            variable=self._value_var, resolution=0.1,
            bg=BG, fg=TEXT, troughcolor=BG2, highlightthickness=0,
            length=220, command=self._on_scale_change,
        )
        scale.pack(side="left")
        self._display_var = tk.StringVar(value="0.0")
        tk.Label(row, textvariable=self._display_var, fg=GREEN, bg=BG,
                 font=("Consolas", 12, "bold"), width=4).pack(side="left", padx=(8, 0))

        btn_row = ttk.Frame(self, padding=(16, 8, 16, 14))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Done", command=self._handle_close).pack(side="right")

        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px - w // 2}+{py - h // 2}")

    def _on_scale_change(self, _val):
        if not self.slider_moved:
            self.slider_moved = True
            if self._on_slider_moved:
                self._on_slider_moved()
        v = round(float(self._value_var.get()), 1)
        self._display_var.set(f"{v:.1f}")
        try:
            update_strike_feedback(self._strike_path, v)
            if self._on_written:
                self._on_written(self._strike_path)
        except Exception:
            pass

    def get_value(self) -> float:
        return round(float(self._value_var.get()), 1)

    def _handle_close(self):
        if not self._closed:
            self._closed = True
            if self._on_close:
                self._on_close()
        self.destroy()


class StrikeEditDialog(tk.Toplevel):
    """Edit metadata fields on an existing strike JSON file."""

    def __init__(self, parent, strike: StrikeData, strike_path: str):
        super().__init__(parent)
        self.title("Edit Strike Metadata")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self._strike = strike
        self._path = strike_path
        self.saved = False

        form = ttk.Frame(self, padding=16)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self._v_event = tk.StringVar(value=strike.event)
        self._v_name = tk.StringVar(value=strike.name)
        self._v_weapon = tk.StringVar(value=strike.weapon_type)
        self._v_kingdom = tk.StringVar(value=strike.kingdom or "NA")
        self._v_rank = tk.StringVar(value=strike.rank or "NA")

        for r, label, var in [
            (0, "Event", self._v_event),
            (1, "Name", self._v_name),
            (2, "Weapon Type", self._v_weapon),
        ]:
            tk.Label(form, text=label, fg=TEXT, bg=BG, width=14, anchor="e").grid(
                row=r, column=0, sticky="e", padx=(0, 10), pady=5)
            ttk.Entry(form, textvariable=var, width=28).grid(row=r, column=1, sticky="ew", pady=5)

        tk.Label(form, text="Kingdom", fg=TEXT, bg=BG, width=14, anchor="e").grid(
            row=3, column=0, sticky="e", padx=(0, 10), pady=5)
        ttk.Combobox(form, textvariable=self._v_kingdom, values=KINGDOMS,
                     state="readonly", width=26).grid(row=3, column=1, sticky="ew", pady=5)

        tk.Label(form, text="Rank", fg=TEXT, bg=BG, width=14, anchor="e").grid(
            row=4, column=0, sticky="e", padx=(0, 10), pady=5)
        rank_row = ttk.Frame(form)
        rank_row.grid(row=4, column=1, sticky="w", pady=5)
        for value in ("NA", "None", "Blue", "Yellow", "White"):
            ttk.Radiobutton(rank_row, text=value, variable=self._v_rank,
                            value=value).pack(side="left", padx=(0, 8))

        tk.Label(form, text="Notes", fg=TEXT, bg=BG, anchor="e").grid(
            row=5, column=0, sticky="ne", padx=(0, 10), pady=5)
        self._notes_box = tk.Text(form, bg=BG2, fg=TEXT, font=("Segoe UI", 9),
                                  relief="flat", width=30, height=3)
        self._notes_box.insert("1.0", strike.notes)
        self._notes_box.grid(row=5, column=1, sticky="ew", pady=5)

        tk.Label(form, text="Cal Feedback", fg=TEXT, bg=BG, anchor="e").grid(
            row=6, column=0, sticky="e", padx=(0, 10), pady=5)
        feedback_row = ttk.Frame(form)
        feedback_row.grid(row=6, column=1, sticky="ew", pady=5)

        self._feedback_unset = tk.BooleanVar(
            value=strike.user_calibration_feedback is None)
        self._feedback_var = tk.DoubleVar(
            value=strike.user_calibration_feedback if strike.user_calibration_feedback is not None else 0.0)
        self._feedback_display = tk.StringVar(
            value=f"{strike.user_calibration_feedback:.1f}"
            if strike.user_calibration_feedback is not None else "--")

        self._feedback_scale = tk.Scale(
            feedback_row, from_=0, to=5, orient="horizontal",
            variable=self._feedback_var, resolution=0.1,
            bg=BG, fg=TEXT, troughcolor=BG2, highlightthickness=0,
            length=180, command=self._on_feedback_scale,
        )
        self._feedback_scale.pack(side="left")
        tk.Label(feedback_row, textvariable=self._feedback_display, fg=GREEN, bg=BG,
                 font=("Consolas", 12, "bold"), width=4).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            feedback_row, text="Not set",
            variable=self._feedback_unset,
            command=self._on_feedback_unset_toggle,
        ).pack(side="left", padx=(12, 0))
        self._on_feedback_unset_toggle()

        btn_row = ttk.Frame(self, padding=(16, 4, 16, 14))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Save", style="Accent.TButton",
                   command=self._on_save).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="left")

        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px - w // 2}+{py - h // 2}")
        bind_osk_tree(self)

    def _on_feedback_scale(self, _val):
        self._feedback_display.set(f"{round(float(self._feedback_var.get()), 1):.1f}")

    def _on_feedback_unset_toggle(self):
        unset = self._feedback_unset.get()
        self._feedback_scale.configure(state="disabled" if unset else "normal")
        if unset:
            self._feedback_display.set("--")
        else:
            self._on_feedback_scale(self._feedback_var.get())

    def _on_save(self):
        self._strike.event = self._v_event.get().strip()
        self._strike.name = self._v_name.get().strip()
        self._strike.weapon_type = self._v_weapon.get().strip()
        self._strike.kingdom = self._v_kingdom.get().strip()
        self._strike.rank = self._v_rank.get().strip()
        self._strike.notes = self._notes_box.get("1.0", "end-1c").strip()
        if self._feedback_unset.get():
            self._strike.user_calibration_feedback = None
        else:
            self._strike.user_calibration_feedback = round(float(self._feedback_var.get()), 1)
        try:
            update_strike_metadata(self._path, self._strike)
            self.saved = True
        except Exception as exc:
            messagebox.showerror("Save error", str(exc), parent=self)
            return
        self.destroy()


class BridgeHMI(tk.Tk):

    def __init__(self, start_folder: str = "."):
        super().__init__()
        self.title("Stabatha")
        self.geometry("1100x720")
        self.minsize(860, 540)
        self.configure(bg=BG)

        self.folder         = tk.StringVar(value=str(Path(start_folder).resolve()))
        self.strike_files:  list[dict] = []
        self.selected_path: str | None = None
        self._selected_strike: StrikeData | None = None
        self._data_headers: list[str]  = []
        self._data_rows:    list[list] = []

        self._strike_meta = {
            "event": "", "name": "", "weapon_type": "",
            "kingdom": "NA", "rank": "NA", "notes": "",
        }
        self._pending_feedback = None
        self._data_tab_index: int | None = None

        self._cal     = CalibrationStore()
        self._engine  = RecorderEngine()
        self._sampler = CalSampler(self._engine)

        self._engine.save_folder = str(ensure_data_dir())
        self._engine.calibration = self._cal
        self._engine.metadata_provider = self._get_strike_metadata
        self._cal.load()   # loads from ./data/calibration.json

        # Cal sampler pending action: "zero" | "point" | None
        self._cal_pending: str | None = None

        # Tracking vars for poll loop
        self._prev_state       = None
        self._prev_count       = -1
        self._prev_capture_idx = -1
        self._logged_connected = False
        self._logged_trigger   = False

        self._apply_style()
        self._build_ui()
        bind_osk_tree(self)
        self.refresh_file_list()
        self._poll_recorder()
        self._poll_cal_sampler()

        # Auto-connect on startup if Phidget library is available
        if HAS_PHIDGET:
            self.after(500, self._auto_connect)

    # ══════════════════════════════════════════════════════════════════════════
    #  Styles
    # ══════════════════════════════════════════════════════════════════════════

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",              background=BG,  foreground=TEXT, font=("Segoe UI", 10))
        s.configure("TFrame",         background=BG)
        s.configure("TLabel",         background=BG,  foreground=TEXT)
        s.configure("TButton",        background=BG2, foreground=TEXT, relief="flat", padding=(10, 5))
        s.map("TButton",
              background=[("active", ACCENT),   ("pressed", TEXT)],
              foreground=[("active", "#ffffff")])
        s.configure("Accent.TButton", background=ACCENT,   foreground="#ffffff")
        s.map("Accent.TButton",
              background=[("active", TEXT),  ("pressed", BG2)])
        s.configure("Green.TButton",  background="#14532d", foreground="#bbf7d0")
        s.map("Green.TButton",
              background=[("active", GREEN),    ("pressed", BG2)],
              foreground=[("active", "#000000")])
        s.configure("Orange.TButton", background="#7c2d12", foreground="#fed7aa")
        s.map("Orange.TButton",
              background=[("active", ORANGE),   ("pressed", BG2)],
              foreground=[("active", "#000000")])
        s.configure("Red.TButton",    background="#7f1d1d", foreground="#fca5a5")
        s.map("Red.TButton",
              background=[("active", RED),      ("pressed", BG2)])
        s.configure("TEntry",         fieldbackground=BG2, foreground=TEXT, insertcolor=TEXT, relief="flat")
        s.configure("TCombobox",      fieldbackground=BG2, foreground=TEXT, selectbackground=ACCENT)
        s.map("TCombobox",            fieldbackground=[("readonly", BG2)])
        s.configure("TSpinbox",       fieldbackground=BG2, foreground=TEXT, insertcolor=TEXT)
        s.configure("TNotebook",      background=BG,  tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab",  background=BG2, foreground=TEXT, padding=[14, 6])
        s.map("TNotebook.Tab",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
        s.configure("TLabelframe",       background=BG,  foreground=TEXT, relief="flat")
        s.configure("TLabelframe.Label", background=BG,  foreground=TEXT, font=("Segoe UI", 9, "bold"))
        s.configure("Files.Treeview",    background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=28, borderwidth=0)
        s.configure("Files.Treeview.Heading", background=BG2, foreground=TEXT,
                    relief="flat", padding=(6, 6))
        s.map("Files.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])
        s.configure("Data.Treeview",     background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=24, font=("Consolas", 9), borderwidth=0)
        s.configure("Data.Treeview.Heading", background=BG2, foreground=TEXT,
                    relief="flat", padding=(4, 5))
        s.map("Data.Treeview",
              background=[("selected", BG2)],
              foreground=[("selected", TEXT)])
        s.configure("TScrollbar",        background=BG2, troughcolor=BG, arrowcolor=TEXT, relief="flat",
                    width=28, arrowsize=28)
        s.configure("Vertical.TScrollbar",   width=28, arrowsize=28)
        s.configure("Horizontal.TScrollbar", width=28, arrowsize=28)
        s.configure("TSeparator",        background=BG2)

    # ══════════════════════════════════════════════════════════════════════════
    #  Top bar + layout
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── Full-width notebook (no pane split) ───────────────────────────────
        self._build_right_panel(self)

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right_panel(self, parent):
        # Initialise tk variables used across tabs BEFORE building any tab
        self._cal_status_var = tk.StringVar(value="")
        self._cal_prog_lbl   = tk.StringVar(value="")

        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        for text, builder in [
            ("  Setup  ",       self._build_record_tab),
            ("  Calibrate  ",   self._build_cal_tab),
            ("  Stab Info  ",   self._build_stab_info_tab),
            ("  Data  ",        self._build_data_tab),
        ]:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=text)
            builder(tab)
            if "Data" in text:
                self._data_tab_index = self.notebook.index("end") - 1

        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

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
        #ttk.Label(dev, text="Serial number  (blank = auto)", foreground=TEXT,
                  #font=("Segoe UI", 8)).pack(anchor="w")
        #self._r_serial = tk.StringVar()
        #ttk.Entry(dev, textvariable=self._r_serial, width=18).pack(anchor="w", pady=(2, 6))
        ttk.Label(dev, text="Data rate (Hz)", foreground=TEXT,
                  font=("Segoe UI", 8)).pack(anchor="w")
        self._r_data_rate = tk.IntVar(value=1200)
        ttk.Spinbox(
            dev,
            from_=1,
            to=PHIDGET_BRIDGE_MAX_DATA_RATE_HZ,
            increment=1,
            textvariable=self._r_data_rate,
            width=8,
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            dev,
            text=f"{PHIDGET_BRIDGE_MAX_DATA_RATE_HZ} Hz max on PhidgetBridge 1046",
            foreground=TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        # Trigger
        trg = ttk.LabelFrame(cfg, text="Trigger  (Channel 0)", padding=10)
        trg.pack(fill="x", pady=(0, 8))

        self._r_threshold = tk.DoubleVar(value=1.0)
        thresh_row = ttk.Frame(trg)
        thresh_row.pack(fill="x", pady=(0, 6))
        ttk.Label(thresh_row, text="Threshold:", foreground=TEXT,
                  font=("Segoe UI", 8)).pack(side="left")
        ttk.Entry(thresh_row, textvariable=self._r_threshold, width=12).pack(side="left", padx=6)
        tk.Label(thresh_row, text="lbf", fg=TEXT, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")

        # Capture
        cap = ttk.LabelFrame(cfg, text="Capture  (post-trigger only)", padding=10)
        cap.pack(fill="x", pady=(0, 8))
        ttk.Label(cap, text="Max recording duration (s)", foreground=TEXT,
                  font=("Segoe UI", 8)).pack(anchor="w")
        self._r_max_record_s = tk.DoubleVar(value=3.0)
        ttk.Spinbox(cap, from_=0.5, to=30.0, increment=0.5,
                    textvariable=self._r_max_record_s, width=8).pack(anchor="w", pady=(2, 0))

    def _build_live_panel(self, parent):
        live = ttk.Frame(parent)
        live.grid(row=0, column=1, sticky="nsew")

        # State banner + counter
        banner = ttk.Frame(live, padding=(0, 0, 0, 6))
        banner.pack(fill="x")
        self._state_var = tk.StringVar(value="IDLE")
        self._state_lbl = tk.Label(banner, textvariable=self._state_var,
                                    fg=TEXT, bg=BG, font=("Segoe UI", 13, "bold"))
        self._state_lbl.pack(side="left")
        self._prog_lbl = tk.StringVar(value="")
        tk.Label(banner, textvariable=self._prog_lbl,
                 fg=TEXT, bg=BG, font=("Consolas", 11)).pack(side="right", padx=12)

        ttk.Separator(live, orient="horizontal").pack(fill="x", pady=(0, 8))

        # Channel 0 gauge
        gauge_frame = ttk.LabelFrame(live, text="Channel 0  -  Live Value  (V/V)", padding=6)
        gauge_frame.pack(fill="x", pady=(0, 4))

        row = ttk.Frame(gauge_frame)
        row.pack(fill="x")
        tk.Label(row, text="CH 0", fg=TEXT, bg=BG,
                 font=("Consolas", 11, "bold"), width=5).pack(side="left")

        # Calibrated lbf readout (shown only when calibrated) -- packed
        # first so it reserves its space on the right before the raw
        # V/V readout expands to fill what's left.
        self._live_lbf_var = tk.StringVar(value="")
        self._live_lbf_lbl = tk.Label(row, textvariable=self._live_lbf_var,
                                       fg=ORANGE, bg=BG, font=("Consolas", 11))
        self._live_lbf_lbl.pack(side="right")

        self._gauge_var = tk.StringVar(value="---")
        tk.Label(row, textvariable=self._gauge_var, fg=TEXT, bg=BG,
                 font=("Consolas", 11), anchor="e").pack(side="left", fill="x", expand=True, padx=8)

        # Connection buttons
        conn_row = ttk.Frame(live)
        conn_row.pack(fill="x", pady=(0, 2))
        self._btn_connect = ttk.Button(conn_row, text="Connect",
                                        style="Green.TButton", command=self._rec_connect)
        self._btn_connect.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn_disconnect = ttk.Button(conn_row, text="Disconnect",
                                           style="Red.TButton", command=self._rec_disconnect,
                                           state="disabled")
        self._btn_disconnect.pack(side="left", expand=True, fill="x")

        # Arm / Disarm buttons
        arm_row = ttk.Frame(live)
        arm_row.pack(fill="x", pady=(0, 4))
        self._btn_arm = ttk.Button(arm_row, text="Arm Trigger",
                                    style="Accent.TButton", command=self._rec_arm,
                                    state="disabled")
        self._btn_arm.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn_disarm = ttk.Button(arm_row, text="Disarm",
                                       command=self._rec_disarm, state="disabled")
        self._btn_disarm.pack(side="left", expand=True, fill="x")

        if not HAS_PHIDGET:
            ttk.Label(live, text="Phidget22 not installed.\nRecording unavailable.",
                      foreground=YELLOW, font=("Segoe UI", 9), justify="center").pack(pady=(10, 0))

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
        for tag, colour in [("info", TEXT), ("ok", GREEN), ("trigger", YELLOW),
                             ("error", RED), ("done", TEXT)]:
            self._log.tag_configure(tag, foreground=colour)

    # ══════════════════════════════════════════════════════════════════════════
    #  CALIBRATION TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_cal_tab(self, parent):
        # Header
        hdr = ttk.Frame(parent, padding=(12, 6, 12, 2))
        hdr.pack(fill="x")
        tk.Label(hdr, text="Channel 0 Calibration  -  V/V -> lbf",
                 fg=TEXT, bg=BG, font=("Segoe UI", 12, "bold")).pack(side="left")
        # Status text (right-aligned in header)
        tk.Label(hdr, textvariable=self._cal_prog_lbl,
                 fg=GREEN, bg=BG, font=("Consolas", 9)).pack(side="right", padx=6)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8)

        # Instructions
        note_frame = ttk.Frame(parent, padding=(14, 4, 14, 2))
        note_frame.pack(fill="x")
        note = (
            "Step 1 - Remove all load, then click  \"Capture Zero\".\n"
            "Step 2 - Apply a known reference load (lbf), enter the value, "
            "then click  \"Capture Cal Point\".\n"
            f"Each step averages  {CAL_SAMPLES}  live samples (~2.5 s).  "
            "Click  \"Save\"  when done."
        )
        tk.Label(note_frame, text=note, fg=TEXT, bg=BG,
                 font=("Segoe UI", 9), justify="left").pack(anchor="w")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8, pady=(4, 0))

        # ── Main calibration area ─────────────────────────────────────────────
        body = ttk.Frame(parent, padding=(16, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        # Live readout card
        live_lf = ttk.LabelFrame(body, text="Live Reading  -  Channel 0", padding=6)
        live_lf.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        live_inner = ttk.Frame(live_lf)
        live_inner.pack()
        tk.Label(live_inner, text="Raw:", fg=TEXT, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._cal_raw_var = tk.StringVar(value="---")
        tk.Label(live_inner, textvariable=self._cal_raw_var, fg=TEXT, bg=BG,
                 font=("Consolas", 12, "bold"), width=16).grid(row=0, column=1, sticky="w")
        tk.Label(live_inner, text="V/V", fg=TEXT, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=2, sticky="w", padx=(2, 24))

        tk.Label(live_inner, text="Calibrated:", fg=TEXT, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=3, sticky="w", padx=(0, 6))
        self._cal_lbf_live_var = tk.StringVar(value="--")
        tk.Label(live_inner, textvariable=self._cal_lbf_live_var, fg=ORANGE, bg=BG,
                 font=("Consolas", 12, "bold"), width=14).grid(row=0, column=4, sticky="w")
        tk.Label(live_inner, text="lbf", fg=TEXT, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=5, sticky="w", padx=(2, 0))

        # Step 1 - Zero
        zero_lf = ttk.LabelFrame(body, text="Step 1 - Zero Point  (no load on sensor)", padding=8)
        zero_lf.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        zero_row = ttk.Frame(zero_lf)
        zero_row.pack(fill="x")
        self._btn_zero = ttk.Button(zero_row, text="Capture Zero",
                                     style="Green.TButton",
                                     command=self._cal_capture_zero)
        self._btn_zero.pack(side="left")
        self._zero_result_var = tk.StringVar(value="Not captured")
        tk.Label(zero_row, textvariable=self._zero_result_var, fg=TEXT, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Step 2 - Cal point
        cal_lf = ttk.LabelFrame(body, text="Step 2 - Cal Point  (known load applied)", padding=8)
        cal_lf.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        load_row = ttk.Frame(cal_lf)
        load_row.pack(fill="x", pady=(0, 6))
        tk.Label(load_row, text="Known load:", fg=TEXT, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left")
        self._cal_load_var = tk.DoubleVar(value=8.465)
        ttk.Entry(load_row, textvariable=self._cal_load_var, width=10).pack(side="left", padx=6)
        tk.Label(load_row, text="lbf", fg=TEXT, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left")

        cal_row = ttk.Frame(cal_lf)
        cal_row.pack(fill="x")
        self._btn_cal_pt = ttk.Button(cal_row, text="Capture Cal Point",
                                       style="Orange.TButton",
                                       command=self._cal_capture_point)
        self._btn_cal_pt.pack(side="left")
        self._cal_result_var = tk.StringVar(value="Not captured")
        tk.Label(cal_row, textvariable=self._cal_result_var, fg=TEXT, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Equation + status
        info_lf = ttk.LabelFrame(body, text="Calibration Status", padding=(8, 4))
        info_lf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        self._cal_eq_var = tk.StringVar(value="")
        self._cal_eq_lbl = tk.Label(info_lf, textvariable=self._cal_eq_var, fg=TEXT, bg=BG,
                                     font=("Consolas", 9), anchor="w")
        self._cal_badge_var = tk.StringVar(value="NOT CALIBRATED")
        self._cal_badge_lbl = tk.Label(info_lf, textvariable=self._cal_badge_var,
                                        fg=RED, bg=BG, font=("Segoe UI", 10, "bold"))
        self._cal_badge_lbl.pack(anchor="w")

        # Bottom bar
        bot = ttk.Frame(parent, padding=(12, 4))
        bot.pack(fill="x", side="bottom")
        ttk.Button(bot, text="Save Calibration",
                   style="Accent.TButton", command=self._cal_save).pack(side="left", padx=(0, 8))
        ttk.Button(bot, text="Load Calibration",
                   command=self._cal_load_from_file).pack(side="left", padx=(0, 8))
        ttk.Button(bot, text="Reset",
                   style="Red.TButton", command=self._cal_reset).pack(side="left")
        tk.Label(bot, textvariable=self._cal_status_var,
                 fg=GREEN, bg=BG, font=("Segoe UI", 9)).pack(side="right", padx=8)

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
                f"lbf = (raw - {cal.zero_offset:+.5f}) * {cal.scale_factor:.4f}"
                + (f"   [saved: {cal.timestamp[11:19]}]" if cal.timestamp else ""))
            self._cal_eq_lbl.pack(fill="x", pady=(0, 4), before=self._cal_badge_lbl)
            self._cal_badge_var.set("CALIBRATED")
            self._cal_badge_lbl.configure(fg=GREEN)
        else:
            self._cal_eq_var.set("")
            self._cal_eq_lbl.pack_forget()
            self._cal_badge_var.set("NOT CALIBRATED")
            self._cal_badge_lbl.configure(fg=RED)

    def _cal_capture_zero(self):
        if not self._sampler_ready():
            return
        self._cal_pending = "zero"
        self._cal_set_buttons("disabled")
        self._cal_prog_lbl.set(f"Sampling zero  ({CAL_SAMPLES} pts) ...")
        self._sampler.start()

    def _cal_capture_point(self):
        if not self._sampler_ready():
            return
        if self._cal.zero_raw is None:
            messagebox.showwarning("Zero not set", "Capture the zero point first.")
            return
        self._cal_pending = "point"
        self._cal_set_buttons("disabled")
        self._cal_prog_lbl.set(f"Sampling cal point  ({CAL_SAMPLES} pts) ...")
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
                                    "No readings yet - connect the device via the Setup tab first.")
            return False
        return True

    def _cal_set_buttons(self, state: str):
        try:
            self._btn_zero.configure(state=state)
            self._btn_cal_pt.configure(state=state)
        except Exception:
            pass

    def _cal_save(self):
        try:
            saved_path = self._cal.save()
            self._cal_status_var.set(
                f"Saved  {Path(saved_path).name}  ({datetime.now().strftime('%H:%M:%S')})")
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))

    def _cal_load_from_file(self):
        path = filedialog.askopenfilename(
            title="Load Calibration",
            initialdir=str(ensure_data_dir()),
            filetypes=[("Calibration JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            ok = self._cal.load_from_path(path)
        except Exception as exc:
            messagebox.showerror("Load error", str(exc))
            return
        if not ok:
            messagebox.showerror("Load error", f"Could not read calibration from:\n{path}")
            return
        self._cal_refresh()
        self._sync_trigger_calibration_gate()
        self._cal_status_var.set(
            f"Loaded  {Path(path).name}  ({datetime.now().strftime('%H:%M:%S')})")

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
            self._cal_lbf_live_var.set("--")
        else:
            self._cal_raw_var.set(f"{v:+.7f}")
            self._cal_lbf_live_var.set(f"{cal.to_lbf(v):+.4f}" if cal.is_calibrated else "--")

        if s.state == CalSampler.DONE:
            avg = s.result
            if avg is None:
                messagebox.showerror("No data", "Channel 0 returned no readings - check connection.")
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
    #  STAB INFO TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_stab_info_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        info = ttk.LabelFrame(
            wrap, text="Strike Info  (snapshotted at trigger)", padding=12)
        info.pack(fill="x", anchor="n")
        info.columnconfigure(1, weight=1)

        self._r_event = tk.StringVar()
        self._r_name = tk.StringVar()
        self._r_weapon = tk.StringVar()
        self._r_kingdom = tk.StringVar(value="NA")
        self._r_rank = tk.StringVar(value="NA")
        for var in (self._r_event, self._r_name, self._r_weapon, self._r_kingdom):
            var.trace_add("write", self._sync_strike_meta)
        self._r_rank.trace_add("write", self._sync_strike_meta)

        row = 0
        for label, var in [
            ("Event *", self._r_event),
            ("Name *", self._r_name),
            ("Weapon Type *", self._r_weapon),
        ]:
            ttk.Label(info, text=label, foreground=TEXT, font=("Segoe UI", 9)).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=6)
            ttk.Entry(info, textvariable=var, width=30).grid(
                row=row, column=1, sticky="ew", pady=6)
            row += 1

        ttk.Label(info, text="Kingdom", foreground=TEXT, font=("Segoe UI", 9)).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Combobox(info, textvariable=self._r_kingdom, values=KINGDOMS,
                     state="readonly", width=28).grid(row=row, column=1, sticky="ew", pady=6)
        row += 1

        ttk.Label(info, text="Rank", foreground=TEXT, font=("Segoe UI", 9)).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=6)
        rank_row = ttk.Frame(info)
        rank_row.grid(row=row, column=1, sticky="w", pady=6)
        for value in ("NA", "None", "Blue", "Yellow", "White"):
            ttk.Radiobutton(rank_row, text=value, variable=self._r_rank,
                            value=value).pack(side="left", padx=(0, 10))
        row += 1

        ttk.Label(info, text="Notes", foreground=TEXT, font=("Segoe UI", 9)).grid(
            row=row, column=0, sticky="nw", padx=(0, 10), pady=6)
        self._r_notes = tk.Text(info, bg=BG2, fg=TEXT, font=("Segoe UI", 9),
                                relief="flat", width=30, height=4, insertbackground=TEXT)
        self._r_notes.grid(row=row, column=1, sticky="ew", pady=6)
        self._r_notes.bind("<KeyRelease>", self._sync_strike_meta)

        ttk.Label(
            wrap,
            text="* Required fields - filled in here are captured with every "
                 "strike the moment the trigger fires.",
            foreground=TEXT, font=("Segoe UI", 8), wraplength=420, justify="left",
        ).pack(anchor="w", pady=(8, 0))

    # ══════════════════════════════════════════════════════════════════════════
    #  DATA TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _build_data_tab(self, parent):
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        files_frame = ttk.Frame(paned)
        detail_frame = ttk.Frame(paned)
        paned.add(files_frame, weight=1)
        paned.add(detail_frame, weight=2)

        self._build_files_list(files_frame)
        self._build_stats_panel(detail_frame)

    def _build_files_list(self, parent):
        tb = ttk.Frame(parent, padding=(8, 6, 8, 4))
        tb.pack(fill="x")

        ttk.Button(tb, text="Refresh", style="Accent.TButton",
                   command=self.refresh_file_list).pack(side="right")
        ttk.Button(tb, text="Edit Metadata",
                   command=self._files_edit_metadata).pack(side="right", padx=(0, 6))
        ttk.Button(tb, text="Delete",
                   command=self._files_delete_selected).pack(side="right", padx=(0, 6))

        frame = ttk.Frame(parent, padding=(8, 4, 8, 8))
        frame.pack(fill="both", expand=True)

        cols = ("id", "event", "name", "weapon_type",
                "peak_force_lbf", "impulse", "notes", "feedback")
        self.file_tree = ttk.Treeview(frame, columns=cols, show="headings",
                                       style="Files.Treeview", selectmode="browse")

        for col, label, w, anc in [
            ("id",              "ID",             155, "center"),
            ("event",           "Event",          110, "center"),
            ("name",            "Name",           110, "center"),
            ("weapon_type",     "Weapon Type",     80, "center"),
            ("peak_force_lbf",  "Peak (lbf)",      60, "center"),
            ("impulse",         "Impulse",         60, "center"),
            ("notes",           "Notes",          180, "w"),
            ("feedback",        "Calibration Feedback",    60, "center"),
        ]:
            self.file_tree.heading(col, text=label,
                                   command=lambda c=col: self._files_sort(c))
            self.file_tree.column(col, width=w, anchor=anc, minwidth=40)

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.file_tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.file_tree.xview)
        self.file_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.file_tree.bind("<<TreeviewSelect>>", self._on_file_select)

        # Sort state
        self._files_sort_col = "id"
        self._files_sort_rev = True

    def _files_edit_metadata(self):
        """Open editor for the currently selected strike file."""
        sel = self.file_tree.selection()
        if not sel:
            messagebox.showinfo("No file selected", "Select a file first.")
            return
        path = self.file_tree.item(sel[0], "tags")[0]
        try:
            strike = load_strike(path)
        except Exception as exc:
            messagebox.showerror("Read error", str(exc))
            return
        dlg = StrikeEditDialog(self, strike, path)
        self.wait_window(dlg)
        if dlg.saved:
            self._on_strike_json_written(path)

    def _files_sort(self, col: str):
        """Sort file list by clicked column header."""
        if self._files_sort_col == col:
            self._files_sort_rev = not self._files_sort_rev
        else:
            self._files_sort_col = col
            numeric_cols = {"id", "peak_force_lbf", "impulse", "feedback"}
            self._files_sort_rev = col in numeric_cols
        self.refresh_file_list()

    def _files_delete_selected(self):
        sel = self.file_tree.selection()
        if not sel:
            return
        path = self.file_tree.item(sel[0], "tags")[0]
        fname = Path(path).name
        if not messagebox.askyesno("Delete file",
                                    f"Permanently delete:\n{fname}?"):
            return
        try:
            Path(path).unlink()
        except Exception as exc:
            messagebox.showerror("Delete failed", str(exc))
            return
        # Clear viewer tabs if this file was selected
        if self.selected_path == path:
            self.selected_path = None
            self._selected_strike = None
            self._data_headers = []
            self._data_rows    = []
            self._populate_stats(None, [], [])
            if HAS_MPL:
                self._populate_chart([], [])
        self.refresh_file_list()

    def _build_stats_panel(self, parent):
        if HAS_MPL:
            paned = ttk.PanedWindow(parent, orient="vertical")
            paned.pack(fill="both", expand=True, padx=4, pady=4)
            stats_frame = ttk.Frame(paned)
            chart_frame = ttk.Frame(paned)
            paned.add(stats_frame, weight=1)
            paned.add(chart_frame, weight=2)
        else:
            stats_frame = parent
            chart_frame = None

        self.stats_text = tk.Text(stats_frame, bg=BG2, fg=TEXT, font=("Consolas", 10),
                                   relief="flat", wrap="none", state="disabled",
                                   height=7, insertbackground=TEXT,
                                   selectbackground=ACCENT)
        #sb = ttk.Scrollbar(stats_frame, orient="vertical", command=self.stats_text.yview)
        #self.stats_text.configure(yscrollcommand=sb.set)
        self.stats_text.pack(side="left", fill="both", expand=True, padx=(0, 0), pady=0)
        #sb.pack(side="right", fill="y")
        for tag, colour, bold in [
            ("header", TEXT, True),
            ("label",  TEXT, False),
            ("value",  GREEN,    False),
            ("raw",    TEXT,   False),
            ("lbf",    ORANGE,   False),
        ]:
            kw: dict = {"foreground": colour}
            if bold:
                kw["font"] = ("Consolas", 10, "bold")
            self.stats_text.tag_configure(tag, **kw)

        if chart_frame is not None:
            self._fig = Figure(figsize=(6, 4), dpi=100, facecolor=BG)
            self._ax = self._fig.add_subplot(111, facecolor=BG2)
            self._canvas = FigureCanvasTkAgg(self._fig, master=chart_frame)
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
            tf = ttk.Frame(chart_frame)
            tf.pack(fill="x")
            NavigationToolbar2Tk(self._canvas, tf)

    # ══════════════════════════════════════════════════════════════════════════
    #  Recorder control
    # ══════════════════════════════════════════════════════════════════════════

    def _get_strike_metadata(self) -> dict:
        return dict(self._strike_meta)

    def _sync_strike_meta(self, *_args):
        self._strike_meta["event"] = self._r_event.get().strip()
        self._strike_meta["name"] = self._r_name.get().strip()
        self._strike_meta["weapon_type"] = self._r_weapon.get().strip()
        self._strike_meta["kingdom"] = self._r_kingdom.get().strip()
        self._strike_meta["rank"] = self._r_rank.get().strip()
        self._strike_meta["notes"] = self._r_notes.get("1.0", "end-1c").strip()

    def _resolve_pending_feedback(self):
        pending = self._pending_feedback
        if not pending:
            return
        dialog = pending.get("dialog")
        path = pending.get("path")
        if dialog and dialog.winfo_exists():
            if pending.get("slider_moved"):
                try:
                    update_strike_feedback(path, dialog.get_value())
                except Exception:
                    pass
            dialog.destroy()
        if path and pending and pending.get("slider_moved"):
            self._on_strike_json_written(path)
        self._pending_feedback = None

    def _open_strike_feedback(self, path: str):
        self._resolve_pending_feedback()

        def on_moved():
            if self._pending_feedback:
                self._pending_feedback["slider_moved"] = True
            self._rearm_after_feedback()

        def on_closed():
            self._rearm_after_feedback()

        dialog = StrikeFeedbackDialog(
            self, path, on_slider_moved=on_moved, on_written=self._on_strike_json_written,
            on_close=on_closed)
        self._pending_feedback = {
            "path": path,
            "dialog": dialog,
            "slider_moved": False,
            "rearmed": False,
        }

    def _rearm_after_feedback(self):
        """Re-arm the trigger once calibration feedback has been entered
        (or the feedback dialog has been dismissed), per pending feedback.
        The engine still confirms the force has settled below the trigger
        threshold before it actually starts watching for the next strike."""
        pending = self._pending_feedback
        if pending is not None:
            if pending.get("rearmed"):
                return
            pending["rearmed"] = True
        self._engine.arm()
        self._log_append(
            "Feedback recorded -- rearming (confirming force has settled) ...", "info")

    def _apply_engine_config(self) -> bool:
        """Push current UI settings into the engine. Must be called on the main thread."""
        e = self._engine
        e.save_folder             = str(ensure_data_dir())
        #serial                    = self._r_serial.get().strip()
        #e.serial_number           = serial if serial else None
        e.data_rate               = float(self._r_data_rate.get())
        e.max_record_seconds      = float(self._r_max_record_s.get())
        e.calibration             = self._cal

        # Trigger threshold is always entered in lbf.
        self._sync_trigger_calibration_gate()
        self._sync_strike_meta()
        return True

    def _auto_connect(self):
        """Called once via after() at startup -- applies config then connects off-thread."""
        if not self._apply_engine_config():
            return
        self._start_connect_thread()

    def _rec_connect(self):
        if not HAS_PHIDGET:
            messagebox.showerror("Phidget22 not found",
                                  "Install with:  pip install Phidget22")
            return
        if not self._apply_engine_config():
            return
        self._start_connect_thread()

    def _start_connect_thread(self):
        """Log the intent and launch e.connect() on a daemon thread."""
        e      = self._engine
        #serial = self._r_serial.get().strip()
        self._log_clear()
        self._log_append(
            "Connecting to PhidgetBridge  CH0(auto-detect)", "info")
        raw_thresh = float(self._r_threshold.get())
        self._log_append(
            f"Trigger: {raw_thresh:.4f} lbf  =  {e.trigger_threshold:+.6f} V/V  (rising)",
            "info")
        if not self._cal.is_calibrated:
            self._log_append(
                "Not calibrated yet -- connecting disarmed. Calibrate "
                "Channel 0, then use Arm Trigger.", "trigger")
        self._btn_connect.configure(state="disabled")
        # Run e.connect() on a daemon thread -- it spawns the engine thread
        # and returns immediately, but we keep the GUI thread free.
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        """Off-thread: call engine.connect() (non-blocking -- just starts the engine thread)."""
        self._engine.connect()
        if not self._cal.is_calibrated:
            # Refuse to arm on raw, uncalibrated sensor units -- require an
            # explicit Arm Trigger click once Channel 0 has been calibrated.
            self._engine.disarm()
        # Nothing to schedule back; _poll_recorder will pick up state changes.

    def _rec_disconnect(self):
        self._log_append("Disconnecting", "info")
        self._btn_disconnect.configure(state="disabled")
        self._btn_arm.configure(state="disabled")
        self._btn_disarm.configure(state="disabled")
        threading.Thread(target=self._do_disconnect, daemon=True).start()

    def _do_disconnect(self):
        self._engine.disconnect()
        self.after(0, self._on_disconnect_done)

    def _on_disconnect_done(self):
        self._set_banner("IDLE", TEXT)
        self._prog_lbl.set("")
        self._log_append("Disconnected.", "info")
        self._btn_connect.configure(state="normal")
        self._btn_disconnect.configure(state="disabled")
        self._btn_arm.configure(state="disabled")
        self._btn_disarm.configure(state="disabled")
        self._logged_connected = False
        self._logged_trigger   = False
        self._prev_state       = RecorderEngine.IDLE

    def _rec_arm(self):
        if not self._cal.is_calibrated:
            messagebox.showwarning(
                "Not calibrated",
                "Calibrate Channel 0 before arming the trigger.")
            return
        self._engine.arm()
        self._log_append("Trigger armed -- watching for event ...", "info")
        self._btn_arm.configure(state="disabled")
        self._btn_disarm.configure(state="normal")

    def _rec_disarm(self):
        self._engine.disarm()
        self._log_append("Trigger disarmed.", "info")
        self._btn_arm.configure(
            state="normal" if self._cal.is_calibrated else "disabled")
        self._btn_disarm.configure(state="disabled")

    def _sync_trigger_calibration_gate(self):
        """Keep the trigger threshold current and refuse to arm/trigger on
        raw, uncalibrated sensor units. Called on every poll tick so it
        reacts as soon as Channel 0 is (or stops being) calibrated."""
        e = self._engine
        raw_thresh = float(self._r_threshold.get())
        e.trigger_threshold = raw_thresh / self._cal.scale_factor + self._cal.zero_offset

        calibrated = self._cal.is_calibrated
        if e.state == RecorderEngine.WAITING and not calibrated:
            # Should not happen (arming is gated), but guard against a
            # calibration being cleared while already armed.
            e.disarm()
        if e.state == RecorderEngine.DISARMED:
            self._btn_arm.configure(state="normal" if calibrated else "disabled")

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

    # ══════════════════════════════════════════════════════════════════════════
    #  Recorder polling
    # ══════════════════════════════════════════════════════════════════════════

    def _poll_recorder(self):
        e     = self._engine
        state = e.state
        v     = e.latest

        # ── Gauge update ──────────────────────────────────────────────────────
        self._gauge_var.set(f"{v:+.6f} V/V" if v is not None else "---")
        self._live_lbf_var.set(
            f"{self._cal.to_lbf(v):+.4f}  lbf"
            if (v is not None and self._cal.is_calibrated) else "")
        self._sync_trigger_calibration_gate()

        # ── State transitions ─────────────────────────────────────────────────
        if state != self._prev_state:
            self._prev_state = state

            if state == RecorderEngine.CONNECTING:
                self._set_banner("CONNECTING", YELLOW)
                self._btn_connect.configure(state="disabled")
                self._btn_disconnect.configure(state="disabled")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="disabled")

            elif state == RecorderEngine.WAITING:
                self._set_banner("WAITING FOR TRIGGER", YELLOW)
                self._prog_lbl.set(f"0 / ~{e.capture_target}")
                self._btn_connect.configure(state="disabled")
                self._btn_disconnect.configure(state="normal")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="normal")
                if not self._logged_connected:
                    self._logged_connected = True
                    self._log_append("Channel 0 attached and armed.", "ok")
                    self._log_append(
                        f"Watching for rising trigger  (> {self._r_threshold.get():.2f} lbf "
                        f"= {e.trigger_threshold:+.6f} V/V) ...",
                        "info")
                else:
                    self._log_append("Rearmed -- waiting for next trigger ...", "info")
                self._logged_trigger = False

            elif state == RecorderEngine.DISARMED:
                self._btn_arm.configure(
                    state="normal" if self._cal.is_calibrated else "disabled")
                self._btn_disarm.configure(state="disabled")

            elif state == RecorderEngine.RECORDING:
                self._set_banner("RECORDING", GREEN)
                if not self._logged_trigger:
                    self._logged_trigger = True
                    self._log_append(
                        f"Trigger fired  [{e.capture_index + 1}]  -- "
                        f"recording up to {e.max_record_seconds:.1f}s, or until "
                        f"force drops below threshold ...", "trigger")

            elif state == RecorderEngine.SAVING:
                self._set_banner("SAVING ...", TEXT)

            elif state == RecorderEngine.SETTLING:
                self._set_banner("SETTLING -- CONFIRMING FORCE BELOW THRESHOLD", YELLOW)
                self._btn_connect.configure(state="disabled")
                self._btn_disconnect.configure(state="normal")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="normal")
                self._log_append(
                    f"Confirming force stays below threshold for "
                    f"{e.reset_hold_seconds:.1f}s before rearming ...", "info")

            elif state == RecorderEngine.ERROR:
                self._set_banner("ERROR", RED)
                self._log_append(f"{e.error_msg}", "error")
                self._btn_connect.configure(state="normal")
                self._btn_disconnect.configure(state="disabled")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="disabled")
                self._logged_connected = False

            elif state == RecorderEngine.IDLE:
                self._set_banner("IDLE", TEXT)
                self._btn_connect.configure(state="normal")
                self._btn_disconnect.configure(state="disabled")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="disabled")

        # ── Keep the DISARMED banner in sync with pending-feedback status ──────
        if state == RecorderEngine.DISARMED:
            pending = self._pending_feedback
            if pending and not pending.get("rearmed"):
                self._set_banner("DISARMED -- AWAITING FEEDBACK", YELLOW)
            else:
                self._set_banner("DISARMED", TEXT)

        # ── Progress counter while recording ──────────────────────────────────
        if state == RecorderEngine.RECORDING:
            cnt = e.capture_count
            if cnt != self._prev_count:
                self._prev_count = cnt
                self._prog_lbl.set(f"{cnt} / ~{e.capture_target}")

        # ── Detect completed capture (capture_index incremented by engine) ────
        if e.capture_index != self._prev_capture_idx and e.last_saved_name:
            self._prev_capture_idx = e.capture_index
            n_total = e.capture_count
            strike = e.last_strike
            rate = (strike.data_rate_hz if strike and strike.data_rate_hz else e.data_rate) or 1.0
            duration_s = n_total / rate
            self._log_append(
                f"Capture #{e.capture_index}  saved  "
                f"({n_total} samples, ~{duration_s:.2f}s)  -> {e.last_saved_name}", "done")
            self._prog_lbl.set(f"0 / ~{e.capture_target}")
            if e.saved_path:
                self._on_strike_json_written(e.saved_path)
            else:
                self.refresh_file_list()

            self._log_append(
                "Trigger disarmed -- enter calibration feedback to rearm.", "info")
            if e.saved_path:
                self._open_strike_feedback(e.saved_path)

        self.after(80, self._poll_recorder)

    def _set_banner(self, text: str, colour: str):
        self._state_var.set(text)
        self._state_lbl.configure(fg=colour)

    # ══════════════════════════════════════════════════════════════════════════
    #  Folder / file helpers
    # ══════════════════════════════════════════════════════════════════════════

    # (folder browsing removed -- all files saved to ./data/)

    def _on_notebook_tab_changed(self, _event=None):
        idx = getattr(self, "_data_tab_index", None)
        if idx is None:
            return
        try:
            if self.notebook.index(self.notebook.select()) == idx:
                self.refresh_file_list()
        except tk.TclError:
            pass

    def _on_strike_json_written(self, path: str | None = None):
        """Refresh file list after a strike JSON file is created or updated."""
        if not hasattr(self, "file_tree"):
            return
        reselect = path or self.selected_path
        if path:
            self.selected_path = path
        self.refresh_file_list(reselect_path=reselect)

    def refresh_file_list(self, *, reselect_path: str | None = None):
        folder = ensure_data_dir()
        self.strike_files = find_strike_files(str(folder))

        col = getattr(self, "_files_sort_col", "id")
        rev = getattr(self, "_files_sort_rev", True)
        key_map = {
            "id":             lambda m: m["id"],
            "event":          lambda m: m["event"].lower(),
            "name":           lambda m: m["name"].lower(),
            "weapon_type":    lambda m: m["weapon_type"].lower(),
            "peak_force_lbf": lambda m: float(m["peak_force_lbf"]) if m["peak_force_lbf"] else 0.0,
            "impulse":        lambda m: float(m["impulse"]) if m["impulse"] else 0.0,
            "notes":          lambda m: m["notes"].lower(),
            "feedback":       lambda m: float(m["feedback"]) if m["feedback"] else -1.0,
        }
        self.strike_files.sort(key=key_map.get(col, key_map["id"]), reverse=rev)

        keep_path = reselect_path if reselect_path is not None else self.selected_path
        self.file_tree.delete(*self.file_tree.get_children())
        reselect_item = None
        for meta in self.strike_files:
            item = self.file_tree.insert("", "end",
                values=(
                    meta["id"],
                    meta["event"],
                    meta["name"],
                    meta["weapon_type"],
                    meta["peak_force_lbf"],
                    meta["impulse"],
                    meta["notes"],
                    meta["feedback"],
                ),
                tags=(meta["path"],))
            if keep_path and meta["path"] == keep_path:
                reselect_item = item

        if reselect_item:
            self.file_tree.selection_set(reselect_item)
            self.file_tree.see(reselect_item)
            self._on_file_select()

    def _on_file_select(self, _event=None):
        sel = self.file_tree.selection()
        if not sel:
            return
        path = self.file_tree.item(sel[0], "tags")[0]
        self.selected_path = path
        try:
            strike = load_strike(path)
            headers, rows = strike_to_tabular(strike)
        except Exception as exc:
            messagebox.showerror("Read error", str(exc))
            return
        self._selected_strike = strike
        self._data_headers = headers
        self._data_rows    = rows
        self._populate_stats(strike, headers, rows)
        if HAS_MPL:
            self._populate_chart(headers, rows)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _populate_stats(self, strike: StrikeData | None, headers, rows):
        t = self.stats_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not rows:
            t.insert("end", "No data.\n")
            t.configure(state="disabled")
            return

        if strike:
            t.insert("end", "  STRIKE METADATA\n", "header")
            t.insert("end", "  " + "-" * 44 + "\n", "label")
            feedback = (
                f"{strike.user_calibration_feedback:.1f}"
                if strike.user_calibration_feedback is not None
                else "--"
            )
            for label, val in [
                ("Date / Time",    strike.datetime),
                ("Event",          strike.event or "--"),
                ("Name",           strike.name or "--"),
                ("Weapon Type",    strike.weapon_type or "--"),
                ("Kingdom",        strike.kingdom or "--"),
                ("Rank",           strike.rank or "--"),
                ("Notes",          strike.notes or "--"),
                ("Peak Force",     f"{strike.peak_force_lbf:.4f} lbf"),
                ("Impulse",        f"{strike.total_energy_lbf_s:.6f} lbf·s"),
                ("Cal Feedback",   feedback),
            ]:
                t.insert("end", f"  {label:<14}", "label")
                t.insert("end", f"  {val}\n", "value")

        t.configure(state="disabled")

    # ── Chart ─────────────────────────────────────────────────────────────────

    def _populate_chart(self, headers, rows):
        if not HAS_MPL:
            return
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG2)
        self._fig.patch.set_facecolor(BG)

        col = "ch0_lbf"
        if not rows or col not in headers or "timestamp" not in headers:
            ax.set_xlabel("Time", color=TEXT, fontsize=9)
            ax.set_ylabel("lbf", color=TEXT, fontsize=9)
            ax.tick_params(colors=TEXT)
            for spine in ax.spines.values():
                spine.set_edgecolor(BG2)
            self._canvas.draw()
            return

        ts_idx = headers.index("timestamp")
        lbf_idx = headers.index(col)
        times: list[datetime] = []
        vals: list[float] = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row[ts_idx])
                lbf = float(row[lbf_idx])
            except (ValueError, IndexError, TypeError):
                continue
            times.append(ts)
            vals.append(lbf)

        if not times:
            ax.set_xlabel("Time", color=TEXT, fontsize=9)
            ax.set_ylabel("lbf", color=TEXT, fontsize=9)
            ax.tick_params(colors=TEXT)
            for spine in ax.spines.values():
                spine.set_edgecolor(BG2)
            self._canvas.draw()
            return

        ax.plot(times, vals, color=ORANGE, linewidth=1.6, alpha=0.9)

        ax.set_xlabel("Time", color=TEXT, fontsize=9)
        ax.set_ylabel("lbf", color=TEXT, fontsize=9)
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.tick_params(colors=TEXT)
        for spine in ax.spines.values():
            spine.set_edgecolor(BG2)
        ax.grid(True, color=BG2, linewidth=0.6, linestyle="--")
        self._fig.autofmt_xdate()
        ax.tick_params(colors=TEXT)
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_color(TEXT)
        self._canvas.draw()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def destroy(self):
        self._resolve_pending_feedback()
        self._engine.disconnect()
        super().destroy()


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

