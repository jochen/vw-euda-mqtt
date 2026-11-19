"""Offline unit tests for SOC interpolation logic and soc_live MQTT messages.

All tests are fully offline — no VW login, no MQTT broker connection.
Run with:
    ./venv/bin/python -m pytest tests/test_interpolation.py -v
or simply:
    ./venv/bin/python tests/test_interpolation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sure the project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from euda_service import Hub, _handle_evcc_message
from euda.mqtt import build_soc_live_messages
from euda.soc_estimator import SocEstimator


CAPACITY_KWH = 58.0
EFFICIENCY = 1.0
H = 3600.0
T0 = 1_795_000_000.0  # arbitrary epoch base


class _FakeCfg:
    """Minimal Config-like object for _handle_evcc_message."""
    evcc_loadpoint = "1"
    evcc_base_topic = "ohm/evcc"

    def evcc_topic(self, field: str) -> str:
        return f"{self.evcc_base_topic}/loadpoints/{self.evcc_loadpoint}/{field}"


def _est(**kw) -> SocEstimator:
    return SocEstimator(capacity_kwh=CAPACITY_KWH, efficiency=kw.pop("efficiency", EFFICIENCY),
                        stale_after_s=kw.pop("stale_after_s", 2700.0))


def _charge(est: SocEstimator, t_start: float, t_end: float, kw: float, step: float = 30.0,
            session_start_wh: float = 0.0) -> float:
    """Simulate evcc publishing chargedEnergy every ``step`` s at ``kw``."""
    t, wh = t_start, session_start_wh
    while t <= t_end:
        est.evcc_energy(t, wh)
        t += step
        wh += kw * 1000.0 * step / 3600.0
    return wh


# ---------------------------------------------------------------------------
# Basic math
# ---------------------------------------------------------------------------

def test_basic_interpolation():
    """50 % + 2.9 kWh on a 58 kWh pack → 55 %."""
    est = _est()
    est.evcc_energy(T0, 0.0)
    est.portal_ok(T0)
    est.vw_reading(50.0, T0)
    est.evcc_energy(T0 + 600, 2900.0)
    assert abs(est.estimate(T0 + 600) - 55.0) < 1e-6


def test_clamp_to_100():
    est = _est()
    est.evcc_energy(T0, 0.0)
    est.portal_ok(T0)
    est.vw_reading(95.0, T0)
    est.evcc_energy(T0 + 60, 99999.0)
    assert est.estimate(T0 + 60) == 100.0


def test_efficiency_factor():
    est = _est(efficiency=0.9)
    est.evcc_energy(T0, 0.0)
    est.portal_ok(T0)
    est.vw_reading(50.0, T0)
    est.evcc_energy(T0 + 60, 2900.0)
    assert abs(est.estimate(T0 + 60) - (50.0 + 5.0 * 0.9)) < 1e-6


def test_no_reading_returns_none():
    est = _est()
    est.portal_ok(T0)
    est.evcc_energy(T0, 500.0)
    assert est.estimate(T0) is None


def test_not_charging_returns_vw_soc():
    est = _est()
    est.evcc_energy(T0, 1234.0)
    est.portal_ok(T0)
    est.vw_reading(72.0, T0)
    assert est.estimate(T0 + 100) == 72.0


# ---------------------------------------------------------------------------
# The 2026-11-18 failure modes
# ---------------------------------------------------------------------------

def test_repeated_old_value_does_not_jump_back():
    """VW keeps re-sending 44 % in every dataset while the car charges.
    The old code re-anchored on each dataset and soc_live fell back to 44 %."""
    est = _est()
    est.portal_ok(T0)
    est.vw_reading(44.0, T0)                      # real reading before charging
    _charge(est, T0 + 60, T0 + 1 * H, kw=11.0)    # ~11 kWh
    for k in range(1, 5):                         # 4 datasets repeating 44 %
        est.portal_ok(T0 + k * 900)
        assert est.vw_reading(44.0, T0 + k * 900) is False
    soc = est.estimate(T0 + 1 * H)
    assert soc > 62.0, soc                        # 44 + ~11/58*100 ≈ 63


def test_late_reading_is_advanced_by_energy_since_measurement():
    """A changed VW value that arrives late is corrected by the energy charged
    after it was measured (it must not throw the estimate back)."""
    est = _est()
    est.portal_ok(T0)
    est.vw_reading(44.0, T0)
    _charge(est, T0, T0 + 2 * H, kw=11.6)          # 23.2 kWh → +40 %
    # Reading 64 % measured after 1 h of charging, delivered at 2 h.
    est.portal_ok(T0 + 2 * H)
    assert est.vw_reading(64.0, T0 + 1 * H) is True
    soc = est.estimate(T0 + 2 * H)
    assert abs(soc - 84.0) < 0.5, soc


def test_real_values_2026_11_18():
    """VW 44 % before, 15.36 kWh charged, VW 70 % after (real data)."""
    est = _est()
    est.portal_ok(T0)
    est.vw_reading(44.0, T0)
    _charge(est, T0 + 60, T0 + 60 + 1.4 * H, kw=10.97)
    est.portal_ok(T0 + 2 * H)
    soc = est.estimate(T0 + 2 * H)
    assert 69.0 < soc < 72.0, soc


def test_stale_portal_withholds_estimate():
    """No successful portal poll for longer than stale_after → None, so evcc's
    MQTT timeout kicks in instead of charging against a frozen value."""
    est = _est(stale_after_s=2700)
    est.portal_ok(T0)
    est.vw_reading(76.0, T0)
    assert est.estimate(T0 + 2000) == 76.0
    assert est.estimate(T0 + 2701) is None
    est.portal_ok(T0 + 3000)
    assert est.estimate(T0 + 3000) == 76.0


def test_older_changed_reading_is_ignored():
    est = _est()
    est.portal_ok(T0)
    est.vw_reading(60.0, T0 + 100)
    assert est.vw_reading(50.0, T0) is False
    assert est.vw_soc == 60.0


def test_session_reset_keeps_cumulative_energy():
    """chargedEnergy resets per evcc session; energy must stay cumulative."""
    est = _est()
    est.portal_ok(T0)
    est.vw_reading(40.0, T0)
    est.evcc_energy(T0, 0.0)
    est.evcc_energy(T0 + 600, 2900.0)     # session 1: +5 %
    est.evcc_energy(T0 + 700, 0.0)        # unplug / new session
    est.evcc_energy(T0 + 1300, 2900.0)    # session 2: +5 %
    assert abs(est.estimate(T0 + 1300) - 50.0) < 1e-6


def test_reading_before_history_start_is_conservative():
    """Energy before the service started is unknown → no extra delta."""
    est = _est()
    est.evcc_energy(T0 + 600, 5000.0)     # first value we ever see
    est.portal_ok(T0 + 600)
    est.vw_reading(50.0, T0)              # measured before history start
    assert est.estimate(T0 + 600) == 50.0


# ---------------------------------------------------------------------------
# evcc MQTT handler
# ---------------------------------------------------------------------------

def test_handler_feeds_energy_and_charging():
    hub = Hub()
    cfg = _FakeCfg()
    _handle_evcc_message(hub, cfg.evcc_topic("charging"), "true", cfg)
    _handle_evcc_message(hub, cfg.evcc_topic("chargedEnergy"), "1500", cfg)
    assert hub.charging is True
    assert hub.charged_energy_wh == 1500.0
    assert hub.est.energy.now() == 1500.0
    _handle_evcc_message(hub, cfg.evcc_topic("chargedEnergy"), "oops", cfg)
    assert hub.charged_energy_wh == 1500.0


# ---------------------------------------------------------------------------
# Test 8: soc_live MQTT discovery + state messages
# ---------------------------------------------------------------------------

def test_soc_live_messages_structure():
    """build_soc_live_messages returns 2 messages with correct topics/payload/retain."""
    vin = "WVWZZZE1ZNP000001"
    base_topic = "test/vweuda"
    subtopic = "soc_live"
    soc_value = 67.3

    msgs = build_soc_live_messages(vin, base_topic, subtopic, soc_value)
    assert len(msgs) == 2, f"Expected 2 messages, got {len(msgs)}"

    config_msg, state_msg = msgs

    # Config (discovery) message
    vin_lower = vin.lower()
    expected_config_topic = f"homeassistant/sensor/vw_id3_{vin_lower}_soc_live/config"
    assert config_msg.topic == expected_config_topic, (
        f"Config topic mismatch: {config_msg.topic!r}"
    )
    assert config_msg.retain is True   # discovery config stays retained
    # State is NON-retained by default (evcc fail-safe).
    assert state_msg.retain is False

    config_payload = json.loads(config_msg.payload)
    assert config_payload["device_class"] == "battery"
    assert config_payload["unit_of_measurement"] == "%"
    assert config_payload["state_class"] == "measurement"
    assert config_payload["unique_id"] == f"vw_id3_{vin_lower}_soc_live"
    assert config_payload["name"] == "SOC (live)"
    assert config_payload["state_topic"] == f"{base_topic}/{subtopic}"
    assert "device" in config_payload
    assert config_payload["device"]["manufacturer"] == "Volkswagen"

    # State message
    expected_state_topic = f"{base_topic}/{subtopic}"
    assert state_msg.topic == expected_state_topic, (
        f"State topic mismatch: {state_msg.topic!r}"
    )
    assert state_msg.payload == "67.3", f"State payload mismatch: {state_msg.payload!r}"


def test_soc_live_state_retain_flag():
    """soc_live state is non-retained by default, retained only when requested."""
    from euda.mqtt import soc_live_state
    assert soc_live_state("ohm/car/id3", "soc_live", 50.0).retain is False
    assert soc_live_state("ohm/car/id3", "soc_live", 50.0, retain=True).retain is True


def test_soc_live_state_payload_format():
    """State payload is formatted to 1 decimal place."""
    msgs = build_soc_live_messages("VIN123", "ohm/car/id3", "soc_live", 55.0)
    _, state_msg = msgs
    assert state_msg.payload == "55.0"

    msgs2 = build_soc_live_messages("VIN123", "ohm/car/id3", "soc_live", 100.0)
    _, state_msg2 = msgs2
    assert state_msg2.payload == "100.0"


# ---------------------------------------------------------------------------
# Runner (when executed directly, not via pytest)
# ---------------------------------------------------------------------------

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")
    ]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} tests passed.")
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
