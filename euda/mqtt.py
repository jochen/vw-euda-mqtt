"""Build Home Assistant MQTT Discovery + state messages from an EUDA Dataset.

Maps a curated subset of the EU-Data-Act data points onto stable MQTT subtopics
(matching the old weconnect-to-mqtt layout, base topic e.g. ``ohm/car/id3``) and
reuses the device_class / unit / state_class metadata from the curated registry
in euda/data.py.

Also provides :func:`build_soc_live_messages` for the interpolated live-SOC
sensor that is fed by the evcc feedback loop in euda_service.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .data import (
    CURATED_BINARY,
    CURATED_SENSORS,
    CuratedBinary,
    CuratedSensor,
    Dataset,
)

# field_name -> (mqtt subtopic, German display name). Only these are published;
# everything else in the dataset is ignored to keep the HA device tidy.
SENSOR_TOPICS: dict[str, tuple[str, str]] = {
    "battery_state_report.soc": ("soc", "SOC"),
    "settings.target_soc": ("target_soc", "Ziel-SOC"),
    "battery_state_report.charge_power": ("charge_power_kw", "Ladeleistung"),
    "battery_state_report.charge_rate": ("charge_rate", "Laderate"),
    "battery_state_report.charge_energy": ("charge_energy_kwh", "Geladene Energie"),
    "battery_state_report.remaining_charging_time_complete": (
        "remaining_charge_time_s", "Restladezeit"),
    "charging_state_report.current_charge_state": ("charge_state", "Ladestatus"),
    "charging_state_report.charge_type": ("charge_type", "Ladetyp"),
    "mileage.value": ("odometer_km", "Kilometerstand"),
    "range": ("range_km", "Reichweite"),
    "min_temperature": ("battery_temp_min_c", "Akku-Temp Min"),
    "max_temperature": ("battery_temp_max_c", "Akku-Temp Max"),
}

BINARY_TOPICS: dict[str, tuple[str, str]] = {
    "locked": ("locked", "Verriegelt"),
    "parking_brake": ("parking_brake", "Handbremse"),
}

_CURATED_SENSOR_BY_FIELD: dict[str, CuratedSensor] = {c.field_name: c for c in CURATED_SENSORS}
_CURATED_BINARY_BY_FIELD: dict[str, CuratedBinary] = {c.field_name: c for c in CURATED_BINARY}


@dataclass
class MqttMessage:
    topic: str
    payload: str
    retain: bool = True


def _device(vin: str) -> dict:
    return {
        "identifiers": [vin],
        "name": "VW ID.3",
        "model": "ID.3",
        "manufacturer": "Volkswagen",
    }


def build_soc_live_messages(vin: str, base_topic: str, subtopic: str, soc_live: float) -> list[MqttMessage]:
    """Return [discovery_config_msg, state_msg] for the interpolated live-SOC sensor.

    The discovery config is retained so HA picks it up after restarts.
    The state message is also retained so HA shows the last value immediately.
    """
    device = _device(vin)
    object_id = f"vw_id3_{vin.lower()}_soc_live"
    state_topic = f"{base_topic}/{subtopic}"
    config_topic = f"homeassistant/sensor/{object_id}/config"
    ha_config = {
        "name": "SOC (live)",
        "unique_id": object_id,
        "device_class": "battery",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "state_topic": state_topic,
        "device": device,
    }
    return [
        MqttMessage(config_topic, json.dumps(ha_config), retain=True),
        MqttMessage(state_topic, f"{soc_live:.1f}", retain=True),
    ]


def build_messages(ds: Dataset, base_topic: str) -> list[MqttMessage]:
    """Return discovery + state messages for the curated fields present in ``ds``."""
    vin = ds.vin
    object_id_base = f"vw_id3_{vin.lower()}"
    device = _device(vin)
    msgs: list[MqttMessage] = []

    def add(ha_type: str, subtopic: str, ha_config: dict, state: str) -> None:
        object_id = f"{object_id_base}_{subtopic}"
        state_topic = f"{base_topic}/{subtopic}"
        config = {
            **ha_config,
            "unique_id": object_id,
            "state_topic": state_topic,
            "device": device,
        }
        config_topic = f"homeassistant/{ha_type}/{object_id}/config"
        msgs.append(MqttMessage(config_topic, json.dumps(config), retain=True))
        msgs.append(MqttMessage(state_topic, state, retain=True))

    # --- sensors ---
    for field_name, (subtopic, name) in SENSOR_TOPICS.items():
        dp = ds.by_field(field_name)
        if dp is None or dp.value is None:
            continue
        cur = _CURATED_SENSOR_BY_FIELD.get(field_name)
        unit = dp.unit or (cur.unit if cur else None)
        ha = {"name": name}
        if cur and cur.device_class:
            ha["device_class"] = cur.device_class
        if unit:
            ha["unit_of_measurement"] = unit
        if cur and cur.state_class:
            ha["state_class"] = cur.state_class
        if cur and cur.icon:
            ha["icon"] = cur.icon
        add("sensor", subtopic, ha, str(dp.value))

    # --- binary sensors ---
    for field_name, (subtopic, name) in BINARY_TOPICS.items():
        dp = ds.by_field(field_name)
        if dp is None or dp.value is None:
            continue
        cur = _CURATED_BINARY_BY_FIELD.get(field_name)
        is_on = bool(dp.value)
        if cur and cur.invert:
            is_on = not is_on
        ha = {"name": name, "payload_on": "ON", "payload_off": "OFF"}
        if cur and cur.device_class:
            ha["device_class"] = cur.device_class
        if cur and cur.icon:
            ha["icon"] = cur.icon
        add("binary_sensor", subtopic, ha, "ON" if is_on else "OFF")

    return msgs
