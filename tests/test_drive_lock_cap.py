"""Start value via mileage, trust rules, phantom-energy cap (2026-11-21)."""
import pytest

from euda.soc_estimator import SocEstimator
from euda_service import Config, Hub, _handle_evcc_message, _mileage, _site_consumption
from euda.data import Dataset

H = 3600.0


def _est(**kw):
    kw.setdefault("stale_after_s", 1e9)
    return SocEstimator(capacity_kwh=58.0, efficiency=1.0, **kw)


# -- driving -----------------------------------------------------------------

def test_driving_lowers_estimate():
    e = _est(drive_kwh_per_km=0.29)          # 0.29 kWh/km → 10 km = 5 %
    e.mileage(1000)
    e.manual_reading(71, 0.0)
    e.mileage(1010)
    assert e.estimate(100.0) == pytest.approx(66.0)


def test_unplug_value_survives_to_next_plugin():
    # 71 % at unplug, short trip, plug in again and charge 5.8 kWh.
    e = _est(drive_kwh_per_km=0.29)
    e.mileage(72885)
    e.evcc_energy(0.0, 0.0)
    e.manual_reading(71, 10.0)
    e.mileage(72889)                          # 4 km → 2 %
    e.evcc_energy(5 * H, 0.0)                 # new session
    e.evcc_energy(6 * H, 5800.0)              # +10 %
    assert e.estimate(6 * H) == pytest.approx(79.0)


def test_anchor_without_mileage_gets_first_mileage():
    e = _est()
    e.vw_reading(50, 0.0)
    assert e.anchor_km is None
    e.mileage(500)
    assert e.anchor_km == 500 and e.km_since_anchor() == 0.0


def test_vw_reading_after_drive_is_accepted_and_calibrates_consumption():
    e = _est(drive_kwh_per_km=0.2)
    e.mileage(72786)
    e.vw_reading(74, 0.0)
    e.mileage(72879)                          # 93 km
    assert e.vw_reading(36, 40 * H) is True
    cal = e.drive_calibrations[-1]
    assert cal["km"] == 93 and cal["kwh_per_km"] == pytest.approx(0.237, abs=1e-3)
    assert e.anchor_km == 72879


def test_mileage_from_dataset_takes_max():
    ds = Dataset.from_json({"vin": "X", "Data": [
        {"key": "a", "dataFieldName": "mileage.value", "value": "72880"},
        {"key": "b", "dataFieldName": "mileage.value", "value": "72883"},
        {"key": "c", "dataFieldName": "mileage.state", "value": "VALID"},
    ]})
    assert _mileage(ds) == 72883


# -- trust rules -------------------------------------------------------------

def test_manual_lock_ends_after_driving():
    e = _est(drive_kwh_per_km=0.2)
    e.mileage(1000)
    e.manual_reading(70, 0.0)
    e.mileage(1040)                           # > 30 km → lock off
    assert e.vw_reading(55, H) is True
    assert e.anchor_source == "vw"


def test_stale_low_value_during_charge_is_rejected():
    # 18.11.-style: anchored 44 %, charged +20 %, car did not move; a report
    # from before the charge shows up with a fresh timestamp.
    e = _est()
    e.mileage(1000)
    e.evcc_energy(0.0, 0.0)
    e.vw_reading(44, 0.0)
    e.evcc_energy(H, 11600.0)
    assert e.vw_reading(42, 2 * H) is False
    assert e.last_verdict == "conflict" and e.last_expected == pytest.approx(64.0)
    e.portal_ok(2 * H)
    assert e.estimate(2 * H) == pytest.approx(64.0)
    # A higher (or matching) fresh report is fine.
    assert e.vw_reading(66, 3 * H) is True


def test_lower_value_without_charging_is_accepted():
    e = _est()
    e.mileage(1000)
    e.vw_reading(60, 0.0)
    assert e.vw_reading(55, H) is True       # standby/drift, nothing to contradict


# -- phantom energy cap ------------------------------------------------------

