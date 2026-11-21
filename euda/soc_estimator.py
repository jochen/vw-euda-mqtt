"""Live-SOC estimation from sparse, delayed VW readings plus evcc energy.

Why this exists (incident 2026-11-18):

* The EU-Data-Act portal ships a dataset every ~15 min, but each dataset is an
  unordered merge of the car's *last* reports. ``battery_state_report.soc``
  only changes when the car actually sends a new battery report; in between,
  every dataset repeats the old value (e.g. 44 % for four hours).
* The old service re-anchored on *every* new dataset — "VW says 44 %, energy
  now" — so a repeated old value during charging threw ``soc_live`` back to
  the pre-charge level. evcc's limitSoc became useless.
* When the portal stopped delivering altogether, the last value was used
  forever (76 % from 16.11. while the car really had 44 %).

The model here:

* A VW reading only counts when its value *changes* (or it is the first one).
  Its measurement time is taken from the dataset (car capture time, bounded by
  the dataset's creation time).
* evcc's per-session ``chargedEnergy`` is folded into a monotonic cumulative
  counter with a timestamped history, so we can ask "how much energy went in
  since the VW reading was measured?" — even if the reading arrives late.
* ``soc_live = vw_soc + (E(now) - E(t_reading)) / capacity * efficiency``.
* If the portal has not delivered successfully for ``stale_after`` seconds,
  the estimate is withheld (``None``) so evcc's MQTT timeout kicks in instead
  of charging against a frozen number.
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

HISTORY_KEEP_S = 48 * 3600
# Below this, 1 % SOC rounding dominates the efficiency estimate.
MIN_CALIBRATION_WH = 3000.0
MAX_CALIBRATIONS = 30


@dataclass
class EnergyHistory:
    """Monotonic cumulative charged energy (Wh) with timestamps (epoch s)."""

    times: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    _offset: float = 0.0           # sum of finished sessions
    _last_session: Optional[float] = None

    def add_session_value(self, ts: float, session_wh: float) -> None:
        """Feed evcc's per-session chargedEnergy; detects session resets."""
        if self._last_session is not None and session_wh < self._last_session:
            # New session (or reset to 0 on unplug): bank the old one.
            self._offset += self._last_session
        self._last_session = session_wh
        cum = self._offset + session_wh
        if self.times and ts < self.times[-1]:
            ts = self.times[-1]
        if self.values and cum == self.values[-1]:
            return  # step function: an unchanged value adds no information
        self.times.append(ts)
        self.values.append(cum)
        self._prune(ts)

    def _prune(self, now: float) -> None:
        cutoff = now - HISTORY_KEEP_S
        i = bisect.bisect_left(self.times, cutoff)
        if i > 1:
            del self.times[: i - 1]
            del self.values[: i - 1]

    @property
    def start(self) -> Optional[float]:
        return self.times[0] if self.times else None

    def at(self, ts: float) -> Optional[float]:
        """Cumulative energy at ``ts`` (step function; last known value).

        Before the first known point we return the first value: energy that
        went in before the service started is unknown, so we assume none of
        it happened after the reading (conservative = lower estimate).
        """
        if not self.times:
            return None
        i = bisect.bisect_right(self.times, ts)
        if i == 0:
            return self.values[0]
        return self.values[i - 1]

    def now(self) -> Optional[float]:
        return self.values[-1] if self.values else None


