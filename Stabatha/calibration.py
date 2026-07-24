"""Load-cell calibration: V/V to lbf conversion and persistence."""

from datetime import datetime

from file_access import load_calibration_json, save_calibration_json


class CalibrationStore:
    """
    Single-channel calibration.
    lbf = (raw_V_per_V - zero_offset) * scale_factor
    """

    def __init__(self):
        self._d = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {
            "zero_offset": 0.0,
            "scale_factor": 1.0,
            "cal_load_lbf": 0.0,
            "zero_raw": None,
            "cal_raw": None,
            "calibrated": False,
            "timestamp": "",
        }

    def to_lbf(self, raw: float) -> float:
        return (raw - self._d["zero_offset"]) * self._d["scale_factor"]

    @property
    def is_calibrated(self) -> bool:
        return bool(self._d.get("calibrated", False))

    def set_zero(self, raw_avg: float):
        self._d["zero_offset"] = raw_avg
        self._d["zero_raw"] = raw_avg
        self._d["calibrated"] = False
        self._d["timestamp"] = datetime.now().isoformat(timespec="seconds")

    def set_cal_point(self, raw_avg: float, load_lbf: float):
        span = raw_avg - self._d["zero_offset"]
        if abs(span) < 1e-12:
            raise ValueError("Cal point too close to zero — apply a larger load.")
        self._d["cal_raw"] = raw_avg
        self._d["cal_load_lbf"] = load_lbf
        self._d["scale_factor"] = load_lbf / span
        self._d["calibrated"] = True
        self._d["timestamp"] = datetime.now().isoformat(timespec="seconds")

    def save(self, folder: str = None) -> str:
        return save_calibration_json(self._d)

    def load(self, folder: str = None) -> bool:
        data = load_calibration_json(folder)
        if data is None:
            return False
        self._d.update(data)
        return True

    def reset(self):
        self._d = self._blank()

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
