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

from euda_service import Hub, _handle_evcc_message, _re_anchor, interpolate_soc
from euda.mqtt import build_soc_live_messages


CAPACITY_KWH = 58.0
EFFICIENCY = 1.0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_hub(**kwargs) -> Hub:
    h = Hub()
    for k, v in kwargs.items():
        setattr(h, k, v)
    return h


class _FakeCfg:
    """Minimal Config-like object for _handle_evcc_message."""
    evcc_loadpoint = "1"
    evcc_base_topic = "ohm/evcc"

    def evcc_topic(self, field: str) -> str:
        return f"{self.evcc_base_topic}/loadpoints/{self.evcc_loadpoint}/{field}"


# ---------------------------------------------------------------------------
# Test 1: basic interpolation math
# ---------------------------------------------------------------------------

def test_basic_interpolation():
    """anchor_soc=50%, 0→2900 Wh charged → soc_live should be 55.0%."""
    hub = _make_hub(
        last_real_soc=50.0,
        charging=True,
        charged_energy_wh=2900.0,
        anchor_soc=50.0,
        anchor_energy_wh=0.0,
    )
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result is not None
    assert abs(result - 55.0) < 1e-6, f"Expected 55.0, got {result}"


def test_basic_interpolation_partial():
    """anchor_soc=30%, 1000 Wh added → soc_live ~31.72%."""
    hub = _make_hub(
        last_real_soc=30.0,
        charging=True,
        charged_energy_wh=1000.0,
        anchor_soc=30.0,
        anchor_energy_wh=0.0,
    )
    expected = 30.0 + 1000.0 / (58000.0) * 100.0
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result is not None
    assert abs(result - expected) < 1e-6, f"Expected {expected}, got {result}"


# ---------------------------------------------------------------------------
# Test 2: clamp to 100
# ---------------------------------------------------------------------------

def test_clamp_to_100():
    """Very large chargedEnergy should not push soc_live above 100%."""
    hub = _make_hub(
        last_real_soc=95.0,
        charging=True,
        charged_energy_wh=99999.0,
        anchor_soc=95.0,
        anchor_energy_wh=0.0,
    )
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result is not None
    assert result == 100.0, f"Expected 100.0 (clamped), got {result}"


def test_clamp_floor_is_anchor_soc():
    """Result is never below anchor_soc even with negative delta (e.g. rounding)."""
    hub = _make_hub(
        last_real_soc=60.0,
        charging=True,
        charged_energy_wh=5.0,   # tiny delta — rounds to anchor_soc floor
        anchor_soc=60.0,
        anchor_energy_wh=5.0,    # same → delta=0
    )
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result is not None
    assert result >= 60.0, f"Expected >= 60.0 (anchor floor), got {result}"


# ---------------------------------------------------------------------------
# Test 3: not-charging path → return last_real_soc
# ---------------------------------------------------------------------------

def test_not_charging_returns_last_real_soc():
    hub = _make_hub(
        last_real_soc=72.5,
        charging=False,
        charged_energy_wh=1234.0,
        anchor_soc=70.0,
        anchor_energy_wh=100.0,
    )
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result == 72.5, f"Expected 72.5, got {result}"


def test_no_real_soc_returns_none():
    hub = _make_hub(
        last_real_soc=None,
        charging=True,
        charged_energy_wh=500.0,
        anchor_soc=50.0,
        anchor_energy_wh=0.0,
    )
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result is None, f"Expected None, got {result}"


# ---------------------------------------------------------------------------
# Test 4: re-anchor on new real VW SOC
# ---------------------------------------------------------------------------

def test_reanchor_on_new_real_soc():
    """After _re_anchor with a new VW SOC, interpolation uses the new anchor."""
    hub = _make_hub(
        last_real_soc=60.0,
        charging=True,
        charged_energy_wh=2000.0,
        anchor_soc=50.0,
        anchor_energy_wh=0.0,
    )
    # Simulate a new real SOC arriving while charging.
    hub.last_real_soc = 62.0
    _re_anchor(hub, 62.0)
    # After re-anchor, anchor_energy_wh = 2000 Wh. No delta → soc_live = 62.0.
    result = interpolate_soc(hub, CAPACITY_KWH, EFFICIENCY)
    assert result is not None
    assert abs(result - 62.0) < 1e-6, f"Expected 62.0 after re-anchor, got {result}"


