"""PhidgetBridge sensor connection, triggering, and capture recording."""

import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from calibration import CalibrationStore
from constants import (
    BASELINE_WINDOW_SECONDS,
    CAL_SAMPLES,
    PHIDGET_BRIDGE_MAX_DATA_RATE_HZ,
    PHIDGET_BRIDGE_MIN_DATA_RATE_HZ,
    POST_TRIGGER_MAX_SECONDS,
    TRIGGER_RESET_HOLD_SECONDS,
)
from file_access import save_strike
from strike_data import StrikeData, StrikeSample

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
    SETTLING = "settling"
    ERROR = "error"

    def __init__(self):
        self.state = self.IDLE
        self.latest: float | None = None
        self.current_strike: StrikeData | None = None
        self.capture_target = 0
        self.error_msg = ""
        self._ch = None
        self._stop_evt = threading.Event()
        self._disarm_evt = threading.Event()
        self._attach_evt = threading.Event()
        self._trigger_evt = threading.Event()
        self._record_done_evt = threading.Event()
        self._recording_active = False
        self._record_start_mono = 0.0
        self._thread = None

        # Rolling (time, raw V/V) history of recent non-recording readings,
        # used to synthesize a settled "zero point" baseline sample right
        # before each trigger. Only a cheap snapshot copy of this buffer is
        # taken at trigger time (on the time-critical Phidget callback
        # thread); the actual averaging happens later in _save_strike(),
        # off that thread, once recording has finished.
        self.baseline_window_seconds = BASELINE_WINDOW_SECONDS
        self._baseline_buffer: deque[tuple[float, float]] = deque()
        self._baseline_snapshot: list[tuple[float, float]] = []
        self._baseline_trigger_dt: datetime | None = None

        self.serial_number = None
        self.data_rate = 1200.0
        self._applied_data_rate = self.data_rate
        self.trigger_threshold = 0.01
        # Post-trigger-only capture: keep recording until the force drops
        # below trigger_threshold or this many seconds have elapsed.
        self.max_record_seconds = POST_TRIGGER_MAX_SECONDS
        # Once re-arming is requested, the force must stay below
        # trigger_threshold continuously for this long before the trigger
        # actually starts watching for the next strike.
        self.reset_hold_seconds = TRIGGER_RESET_HOLD_SECONDS
        self.save_folder = "."
        self.calibration: CalibrationStore | None = None
        self.metadata_provider: Callable[[], dict] | None = None

        self.saved_path = ""
        self.last_strike: StrikeData | None = None
        self.capture_index = 0
        self.last_saved_name = ""

    def connect(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._disarm_evt.clear()
        self._attach_evt.clear()
        self._trigger_evt.clear()
        self._record_done_evt.clear()
        self._recording_active = False
        self._baseline_buffer.clear()
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

    def _run(self):
        try:
            self.state = self.CONNECTING
            self._open_channel()
            while not self._stop_evt.is_set():
                # Check disarm *before* resetting capture state -- otherwise
                # a capture that just finished (which disarms itself below)
                # would have saved_path/last_saved_name wiped by
                # _reset_for_next_capture() before the HMI's poll loop gets
                # a chance to notice the completed capture and show the
                # feedback dialog.
                if self._disarm_evt.is_set():
                    self.state = self.DISARMED
                    self._wait_while_disarmed()
                    if self._stop_evt.is_set():
                        break
                    # Re-arm was requested (e.g. feedback entered) -- confirm
                    # the force has actually settled below the trigger
                    # threshold before watching for the next strike.
                    self.state = self.SETTLING
                    self._wait_for_reset()
                    continue
                self._reset_for_next_capture()
                self.state = self.WAITING
                self._wait_for_trigger()
                if self._stop_evt.is_set():
                    break
                if self._disarm_evt.is_set():
                    continue
                self.state = self.RECORDING
                self._record()
                if self._stop_evt.is_set():
                    break
                self.state = self.SAVING
                self._save_strike()
                self.capture_index += 1
                # Disarm immediately after a capture -- the trigger stays
                # disarmed while the HMI shows the calibration feedback
                # dialog, and re-arms only once the caller explicitly
                # calls arm() (i.e. once feedback has been entered).
                self.disarm()
            self.state = self.IDLE
        except Exception as exc:
            self.error_msg = str(exc)
            self.state = self.ERROR
        finally:
            self._close_channel()

    def _reset_for_next_capture(self):
        self._trigger_evt.clear()
        self._record_done_evt.clear()
        self._recording_active = False
        self.current_strike = None
        self.saved_path = ""
        self.capture_target = max(
            1, int(round(self.max_record_seconds * max(self._applied_data_rate, 1.0)))
        )

    def _begin_strike(self, trigger_value: float):
        """Called from the Phidget callback the instant the trigger
        threshold is crossed. Appends the triggering sample immediately
        so it is not lost while the recorder thread wakes up.

        Only a cheap snapshot copy (a few microseconds, no arithmetic) of
        the pre-trigger baseline buffer is taken here, on this
        time-critical callback thread. The buffer is guaranteed to stay
        untouched for the whole recording (the buffer-feeding branch in
        _on_value_change only runs while NOT recording), so the actual
        averaging is deferred to _save_strike() once recording is done --
        see _insert_baseline_sample()."""
        strike = StrikeData()
        strike.datetime = datetime.now().isoformat(timespec="seconds")
        strike.data_rate_hz = self._applied_data_rate
        strike.pre_trigger_count = 0
        strike.post_trigger_count = 0
        strike.user_calibration_feedback = None

        if self.metadata_provider:
            strike.apply_metadata(**self.metadata_provider())

        trigger_dt = datetime.now()
        self._baseline_snapshot = list(self._baseline_buffer)
        self._baseline_trigger_dt = trigger_dt

        ts = trigger_dt.isoformat(timespec="milliseconds")
        strike.append_sample(ts, trigger_value, pre_trigger=False, calibration=self.calibration)

        self.current_strike = strike

    def _insert_baseline_sample(self, strike: StrikeData):
        """Runs off the time-critical callback thread (called from
        _save_strike(), after recording has finished). Averages the
        pre-trigger baseline snapshot taken by _begin_strike() and
        prepends it as a settled "zero point" sample, timestamped one
        sample period before the trigger -- see _begin_strike() docstring
        for why the heavier averaging arithmetic is done here instead."""
        snapshot = self._baseline_snapshot
        trigger_dt = self._baseline_trigger_dt
        if not snapshot or trigger_dt is None:
            return
        baseline = sum(v for _, v in snapshot) / len(snapshot)
        period = 1.0 / max(self._applied_data_rate, 1.0)
        baseline_ts = (trigger_dt - timedelta(seconds=period)).isoformat(timespec="milliseconds")
        lbf = None
        if self.calibration and self.calibration.is_calibrated:
            lbf = self.calibration.to_lbf(baseline)
        strike.samples.insert(0, StrikeSample(
            timestamp=baseline_ts, pre_trigger=True, ch0_v_per_v=baseline, ch0_lbf=lbf,
        ))
        strike.pre_trigger_count = 1

    def _wait_for_trigger(self):
        """Block until the Phidget callback fires the trigger event.
        Uses Event.wait() so the recorder wakes immediately when the
        threshold is crossed, instead of polling on a fixed interval.
        The short timeout just lets us re-check stop/disarm periodically."""
        while (not self._trigger_evt.is_set()
               and not self._stop_evt.is_set()
               and not self._disarm_evt.is_set()):
            self._trigger_evt.wait(timeout=0.05)

    def _wait_while_disarmed(self):
        while self._disarm_evt.is_set() and not self._stop_evt.is_set():
            time.sleep(0.05)

    def _wait_for_reset(self):
        """Block until the force has stayed below trigger_threshold
        continuously for reset_hold_seconds before re-arming the trigger.
        Returns early (without confirming settle) if disarmed or stopped
        again while waiting -- the caller re-checks those flags."""
        below_since: float | None = None
        while not self._stop_evt.is_set() and not self._disarm_evt.is_set():
            val = self.latest
            now = time.monotonic()
            if val is not None and val < self.trigger_threshold:
                if below_since is None:
                    below_since = now
                elif now - below_since >= self.reset_hold_seconds:
                    return
            else:
                below_since = None
            time.sleep(0.02)

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
        """Runs on the Phidget22 library's own callback thread, once per
        real hardware sample. Recording is handled entirely here (not by
        a separately-timed polling loop on the recorder thread) so every
        saved sample is the actual hardware reading at the actual moment
        it arrived -- no polling jitter/drift, and no handoff delay
        between trigger and the start of recording."""
        self.latest = value
        if self._recording_active:
            strike = self.current_strike
            if strike is None:
                return
            ts = datetime.now().isoformat(timespec="milliseconds")
            strike.append_sample(ts, value, pre_trigger=False, calibration=self.calibration)
            elapsed = time.monotonic() - self._record_start_mono
            if elapsed >= self.max_record_seconds or value < self.trigger_threshold:
                strike.post_trigger_count = len(strike.samples) - strike.pre_trigger_count
                self._recording_active = False
                self._record_done_evt.set()
            return

        if self.state == self.WAITING and not self._trigger_evt.is_set() and value > self.trigger_threshold:
            self._begin_strike(value)
            self._record_start_mono = time.monotonic()
            # Set before the trigger event so the very next callback
            # (which may arrive within microseconds) is already
            # recognised as part of the recording, not dropped while
            # the recorder thread wakes up and updates self.state.
            self._recording_active = True
            self._trigger_evt.set()
            return

        # Not recording and not triggering on this sample -- feed the
        # rolling baseline buffer so a settled "zero point" average is
        # ready the instant the next trigger fires.
        now = time.monotonic()
        buf = self._baseline_buffer
        buf.append((now, value))
        cutoff = now - self.baseline_window_seconds
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _record(self):
        """Post-trigger samples are appended directly by _on_value_change()
        as they arrive. This just waits for that callback to signal that
        recording finished (force dropped below threshold, or
        max_record_seconds elapsed)."""
        strike = self.current_strike
        if strike is None:
            return
        while not self._record_done_evt.is_set() and not self._stop_evt.is_set():
            self._record_done_evt.wait(timeout=0.05)

    def _save_strike(self):
        strike = self.current_strike
        if not strike or not strike.samples:
            return
        self._insert_baseline_sample(strike)
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
