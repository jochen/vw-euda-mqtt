"""battery_state_report.soc exists under several keys with different meaning
(incident 2026-11-21): only the current-SOC key may be used."""
from euda.data import PINNED_FIELD_KEYS, Dataset

CURRENT = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
AT_CHARGE_START_A = "93b55324-6628-36df-8f76-8eba797fc59c"
AT_CHARGE_START_B = "7bddd5e7-43a4-3878-bd63-9502782f77a5"
FIELD = "battery_state_report.soc"


def _ds(*entries):
    return Dataset.from_json({"vin": "X", "Data": [
        {"key": k, "dataFieldName": FIELD, "value": v} for k, v in entries]})


def test_pinned_key_is_current_soc():
    assert PINNED_FIELD_KEYS[FIELD] == CURRENT


def test_current_soc_wins_over_charge_start_values():
    # Order as seen in dataset 20261121091213: charge-start keys sort first.
    ds = _ds((AT_CHARGE_START_A, "74"), (AT_CHARGE_START_B, "74"), (CURRENT, "36"))
    assert ds.by_field(FIELD).value == 36


def test_missing_current_soc_gives_none_not_charge_start_value():
    # Dataset 20261121105607 had only the charge-start keys → must not yield 74.
    ds = _ds((AT_CHARGE_START_A, "74"), (AT_CHARGE_START_B, "74"))
    assert ds.by_field(FIELD) is None


def test_other_fields_unchanged():
    ds = Dataset.from_json({"vin": "X", "Data": [
        {"key": "b3b04f31-b10e-38aa-b8ad-c0da7c06caea", "dataFieldName": "settings.target_soc", "value": "80"},
        {"key": "e21d4d25-98bc-3016-a234-1ad061cc313b", "dataFieldName": "settings.target_soc", "value": "80"},
    ]})
    assert ds.by_field("settings.target_soc").value == 80


def test_estimator_state_roundtrip():
    from euda.soc_estimator import SocEstimator
    a = SocEstimator(stale_after_s=1e9)
    a.portal_ok(1000.0)
    a.evcc_energy(1000.0, 0.0)
    a.vw_reading(36, 500.0)
    a.evcc_energy(2000.0, 5800.0)
    b = SocEstimator(stale_after_s=1e9)
    b.load_dict(a.to_dict())
    assert b.estimate(2100.0) == a.estimate(2100.0) == 46.0
    # Session keeps growing after restart → estimate continues, no drop.
    b.evcc_energy(3000.0, 11600.0)
    assert b.estimate(3100.0) == 56.0
