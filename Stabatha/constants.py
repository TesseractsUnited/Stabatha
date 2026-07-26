"""Shared constants, theme colors, and path helpers."""

from pathlib import Path

# Theme
BG = "#d9d1c7" #background Color light orange/intended to look like parchment
BG2 = "#ede5da" #background Color 2 textboxes, light parchment
ACCENT = "#d17f13" #accent Color light orange
TEXT = "#0a0a0a" #text Color black
# = "#5a5a5a" #text Color Dim gray
GREEN = "#4ade80" #green Color
YELLOW = "#fbbf24" #yellow Color
RED = "#f87171" #red Color
ORANGE = "#fb923c" #orange Color
#CH_COL = "#7c6af7" #chat Color

STRIKE_GLOB = "strike_*.json"
CAL_FILE = "calibration.json"
DATA_DIR = Path(__file__).parent / "data"
CAL_SAMPLES = 50
STRIKE_SCHEMA_VERSION = 1

# PhidgetBridge 1046_1 sample rate limits (Hz)
PHIDGET_BRIDGE_MIN_DATA_RATE_HZ = 1
PHIDGET_BRIDGE_MAX_DATA_RATE_HZ = 1200

# Post-trigger-only capture: record until force drops below the trigger
# threshold, or this many seconds have elapsed since the trigger fired.
POST_TRIGGER_MAX_SECONDS = 3.0
# After re-arming is requested (e.g. once feedback has been entered), the
# trigger will not actually start watching for the next strike until the
# force has stayed below the trigger threshold continuously for this long.
TRIGGER_RESET_HOLD_SECONDS = 3.0

STRIKE_META_FIELDS = [
    "datetime",
    "event",
    "name",
    "weapon_type",
    "kingdom",
    "rank",
    "notes",
    "user_calibration_feedback",
    "peak_force_lbf",
    "total_energy_lbf_s",
    "data_rate_hz",
    "pre_trigger_count",
    "post_trigger_count",
    "sample_count",
]


def ensure_data_dir() -> Path:
    """Create ./data/ beside this package if missing; return its Path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
