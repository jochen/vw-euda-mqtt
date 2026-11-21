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

Additions 2026-11-21 (start value + protection + measured cap):

* **Driving:** the portal SOC often lags 12–24 h, but the mileage arrives in
  almost every dataset. ``soc = anchor + charged − km_since_anchor × kWh/km``,
  so a SOC reported at unplug stays valid at the next plug-in. (21.11.: 36 %
  on 20.11., 6 km driven, VW then said 34 % — 6 km × 0.2 kWh ≈ 2 %.)
* **Trust:** a manual reading (VW app via Gaston) locks the anchor for
  ``manual_lock_s`` / ``manual_lock_km``; VW values in that window only
  confirm or conflict, never override. A VW value well *below* the model
  after charging without any driving is a stale report and is rejected.
  Conflicts are exposed (``last_verdict``) so someone can ask the human.
* **Phantom energy:** the Elli Connect reports no measurement over EEBUS, so
  evcc's chargePower is offered current × 230 V × 3. When the car stops
  drawing, evcc keeps counting (21.11. 13:55–13:57: 7.6 kW "charging" with no
  extra site consumption). :meth:`site_power` caps the charge power at the
  measured site consumption (PV + grid + battery) minus a base load and
  subtracts the excess from evcc's session energy.
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
# Drive-consumption samples need some distance to be meaningful.
MIN_DRIVE_CALIBRATION_KM = 10.0
# Longer gaps between site power samples are not integrated (data outage).
MAX_SITE_GAP_S = 300.0


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
    drive_kwh_per_km: float = 0.22
    tolerance_pct: float = 3.0
    manual_lock_s: float = 24 * 3600.0
    manual_lock_km: float = 30.0
    home_base_w: float = 300.0

    vw_soc: Optional[float] = None
    vw_soc_time: Optional[float] = None      # epoch s of the measurement
    last_portal_ok: Optional[float] = None   # epoch s of last good list/poll
    energy: EnergyHistory = field(default_factory=EnergyHistory)
    calibrations: list = field(default_factory=list)  # from manual readings
    anchor_source: str = "vw"                # "vw" | "manual"
    anchor_km: Optional[float] = None        # mileage when the anchor was set
    km: Optional[float] = None               # highest mileage seen
    drive_calibrations: list = field(default_factory=list)
    phantom_wh: float = 0.0                  # evcc energy not backed by site load
    _raw_session: Optional[float] = None     # last evcc chargedEnergy (raw)
    _fed_session: Optional[float] = None     # last value fed to energy history
    _site_prev: Optional[tuple] = None       # (ts, charge_w, consumption_w)
    # Outcome of the last vw_reading(): accepted|repeat|older|confirmed|conflict
    last_verdict: str = ""
    last_expected: Optional[float] = None

    # -- model ---------------------------------------------------------------

    def _pct(self, wh: float) -> float:
        return wh / (self.capacity_kwh * 1000.0) * 100.0

    def km_since_anchor(self, km: Optional[float] = None) -> Optional[float]:
        km = self.km if km is None else km
        if km is None or self.anchor_km is None:
            return None
        return max(0.0, km - self.anchor_km)

    def _charged_wh(self, ts: float) -> float:
        """Energy charged between the anchor and ``ts``."""
        if self.vw_soc_time is None:
            return 0.0
        e_ts, e_then = self.energy.at(ts), self.energy.at(self.vw_soc_time)
        if e_ts is None or e_then is None:
            return 0.0
        return max(0.0, e_ts - e_then)

    def model(self, ts: float, km: Optional[float] = None) -> Optional[float]:
        """anchor + charged × efficiency − driven × consumption, ignoring staleness."""
        if self.vw_soc is None:
            return None
        soc = self.vw_soc + self._pct(self._charged_wh(ts)) * self.efficiency
        driven = self.km_since_anchor(km)
        if driven:
            soc -= self._pct(driven * self.drive_kwh_per_km * 1000.0)
        return max(0.0, min(100.0, soc))

    # -- inputs ------------------------------------------------------------

    def portal_ok(self, ts: float) -> None:
        self.last_portal_ok = ts

    def mileage(self, km: Optional[float]) -> None:
        """Feed the highest mileage of a dataset (km, monotonic)."""
        if km is None:
            return
        if self.km is None or km > self.km:
            self.km = float(km)
        if self.anchor_km is None and self.vw_soc is not None:
            # Anchor from before mileage was tracked: assume no driving yet.
            self.anchor_km = self.km

    def _locked(self, ts: float) -> bool:
        if self.anchor_source != "manual" or self.vw_soc_time is None:
            return False
        if ts - self.vw_soc_time >= self.manual_lock_s:
            return False
        return (self.km_since_anchor() or 0.0) < self.manual_lock_km

    def _set_anchor(self, soc: float, ts: float, source: str) -> None:
        self._drive_calibration(soc, ts)
        self.vw_soc = float(soc)
        self.vw_soc_time = ts
        self.anchor_source = source
        self.anchor_km = self.km

    def _drive_calibration(self, soc: float, ts: float) -> None:
        """Record consumption when only driving happened since the anchor."""
        driven = self.km_since_anchor()
        if (self.vw_soc is None or driven is None or driven < MIN_DRIVE_CALIBRATION_KM
                or self._charged_wh(ts) > 300.0 or soc >= self.vw_soc):
            return
        kwh_km = (self.vw_soc - soc) / 100.0 * self.capacity_kwh / driven
        self.drive_calibrations.append({"ts": ts, "from_soc": self.vw_soc, "to_soc": float(soc),
                                        "km": round(driven, 1), "kwh_per_km": round(kwh_km, 3)})
        del self.drive_calibrations[:-MAX_CALIBRATIONS]
        log.info("Fahrverbrauch gemessen: %.0f → %.0f %% über %.0f km = %.3f kWh/km "
                 "(eingestellt %.3f)", self.vw_soc, soc, driven, kwh_km, self.drive_kwh_per_km)

    def vw_reading(self, soc: float, measured_at: float) -> bool:
        """Offer a VW SOC from a dataset. Returns True if it became the anchor.

        Repeated values are ignored: the portal re-sends the last battery
        report in every dataset, and treating that as "fresh" is what made
        soc_live jump back during charging. See the module doc for the lock
        and the stale-drop rule; ``last_verdict`` tells why it was refused.
        """
        self.last_expected = None
        if self.vw_soc is not None and soc == self.vw_soc:
            self.last_verdict = "repeat"
            return False
        if self.vw_soc_time is not None and measured_at < self.vw_soc_time:
            log.log(logging.DEBUG if self.anchor_source == "manual" else logging.INFO,
                    "VW-SOC %.0f%% mit älterem Zeitstempel als der aktuelle Anker — ignoriert", soc)
            self.last_verdict = "older"
            return False
        if self.vw_soc is not None:
            expected = self.model(measured_at)
            self.last_expected = round(expected, 1)
            dev = soc - expected
            if self._locked(measured_at):
                self.last_verdict = "confirmed" if abs(dev) <= self.tolerance_pct else "conflict"
                return False
            charged_pct = self._pct(self._charged_wh(measured_at))
            if dev < -self.tolerance_pct and self.km_since_anchor() == 0.0 and charged_pct > 0.5:
                # Lower than the model after charging, and the car has not moved:
                # this is a report from before the charge with a too-new timestamp.
                self.last_verdict = "conflict"
                return False
        if self.energy.start is not None and measured_at < self.energy.start:
            log.info("VW-SOC %.0f%% gemessen vor Beginn der Energie-Historie — "
                     "Energie davor unbekannt, Schätzung ggf. etwas zu niedrig", soc)
        self._set_anchor(soc, measured_at, "vw")
        self.last_verdict = "accepted"
        return True

    def evcc_energy(self, ts: float, session_wh: float) -> None:
        """evcc's per-session chargedEnergy, minus the phantom energy."""
        if self._raw_session is not None and session_wh < self._raw_session:
            # New session: its phantom starts at zero.
            self.phantom_wh = 0.0
            self._fed_session = None
        self._raw_session = session_wh
        adj = max(0.0, session_wh - self.phantom_wh)
        if self._fed_session is not None:
            adj = max(adj, self._fed_session)   # never look like a session reset
        self._fed_session = adj
        self.energy.add_session_value(ts, adj)

    def site_power(self, ts: float, charge_w: float, consumption_w: Optional[float]) -> None:
        """One evcc cycle: estimated charge power and measured site consumption
        (PV + grid + battery, None if unknown). Integrates the part of the
        estimated charge power that the site meter cannot account for."""
        prev, self._site_prev = self._site_prev, (ts, charge_w, consumption_w)
        if prev is None:
            return
        t0, c0, cons0 = prev
        dt = ts - t0
        if (self.home_base_w < 0.0 or not 0.0 < dt <= MAX_SITE_GAP_S
                or c0 <= 0.0 or cons0 is None):
            return
        real = max(0.0, cons0 - self.home_base_w)
        if c0 > real:
            self.phantom_wh += (c0 - real) * dt / 3600.0

    def manual_reading(self, soc: float, ts: float) -> dict:
        """A SOC reported by a human (e.g. read from the VW app via Gaston).

        Always becomes the new anchor — it is fresher and more trustworthy than
        anything the portal delivers — and locks it against VW values for a
        while. If enough energy went in since the old anchor, the implied
        charging efficiency is computed and remembered (``calibrations``).
        """
        result: dict = {"soc": float(soc), "ts": ts,
                        "previous_estimate": None, "efficiency": None, "delta_wh": None}
        if self.vw_soc is not None and self.vw_soc_time is not None:
            # Ignore staleness here: the human value is what matters.
            delta_wh = self._charged_wh(ts)
            result["previous_estimate"] = round(self.model(ts), 1)
            result["delta_wh"] = round(delta_wh)
            if delta_wh >= MIN_CALIBRATION_WH and soc > self.vw_soc:
                eff = (soc - self.vw_soc) / 100.0 * self.capacity_kwh * 1000.0 / delta_wh
                result["efficiency"] = round(eff, 3)
                self.calibrations.append({"ts": ts, "from_soc": self.vw_soc, "to_soc": float(soc),
                                          "delta_wh": round(delta_wh), "efficiency": round(eff, 3)})
                del self.calibrations[:-MAX_CALIBRATIONS]
        self._set_anchor(soc, ts, "manual")
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
            "anchor_source": self.anchor_source,
            "anchor_km": self.anchor_km,
            "km": self.km,
            "drive_calibrations": self.drive_calibrations,
            "phantom_wh": self.phantom_wh,
            "raw_session": self._raw_session,
            "fed_session": self._fed_session,
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
        src = d.get("anchor_source")
        if src is None:
            # State from before 2026-11-21: the anchor was manual if it is the
            # timestamp of the last calibration.
            src = "manual" if any(c.get("ts") == self.vw_soc_time for c in self.calibrations) else "vw"
        self.anchor_source = src
        self.anchor_km = d.get("anchor_km")
        self.km = d.get("km")
        self.drive_calibrations = list(d.get("drive_calibrations") or [])
        self.phantom_wh = float(d.get("phantom_wh") or 0.0)
        self._raw_session = d.get("raw_session")
        self._fed_session = d.get("fed_session")
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
        # energy.at(now) == energy.now() for any now after the last sample.
        return self.model(max(now, self.energy.times[-1]) if self.energy.times else now)
