"""
PhidgetBridge CSV Viewer
-------------------------
A Tkinter GUI that lists all bridge_data_*.csv files produced by
phidgetbridge_recorder.py and displays their contents in an interactive table.

Requirements:
    pip install matplotlib   (for the optional chart view)

Usage:
    python phidgetbridge_viewer.py
    python phidgetbridge_viewer.py /path/to/csv/folder
"""

import os
import sys
import csv
import json
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path

# matplotlib is optional — chart tab is hidden if not installed
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ── Theme colours ─────────────────────────────────────────────────────────────
BG          = "#1e1e2e"
BG2         = "#2a2a3e"
BG3         = "#313145"
ACCENT      = "#7c6af7"
ACCENT2     = "#a78bfa"
TEXT        = "#e2e0f0"
TEXT_DIM    = "#8884aa"
GREEN       = "#4ade80"
YELLOW      = "#fbbf24"
RED         = "#f87171"
CH_COLOURS  = ["#7c6af7", "#4ade80", "#fbbf24", "#f87171"]

CSV_GLOB    = "bridge_data_*.csv"


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_csv_files(folder: str) -> list[dict]:
    """Return metadata dicts for every matching CSV in *folder*."""
    folder = Path(folder)
    files = []
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
            return sum(1 for _ in f) - 1   # subtract header
    except Exception:
        return 0


def load_csv(path: str) -> tuple[list[str], list[list]]:
    """Return (headers, rows) from a CSV file."""
    headers, rows = [], []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0:
                headers = row
            else:
                rows.append(row)
    return headers, rows


# ── Main Application ──────────────────────────────────────────────────────────

