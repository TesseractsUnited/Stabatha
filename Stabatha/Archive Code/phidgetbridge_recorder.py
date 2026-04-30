"""
PhidgetBridge 4-Input Sensor Data Recorder
-------------------------------------------
Waits for a configurable trigger condition on any channel before recording
100 data points across all active channels.

Requirements:
    pip install Phidget22

Hardware:
    Phidget PhidgetBridge 4-Input (e.g. 1046, HIN1101)
"""

import time
import sys
from datetime import datetime
from Phidget22.Phidget import *
from Phidget22.Devices.VoltageRatioInput import *

# ── Configuration ────────────────────────────────────────────────────────────

NUM_CHANNELS     = 1          # PhidgetBridge has 4 inputs (0-3)
NUM_POINTS       = 100        # Data points to capture after trigger
DATA_INTERVAL_MS = 8         # Polling interval in milliseconds
TRIGGER_CHANNEL  = 0          # Which channel to watch for the trigger
TRIGGER_THRESHOLD = 0.0001      # Trigger fires when |value| exceeds this (V/V)
TRIGGER_DIRECTION = "either"  # "rising", "falling", or "either"

# Set to None to auto-detect, or specify e.g. 545098 to target a device
SERIAL_NUMBER    = None

# ── Global State ─────────────────────────────────────────────────────────────

latest_values = [None] * NUM_CHANNELS   # Most recent reading per channel
channels      = []                       # Phidget channel handles
triggered     = False
recording     = False
recorded_data = []                       # List of (timestamp, [ch0..ch3]) tuples


# ── Phidget Callbacks ─────────────────────────────────────────────────────────

def on_voltage_ratio_change(self, voltageRatio):
    """Called by the Phidget library whenever a channel value changes."""
    global triggered, recording

    ch_index = self.getChannel()
    latest_values[ch_index] = voltageRatio

    # ── Trigger detection (on the designated trigger channel) ────────────────
    if not triggered and ch_index == TRIGGER_CHANNEL:
        fired = False
        if TRIGGER_DIRECTION in ("rising", "either") and voltageRatio >  TRIGGER_THRESHOLD:
            fired = True
        if TRIGGER_DIRECTION in ("falling", "either") and voltageRatio < -TRIGGER_THRESHOLD:
            fired = True

        if fired:
            triggered = True
            print(f"\n[TRIGGER] Channel {TRIGGER_CHANNEL} = {voltageRatio:.6f} V/V "
                  f"— starting capture of {NUM_POINTS} points …\n")


def on_attach(self):
    ch = self.getChannel()
    print(f"  ✓ Channel {ch} attached  (serial {self.getDeviceSerialNumber()})")
    self.setDataInterval(DATA_INTERVAL_MS)


def on_detach(self):
    print(f"  ✗ Channel {self.getChannel()} detached")


def on_error(self, code, description):
    print(f"  ! Error on channel {self.getChannel()}: [{code}] {description}")


# ── Setup / Teardown ─────────────────────────────────────────────────────────

def open_channels():
    """Open all four VoltageRatioInput channels on the PhidgetBridge."""
    print(f"Opening {NUM_CHANNELS} PhidgetBridge channels …")
    for i in range(NUM_CHANNELS):
        ch = VoltageRatioInput()
        ch.setChannel(i)
        if SERIAL_NUMBER is not None:
            ch.setDeviceSerialNumber(SERIAL_NUMBER)

        ch.setOnAttachHandler(on_attach)
        ch.setOnDetachHandler(on_detach)
        ch.setOnErrorHandler(on_error)
        ch.setOnVoltageRatioChangeHandler(on_voltage_ratio_change)

        ch.openWaitForAttachment(5000)   # 5-second timeout
        channels.append(ch)

    print()


def close_channels():
    for ch in channels:
        try:
            ch.close()
        except Exception:
            pass


# ── Main Recording Loop ───────────────────────────────────────────────────────

def wait_for_trigger():
    """Block until the trigger condition is satisfied."""
    print(f"Waiting for trigger on channel {TRIGGER_CHANNEL} "
          f"(|value| > {TRIGGER_THRESHOLD} V/V, direction='{TRIGGER_DIRECTION}') …")
    print("Press Ctrl+C to abort.\n")
    while not triggered:
        # Print a live readout so the user can see current values
        vals = " | ".join(
            f"CH{i}: {v:+.5f}" if v is not None else f"CH{i}: ---"
            for i, v in enumerate(latest_values)
        )
        print(f"\r  {vals}  ", end="", flush=True)
        time.sleep(0.1)


def record_data():
    """Collect exactly NUM_POINTS samples at DATA_INTERVAL_MS spacing."""
    global recorded_data
    print(f"Recording {NUM_POINTS} samples at {1000 // DATA_INTERVAL_MS} Hz …")

    for point in range(NUM_POINTS):
        snapshot = list(latest_values)           # atomic snapshot
        ts = datetime.now().isoformat(timespec="milliseconds")
        recorded_data.append((ts, snapshot))
        print(f"  [{point+1:>3}/{NUM_POINTS}]  {ts}  "
              + "  ".join(
                  f"CH{i}: {v:+.6f}" if v is not None else f"CH{i}:   None  "
                  for i, v in enumerate(snapshot)
              ))
        time.sleep(DATA_INTERVAL_MS / 1000.0)

    print(f"\nCapture complete — {len(recorded_data)} points recorded.")


def save_csv(filename: str = None):
    """Write recorded data to a CSV file."""
    if not recorded_data:
        return

    if filename is None:
        filename = f"bridge_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    headers = ["timestamp"] + [f"ch{i}_V_per_V" for i in range(NUM_CHANNELS)]
    with open(filename, "w") as f:
        f.write(",".join(headers) + "\n")
        for ts, vals in recorded_data:
            row = [ts] + [f"{v:.8f}" if v is not None else "" for v in vals]
            f.write(",".join(row) + "\n")

    print(f"Data saved → {filename}")


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    try:
        open_channels()
        wait_for_trigger()
        record_data()
        save_csv()

    except PhidgetException as e:
        print(f"\nPhidget error: [{e.code}] {e.details}", file=sys.stderr)
        sys.exit(1)

    except KeyboardInterrupt:
        print("\nAborted by user.")

    finally:
        close_channels()
        print("Channels closed.")


if __name__ == "__main__":
    main()