@dataclass
class SocEstimator:
    capacity_kwh: float = 58.0
    efficiency: float = 1.0
    stale_after_s: float = 2700.0

    vw_soc: Optional[float] = None
    vw_soc_time: Optional[float] = None      # epoch s of the measurement
    last_portal_ok: Optional[float] = None   # epoch s of last good list/poll
    energy: EnergyHistory = field(default_factory=EnergyHistory)
    calibrations: list = field(default_factory=list)  # from manual readings

    # -- inputs ------------------------------------------------------------

    def portal_ok(self, ts: float) -> None:
        self.last_portal_ok = ts

    def vw_reading(self, soc: float, measured_at: float) -> bool:
        """Offer a VW SOC from a dataset. Returns True if it was taken.

        Repeated values are ignored: the portal re-sends the last battery
        report in every dataset, and treating that as "fresh" is what made
        soc_live jump back during charging.
        """
        if self.vw_soc is not None and soc == self.vw_soc:
            return False
        if self.vw_soc_time is not None and measured_at < self.vw_soc_time:
            log.warning("VW-SOC %.0f%% mit älterem Zeitstempel als der aktuelle "
                        "Anker — ignoriert", soc)
            return False
        if self.energy.start is not None and measured_at < self.energy.start:
            log.info("VW-SOC %.0f%% gemessen vor Beginn der Energie-Historie — "
                     "Energie davor unbekannt, Schätzung ggf. etwas zu niedrig", soc)
        self.vw_soc = float(soc)
        self.vw_soc_time = measured_at
        return True

    def evcc_energy(self, ts: float, session_wh: float) -> None:
        self.energy.add_session_value(ts, session_wh)

    def manual_reading(self, soc: float, ts: float) -> dict:
        """A SOC reported by a human (e.g. read from the VW app via Gaston).

        Always becomes the new anchor — it is fresher and more trustworthy than
        anything the portal delivers. If enough energy went in since the old
        anchor, the implied charging efficiency is computed and remembered
        (``calibrations``) so CHARGE_EFFICIENCY can be tuned from real data.
        """
        result: dict = {"soc": float(soc), "ts": ts,
                        "previous_estimate": None, "efficiency": None, "delta_wh": None}
        if self.vw_soc is not None and self.vw_soc_time is not None:
            # Ignore staleness here: the human value is what matters.
            e_now, e_then = self.energy.now(), self.energy.at(self.vw_soc_time)
            delta_wh = max(0.0, e_now - e_then) if e_now is not None and e_then is not None else 0.0
            prev = self.vw_soc + delta_wh / (self.capacity_kwh * 1000.0) * 100.0 * self.efficiency
            result["previous_estimate"] = round(max(self.vw_soc, min(100.0, prev)), 1)
            result["delta_wh"] = round(delta_wh)
            if delta_wh >= MIN_CALIBRATION_WH and soc > self.vw_soc:
                eff = (soc - self.vw_soc) / 100.0 * self.capacity_kwh * 1000.0 / delta_wh
                result["efficiency"] = round(eff, 3)
                self.calibrations.append({"ts": ts, "from_soc": self.vw_soc, "to_soc": float(soc),
                                          "delta_wh": round(delta_wh), "efficiency": round(eff, 3)})
                del self.calibrations[:-MAX_CALIBRATIONS]
        self.vw_soc = float(soc)
        self.vw_soc_time = ts
        self.last_portal_ok = max(self.last_portal_ok or 0.0, ts)
        return result

    # -- persistence (survive service restarts) ----------------------------

    def to_dict(self) -> dict:
        e = self.energy
        return {
            "vw_soc": self.vw_soc,
            "vw_soc_time": self.vw_soc_time,
            "last_portal_ok": self.last_portal_ok,
            "calibrations": self.calibrations,
            "energy": {
                "times": e.times, "values": e.values,
                "offset": e._offset, "last_session": e._last_session,
            },
        }

    def load_dict(self, d: dict) -> None:
        """Restore state saved by :meth:`to_dict`; tunables stay as configured."""
        self.vw_soc = d.get("vw_soc")
        self.vw_soc_time = d.get("vw_soc_time")
        self.last_portal_ok = d.get("last_portal_ok")
        self.calibrations = list(d.get("calibrations") or [])
        e = d.get("energy") or {}
        times, values = list(e.get("times") or []), list(e.get("values") or [])
        if len(times) != len(values):
            times, values = [], []
        self.energy = EnergyHistory(times=times, values=values,
                                    _offset=float(e.get("offset") or 0.0),
                                    _last_session=e.get("last_session"))

    # -- output ------------------------------------------------------------

    def is_stale(self, now: float) -> bool:
        return self.last_portal_ok is None or now - self.last_portal_ok > self.stale_after_s

    def estimate(self, now: float) -> Optional[float]:
        """Live SOC estimate, or None if no reading yet or portal data stale."""
        if self.vw_soc is None or self.vw_soc_time is None:
            return None
        if self.is_stale(now):
            return None
        e_now = self.energy.now()
        e_then = self.energy.at(self.vw_soc_time)
        delta_wh = 0.0
        if e_now is not None and e_then is not None:
            delta_wh = max(0.0, e_now - e_then)
        soc = self.vw_soc + delta_wh / (self.capacity_kwh * 1000.0) * 100.0 * self.efficiency
        return max(self.vw_soc, min(100.0, soc))
