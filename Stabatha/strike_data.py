"""Unified strike capture: metadata + time-series in one object."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def _parse_ts_seconds(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


@dataclass
class StrikeSample:
    timestamp: str
    pre_trigger: bool
    ch0_v_per_v: float | None
    ch0_lbf: float | None = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "pre_trigger": self.pre_trigger,
            "ch0_v_per_v": self.ch0_v_per_v,
            "ch0_lbf": self.ch0_lbf,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StrikeSample":
        return cls(
            timestamp=data.get("timestamp", ""),
            pre_trigger=bool(data.get("pre_trigger", False)),
            ch0_v_per_v=data.get("ch0_v_per_v"),
            ch0_lbf=data.get("ch0_lbf"),
        )


@dataclass
class StrikeData:
    datetime: str = ""
    event: str = ""
    name: str = ""
    weapon_type: str = ""
    notes: str = ""
    user_calibration_feedback: float | None = None
    peak_force_lbf: float = 0.0
    total_energy_lbf_s: float = 0.0
    data_rate_hz: float = 0.0
    pre_trigger_count: int = 0
    post_trigger_count: int = 0
    samples: list[StrikeSample] = field(default_factory=list)

    def apply_metadata(
        self,
        *,
        event: str = "",
        name: str = "",
        weapon_type: str = "",
        notes: str = "",
    ):
        self.event = event.strip()
        self.name = name.strip()
        self.weapon_type = weapon_type.strip()
        self.notes = notes.strip()

    def append_sample(
        self,
        ts: str,
        raw_vv: float | None,
        pre_trigger: bool,
        calibration,
    ):
        lbf = None
        if calibration and calibration.is_calibrated and raw_vv is not None:
            lbf = calibration.to_lbf(raw_vv)
        self.samples.append(
            StrikeSample(
                timestamp=ts,
                pre_trigger=pre_trigger,
                ch0_v_per_v=raw_vv,
                ch0_lbf=lbf,
            )
        )

    def finalize(self):
        lbf_values = [s.ch0_lbf for s in self.samples if s.ch0_lbf is not None]
        if not lbf_values:
            self.peak_force_lbf = 0.0
            self.total_energy_lbf_s = 0.0
            return

        self.peak_force_lbf = round(max(lbf_values), 6)
        self.total_energy_lbf_s = round(self._compute_impulse(), 6)

    def _compute_impulse(self) -> float:
        impulse = 0.0
        prev: StrikeSample | None = None
        for sample in self.samples:
            if prev is None:
                prev = sample
                continue
            if prev.ch0_lbf is None or sample.ch0_lbf is None:
                prev = sample
                continue
            try:
                dt = _parse_ts_seconds(sample.timestamp) - _parse_ts_seconds(prev.timestamp)
            except ValueError:
                prev = sample
                continue
            if dt <= 0:
                prev = sample
                continue
            impulse += 0.5 * (prev.ch0_lbf + sample.ch0_lbf) * dt
            prev = sample
        return impulse

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "metadata": {
                "datetime": self.datetime,
                "event": self.event,
                "name": self.name,
                "weapon_type": self.weapon_type,
                "notes": self.notes,
                "user_calibration_feedback": self.user_calibration_feedback,
                "peak_force_lbf": self.peak_force_lbf,
                "total_energy_lbf_s": self.total_energy_lbf_s,
                "data_rate_hz": self.data_rate_hz,
                "pre_trigger_count": self.pre_trigger_count,
                "post_trigger_count": self.post_trigger_count,
                "sample_count": self.sample_count,
            },
            "samples": [s.to_dict() for s in self.samples],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StrikeData":
        meta = data.get("metadata", {})
        raw_feedback = meta.get("user_calibration_feedback")
        feedback = None if raw_feedback is None else round(float(raw_feedback), 1)
        strike = cls(
            datetime=meta.get("datetime", ""),
            event=meta.get("event", ""),
            name=meta.get("name", ""),
            weapon_type=meta.get("weapon_type", ""),
            notes=meta.get("notes", ""),
            user_calibration_feedback=feedback,
            peak_force_lbf=float(meta.get("peak_force_lbf", 0.0) or 0.0),
            total_energy_lbf_s=float(meta.get("total_energy_lbf_s", 0.0) or 0.0),
            data_rate_hz=float(meta.get("data_rate_hz", 0.0) or 0.0),
            pre_trigger_count=int(meta.get("pre_trigger_count", 0) or 0),
            post_trigger_count=int(meta.get("post_trigger_count", 0) or 0),
            samples=[StrikeSample.from_dict(s) for s in data.get("samples", [])],
        )
        return strike
