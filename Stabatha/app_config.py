"""Load and save runtime settings from config.json beside this package."""

import copy
import json
from pathlib import Path

from constants import (
    ACCENT,
    BASELINE_WINDOW_SECONDS,
    BG,
    BG2,
    CAL_SAMPLES,
    GREEN,
    ORANGE,
    PHIDGET_BRIDGE_MAX_DATA_RATE_HZ,
    PHIDGET_BRIDGE_MIN_DATA_RATE_HZ,
    POST_TRIGGER_MAX_SECONDS,
    RED,
    TEXT,
    TRIGGER_RESET_HOLD_SECONDS,
    YELLOW,
)

CONFIG_FILE = "config.json"
CONFIG_PATH = Path(__file__).parent / CONFIG_FILE
PACKAGE_DIR = Path(__file__).parent

DEFAULTS = {
    "data_rate_hz": 1200,
    "trigger_threshold_lbf": 1.0,
    "max_record_seconds": POST_TRIGGER_MAX_SECONDS,
    "reset_hold_seconds": TRIGGER_RESET_HOLD_SECONDS,
    "baseline_window_seconds": BASELINE_WINDOW_SECONDS,
    "cal_samples": CAL_SAMPLES,
    "cal_known_load_lbf": 8.465,
    "phidget_serial": "",
    "data_dir": "data",
    "window": {
        "maximize": True,
        "width": 1100,
        "height": 720,
        "min_width": 860,
        "min_height": 540,
    },
    "colors": {
        "bg": BG,
        "bg2": BG2,
        "accent": ACCENT,
        "text": TEXT,
        "green": GREEN,
        "yellow": YELLOW,
        "red": RED,
        "orange": ORANGE,
    },
}

_COLOR_ATTRS = {
    "bg": "BG",
    "bg2": "BG2",
    "accent": "ACCENT",
    "text": "TEXT",
    "green": "GREEN",
    "yellow": "YELLOW",
    "red": "RED",
    "orange": "ORANGE",
}


def _merge(defaults, data):
    out = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        if key not in out or value is None:
            continue
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _clamp(cfg: dict) -> dict:
    rate = int(cfg["data_rate_hz"])
    cfg["data_rate_hz"] = max(
        PHIDGET_BRIDGE_MIN_DATA_RATE_HZ,
        min(rate, PHIDGET_BRIDGE_MAX_DATA_RATE_HZ),
    )
    cfg["trigger_threshold_lbf"] = max(0.0, float(cfg["trigger_threshold_lbf"]))
    cfg["max_record_seconds"] = max(0.1, float(cfg["max_record_seconds"]))
    cfg["reset_hold_seconds"] = max(0.0, float(cfg["reset_hold_seconds"]))
    cfg["baseline_window_seconds"] = max(0.1, float(cfg["baseline_window_seconds"]))
    cfg["cal_samples"] = max(1, int(cfg["cal_samples"]))
    cfg["cal_known_load_lbf"] = float(cfg["cal_known_load_lbf"])
    cfg["phidget_serial"] = str(cfg.get("phidget_serial") or "").strip()
    cfg["data_dir"] = str(cfg.get("data_dir") or "data").strip() or "data"

    win = cfg["window"]
    win["maximize"] = bool(win.get("maximize", True))
    win["width"] = max(400, int(win.get("width", 1100)))
    win["height"] = max(300, int(win.get("height", 720)))
    win["min_width"] = max(200, int(win.get("min_width", 860)))
    win["min_height"] = max(200, int(win.get("min_height", 540)))

    colors = cfg["colors"]
    for key, default in DEFAULTS["colors"].items():
        value = str(colors.get(key, default) or default).strip()
        colors[key] = value if value.startswith("#") else default
    return cfg


def resolve_data_dir(cfg: dict) -> Path:
    path = Path(str(cfg.get("data_dir") or "data")).expanduser()
    if not path.is_absolute():
        path = (PACKAGE_DIR / path).resolve()
    return path


def apply_to_runtime(cfg: dict):
    """Push theme colors and data folder into the constants module."""
    import constants

    for key, attr in _COLOR_ATTRS.items():
        setattr(constants, attr, cfg["colors"][key])
    constants.DATA_DIR = resolve_data_dir(cfg)


