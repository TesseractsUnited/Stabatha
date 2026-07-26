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
    for key in ("event", "name", "weapon_type", "kingdom", "rank", "notes",
                "user_calibration_feedback"):
        value = getattr(strike, key)
        if key == "user_calibration_feedback":
            value = normalize_calibration_feedback(value)
        meta[key] = value
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# Cache of already-parsed strike metadata, keyed by file path, so that
# repeated calls to find_strike_files() (e.g. after every single capture)
# don't have to re-read and re-parse every accumulated strike file's full
# JSON -- including its (potentially large, thousands-of-points) samples
# array -- just to redisplay the same few metadata fields in the table.
# Entries are invalidated by (mtime, size) so edits (feedback/metadata) are
# still picked up.
_metadata_cache: dict[str, tuple[float, int, dict]] = {}


def _load_strike_metadata_cached(path: Path) -> dict | None:
    """Return just the "metadata" dict from a strike JSON file -- never
    parses/builds the samples array -- reusing a cached result when the
    file's mtime/size haven't changed since it was last read."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _metadata_cache.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    meta = data.get("metadata", {})
    _metadata_cache[key] = (st.st_mtime, st.st_size, meta)
    return meta


def find_strike_files(folder: str) -> list[dict]:
    folder_path = Path(folder)
    files = []
    paths = list(folder_path.glob(STRIKE_GLOB))
    paths.sort(
        key=lambda p: _strike_datetime_from_filename(p) or datetime.min,
        reverse=True,
    )
    seen = set()
    for path in paths:
        meta = _load_strike_metadata_cached(path)
        if meta is None:
            continue
        seen.add(str(path))
        peak = float(meta.get("peak_force_lbf", 0.0) or 0.0)
        impulse = meta.get("total_energy_lbf_s")
        feedback = meta.get("user_calibration_feedback")
        files.append(
            {
                "path": str(path),
                "id": strike_id_from_filename(path),
                "event": meta.get("event", ""),
                "name": meta.get("name", ""),
                "weapon_type": meta.get("weapon_type", ""),
                "peak_force_lbf": f"{peak:.1f}",
                "impulse": f"{float(impulse):.3f}" if impulse else "",
                "notes": meta.get("notes") or "",
                "feedback": format_calibration_feedback(feedback),
            }
        )
    # Drop cache entries for files that no longer exist (e.g. deleted).
    for stale_path in list(_metadata_cache):
        if stale_path not in seen:
            del _metadata_cache[stale_path]
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


def load_calibration_json_from_path(path: str) -> dict | None:
    """Load raw calibration dict from an arbitrary JSON file path."""
    try:
        with open(path) as f:
            data = json.load(f)
        if "channels" in data:
            data = data["channels"][0]
        return data
    except Exception:
        return None
