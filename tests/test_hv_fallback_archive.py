"""battery_level_HV fallback and raw dataset archive."""
import gzip
import json

import euda_service as svc
from euda.data import Dataset
from euda.soc_estimator import SocEstimator

CUR = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
START = "7bddd5e7-43a4-3878-bd63-9502782f77a5"
HV = "ac1108b1-b8cc-3db9-a663-03d387e42223"
HV_STATE = "2a9de7f7-a846-3d18-9883-650de9c4e4e1"


def _ds(cur=None, hv=None, start="74", hv_state="VALID"):
    data = [{"key": START, "dataFieldName": "battery_state_report.soc", "value": start}]
    if cur is not None:
        data.append({"key": CUR, "dataFieldName": "battery_state_report.soc", "value": cur})
    if hv is not None:
        data += [{"key": HV, "dataFieldName": "battery_level_HV.value", "value": hv},
                 {"key": HV_STATE, "dataFieldName": "battery_level_HV.state", "value": hv_state}]
    return Dataset.from_json({"vin": "X", "Data": data})


def _hub():
    return svc.Hub(est=SocEstimator())


def test_primary_wins():
    h = _hub()
    assert svc._select_soc(_ds(cur="36", hv="36.0"), h) == (36.0, "battery_state_report")


def test_real_sequence_2026_11_21():
    h = _hub()
    for _ in range(3):
        assert svc._select_soc(_ds(cur="36", hv="36.0"), h)[0] == 36.0
    # 10:56 dataset: primary missing, HV changed 36 → 34 → use it.
    assert svc._select_soc(_ds(hv="34.0"), h) == (34.0, "battery_level_HV (Fallback)")


def test_unchanged_hv_offset_does_not_pull_anchor():
    # 18.11.: primary 44, HV 42 constantly. A dataset without primary but the
    # same HV 42 carries no news → no reading (would otherwise drop 44 → 42).
    h = _hub()
    svc._select_soc(_ds(cur="44", hv="42.0"), h)
    assert svc._select_soc(_ds(hv="42.0"), h) is None


def test_invalid_hv_ignored():
    h = _hub()
    assert svc._select_soc(_ds(hv="12.0", hv_state="INVALID"), h) is None


def test_neither_present():
    assert svc._select_soc(_ds(), _hub()) is None


def test_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "ARCHIVE_DIR", tmp_path)
    payload = {"vin": "X", "Data": [{"key": "k", "value": "1"}]}
    svc._archive_dataset("20261121105607_WVWZZZE1ZMP034416.zip", payload)
    f = tmp_path / "2026-11" / "20261121105607_WVWZZZE1ZMP034416.json.gz"
    assert json.loads(gzip.open(f, "rt").read()) == payload
    svc._archive_dataset("20261121105607_WVWZZZE1ZMP034416.zip", {"other": 1})  # no overwrite
    assert json.loads(gzip.open(f, "rt").read()) == payload