class BridgeViewer(tk.Tk):
    def __init__(self, start_folder: str = "."):
        super().__init__()
        self.title("PhidgetBridge CSV Viewer")
        self.geometry("1100x700")
        self.minsize(800, 500)
        self.configure(bg=BG)
        self._apply_style()

        self.folder = tk.StringVar(value=str(Path(start_folder).resolve()))
        self.csv_files: list[dict] = []
        self.selected_path: str | None = None

        self._build_ui()
        self.refresh_file_list()

    # ── Style ─────────────────────────────────────────────────────────────────

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",            background=BG,  foreground=TEXT,  font=("Segoe UI", 10))
        s.configure("TFrame",       background=BG)
        s.configure("TLabel",       background=BG,  foreground=TEXT)
        s.configure("TButton",      background=BG3, foreground=TEXT,  relief="flat",
                    padding=(10, 5))
        s.map("TButton",
              background=[("active", ACCENT), ("pressed", ACCENT2)],
              foreground=[("active", "#ffffff")])

        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        s.map("Accent.TButton",
              background=[("active", ACCENT2), ("pressed", BG3)])

        s.configure("TEntry",       fieldbackground=BG3, foreground=TEXT,
                    insertcolor=TEXT, relief="flat")
        s.configure("TNotebook",    background=BG,  tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab",background=BG3, foreground=TEXT_DIM,
                    padding=[14, 6])
        s.map("TNotebook.Tab",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])

        # File-list treeview
        s.configure("Files.Treeview",
                    background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=28, borderwidth=0)
        s.configure("Files.Treeview.Heading",
                    background=BG3, foreground=ACCENT2, relief="flat", padding=(6, 6))
        s.map("Files.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "#ffffff")])

        # Data treeview
        s.configure("Data.Treeview",
                    background=BG2, foreground=TEXT, fieldbackground=BG2,
                    rowheight=24, font=("Consolas", 9), borderwidth=0)
        s.configure("Data.Treeview.Heading",
                    background=BG3, foreground=ACCENT2, relief="flat", padding=(4, 5))
        s.map("Data.Treeview",
              background=[("selected", BG3)],
              foreground=[("selected", ACCENT2)])

        s.configure("TScrollbar", background=BG3, troughcolor=BG,
                    arrowcolor=TEXT_DIM, relief="flat")

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──────────────────────────────────────────────────────────
        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")

        ttk.Label(top, text="📁 Folder:", foreground=TEXT_DIM).pack(side="left")
        ttk.Entry(top, textvariable=self.folder, width=55).pack(side="left", padx=6)
        ttk.Button(top, text="Browse…",       command=self._browse).pack(side="left", padx=2)
        ttk.Button(top, text="⟳ Refresh",     command=self.refresh_file_list,
                   style="Accent.TButton").pack(side="left", padx=(8, 0))

        # Status label on the right
        self.status_var = tk.StringVar(value="No folder loaded")
        ttk.Label(top, textvariable=self.status_var, foreground=TEXT_DIM).pack(
            side="right", padx=8)

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # ── Paned layout: left file list / right detail ───────────────────────
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=0, pady=0)

        # Left panel — file list
        left = ttk.Frame(pane, padding=(8, 8, 4, 8))
        pane.add(left, weight=1)
        self._build_file_list(left)

        # Right panel — tabbed detail
        right = ttk.Frame(pane, padding=(4, 8, 8, 8))
        pane.add(right, weight=3)
        self._build_detail_panel(right)

    def _build_file_list(self, parent):
        ttk.Label(parent, text="CSV FILES", foreground=ACCENT2,
                  font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 6))

        cols = ("filename", "rows", "size_kb", "modified")
        self.file_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                       style="Files.Treeview", selectmode="browse")

        self.file_tree.heading("filename",  text="File")
        self.file_tree.heading("rows",      text="Rows")
        self.file_tree.heading("size_kb",   text="KB")
        self.file_tree.heading("modified",  text="Modified")

        self.file_tree.column("filename",  width=160, anchor="w")
        self.file_tree.column("rows",      width=55,  anchor="center")
        self.file_tree.column("size_kb",   width=55,  anchor="center")
        self.file_tree.column("modified",  width=140, anchor="center")

        sb = ttk.Scrollbar(parent, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=sb.set)

        self.file_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.file_tree.bind("<<TreeviewSelect>>", self._on_file_select)

    def _build_detail_panel(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True)

        # Tab 1 — raw data table
        tab_data = ttk.Frame(self.notebook)
        self.notebook.add(tab_data, text="  📋 Data Table  ")
        self._build_data_table(tab_data)

        # Tab 2 — summary stats
        tab_stats = ttk.Frame(self.notebook)
        self.notebook.add(tab_stats, text="  📊 Summary  ")
        self._build_stats_panel(tab_stats)

        # Tab 3 — chart (optional)
        if HAS_MPL:
            tab_chart = ttk.Frame(self.notebook)
            self.notebook.add(tab_chart, text="  📈 Chart  ")
            self._build_chart_panel(tab_chart)

        # Placeholder when nothing is selected
        self.placeholder = ttk.Label(
            parent,
            text="← Select a file to view its contents",
            foreground=TEXT_DIM, font=("Segoe UI", 12),
        )
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

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

        ttk.Button(toolbar, text="Export copy…", command=self._export_filtered).pack(
            side="right", padx=4)

        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=4, pady=4)

        self.data_tree = ttk.Treeview(frame, show="headings",
                                       style="Data.Treeview", selectmode="browse")
        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.data_tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal",  command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.data_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # Alternating row colours
        self.data_tree.tag_configure("odd",  background=BG2)
        self.data_tree.tag_configure("even", background="#252536")

        self._data_headers: list[str] = []
        self._data_rows:    list[list] = []

    def _build_stats_panel(self, parent):
        self.stats_text = tk.Text(
            parent, bg=BG2, fg=TEXT, font=("Consolas", 10),
            relief="flat", wrap="none", state="disabled",
            insertbackground=TEXT, selectbackground=ACCENT,
        )
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.stats_text.yview)
        self.stats_text.configure(yscrollcommand=sb.set)
        self.stats_text.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        sb.pack(side="right", fill="y", pady=4)

        # Text tags for colouring
        self.stats_text.tag_configure("header",  foreground=ACCENT2, font=("Consolas", 10, "bold"))
        self.stats_text.tag_configure("label",   foreground=TEXT_DIM)
        self.stats_text.tag_configure("value",   foreground=GREEN)
        self.stats_text.tag_configure("ch0",     foreground=CH_COLOURS[0])
        self.stats_text.tag_configure("ch1",     foreground=CH_COLOURS[1])
        self.stats_text.tag_configure("ch2",     foreground=CH_COLOURS[2])
        self.stats_text.tag_configure("ch3",     foreground=CH_COLOURS[3])

    def _build_chart_panel(self, parent):
        self._fig = Figure(figsize=(6, 4), dpi=100, facecolor=BG)
        self._ax  = self._fig.add_subplot(111, facecolor=BG2)
        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)
        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill="x")
        NavigationToolbar2Tk(self._canvas, toolbar_frame)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse(self):
        folder = filedialog.askdirectory(initialdir=self.folder.get(),
                                          title="Select CSV folder")
        if folder:
            self.folder.set(folder)
            self.refresh_file_list()

    def refresh_file_list(self):
        folder = self.folder.get()
        if not Path(folder).is_dir():
            messagebox.showerror("Invalid folder", f"Not a directory:\n{folder}")
            return

        self.csv_files = find_csv_files(folder)
        self.file_tree.delete(*self.file_tree.get_children())

        for meta in self.csv_files:
            self.file_tree.insert(
                "", "end",
                values=(meta["filename"], meta["rows"], meta["size_kb"], meta["modified"]),
                tags=(meta["path"],),
            )

        count = len(self.csv_files)
        self.status_var.set(f"{count} file{'s' if count != 1 else ''} found")

    def _on_file_select(self, _event=None):
        sel = self.file_tree.selection()
        if not sel:
            return
        item   = self.file_tree.item(sel[0])
        # Tags stores the full path
        path   = self.file_tree.item(sel[0], "tags")[0]
        self.selected_path = path
        self.placeholder.place_forget()

        try:
            headers, rows = load_csv(path)
        except Exception as e:
            messagebox.showerror("Read error", str(e))
            return

        self._data_headers = headers
        self._data_rows    = rows
        self.filter_var.set("")
        self._populate_data_table(headers, rows)
        self._populate_stats(headers, rows)
        if HAS_MPL:
            self._populate_chart(headers, rows)

    def _populate_data_table(self, headers, rows):
        tree = self.data_tree
        tree.delete(*tree.get_children())

        tree["columns"] = headers
        for col in headers:
            tree.heading(col, text=col, anchor="center")
            width = max(100, len(col) * 11)
            tree.column(col, width=width, anchor="center", minwidth=70)

        for i, row in enumerate(rows):
            tag = "odd" if i % 2 else "even"
            tree.insert("", "end", values=row, tags=(tag,))

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
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            title="Save filtered data",
        )
        if path:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(self._data_headers)
                w.writerows(rows)
            messagebox.showinfo("Exported", f"Saved {len(rows)} rows to:\n{path}")

    def _populate_stats(self, headers, rows):
        """Compute min/max/mean/std for each channel column."""
        t = self.stats_text
        t.configure(state="normal")
        t.delete("1.0", "end")

        if not rows:
            t.insert("end", "No data.\n")
            t.configure(state="disabled")
            return

        # Identify channel columns (skip timestamp)
        ch_cols = [h for h in headers if h.startswith("ch")]

        t.insert("end", f"  File: ", "label")
        t.insert("end", f"{Path(self.selected_path).name}\n", "value")
        t.insert("end", f"  Rows: ", "label")
        t.insert("end", f"{len(rows)}\n\n", "value")
        t.insert("end", "  CHANNEL STATISTICS\n", "header")
        t.insert("end", "  " + "─" * 58 + "\n", "label")

        col_w = 14
        header_line = "  " + f"{'Metric':<12}" + "".join(f"{c:>{col_w}}" for c in ch_cols) + "\n"
        t.insert("end", header_line, "header")
        t.insert("end", "  " + "─" * 58 + "\n", "label")

        # Build numeric arrays
        data: dict[str, list[float]] = {c: [] for c in ch_cols}
        for row in rows:
            for c in ch_cols:
                idx = headers.index(c)
                try:
                    data[c].append(float(row[idx]))
                except (ValueError, IndexError):
                    pass

        def fmt(v): return f"{v:+.6f}"

        for metric, fn in [
            ("Min",    min),
            ("Max",    max),
            ("Mean",   lambda xs: sum(xs) / len(xs)),
            ("Range",  lambda xs: max(xs) - min(xs)),
            ("Std",    lambda xs: (sum((x - sum(xs)/len(xs))**2 for x in xs) / len(xs)) ** 0.5),
        ]:
            line = f"  {metric:<12}"
            t.insert("end", line, "label")
            for i, c in enumerate(ch_cols):
                vals = data[c]
                tag  = f"ch{i}" if i < 4 else "value"
                cell = fmt(fn(vals)) if vals else "   N/A  "
                t.insert("end", f"{cell:>{col_w}}", tag)
            t.insert("end", "\n")

        t.insert("end", "  " + "─" * 58 + "\n\n", "label")

        # Time span
        ts_col = headers[0] if headers else None
        if ts_col:
            first = rows[0][0] if rows else ""
            last  = rows[-1][0] if rows else ""
            t.insert("end", "  CAPTURE INFO\n", "header")
            t.insert("end", f"  {'Start':<12}", "label")
            t.insert("end", f"{first}\n", "value")
            t.insert("end", f"  {'End':<12}", "label")
            t.insert("end", f"{last}\n", "value")

        t.configure(state="disabled")

    def _populate_chart(self, headers, rows):
        ax = self._ax
        ax.clear()
        ax.set_facecolor(BG2)
        self._fig.patch.set_facecolor(BG)

        ch_cols = [h for h in headers if h.startswith("ch")]
        x = list(range(len(rows)))

        for i, col in enumerate(ch_cols):
            idx = headers.index(col)
            vals = []
            for row in rows:
                try:    vals.append(float(row[idx]))
                except: vals.append(None)

            clean_x = [xi for xi, v in zip(x, vals) if v is not None]
            clean_y = [v  for v in vals if v is not None]
            ax.plot(clean_x, clean_y, color=CH_COLOURS[i % 4], linewidth=1.4,
                    label=col, alpha=0.9)

        ax.set_xlabel("Sample Index", color=TEXT_DIM, fontsize=9)
        ax.set_ylabel("V / V",        color=TEXT_DIM, fontsize=9)
        ax.set_title(Path(self.selected_path).name, color=TEXT, fontsize=10)
        ax.tick_params(colors=TEXT_DIM)
        for spine in ax.spines.values():
            spine.set_edgecolor(BG3)
        ax.legend(facecolor=BG3, edgecolor=BG3, labelcolor=TEXT, fontsize=9)
        ax.grid(True, color=BG3, linewidth=0.6, linestyle="--")

        self._canvas.draw()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "."
    app = BridgeViewer(start_folder=start)
    app.mainloop()