def test_phantom_energy_subtracted():
    e = _est(home_base_w=300.0)
    e.mileage(1000)
    e.evcc_energy(0.0, 0.0)
    e.vw_reading(50, 0.0)
    # 1 h real charging 7 kW: site sees 8 kW → fully backed.
    e.site_power(0.0, 7000.0, 8000.0)
    e.site_power(H, 7000.0, 1500.0)           # car stopped drawing, evcc still counts
    e.evcc_energy(H, 7000.0)
    assert e.phantom_wh == 0.0
    e.site_power(H + 300, 0.0, 1500.0)        # 300 s phantom: 7000 − 1200 = 5800 W
    assert e.phantom_wh == pytest.approx(5800 * 300 / 3600)
    e.evcc_energy(H + 300, 7000.0 + 7000 * 300 / 3600)
    assert e.energy.now() == pytest.approx(7000.0 + 1200 * 300 / 3600)


def test_phantom_never_looks_like_session_reset():
    e = _est()
    e.evcc_energy(0.0, 0.0)
    e.evcc_energy(10.0, 1000.0)
    e.phantom_wh = 800.0
    e.evcc_energy(20.0, 1100.0)               # 300 < 1000 fed → stays 1000
    assert e.energy.now() == 1000.0 and e.energy._offset == 0.0
    e.evcc_energy(30.0, 0.0)                  # real new session resets phantom
    assert e.phantom_wh == 0.0


def test_cap_off_without_site_data_or_when_disabled():
    e = _est()
    e.site_power(0.0, 7000.0, None)
    e.site_power(60.0, 7000.0, None)
    assert e.phantom_wh == 0.0
    d = _est(home_base_w=-1.0)
    d.site_power(0.0, 7000.0, 0.0)
    d.site_power(60.0, 7000.0, 0.0)
    assert d.phantom_wh == 0.0
    g = _est()
    g.site_power(0.0, 7000.0, 0.0)
    g.site_power(1000.0, 7000.0, 0.0)          # gap > 300 s: not integrated
    assert g.phantom_wh == 0.0


def test_service_feeds_site_power(monkeypatch):
    monkeypatch.setenv("EVCC_BASE_TOPIC", "ohm/evcc")
    cfg = Config()
    hub = Hub(est=_est())
    for t, v in (("pvPower", "1000"), ("grid/power", "500"), ("battery/power", "-200")):
        _handle_evcc_message(hub, f"ohm/evcc/site/{t}", v, cfg, now=100.0)
    assert _site_consumption(hub, 100.0) == 1300.0
    assert _site_consumption(hub, 300.0) is None   # stale
    _handle_evcc_message(hub, cfg.evcc_topic("chargePower"), "6900", cfg, now=100.0)
    _handle_evcc_message(hub, cfg.evcc_topic("chargePower"), "6900", cfg, now=130.0)
    # 6900 − (1300 − 300) = 5900 W for 30 s
    assert hub.est.phantom_wh == pytest.approx(5900 * 30 / 3600)


def test_state_roundtrip_new_fields():
    a = _est()
    a.mileage(100)
    a.manual_reading(70, 0.0)
    a.phantom_wh = 12.0
    b = _est()
    b.load_dict(a.to_dict())
    assert (b.anchor_source, b.anchor_km, b.km, b.phantom_wh) == ("manual", 100, 100, 12.0)


def test_legacy_state_detects_manual_anchor():
    b = _est()
    b.load_dict({"vw_soc": 71.0, "vw_soc_time": 5.0, "last_portal_ok": 5.0,
                 "calibrations": [{"ts": 5.0, "efficiency": 1.0}], "energy": {}})
    assert b.anchor_source == "manual"


def test_late_mileage_from_before_manual_anchor_is_not_driving():
    # 21.11.: 71 % manual at 14:01, out 24 km (VW knew), return 24 km (VW late).
    # 48 % manual at 23:07; +24 km arrives 00:11 in a dataset captured 21:10.
    e = _est(drive_kwh_per_km=0.22)
    e.mileage(72885, 0.0)
    e.manual_reading(71, 14 * H)
    e.mileage(72909, 17 * H)
    e.manual_reading(48, 23 * H)
    assert e.drive_calibrations == []           # manual: mileage may lag
    e.mileage(72933, 21 * H)                    # captured before the anchor
    e.portal_ok(24 * H)
    assert e.anchor_km == 72933
    assert e.estimate(24 * H) == pytest.approx(48.0)
    e.mileage(72943, 25 * H)                    # real driving after the anchor
    e.portal_ok(25 * H)
    assert e.estimate(25 * H) == pytest.approx(48.0 - 10 * 0.22 / 58 * 100)
