"""
PhidgetBridge HMI  â€”  Recorder + Calibration + Viewer  (Channel 0)
===================================================================
Monitors and records Channel 0 of a PhidgetBridge 4-Input device.

Tabs
â”€â”€â”€â”€
  ðŸ”´ Record     â€” live gauge, trigger config, capture log
  âš–  Calibrate  â€” zero-point + 1-point calibration  (V/V â†’ lbf)
  ðŸ“‹ Data Table â€” browse CSV files, filter, export
  ðŸ“Š Summary    â€” statistics for the selected file
  ðŸ“ˆ Chart      â€” line plot  (requires matplotlib)

Calibration model
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  lbf = (raw_V_per_V âˆ’ zero_offset) Ã— scale_factor

  zero_offset  = avg V/V at zero load
  scale_factor = known_load_lbf / (cal_avg âˆ’ zero_offset)

Calibration is saved to  calibration.json  in the working folder.

Requirements
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    pip install Phidget22
    pip install matplotlib     # optional â€” enables Chart tab

Usage
â”€â”€â”€â”€â”€
    python phidgetbridge.py
    python phidgetbridge.py /path/to/save/folder
"""

import os
import sys
import csv
import json
import time
import collections
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path

# â”€â”€ Optional imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

# â”€â”€ Theme â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
DATA_DIR    = Path(__file__).parent / "data"   # always relative to script
CAL_SAMPLES = 50              # samples averaged per calibration step

META_FIELDS  = ["datetime", "event", "name", "weapon_type", "notes",
                "peak_force_lbf", "total_energy"]


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Metadata model
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class EventMetadata:
    """
    Sidecar metadata for one recorded event.
    Saved to  <csv_basename>.json  beside the CSV file.
    """

    def __init__(self):
        self.datetime       = ""     # ISO string set at save time
        self.event          = ""
        self.name           = ""
        self.weapon_type    = ""
        self.notes          = ""
        self.peak_force_lbf = 0.0   # auto-computed from captured lbf data
        self.total_energy   = 0.0   # reserved for future use

    # â”€â”€ Persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def sidecar_path(csv_path: str) -> Path:
        return Path(csv_path).with_suffix(".json")

    def save(self, csv_path: str):
        p = self.sidecar_path(csv_path)
        with open(p, "w") as f:
            json.dump({
                "datetime":       self.datetime,
                "event":          self.event,
                "name":           self.name,
                "weapon_type":    self.weapon_type,
                "notes":          self.notes,
                "peak_force_lbf": self.peak_force_lbf,
                "total_energy":   self.total_energy,
            }, f, indent=2)

    @classmethod
    def load(cls, csv_path: str) -> "EventMetadata | None":
        p = cls.sidecar_path(csv_path)
        if not p.exists():
            return None
        try:
            with open(p) as f:
                d = json.load(f)
            m = cls()
            for k in META_FIELDS:
                if k in d:
                    setattr(m, k, d[k])
            return m
        except Exception:
            return None


