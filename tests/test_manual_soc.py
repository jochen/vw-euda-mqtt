"""Manually reported SOC (e.g. from the VW app via Gaston)."""
import pytest

import euda_service as svc
from euda.soc_estimator import SocEstimator


def _est():
    e = SocEstimator(capacity_kwh=58.0, efficiency=1.0, stale_after_s=2700)
    e.portal_ok(0.0)
    e.evcc_energy(0.0, 0.0)
    e.vw_reading(36, 0.0)
    return e


def test_manual_sets_anchor_and_measures_efficiency():
    e = _est()
    e.evcc_energy(3600.0, 9984.0)          # 9.98 kWh into the car
    r = e.manual_reading(50, 3700.0)
    assert r["previous_estimate"] == pytest.approx(53.2, abs=0.05)
    assert r["efficiency"] == pytest.approx(0.813, abs=0.001)
    assert e.vw_soc == 50 and e.vw_soc_time == 3700.0
    assert len(e.calibrations) == 1
    # Estimate continues from the manual anchor.
    e.evcc_energy(4000.0, 9984.0 + 5800.0)
    assert e.estimate(4000.0) == pytest.approx(60.0)


def test_small_delta_gives_no_efficiency():
    e = _est()
    e.evcc_energy(600.0, 1000.0)
    r = e.manual_reading(38, 700.0)
    assert r["efficiency"] is None and e.calibrations == []


def test_old_portal_value_does_not_override_manual():
    e = _est()
    e.manual_reading(50, 1000.0)
    assert e.vw_reading(36, 0.0) is False     # repeated old portal value
    # Within the manual lock, VW only confirms or conflicts, never overrides.
    assert e.vw_reading(52, 2000.0) is False
    assert e.last_verdict == "confirmed"
    assert e.vw_reading(60, 3000.0) is False
    assert e.last_verdict == "conflict"
    # After the lock (24 h) a newer VW report wins again.
    assert e.vw_reading(57, 1000.0 + 25 * 3600) is True


def test_manual_revives_stale_estimator():
    e = _est()
    assert e.estimate(10_000.0) is None       # portal stale
    e.manual_reading(60, 10_000.0)
    assert e.estimate(10_001.0) == 60.0


@pytest.mark.parametrize("raw,val", [("50", 50.0), ("50%", 50.0), ("50,5", 50.5),
                                     ('{"soc": 47}', 47.0)])
def test_parse(raw, val):
    assert svc._parse_manual_soc(raw) == val


@pytest.mark.parametrize("raw", ["", "abc", "120", "-1"])
def test_parse_rejects(raw):
    with pytest.raises((ValueError, TypeError)):
        svc._parse_manual_soc(raw)


def test_calibrations_persist():
    e = _est()
    e.evcc_energy(3600.0, 9984.0)
    e.manual_reading(50, 3700.0)
    f = SocEstimator()
    f.load_dict(e.to_dict())
    assert f.calibrations == e.calibrations
