"""PhidgetBridge sensor connection, triggering, and capture recording."""

import collections
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from calibration import CalibrationStore
from constants import (
    CAL_SAMPLES,
    PHIDGET_BRIDGE_MAX_DATA_RATE_HZ,
    PHIDGET_BRIDGE_MIN_DATA_RATE_HZ,
)
from file_access import save_strike
from strike_data import StrikeData

try:
    from Phidget22.Devices.VoltageRatioInput import VoltageRatioInput
    HAS_PHIDGET = True
except ImportError:
    HAS_PHIDGET = False


def clamp_data_rate(rate, channel=None) -> float:
    """Clamp requested sample rate to app and device limits (Hz)."""
    rate = float(max(rate, PHIDGET_BRIDGE_MIN_DATA_RATE_HZ))
    min_rate = PHIDGET_BRIDGE_MIN_DATA_RATE_HZ
    max_rate = PHIDGET_BRIDGE_MAX_DATA_RATE_HZ
    if channel is not None:
        if hasattr(channel, "getMinDataRate"):
            min_rate = channel.getMinDataRate()
        if hasattr(channel, "getMaxDataRate"):
            max_rate = channel.getMaxDataRate()
    return min(max(rate, min_rate), max_rate)


class RecorderEngine:
    IDLE = "idle"
    CONNECTING = "connecting"
    WAITING = "waiting"
    RECORDING = "recording"
    SAVING = "saving"
    DISARMED = "disarmed"
    ERROR = "error"

    def __init__(self):
        self.state = self.IDLE
        self.latest: float | None = None
        self.current_strike: StrikeData | None = None
        self.capture_target = 110
        self.error_msg = ""
        self._ch = None
        self._stop_evt = threading.Event()
        self._disarm_evt = threading.Event()
        self._attach_evt = threading.Event()
        self._thread = None

        self.serial_number = None
        self.data_rate = 1200.0
        self._applied_data_rate = self.data_rate
        self.trigger_threshold = 0.01
        self.num_points = 100
        self.pre_trigger_buffer_size = 5
        self.save_folder = "."
        self.calibration: CalibrationStore | None = None
        self.metadata_provider: Callable[[], dict] | None = None

        self._triggered = False
        self.saved_path = ""
        self._pre_buffer: collections.deque = collections.deque()
        self.last_strike: StrikeData | None = None
        self.capture_index = 0
        self.last_saved_name = ""

    def connect(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._disarm_evt.clear()
        self._attach_evt.clear()
        self.error_msg = ""
        self.latest = None
        self.capture_index = 0
        self.last_saved_name = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop_evt.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self.state = self.IDLE
        self.latest = None

    def disarm(self):
        self._disarm_evt.set()

    def arm(self):
        self._disarm_evt.clear()

    @property
    def capture_count(self):
        if self.current_strike is None:
            return 0
        return len(self.current_strike.samples)

    def _sample_period_s(self) -> float:
        return 1.0 / max(self._applied_data_rate, PHIDGET_BRIDGE_MIN_DATA_RATE_HZ)

    def _run(self):
        try:
            self.state = self.CONNECTING
            self._open_channel()
            while not self._stop_evt.is_set():
                self._reset_for_next_capture()
                self.state = self.WAITING
                self._wait_for_trigger()
                if self._stop_evt.is_set():
                    break
                if self._disarm_evt.is_set():
                    self.state = self.DISARMED
                    self._wait_while_disarmed()
                    continue
                self.state = self.RECORDING
                self._record()
                if self._stop_evt.is_set():
                    break
                self.state = self.SAVING
                self._save_strike()
                self.capture_index += 1
                time.sleep(0.15)
            self.state = self.IDLE
        except Exception as exc:
            self.error_msg = str(exc)
            self.state = self.ERROR
        finally:
            self._close_channel()

    def _reset_for_next_capture(self):
        self._triggered = False
        self.current_strike = None
        self.saved_path = ""
        self.capture_target = self.pre_trigger_buffer_size + self.num_points
        self._pre_buffer = collections.deque(maxlen=self.pre_trigger_buffer_size)

    def _begin_strike(self):
        strike = StrikeData()
        strike.datetime = datetime.now().isoformat(timespec="seconds")
        strike.data_rate_hz = self._applied_data_rate
        strike.pre_trigger_count = self.pre_trigger_buffer_size
        strike.post_trigger_count = self.num_points
        strike.user_calibration_feedback = None

        if self.metadata_provider:
            strike.apply_metadata(**self.metadata_provider())

        for ts, value in self._pre_buffer:
            strike.append_sample(ts, value, pre_trigger=True, calibration=self.calibration)

        self.current_strike = strike

    def _wait_for_trigger(self):
        while not self._triggered and not self._stop_evt.is_set() and not self._disarm_evt.is_set():
            time.sleep(0.02)

    def _wait_while_disarmed(self):
        while self._disarm_evt.is_set() and not self._stop_evt.is_set():
            time.sleep(0.05)

    def _open_channel(self):
        if not HAS_PHIDGET:
            raise RuntimeError("Phidget22 not installed.")
        self._attach_evt = threading.Event()
        ch = VoltageRatioInput()
        ch.setChannel(0)
        if self.serial_number:
            ch.setDeviceSerialNumber(int(self.serial_number))
        ch.setOnAttachHandler(self._on_attach)
        ch.setOnDetachHandler(lambda c: None)
        ch.setOnErrorHandler(self._on_error)
        ch.setOnVoltageRatioChangeHandler(self._on_value_change)
        ch.open()
        self._ch = ch
        for _ in range(200):
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
        self._applied_data_rate = clamp_data_rate(self.data_rate, ch)
        ch.setDataRate(self._applied_data_rate)
        self._attach_evt.set()

    def _on_error(self, ch, code, desc):
        self.error_msg = f"CH0 error [{code}]: {desc}"

    def _on_value_change(self, ch, value):
        self.latest = value
        if self.state == self.WAITING and not self._triggered:
            ts = datetime.now().isoformat(timespec="milliseconds")
            self._pre_buffer.append((ts, value))
            if value > self.trigger_threshold:
                self._begin_strike()
                self._triggered = True

    def _record(self):
        period = self._sample_period_s()
        strike = self.current_strike
        if strike is None:
            return
        for _ in range(self.num_points):
            if self._stop_evt.is_set():
                break
            ts = datetime.now().isoformat(timespec="milliseconds")
            val = self.latest
            strike.append_sample(ts, val, pre_trigger=False, calibration=self.calibration)
            time.sleep(period)

    def _save_strike(self):
        strike = self.current_strike
        if not strike or not strike.samples:
            return
        strike.finalize()
        path = save_strike(strike, self.save_folder)
        self.saved_path = path
        self.last_saved_name = Path(path).name
        self.last_strike = strike


class CalSampler:
    """Average N live readings from channel 0."""

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"

    def __init__(self, engine: RecorderEngine):
        self._engine = engine
        self.state = self.IDLE
        self.result: float | None = None
        self.error_msg = ""

    def start(self, n: int = CAL_SAMPLES):
        self._n = n
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
            self.state = self.DONE
        except Exception as exc:
            self.error_msg = str(exc)
            self.state = self.ERROR