def ensure_data_dir() -> Path:
    """Create ./data/ beside the script if it doesn't exist, return its Path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Calibration model
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class CalibrationStore:
    """
    Single-channel calibration.
    lbf = (raw_V_per_V âˆ’ zero_offset) Ã— scale_factor
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

    # â”€â”€ Conversion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def to_lbf(self, raw: float) -> float:
        return (raw - self._d["zero_offset"]) * self._d["scale_factor"]

    @property
    def is_calibrated(self) -> bool:
        return bool(self._d.get("calibrated", False))

    # â”€â”€ Setters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def set_zero(self, raw_avg: float):
        self._d["zero_offset"] = raw_avg
        self._d["zero_raw"]    = raw_avg
        self._d["calibrated"]  = False
        self._d["timestamp"]   = datetime.now().isoformat(timespec="seconds")

    def set_cal_point(self, raw_avg: float, load_lbf: float):
        span = raw_avg - self._d["zero_offset"]
        if abs(span) < 1e-12:
            raise ValueError("Cal point too close to zero â€” apply a larger load.")
        self._d["cal_raw"]      = raw_avg
        self._d["cal_load_lbf"] = load_lbf
        self._d["scale_factor"] = load_lbf / span
        self._d["calibrated"]   = True
        self._d["timestamp"]    = datetime.now().isoformat(timespec="seconds")

    # â”€â”€ Persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def save(self, folder: str = None):
        path = ensure_data_dir() / CAL_FILE
        with open(path, "w") as f:
            json.dump(self._d, f, indent=2)
        return str(path)

    def load(self, folder: str = None) -> bool:
        # Always try the canonical DATA_DIR location first
        candidates = [ensure_data_dir() / CAL_FILE]
        if folder:
            candidates.append(Path(folder) / CAL_FILE)
        for p in candidates:
            if not p.exists():
                continue
            try:
                with open(p) as f:
                    data = json.load(f)
                if "channels" in data:      # old multi-channel format
                    data = data["channels"][0]
                self._d.update(data)
                return True
            except Exception:
                continue
        return False

    def reset(self):
        self._d = self._blank()

    # â”€â”€ Read-only properties for display â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Recorder engine  (persistent connection, auto-rearm after each capture)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class RecorderEngine:
    IDLE        = "idle"        # not connected
    CONNECTING  = "connecting"  # opening Phidget channel
    WAITING     = "waiting"     # connected, armed, watching for trigger
    RECORDING   = "recording"   # trigger fired, collecting post-trigger samples
    SAVING      = "saving"      # writing CSV, then auto-rearms
    DISARMED    = "disarmed"    # connected but not watching trigger
    ERROR       = "error"

    def __init__(self):
        self.state          = self.IDLE
        self.latest: float | None = None
        self.captured       = []            # current capture: list of (ts, value)
        self.capture_target = 110
        self.error_msg      = ""
        self._ch            = None          # VoltageRatioInput handle (kept open)
        self._stop_evt   = threading.Event()   # set â†’ disconnect
        self._disarm_evt = threading.Event()   # set â†’ pause trigger watching
        self._attach_evt = threading.Event()   # set when CH0 attaches
        self._thread     = None

        self.serial_number           = None
        self.data_interval_ms        = 50
        self.trigger_threshold       = 0.01
        self.trigger_direction       = "either"
        self.num_points              = 100
        self.pre_trigger_buffer_size = 10
        self.save_folder             = "."
        self.calibration: CalibrationStore | None = None

        self._triggered  = False
        self.saved_path  = ""
        self._pre_buffer = collections.deque()

        # Set by _save_csv; GUI polls and opens metadata dialog
        self.pending_metadata: EventMetadata | None = None

        # Counters visible to the GUI
        self.capture_index = 0      # increments with each saved file
        self.last_saved_name = ""

    # â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def connect(self):
        """Open the Phidget channel and enter WAITING state."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._disarm_evt.clear()
        self._attach_evt.clear()
        self.error_msg       = ""
        self.latest          = None
        self.capture_index   = 0
        self.last_saved_name = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        """Close the Phidget channel and return to IDLE."""
        self._stop_evt.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self.state  = self.IDLE
        self.latest = None

    def disarm(self):
        """Pause trigger monitoring while keeping the channel open."""
        self._disarm_evt.set()

    def arm(self):
        """Resume trigger monitoring."""
        self._disarm_evt.clear()

    @property
    def capture_count(self):
        return len(self.captured)

    # â”€â”€ Background thread (runs for the life of the connection) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _run(self):
        try:
            self.state = self.CONNECTING
            self._open_channel()
            # Main loop: wait-for-trigger â†’ record â†’ save â†’ repeat
            while not self._stop_evt.is_set():
                self._reset_for_next_capture()
                self.state = self.WAITING
                self._wait_for_trigger()
                if self._stop_evt.is_set():
                    break
                if self._disarm_evt.is_set():
                    # disarm() was called while waiting â€” park in DISARMED
                    self.state = self.DISARMED
                    self._wait_while_disarmed()
                    continue   # loop back to arm check
                # Trigger fired â€” record post-trigger samples
                self.state = self.RECORDING
                self._record()
                if self._stop_evt.is_set():
                    break
                # Save to CSV
                self.state = self.SAVING
                self._save_csv()
                self.capture_index += 1
                # Brief pause so the GUI can render the SAVING state
                time.sleep(0.15)
                # Loop automatically rearms (back to top of while)
            self.state = self.IDLE
        except Exception as exc:
            self.error_msg = str(exc)
            self.state     = self.ERROR
        finally:
            self._close_channel()

    def _reset_for_next_capture(self):
        """Clear capture buffer and pre-trigger buffer ready for next trigger."""
        self._triggered  = False
        self.captured    = []
        self.saved_path  = ""
        self.capture_target = self.pre_trigger_buffer_size + self.num_points
        self._pre_buffer = collections.deque(maxlen=self.pre_trigger_buffer_size)

    def _wait_for_trigger(self):
        while not self._triggered and not self._stop_evt.is_set() and not self._disarm_evt.is_set():
            time.sleep(0.02)

    def _wait_while_disarmed(self):
        while self._disarm_evt.is_set() and not self._stop_evt.is_set():
            time.sleep(0.05)

    # â”€â”€ Phidget channel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _open_channel(self):
        """Open CH0 asynchronously â€” never blocks the calling thread."""
        self._attach_evt = threading.Event()
        ch = VoltageRatioInput()
        ch.setChannel(0)
        if self.serial_number:
            ch.setDeviceSerialNumber(int(self.serial_number))
        ch.setOnAttachHandler(self._on_attach)
        ch.setOnDetachHandler(lambda c: None)
        ch.setOnErrorHandler(self._on_error)
        ch.setOnVoltageRatioChangeHandler(self._on_value_change)
        ch.open()          # non-blocking â€” attach fires via callback when ready
        self._ch = ch
        # Wait for the attach callback to fire (up to 10 s); check stop_evt too
        for _ in range(200):          # 200 Ã— 50 ms = 10 s max
            if self._attach_evt.is_set():
                return
            if self._stop_evt.is_set():
                return
            time.sleep(0.05)
        raise TimeoutError("PhidgetBridge CH0 did not attach within 10 seconds.")

    def _close_channel(self):
        if self._ch is not None:
            try:
                self._ch.close()
            except Exception:
                pass
            self._ch = None

    def _on_attach(self, ch):
        ch.setDataInterval(self.data_interval_ms)
        self._attach_evt.set()     # unblocks _open_channel wait loop

    def _on_error(self, ch, code, desc):
        self.error_msg = f"CH0 error [{code}]: {desc}"

    def _on_value_change(self, ch, value):
        self.latest = value
        if self.state == self.WAITING and not self._triggered:
            ts = datetime.now().isoformat(timespec="milliseconds")
            self._pre_buffer.append((ts, value))
            fired = False
            if self.trigger_direction in ("rising",  "either") and value >  self.trigger_threshold:
                fired = True
            if self.trigger_direction in ("falling", "either") and value < -self.trigger_threshold:
                fired = True
            if fired:
                self.captured   = list(self._pre_buffer)
                self._triggered = True

    # â”€â”€ Data capture & save â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

        n_pre   = len(self.captured) - self.num_points
        n_pre   = max(0, n_pre)
        lbf_col = cal and cal.is_calibrated
        headers = ["timestamp", "pre_trigger", "ch0_V_per_V"] + (["ch0_lbf"] if lbf_col else [])

        # Compute peak force from all captured lbf values
        lbf_values = []
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for i, (ts, v) in enumerate(self.captured):
                raw_str  = f"{v:.8f}" if v is not None else ""
                pre_flag = "1" if i < n_pre else "0"
                row = [ts, pre_flag, raw_str]
                if lbf_col and v is not None:
                    lbf = cal.to_lbf(v)
                    lbf_values.append(lbf)
                    row.append(f"{lbf:.6f}")
                elif lbf_col:
                    row.append("")
                w.writerow(row)

        self.saved_path      = path
        self.last_saved_name = fname

        # Build pending metadata with auto-computed peak force
        meta = EventMetadata()
        meta.datetime       = datetime.now().isoformat(timespec="seconds")
        meta.peak_force_lbf = round(max(lbf_values), 6) if lbf_values else 0.0
        meta.total_energy   = 0.0
        self.pending_metadata = meta


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Calibration sampler  (background thread)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  CSV helpers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def find_csv_files(folder: str) -> list[dict]:
    folder = Path(folder)
    files  = []
    for p in sorted(folder.glob(CSV_GLOB), key=os.path.getmtime, reverse=True):
        stat = p.stat()
        meta = EventMetadata.load(str(p))
        files.append({
            "path":          str(p),
            "filename":      p.name,
            "size_kb":       f"{stat.st_size / 1024:.1f}",
            "modified":      datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "rows":          _count_rows(p),
            "event":         meta.event          if meta else "",
            "name":          meta.name           if meta else "",
            "weapon_type":   meta.weapon_type    if meta else "",
            "peak_force_lbf":f"{meta.peak_force_lbf:.3f}" if meta else "",
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Metadata entry dialog
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class MetadataDialog(tk.Toplevel):
    """
    Modal dialog shown after each capture to enter event metadata.
    Pre-fills datetime and peak force; user fills text fields.
    """

    # Remember last-entered values so they persist across captures
    _last_event       = ""
    _last_name        = ""
    _last_weapon_type = ""

    def __init__(self, parent, meta: EventMetadata, csv_path: str):
        super().__init__(parent)
        self.title("Event Metadata")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()          # modal
        self.transient(parent)

        self._meta     = meta
        self._csv_path = csv_path
        self._saved    = False

        self._build(meta)
        self.update_idletasks()

        # Centre over parent
        px = parent.winfo_rootx() + parent.winfo_width()  // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px - w//2}+{py - h//2}")
        self.protocol("WM_DELETE_WINDOW", self._on_skip)

    def _build(self, meta: EventMetadata):
        pad = {"padx": 16, "pady": 6}

        # â”€â”€ Title â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        tk.Label(self, text="ðŸ“‹  Event Metadata", fg=ACCENT2, bg=BG,
                 font=("Segoe UI", 12, "bold")).pack(pady=(14, 2))
        tk.Label(self, text="Fill in details for this capture, then click Save.",
                 fg=TEXT_DIM, bg=BG, font=("Segoe UI", 9)).pack(pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=(0, 8))

        form = ttk.Frame(self, padding=(16, 4, 16, 8))
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        def row(r, label, widget_factory, readonly=False):
            tk.Label(form, text=label, fg=TEXT_DIM, bg=BG,
                     font=("Segoe UI", 9), anchor="e", width=14).grid(
                row=r, column=0, sticky="e", padx=(0, 10), pady=5)
            w = widget_factory(form)
            w.grid(row=r, column=1, sticky="ew", pady=5)
            return w

        s = ttk.Style()

        # Date / Time  (read-only)
        self._v_datetime = tk.StringVar(value=meta.datetime)
        row(0, "Date / Time", lambda p: ttk.Entry(
            p, textvariable=self._v_datetime, state="readonly", width=26))

        # Event
        self._v_event = tk.StringVar(value=MetadataDialog._last_event)
        row(1, "Event *", lambda p: ttk.Entry(p, textvariable=self._v_event, width=26))

        # Name
        self._v_name = tk.StringVar(value=MetadataDialog._last_name)
        row(2, "Name *", lambda p: ttk.Entry(p, textvariable=self._v_name, width=26))

        # Weapon Type
        self._v_weapon = tk.StringVar(value=MetadataDialog._last_weapon_type)
        row(3, "Weapon Type *", lambda p: ttk.Entry(p, textvariable=self._v_weapon, width=26))

        # Notes (multi-line)
        tk.Label(form, text="Notes", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9), anchor="e", width=14).grid(
            row=4, column=0, sticky="ne", padx=(0, 10), pady=5)
        self._notes_box = tk.Text(form, bg=BG3, fg=TEXT, font=("Segoe UI", 9),
                                   relief="flat", width=30, height=3,
                                   insertbackground=TEXT)
        self._notes_box.insert("1.0", meta.notes)
        self._notes_box.grid(row=4, column=1, sticky="ew", pady=5)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=(4, 8))

        # â”€â”€ Auto-computed fields â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        auto = ttk.Frame(self, padding=(16, 0, 16, 8))
        auto.pack(fill="x")
        auto.columnconfigure(1, weight=1)

        tk.Label(auto, text="Auto-computed", fg=ACCENT2, bg=BG,
                 font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        for r, label, val in [
            (1, "Peak Force (lbf)",  f"{meta.peak_force_lbf:.4f}"),
            (2, "Total Energy",      f"{meta.total_energy:.4f}  (reserved)"),
        ]:
            tk.Label(auto, text=label, fg=TEXT_DIM, bg=BG,
                     font=("Segoe UI", 9), anchor="e", width=14).grid(
                row=r, column=0, sticky="e", padx=(0, 10), pady=3)
            tk.Label(auto, text=val, fg=GREEN, bg=BG,
                     font=("Consolas", 10)).grid(row=r, column=1, sticky="w", pady=3)

        # â”€â”€ Buttons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        btn_row = ttk.Frame(self, padding=(16, 4, 16, 14))
        btn_row.pack(fill="x")

        s.configure("DlgSave.TButton", background=ACCENT, foreground="#ffffff")
        s.map("DlgSave.TButton",
              background=[("active", ACCENT2), ("pressed", BG3)])

        ttk.Button(btn_row, text="ðŸ’¾  Save Metadata",
                   style="DlgSave.TButton",
                   command=self._on_save).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ttk.Button(btn_row, text="Skip",
                   command=self._on_skip).pack(side="left", ipadx=10)

    # â”€â”€ Actions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _on_save(self):
        m = self._meta
        m.event       = self._v_event.get().strip()
        m.name        = self._v_name.get().strip()
        m.weapon_type = self._v_weapon.get().strip()
        m.notes       = self._notes_box.get("1.0", "end-1c").strip()

        # Persist last-used values for next dialog
        MetadataDialog._last_event       = m.event
        MetadataDialog._last_name        = m.name
        MetadataDialog._last_weapon_type = m.weapon_type

        try:
            m.save(self._csv_path)
            self._saved = True
        except Exception as exc:
            messagebox.showerror("Save error", str(exc), parent=self)
            return
        self.destroy()

    def _on_skip(self):
        """Close without saving metadata."""
        self.destroy()




class BridgeHMI(tk.Tk):

    def __init__(self, start_folder: str = "."):
        super().__init__()
        self.title("PhidgetBridge HMI  â€”  Channel 0")
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

        self._engine.save_folder = str(ensure_data_dir())
        self._engine.calibration = self._cal
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
        self.refresh_file_list()
        self._poll_recorder()
        self._poll_cal_sampler()

        # Auto-connect on startup if Phidget library is available
        if HAS_PHIDGET:
            self.after(500, self._auto_connect)

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  Styles
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  Top bar + layout
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    def _build_ui(self):
        # â”€â”€ Slim status bar at the very top â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        top = ttk.Frame(self, padding=(12, 6))
        top.pack(fill="x")
        tk.Label(top, text="PhidgetBridge HMI  â€”  Channel 0",
                 fg=ACCENT2, bg=BG, font=("Segoe UI", 11, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status_var, foreground=TEXT_DIM).pack(side="right", padx=8)

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # â”€â”€ Full-width notebook (no pane split) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self._build_right_panel(self)

    # â”€â”€ Right panel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build_right_panel(self, parent):
        # Initialise tk variables used across tabs BEFORE building any tab
        self._cal_status_var = tk.StringVar(value="")
        self._cal_prog_lbl   = tk.StringVar(value="")

        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        for text, builder in [
            ("  ðŸ”´ Record  ",      self._build_record_tab),
            ("  âš–  Calibrate  ",  self._build_cal_tab),
            ("  ðŸ“ Files  ",       self._build_files_tab),
            ("  ðŸ“‹ Data Table  ", self._build_data_table),
            ("  ðŸ“Š Summary  ",    self._build_stats_panel),
        ]:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=text)
            builder(tab)

        if HAS_MPL:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text="  ðŸ“ˆ Chart  ")
            self._build_chart_panel(tab)

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  RECORD TAB
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

        # Threshold unit toggle
        unit_row = ttk.Frame(trg)
        unit_row.pack(fill="x", pady=(0, 6))
        ttk.Label(unit_row, text="Threshold unit:", foreground=TEXT_DIM,
                  font=("Segoe UI", 8)).pack(side="left")
        self._r_trg_unit = tk.StringVar(value="V/V")
        ttk.Radiobutton(unit_row, text="V/V", variable=self._r_trg_unit,
                        value="V/V",  command=self._on_trg_unit_change).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(unit_row, text="lbf", variable=self._r_trg_unit,
                        value="lbf",  command=self._on_trg_unit_change).pack(side="left")

        self._r_threshold = tk.DoubleVar(value=0.01)
        self._trg_unit_lbl = tk.StringVar(value="V/V")
        thresh_row = ttk.Frame(trg)
        thresh_row.pack(fill="x", pady=(0, 6))
        ttk.Label(thresh_row, text="Threshold:", foreground=TEXT_DIM,
                  font=("Segoe UI", 8)).pack(side="left")
        ttk.Entry(thresh_row, textvariable=self._r_threshold, width=12).pack(side="left", padx=6)
        tk.Label(thresh_row, textvariable=self._trg_unit_lbl, fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")

        # Cal-not-available warning (shown when lbf selected but not calibrated)
        self._trg_cal_warn = tk.StringVar(value="")
        tk.Label(trg, textvariable=self._trg_cal_warn, fg=YELLOW, bg=BG,
                 font=("Segoe UI", 8), wraplength=200, justify="left").pack(anchor="w")

        ttk.Label(trg, text="Direction", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_direction = tk.StringVar(value="either")
        ttk.Combobox(trg, textvariable=self._r_direction,
                     values=["either", "rising", "falling"],
                     state="readonly", width=10).pack(anchor="w", pady=(2, 0))

        # Capture
        cap = ttk.LabelFrame(cfg, text="Capture", padding=10)
        cap.pack(fill="x", pady=(0, 8))
        ttk.Label(cap, text="Post-trigger points", foreground=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self._r_npoints = tk.IntVar(value=100)
        ttk.Spinbox(cap, from_=10, to=10000, increment=10,
                    textvariable=self._r_npoints, width=8).pack(anchor="w", pady=(2, 8))
        ttk.Label(cap, text="Pre-trigger buffer (points)", foreground=TEXT_DIM,
                  font=("Segoe UI", 8)).pack(anchor="w")
        self._r_pre_buf = tk.IntVar(value=10)
        ttk.Spinbox(cap, from_=0, to=500, increment=1,
                    textvariable=self._r_pre_buf, width=8).pack(anchor="w", pady=(2, 0))

        # Connection buttons
        conn_row = ttk.Frame(cfg)
        conn_row.pack(fill="x", pady=(4, 4))
        self._btn_connect = ttk.Button(conn_row, text="â»  Connect",
                                        style="Green.TButton", command=self._rec_connect)
        self._btn_connect.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn_disconnect = ttk.Button(conn_row, text="â»  Disconnect",
                                           style="Red.TButton", command=self._rec_disconnect,
                                           state="disabled")
        self._btn_disconnect.pack(side="left", expand=True, fill="x")

        # Arm / Disarm buttons
        arm_row = ttk.Frame(cfg)
        arm_row.pack(fill="x", pady=(0, 4))
        self._btn_arm = ttk.Button(arm_row, text="ðŸŽ¯  Arm Trigger",
                                    style="Accent.TButton", command=self._rec_arm,
                                    state="disabled")
        self._btn_arm.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn_disarm = ttk.Button(arm_row, text="â¸  Disarm",
                                       command=self._rec_disarm, state="disabled")
        self._btn_disarm.pack(side="left", expand=True, fill="x")

        if not HAS_PHIDGET:
            ttk.Label(cfg, text="âš   Phidget22 not installed.\nRecording unavailable.",
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
        gauge_frame = ttk.LabelFrame(live, text="Channel 0  â€”  Live Value  (V/V)", padding=12)
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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  CALIBRATION TAB
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    def _build_cal_tab(self, parent):
        # Header
        hdr = ttk.Frame(parent, padding=(12, 10, 12, 4))
        hdr.pack(fill="x")
        tk.Label(hdr, text="âš–  Channel 0 Calibration  â€”  V/V  â†’  lbf",
                 fg=ACCENT2, bg=BG, font=("Segoe UI", 12, "bold")).pack(side="left")
        # Status text (right-aligned in header)
        tk.Label(hdr, textvariable=self._cal_prog_lbl,
                 fg=GREEN, bg=BG, font=("Consolas", 9)).pack(side="right", padx=6)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8)

        # Instructions
        note_frame = ttk.Frame(parent, padding=(14, 8, 14, 4))
        note_frame.pack(fill="x")
        note = (
            "Step 1 â€” Remove all load, then click  \"âŠ™ Capture Zero\".\n"
            "Step 2 â€” Apply a known reference load (lbf), enter the value, "
            "then click  \"âŠ™ Capture Cal Point\".\n"
            f"Each step averages  {CAL_SAMPLES}  live samples (~2.5 s).  "
            "Click  \"ðŸ’¾ Save\"  when done."
        )
        tk.Label(note_frame, text=note, fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9), justify="left").pack(anchor="w")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8, pady=(4, 0))

        # â”€â”€ Main calibration area â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        body = ttk.Frame(parent, padding=(16, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        # Live readout card
        live_lf = ttk.LabelFrame(body, text="Live Reading  â€”  Channel 0", padding=12)
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
        self._cal_lbf_live_var = tk.StringVar(value="â€”")
        tk.Label(live_inner, textvariable=self._cal_lbf_live_var, fg=ORANGE, bg=BG,
                 font=("Consolas", 12, "bold"), width=14).grid(row=0, column=4, sticky="w")
        tk.Label(live_inner, text="lbf", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 9)).grid(row=0, column=5, sticky="w", padx=(2, 0))

        # Step 1 â€” Zero
        zero_lf = ttk.LabelFrame(body, text="Step 1 â€” Zero Point  (no load on sensor)", padding=12)
        zero_lf.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        self._zero_result_var = tk.StringVar(value="Not captured")
        tk.Label(zero_lf, textvariable=self._zero_result_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 8))
        ttk.Button(zero_lf, text="âŠ™  Capture Zero",
                   style="Green.TButton",
                   command=self._cal_capture_zero).pack(fill="x")

        # Step 2 â€” Cal point
        cal_lf = ttk.LabelFrame(body, text="Step 2 â€” Cal Point  (known load applied)", padding=12)
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
        ttk.Button(cal_lf, text="âŠ™  Capture Cal Point",
                   style="Orange.TButton",
                   command=self._cal_capture_point).pack(fill="x")

        # Equation + status
        info_lf = ttk.LabelFrame(body, text="Calibration Status", padding=12)
        info_lf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self._cal_eq_var = tk.StringVar(value="")
        tk.Label(info_lf, textvariable=self._cal_eq_var, fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(0, 4))
        self._cal_badge_var = tk.StringVar(value="â¬¤  NOT CALIBRATED")
        self._cal_badge_lbl = tk.Label(info_lf, textvariable=self._cal_badge_var,
                                        fg=RED, bg=BG, font=("Segoe UI", 10, "bold"))
        self._cal_badge_lbl.pack(anchor="w")

        # Bottom bar
        bot = ttk.Frame(parent, padding=(12, 6))
        bot.pack(fill="x", side="bottom")
        ttk.Button(bot, text="ðŸ’¾  Save Calibration",
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

    # â”€â”€ Cal helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
                f"lbf = (raw âˆ’ {cal.zero_offset:+.5f}) Ã— {cal.scale_factor:.4f}"
                + (f"   [saved: {cal.timestamp[11:19]}]" if cal.timestamp else ""))
            self._cal_badge_var.set("â¬¤  CALIBRATED")
            self._cal_badge_lbl.configure(fg=GREEN)
        else:
            self._cal_eq_var.set("")
            self._cal_badge_var.set("â¬¤  NOT CALIBRATED")
            self._cal_badge_lbl.configure(fg=RED)

    def _cal_capture_zero(self):
        if not self._sampler_ready():
            return
        self._cal_pending = "zero"
        self._cal_set_buttons("disabled")
        self._cal_prog_lbl.set(f"Sampling zero  ({CAL_SAMPLES} pts) â€¦")
        self._sampler.start()

    def _cal_capture_point(self):
        if not self._sampler_ready():
            return
        if self._cal.zero_raw is None:
            messagebox.showwarning("Zero not set", "Capture the zero point first.")
            return
        self._cal_pending = "point"
        self._cal_set_buttons("disabled")
        self._cal_prog_lbl.set(f"Sampling cal point  ({CAL_SAMPLES} pts) â€¦")
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
                                    "No readings yet â€” connect the device via the Record tab first.")
            return False
        return True

    def _cal_set_buttons(self, state: str):
        for w in self.winfo_children():
            pass  # buttons stored directly â€” use widget references
        try:
            self._btn_zero.configure(state=state)
            self._btn_cal_pt.configure(state=state)
        except Exception:
            pass

    def _cal_save(self):
        try:
            saved_path = self._cal.save()
            self._cal_status_var.set(
                f"âœ“ Saved  {Path(saved_path).name}  ({datetime.now().strftime('%H:%M:%S')})")
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))

    def _cal_reset(self):
        if not messagebox.askyesno("Reset", "Clear calibration data for Channel 0?"):
            return
        self._cal.reset()
        self._cal_refresh()
        self._cal_status_var.set("Calibration reset.")

    # â”€â”€ Cal sampler polling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _poll_cal_sampler(self):
        s   = self._sampler
        cal = self._cal
        v   = self._engine.latest

        # Update live readouts
        if v is None:
            self._cal_raw_var.set("---")
            self._cal_lbf_live_var.set("â€”")
        else:
            self._cal_raw_var.set(f"{v:+.7f}")
            self._cal_lbf_live_var.set(f"{cal.to_lbf(v):+.4f}" if cal.is_calibrated else "â€”")

        if s.state == CalSampler.DONE:
            avg = s.result
            if avg is None:
                messagebox.showerror("No data", "Channel 0 returned no readings â€” check connection.")
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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  FILES TAB
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    def _build_files_tab(self, parent):
        # â”€â”€ Toolbar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        tb = ttk.Frame(parent, padding=(8, 6, 8, 4))
        tb.pack(fill="x")

        tk.Label(tb, text="ðŸ“", fg=ACCENT2, bg=BG,
                 font=("Segoe UI", 11)).pack(side="left")
        tk.Label(tb, text=str(ensure_data_dir()), fg=TEXT_DIM, bg=BG,
                 font=("Consolas", 9)).pack(side="left", padx=(6, 0))

        self._files_count_var = tk.StringVar(value="")
        ttk.Label(tb, textvariable=self._files_count_var,
                  foreground=TEXT_DIM).pack(side="right", padx=(8, 0))
        ttk.Button(tb, text="âŸ³ Refresh", style="Accent.TButton",
                   command=self.refresh_file_list).pack(side="right")
        ttk.Button(tb, text="âœ Edit Metadata",
                   command=self._files_edit_metadata).pack(side="right", padx=(0, 6))
        ttk.Button(tb, text="ðŸ—‘ Delete",
                   command=self._files_delete_selected).pack(side="right", padx=(0, 6))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8)

        # â”€â”€ File treeview â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        frame = ttk.Frame(parent, padding=(8, 4, 8, 8))
        frame.pack(fill="both", expand=True)

        cols = ("filename", "event", "name", "weapon_type",
                "peak_force_lbf", "rows", "size_kb", "modified")
        self.file_tree = ttk.Treeview(frame, columns=cols, show="headings",
                                       style="Files.Treeview", selectmode="browse")

        for col, label, w, anc in [
            ("filename",        "Filename",       180, "w"),
            ("event",           "Event",          110, "w"),
            ("name",            "Name",           110, "w"),
            ("weapon_type",     "Weapon Type",    110, "w"),
            ("peak_force_lbf",  "Peak (lbf)",      80, "center"),
            ("rows",            "Rows",            55, "center"),
            ("size_kb",         "KB",              55, "center"),
            ("modified",        "Last Modified",  155, "center"),
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
        self.file_tree.bind("<Double-1>",          self._files_open_selected)

        # Sort state
        self._files_sort_col = "modified"
        self._files_sort_rev = True

    def _files_edit_metadata(self):
        """Open the metadata dialog for the currently selected file."""
        sel = self.file_tree.selection()
        if not sel:
            messagebox.showinfo("No file selected", "Select a file first.")
            return
        path = self.file_tree.item(sel[0], "tags")[0]
        meta = EventMetadata.load(path) or EventMetadata()
        meta.datetime = meta.datetime or datetime.now().isoformat(timespec="seconds")
        dlg = MetadataDialog(self, meta, path)
        self.wait_window(dlg)
        if dlg._saved:
            self.refresh_file_list()

    def _files_sort(self, col: str):
        """Sort file list by clicked column header."""
        if self._files_sort_col == col:
            self._files_sort_rev = not self._files_sort_rev
        else:
            self._files_sort_col = col
            self._files_sort_rev = col == "modified"
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
            self._data_headers = []
            self._data_rows    = []
            self._populate_data_table([], [])
            self._populate_stats([], [])
        self.refresh_file_list()

    def _files_open_selected(self, _event=None):
        """Double-click: switch to the Data Table tab."""
        if self.selected_path:
            # Find and select the Data Table tab
            for i in range(self.notebook.index("end")):
                if "Data" in self.notebook.tab(i, "text"):
                    self.notebook.select(i)
                    break

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  DATA TABLE TAB
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
        ttk.Button(toolbar, text="Export copyâ€¦",
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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  SUMMARY TAB
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  CHART TAB
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  Recorder control
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    def _apply_engine_config(self) -> bool:
        """Push current UI settings into the engine. Must be called on the main thread."""
        e = self._engine
        e.save_folder             = str(ensure_data_dir())
        serial                    = self._r_serial.get().strip()
        e.serial_number           = serial if serial else None
        e.data_interval_ms        = self._r_interval.get()
        e.trigger_direction       = self._r_direction.get()
        e.num_points              = self._r_npoints.get()
        e.pre_trigger_buffer_size = self._r_pre_buf.get()
        e.calibration             = self._cal

        raw_thresh = float(self._r_threshold.get())
        if self._r_trg_unit.get() == "lbf":
            if not self._cal.is_calibrated:
                messagebox.showerror("Not calibrated",
                                      "Calibrate before using a lbf trigger threshold.")
                return False
            vv_thresh = raw_thresh / self._cal.scale_factor + self._cal.zero_offset
        else:
            vv_thresh = raw_thresh
        e.trigger_threshold = vv_thresh
        return True

    def _auto_connect(self):
        """Called once via after() at startup â€” applies config then connects off-thread."""
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
        serial = self._r_serial.get().strip()
        self._log_clear()
        self._log_append(
            "Connecting to PhidgetBridge  CH0"
            + (f"  (serial {serial})" if serial else "  (auto-detect)") + " â€¦", "info")
        if self._r_trg_unit.get() == "lbf":
            raw_thresh = float(self._r_threshold.get())
            self._log_append(
                f"Trigger: {raw_thresh:.4f} lbf  =  {e.trigger_threshold:+.6f} V/V"
                f"  ({e.trigger_direction})", "info")
        self._btn_connect.configure(state="disabled")
        # Run e.connect() on a daemon thread â€” it spawns the engine thread
        # and returns immediately, but we keep the GUI thread free.
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        """Off-thread: call engine.connect() (non-blocking â€” just starts the engine thread)."""
        self._engine.connect()
        # Nothing to schedule back; _poll_recorder will pick up state changes.

    def _rec_disconnect(self):
        self._log_append("Disconnecting â€¦", "info")
        self._btn_disconnect.configure(state="disabled")
        self._btn_arm.configure(state="disabled")
        self._btn_disarm.configure(state="disabled")
        threading.Thread(target=self._do_disconnect, daemon=True).start()

    def _do_disconnect(self):
        self._engine.disconnect()
        self.after(0, self._on_disconnect_done)

    def _on_disconnect_done(self):
        self._set_banner("IDLE", TEXT_DIM)
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
        self._engine.arm()
        self._log_append("Trigger armed â€” watching for event â€¦", "info")
        self._btn_arm.configure(state="disabled")
        self._btn_disarm.configure(state="normal")

    def _rec_disarm(self):
        self._engine.disarm()
        self._log_append("Trigger disarmed.", "info")
        self._btn_arm.configure(state="normal")
        self._btn_disarm.configure(state="disabled")

    def _on_trg_unit_change(self):
        unit = self._r_trg_unit.get()
        self._trg_unit_lbl.set(unit)
        if unit == "lbf":
            if not self._cal.is_calibrated:
                self._trg_cal_warn.set(
                    "âš  No calibration â€” calibrate first or switch back to V/V.")
                self._r_trg_unit.set("V/V")
                self._trg_unit_lbl.set("V/V")
            else:
                self._trg_cal_warn.set("")
                self._r_threshold.set(round(self._cal.cal_load_lbf * 0.1, 4))
        else:
            self._trg_cal_warn.set("")
            self._r_threshold.set(0.01)

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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  Recorder polling
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    def _poll_recorder(self):
        e     = self._engine
        state = e.state
        v     = e.latest

        # â”€â”€ Gauge update â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self._gauge_var.set(f"{v:+.6f} V/V" if v is not None else "---")
        self._draw_gauge(v)
        self._live_lbf_var.set(
            f"{self._cal.to_lbf(v):+.4f}  lbf"
            if (v is not None and self._cal.is_calibrated) else "")

        # â”€â”€ State transitions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if state != self._prev_state:
            self._prev_state = state

            if state == RecorderEngine.CONNECTING:
                self._set_banner("CONNECTING â€¦", YELLOW)
                self._btn_connect.configure(state="disabled")
                self._btn_disconnect.configure(state="disabled")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="disabled")

            elif state == RecorderEngine.WAITING:
                self._set_banner("WAITING FOR TRIGGER", YELLOW)
                self._prog_lbl.set(f"0 / {e.capture_target}")
                self._btn_connect.configure(state="disabled")
                self._btn_disconnect.configure(state="normal")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="normal")
                if not self._logged_connected:
                    self._logged_connected = True
                    self._log_append("Channel 0 attached and armed.", "ok")
                    dir_txt = "|value|" if e.trigger_direction == "either" else e.trigger_direction
                    self._log_append(
                        f"Watching for trigger  "
                        f"({dir_txt} > {e.trigger_threshold:.6f} V/V) â€¦", "info")
                else:
                    self._log_append("â†º Rearmed â€” waiting for next trigger â€¦", "info")
                self._logged_trigger = False

            elif state == RecorderEngine.DISARMED:
                self._set_banner("DISARMED", TEXT_DIM)
                self._btn_arm.configure(state="normal")
                self._btn_disarm.configure(state="disabled")

            elif state == RecorderEngine.RECORDING:
                self._set_banner("RECORDING", GREEN)
                if not self._logged_trigger:
                    self._logged_trigger = True
                    n_pre = len(e.captured)
                    self._log_append(
                        f"âš¡ Trigger fired  [{e.capture_index + 1}]  â€” "
                        f"{n_pre} pre-trigger sample(s), "
                        f"recording {e.num_points} post-trigger points â€¦", "trigger")

            elif state == RecorderEngine.SAVING:
                self._set_banner("SAVING â€¦", ACCENT2)

            elif state == RecorderEngine.ERROR:
                self._set_banner("ERROR", RED)
                self._log_append(f"âœ— {e.error_msg}", "error")
                self._btn_connect.configure(state="normal")
                self._btn_disconnect.configure(state="disabled")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="disabled")
                self._logged_connected = False

            elif state == RecorderEngine.IDLE:
                self._set_banner("IDLE", TEXT_DIM)
                self._btn_connect.configure(state="normal")
                self._btn_disconnect.configure(state="disabled")
                self._btn_arm.configure(state="disabled")
                self._btn_disarm.configure(state="disabled")

        # â”€â”€ Progress counter while recording â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if state == RecorderEngine.RECORDING:
            cnt = e.capture_count
            if cnt != self._prev_count:
                self._prev_count = cnt
                self._prog_lbl.set(f"{cnt} / {e.capture_target}")

        # â”€â”€ Detect completed capture (capture_index incremented by engine) â”€â”€â”€â”€
        if e.capture_index != self._prev_capture_idx and e.last_saved_name:
            self._prev_capture_idx = e.capture_index
            n_pre = e.capture_count - e.num_points
            n_pre = max(0, n_pre)
            self._log_append(
                f"âœ“ Capture #{e.capture_index}  saved  "
                f"({n_pre} pre + {e.num_points} post)  â†’ {e.last_saved_name}", "done")
            self._prog_lbl.set(f"0 / {e.capture_target}")
            self.refresh_file_list()

            # Open metadata dialog if engine produced pending metadata
            if e.pending_metadata is not None:
                meta     = e.pending_metadata
                csv_path = e.saved_path
                e.pending_metadata = None   # clear before dialog opens
                dlg = MetadataDialog(self, meta, csv_path)
                self.wait_window(dlg)
                # Refresh file list again in case metadata was saved
                self.refresh_file_list()

        self.after(80, self._poll_recorder)

    def _set_banner(self, text: str, colour: str):
        self._state_var.set(text)
        self._state_lbl.configure(fg=colour)

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    #  Folder / file helpers
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    # (folder browsing removed â€” all files saved to ./data/)

    def refresh_file_list(self):
        folder = ensure_data_dir()
        self.csv_files = find_csv_files(str(folder))

        # Apply sort
        col = getattr(self, "_files_sort_col", "modified")
        rev = getattr(self, "_files_sort_rev", True)
        key_map = {
            "filename":       lambda m: m["filename"].lower(),
            "event":          lambda m: m["event"].lower(),
            "name":           lambda m: m["name"].lower(),
            "weapon_type":    lambda m: m["weapon_type"].lower(),
            "peak_force_lbf": lambda m: float(m["peak_force_lbf"]) if m["peak_force_lbf"] else 0.0,
            "rows":           lambda m: int(m["rows"]) if str(m["rows"]).isdigit() else 0,
            "size_kb":        lambda m: float(m["size_kb"]) if m["size_kb"] else 0.0,
            "modified":       lambda m: m["modified"],
        }
        self.csv_files.sort(key=key_map.get(col, key_map["modified"]), reverse=rev)

        self.file_tree.delete(*self.file_tree.get_children())
        for meta in self.csv_files:
            self.file_tree.insert("", "end",
                values=(
                    meta["filename"],
                    meta["event"],
                    meta["name"],
                    meta["weapon_type"],
                    meta["peak_force_lbf"],
                    meta["rows"],
                    meta["size_kb"],
                    meta["modified"],
                ),
                tags=(meta["path"],))

        count = len(self.csv_files)
        label = f"{count} file{'s' if count != 1 else ''}  â€¢  ./data/"
        self.status_var.set(label)
        if hasattr(self, "_files_count_var"):
            self._files_count_var.set(label)

    def _on_file_select(self, _event=None):
        sel = self.file_tree.selection()
        if not sel:
            return
        path = self.file_tree.item(sel[0], "tags")[0]
        self.selected_path = path
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

    # â”€â”€ Data table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    # â”€â”€ Stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _populate_stats(self, headers, rows):
        t = self.stats_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not rows:
            t.insert("end", "No data.\n")
            t.configure(state="disabled")
            return

        # â”€â”€ Metadata block â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if self.selected_path:
            meta = EventMetadata.load(self.selected_path)
            if meta:
                t.insert("end", "  EVENT METADATA\n", "header")
                t.insert("end", "  " + "â”€" * 44 + "\n", "label")
                for label, val in [
                    ("Date / Time",    meta.datetime),
                    ("Event",          meta.event         or "â€”"),
                    ("Name",           meta.name          or "â€”"),
                    ("Weapon Type",    meta.weapon_type   or "â€”"),
                    ("Notes",          meta.notes         or "â€”"),
                    ("Peak Force",     f"{meta.peak_force_lbf:.4f} lbf"),
                    ("Total Energy",   f"{meta.total_energy:.4f}  (reserved)"),
                ]:
                    t.insert("end", f"  {label:<14}", "label")
                    t.insert("end", f"  {val}\n", "value")
                t.insert("end", "\n")

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
            t.insert("end", "  " + "â”€" * 40 + "\n", "label")
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

    # â”€â”€ Chart â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    # â”€â”€ Cleanup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def destroy(self):
        self._engine.disconnect()
        super().destroy()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Entry point
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "."
    app = BridgeHMI(start_folder=start)
    app.mainloop()
