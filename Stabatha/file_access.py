"""Strike JSON files and calibration persistence."""

import json
from datetime import datetime
from pathlib import Path

from constants import CAL_FILE, STRIKE_GLOB, ensure_data_dir
from strike_data import StrikeData


def save_strike(strike: StrikeData, save_folder: str) -> str:
    """Write strike to strike_YYYYMMDD_HHMMSS.json; return file path."""
    fname = f"strike_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path = Path(save_folder) / fname
    with open(path, "w") as f:
        json.dump(strike.to_dict(), f, indent=2)
    return str(path)


def load_strike(path: str) -> StrikeData:
    with open(path) as f:
        return StrikeData.from_dict(json.load(f))


def normalize_calibration_feedback(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def format_calibration_feedback(value: float | int | None) -> str:
    normalized = normalize_calibration_feedback(value)
    if normalized is None:
        return ""
    return f"{normalized:.1f}"


def update_strike_feedback(path: str, value: float | int | None):
    """Update user_calibration_feedback in a saved strike JSON file."""
    with open(path) as f:
        data = json.load(f)
    data.setdefault("metadata", {})["user_calibration_feedback"] = normalize_calibration_feedback(value)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def strike_id_from_filename(path: Path) -> str:
    try:
        dt = datetime.strptime(path.stem[7:], "%Y%m%d_%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def _strike_datetime_from_filename(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem[7:], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def update_strike_metadata(path: str, strike: StrikeData):
    """Rewrite editable metadata fields on a saved strike file."""
    with open(path) as f:
        data = json.load(f)
    meta = data.setdefault("metadata", {})
    for key in ("event", "name", "weapon_type", "notes", "user_calibration_feedback"):
        value = getattr(strike, key)
        if key == "user_calibration_feedback":
            value = normalize_calibration_feedback(value)
        meta[key] = value
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def find_strike_files(folder: str) -> list[dict]:
    folder_path = Path(folder)
    files = []
    paths = list(folder_path.glob(STRIKE_GLOB))
    paths.sort(
        key=lambda p: _strike_datetime_from_filename(p) or datetime.min,
        reverse=True,
    )
    for path in paths:
        try:
            strike = load_strike(str(path))
        except Exception:
            continue
        impulse = strike.total_energy_lbf_s
        feedback = strike.user_calibration_feedback
        files.append(
            {
                "path": str(path),
                "id": strike_id_from_filename(path),
                "event": strike.event,
                "name": strike.name,
                "weapon_type": strike.weapon_type,
                "peak_force_lbf": f"{strike.peak_force_lbf:.3f}",
                "impulse": f"{impulse:.4f}" if impulse else "",
                "notes": strike.notes or "",
                "feedback": format_calibration_feedback(feedback),
            }
        )
    return files


def strike_to_tabular(strike: StrikeData) -> tuple[list[str], list[list]]:
    headers = ["timestamp", "pre_trigger", "ch0_V_per_V", "ch0_lbf"]
    rows = []
    for s in strike.samples:
        rows.append([
            s.timestamp,
            "1" if s.pre_trigger else "0",
            f"{s.ch0_v_per_v:.8f}" if s.ch0_v_per_v is not None else "",
            f"{s.ch0_lbf:.6f}" if s.ch0_lbf is not None else "",
        ])
    return headers, rows


def load_calibration_json(folder: str | None = None) -> dict | None:
    """Load raw calibration dict from calibration.json, or None if missing."""
    candidates = [ensure_data_dir() / CAL_FILE]
    if folder:
        candidates.append(Path(folder) / CAL_FILE)
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if "channels" in data:
                data = data["channels"][0]
            return data
        except Exception:
            continue
    return None


def save_calibration_json(cal_data: dict) -> str:
    """Persist calibration dict; return saved file path."""
    path = ensure_data_dir() / CAL_FILE
    with open(path, "w") as f:
        json.dump(cal_data, f, indent=2)
    return str(path)