# ---------------------------------------------------------------------------
# Test 5: chargedEnergy reset (session reset)
# ---------------------------------------------------------------------------

def test_charged_energy_reset_via_handle():
    """When chargedEnergy drops below anchor_energy_wh while charging,
    _handle_evcc_message should re-anchor anchor_energy_wh to the new lower value."""
    hub = _make_hub(
        last_real_soc=55.0,
        charging=True,
        charged_energy_wh=3000.0,
        anchor_soc=55.0,
        anchor_energy_wh=3000.0,
    )
    cfg = _FakeCfg()
    # Simulate chargedEnergy dropping to 500 (session reset).
    _handle_evcc_message(hub, cfg.evcc_topic("chargedEnergy"), "500", cfg)
    assert hub.charged_energy_wh == 500.0
    # anchor_energy_wh should have been reset to 500.
    assert hub.anchor_energy_wh == 500.0
    # anchor_soc reset to last_real_soc.
    assert hub.anchor_soc == 55.0


def test_charged_energy_advances_normally():
    """Normal energy increase should NOT trigger a re-anchor."""
    hub = _make_hub(
        last_real_soc=50.0,
        charging=True,
        charged_energy_wh=1000.0,
        anchor_soc=50.0,
        anchor_energy_wh=0.0,
    )
    cfg = _FakeCfg()
    _handle_evcc_message(hub, cfg.evcc_topic("chargedEnergy"), "2000", cfg)
    assert hub.charged_energy_wh == 2000.0
    assert hub.anchor_energy_wh == 0.0   # unchanged


# ---------------------------------------------------------------------------
# Test 6: charging start edge (false → true)
# ---------------------------------------------------------------------------

def test_charging_start_triggers_reanchor():
    """Transition from charging=False to charging=True should re-anchor."""
    hub = _make_hub(
        last_real_soc=48.0,
        charging=False,
        charged_energy_wh=500.0,
        anchor_soc=None,
        anchor_energy_wh=None,
    )
    cfg = _FakeCfg()
    _handle_evcc_message(hub, cfg.evcc_topic("charging"), "true", cfg)
    assert hub.charging is True
    assert hub.anchor_soc == 48.0
    assert hub.anchor_energy_wh == 500.0


def test_charging_stop_does_not_reanchor():
    """Transition from charging=True to charging=False should not touch anchors."""
    hub = _make_hub(
        last_real_soc=75.0,
        charging=True,
        charged_energy_wh=5000.0,
        anchor_soc=70.0,
        anchor_energy_wh=1000.0,
    )
    cfg = _FakeCfg()
    _handle_evcc_message(hub, cfg.evcc_topic("charging"), "false", cfg)
    assert hub.charging is False
    assert hub.anchor_soc == 70.0      # unchanged
    assert hub.anchor_energy_wh == 1000.0  # unchanged


# ---------------------------------------------------------------------------
# Test 7: efficiency factor
# ---------------------------------------------------------------------------

def test_efficiency_factor():
    """With efficiency=0.9, the same energy input yields less SOC gain."""
    hub = _make_hub(
        last_real_soc=50.0,
        charging=True,
        charged_energy_wh=2900.0,
        anchor_soc=50.0,
        anchor_energy_wh=0.0,
    )
    result = interpolate_soc(hub, CAPACITY_KWH, efficiency=0.9)
    expected = 50.0 + 2900.0 / 58000.0 * 100.0 * 0.9
    assert result is not None
    assert abs(result - expected) < 1e-6, f"Expected {expected}, got {result}"


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
    assert config_msg.retain is True

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
    assert state_msg.retain is True
    assert state_msg.payload == "67.3", f"State payload mismatch: {state_msg.payload!r}"


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
    tests = [
        test_basic_interpolation,
        test_basic_interpolation_partial,
        test_clamp_to_100,
        test_clamp_floor_is_anchor_soc,
        test_not_charging_returns_last_real_soc,
        test_no_real_soc_returns_none,
        test_reanchor_on_new_real_soc,
        test_charged_energy_reset_via_handle,
        test_charged_energy_advances_normally,
        test_charging_start_triggers_reanchor,
        test_charging_stop_does_not_reanchor,
        test_efficiency_factor,
        test_soc_live_messages_structure,
        test_soc_live_state_payload_format,
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