def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments so json.loads can parse the config file."""
    out = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _render_config(cfg: dict) -> str:
    """Write config.json with // comments describing each setting."""
    c = _clamp(_merge(DEFAULTS, cfg if isinstance(cfg, dict) else {}))
    w = c["window"]
    col = c["colors"]
    return (
        "{\n"
        "  // Sample rate requested from the PhidgetBridge, in Hz.\n"
        "  // Hardware is limited to 1-1200 Hz on the 1046; the program clamps to that range.\n"
        f"  \"data_rate_hz\": {int(c['data_rate_hz'])},\n"
        "\n"
        "  // Force level (pounds-force) that starts a capture when crossed on the way up.\n"
        f"  \"trigger_threshold_lbf\": {float(c['trigger_threshold_lbf'])},\n"
        "\n"
        "  // Longest a capture will run after trigger, in seconds.\n"
        "  // Recording also stops early once force drops back below the trigger threshold.\n"
        f"  \"max_record_seconds\": {float(c['max_record_seconds'])},\n"
        "\n"
        "  // After feedback is entered, force must stay below the trigger this many seconds\n"
        "  // before the trigger re-arms for the next strike.\n"
        f"  \"reset_hold_seconds\": {float(c['reset_hold_seconds'])},\n"
        "\n"
        "  // Seconds of pre-trigger readings averaged into the synthetic \"zero point\"\n"
        "  // sample prepended to each capture.\n"
        f"  \"baseline_window_seconds\": {float(c['baseline_window_seconds'])},\n"
        "\n"
        "  // How many live samples are averaged when capturing zero or a cal point.\n"
        f"  \"cal_samples\": {int(c['cal_samples'])},\n"
        "\n"
        "  // Default known-load value (lbf) shown on the Calibrate tab.\n"
        f"  \"cal_known_load_lbf\": {float(c['cal_known_load_lbf'])},\n"
        "\n"
        "  // PhidgetBridge serial number. Leave \"\" to auto-detect the first attached device.\n"
        f"  \"phidget_serial\": {json.dumps(c['phidget_serial'])},\n"
        "\n"
        "  // Folder for strike files, strike_index.json, and calibration.json.\n"
        "  // A relative path is resolved from the Stabatha program folder.\n"
        f"  \"data_dir\": {json.dumps(c['data_dir'])},\n"
        "\n"
        "  \"window\": {\n"
        "    // true = start maximized. false = use width/height below.\n"
        f"    \"maximize\": {json.dumps(bool(w['maximize']))},\n"
        "    // Window size in pixels when maximize is false.\n"
        f"    \"width\": {int(w['width'])},\n"
        f"    \"height\": {int(w['height'])},\n"
        "    // Smallest size the user can resize the window to.\n"
        f"    \"min_width\": {int(w['min_width'])},\n"
        f"    \"min_height\": {int(w['min_height'])}\n"
        "  },\n"
        "\n"
        "  // Hex colors for the parchment theme. Restart the program after changing these.\n"
        "  \"colors\": {\n"
        "    // Main window background (parchment).\n"
        f"    \"bg\": {json.dumps(col['bg'])},\n"
        "    // Text boxes, tables, and other inset panels.\n"
        f"    \"bg2\": {json.dumps(col['bg2'])},\n"
        "    // Buttons, selected tabs, and highlights.\n"
        f"    \"accent\": {json.dumps(col['accent'])},\n"
        "    // Normal label and body text.\n"
        f"    \"text\": {json.dumps(col['text'])},\n"
        "    // Success / recording / calibrated status.\n"
        f"    \"green\": {json.dumps(col['green'])},\n"
        "    // Waiting / settling / warning status.\n"
        f"    \"yellow\": {json.dumps(col['yellow'])},\n"
        "    // Errors and not-calibrated status.\n"
        f"    \"red\": {json.dumps(col['red'])},\n"
        "    // Live lbf readout and chart trace.\n"
        f"    \"orange\": {json.dumps(col['orange'])}\n"
        "  }\n"
        "}\n"
    )


def load_config() -> dict:
    """Read config.json, filling any missing keys from defaults.

    If the file is missing or unreadable, write a default file and return
    the defaults. Always applies theme and data_dir to runtime constants.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            data = json.loads(_strip_json_comments(raw))
            cfg = _clamp(_merge(DEFAULTS, data))
        except Exception:
            cfg = _clamp(copy.deepcopy(DEFAULTS))
    else:
        cfg = _clamp(cfg)
        save_config(cfg)
    apply_to_runtime(cfg)
    return cfg


def save_config(cfg: dict) -> str:
    """Write config.json with comments preserved. Returns the file path."""
    CONFIG_PATH.write_text(_render_config(cfg), encoding="utf-8")
    return str(CONFIG_PATH)
