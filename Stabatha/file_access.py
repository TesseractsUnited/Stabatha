"""Strike JSON files and calibration persistence."""

import csv
import json
from datetime import datetime
from pathlib import Path

from constants import CAL_FILE, INDEX_FILE, STRIKE_GLOB, ensure_data_dir
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


_INDEX_FIELDS = (
    "filename", "id", "event", "name", "weapon_type", "kingdom", "rank",
    "notes", "peak_force_lbf", "impulse", "feedback",
)


def _index_path() -> Path:
    return ensure_data_dir() / INDEX_FILE


def iter_strike_paths(folder: Path | None = None) -> list[Path]:
    """Strike JSON files in the data folder, excluding the index file itself."""
    folder_path = Path(folder) if folder is not None else ensure_data_dir()
    return [
        p for p in folder_path.glob(STRIKE_GLOB)
        if p.name != INDEX_FILE
    ]


def _read_strike_metadata(path: Path) -> dict | None:
    """Load only the metadata object from a strike JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    meta = data.get("metadata")
    return meta if isinstance(meta, dict) else None


def index_entry_from_meta(path: Path, meta: dict) -> dict:
    """Build a table/index row from a strike file path and its metadata dict."""
    peak = float(meta.get("peak_force_lbf", 0.0) or 0.0)
    impulse = meta.get("total_energy_lbf_s")
    return {
        "filename": path.name,
        "path": str(path),
        "id": strike_id_from_filename(path),
        "event": meta.get("event", "") or "",
        "name": meta.get("name", "") or "",
        "weapon_type": meta.get("weapon_type", "") or "",
        "kingdom": meta.get("kingdom", "") or "",
        "rank": meta.get("rank", "") or "",
        "peak_force_lbf": f"{peak:.1f}",
        "impulse": f"{float(impulse):.3f}" if impulse else "",
        "notes": meta.get("notes") or "",
        "feedback": format_calibration_feedback(meta.get("user_calibration_feedback")),
    }


def index_entry_from_strike(path: str | Path, strike: StrikeData) -> dict:
    """Build a table/index row from an in-memory StrikeData object."""
    path = Path(path)
    return {
        "filename": path.name,
        "path": str(path),
        "id": strike_id_from_filename(path),
        "event": strike.event or "",
        "name": strike.name or "",
        "weapon_type": strike.weapon_type or "",
        "kingdom": strike.kingdom or "",
        "rank": strike.rank or "",
        "peak_force_lbf": f"{strike.peak_force_lbf:.1f}",
        "impulse": f"{strike.total_energy_lbf_s:.3f}" if strike.total_energy_lbf_s else "",
        "notes": strike.notes or "",
        "feedback": format_calibration_feedback(strike.user_calibration_feedback),
    }


class StrikeIndex:
    """In-memory strike table index, persisted as a single JSON file.

    Table refreshes read this object in RAM. The JSON file is rewritten
    when a strike is added, edited, deleted, or when startup reconcile
    finds the directory and the index are out of sync.
    """

    def __init__(self):
        self.entries: list[dict] = []
        self._by_name: dict[str, dict] = {}

    def load_and_reconcile(self) -> dict:
        """Load index.json, drop missing files, add unindexed strike files.

        Returns counts describing what changed. Strike files are opened
        only when they are present on disk but missing from the index.
        """
        folder = ensure_data_dir()
        disk_names = {p.name for p in iter_strike_paths(folder)}
        loaded = self._load_file()

        kept: list[dict] = []
        for raw in loaded:
            name = raw.get("filename") or Path(str(raw.get("path", ""))).name
            if not name or name == INDEX_FILE or name not in disk_names:
                continue
            path = folder / name
            entry = {field: raw.get(field, "") for field in _INDEX_FIELDS}
            entry["filename"] = name
            entry["path"] = str(path)
            if not entry.get("id"):
                entry["id"] = strike_id_from_filename(path)
            kept.append(entry)

        known = {e["filename"] for e in kept}
        added = 0
        for name in sorted(disk_names - known):
            path = folder / name
            meta = _read_strike_metadata(path)
            if meta is None:
                continue
            kept.append(index_entry_from_meta(path, meta))
            added += 1

        removed = len(loaded) - len(kept) + added
        self._set_entries(kept)
        index_path = _index_path()
        dirty = added > 0 or removed > 0 or not index_path.exists()
        if dirty:
            self.save()
        return {
            "count": len(self.entries),
            "added": added,
            "removed": max(0, removed),
            "saved": dirty,
        }

    def _load_file(self) -> list[dict]:
        path = _index_path()
        if not path.exists():
            return []
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            return []
        strikes = data.get("strikes", [])
        return strikes if isinstance(strikes, list) else []

    def _set_entries(self, entries: list[dict]):
        self.entries = entries
        self._by_name = {e["filename"]: e for e in entries}

    def save(self):
        payload = {
            "schema_version": 1,
            "strikes": [
                {field: entry.get(field, "") for field in _INDEX_FIELDS}
                for entry in self.entries
            ],
        }
        with open(_index_path(), "w") as f:
            json.dump(payload, f, indent=2)

    def upsert(self, entry: dict):
        name = entry["filename"]
        entry = dict(entry)
        entry["path"] = str(ensure_data_dir() / name)
        existing = self._by_name.get(name)
        if existing is not None:
            existing.update(entry)
        else:
            self.entries.append(entry)
            self._by_name[name] = entry
        self.save()

    def upsert_strike(self, path: str, strike: StrikeData):
        self.upsert(index_entry_from_strike(path, strike))

    def upsert_from_file(self, path: str):
        file_path = Path(path)
        meta = _read_strike_metadata(file_path)
        if meta is None:
            return
        self.upsert(index_entry_from_meta(file_path, meta))

    def set_feedback(self, path: str, value: float | int | None):
        name = Path(path).name
        entry = self._by_name.get(name)
        if entry is None:
            self.upsert_from_file(path)
            entry = self._by_name.get(name)
            if entry is None:
                return
        entry["feedback"] = format_calibration_feedback(value)
        self.save()

    def remove(self, path: str):
        name = Path(path).name
        self.entries = [e for e in self.entries if e["filename"] != name]
        self._by_name.pop(name, None)
        self.save()


CSV_EXPORT_COLUMNS = (
    ("Strike ID", "id"),
    ("Event", "event"),
    ("Name", "name"),
    ("Weapon", "weapon_type"),
    ("peak_force_lbf", "peak_force_lbf"),
    ("Impulse", "impulse"),
    ("calibration", "feedback"),
    ("notes", "notes"),
)


def export_strikes_csv(entries: list[dict], path: str) -> int:
    """Write strike-index rows to a CSV file. Returns the number of data rows."""
    rows = sorted(entries, key=lambda e: e.get("id", ""), reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([header for header, _ in CSV_EXPORT_COLUMNS])
        for entry in rows:
            writer.writerow([entry.get(key, "") for _, key in CSV_EXPORT_COLUMNS])
    return len(rows)


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
