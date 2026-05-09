#!/usr/bin/env python3
"""VW ID.3 → MQTT publisher mit Home Assistant MQTT Discovery (weconnect >= 0.60.9)"""

import json
import os
import sys
import logging
import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensor-Definitionen
# path:     Schlüsselpfad in vehicle.domains (letzter Eintrag = Attributname)
# subtopic: MQTT-Subtopic unter MQTT_BASE_TOPIC
# ha_type:  "sensor" oder "binary_sensor"
# ha:       HA Discovery-Felder (ohne name, unique_id, state_topic, device)
# transform: optionale Wert-Transformation
# ---------------------------------------------------------------------------
def _kelvin(v):
    return round(v - 273.15, 1)

def _enum(v):
    return v.name if hasattr(v, "name") else str(v).split(".")[-1]

SENSORS = [
    # --- Akku ---
    dict(path=["charging", "batteryStatus", "currentSOC_pct"],
         subtopic="soc",
         ha_type="sensor",
         ha=dict(name="SOC", unit_of_measurement="%", device_class="battery", state_class="measurement")),

    dict(path=["charging", "batteryStatus", "cruisingRangeElectric_km"],
         subtopic="range_km",
         ha_type="sensor",
         ha=dict(name="Reichweite", unit_of_measurement="km", device_class="distance", state_class="measurement")),

    # --- Laden ---
    dict(path=["charging", "chargingStatus", "chargingState"],
         subtopic="charging_state",
         ha_type="sensor",
         ha=dict(name="Ladestatus", icon="mdi:ev-station"),
         transform=_enum),

    dict(path=["charging", "chargingStatus", "chargePower_kW"],
         subtopic="charge_power_kw",
         ha_type="sensor",
         ha=dict(name="Ladeleistung", unit_of_measurement="kW", device_class="power", state_class="measurement")),

    dict(path=["charging", "plugStatus", "plugConnectionState"],
         subtopic="plug_connected",
         ha_type="binary_sensor",
         ha=dict(name="Ladekabel", device_class="plug", payload_on="CONNECTED", payload_off="DISCONNECTED"),
         transform=_enum),

    dict(path=["charging", "plugStatus", "externalPower"],
         subtopic="external_power",
         ha_type="binary_sensor",
         ha=dict(name="Externe Stromversorgung", device_class="power", payload_on="AVAILABLE", payload_off="UNAVAILABLE"),
         transform=_enum),

    dict(path=["charging", "chargingSettings", "targetSOC_pct"],
         subtopic="target_soc",
         ha_type="sensor",
         ha=dict(name="Ziel-SOC", unit_of_measurement="%", device_class="battery")),

    # --- Fahrzeugzustand ---
    dict(path=["access", "accessStatus", "doorLockStatus"],
         subtopic="locked",
         ha_type="binary_sensor",
         # HA: lock device_class → ON = unlocked, OFF = locked
         ha=dict(name="Verriegelt", device_class="lock", payload_on="UNLOCKED", payload_off="LOCKED"),
         transform=_enum),

    dict(path=["access", "accessStatus", "overallStatus"],
         subtopic="overall_status",
         ha_type="sensor",
         ha=dict(name="Fahrzeugstatus", icon="mdi:car"),
         transform=_enum),

    # --- Messwerte ---
    dict(path=["measurements", "odometerStatus", "odometer"],
         subtopic="odometer_km",
         ha_type="sensor",
         ha=dict(name="Kilometerstand", unit_of_measurement="km", device_class="distance", state_class="total_increasing")),

    dict(path=["vehicleHealthInspection", "maintenanceStatus", "inspectionDue_days"],
         subtopic="inspection_due_days",
         ha_type="sensor",
         ha=dict(name="Inspektion in", unit_of_measurement="d", icon="mdi:wrench-clock")),

    dict(path=["measurements", "temperatureBatteryStatus", "temperatureHvBatteryMin_K"],
         subtopic="battery_temp_min_c",
         ha_type="sensor",
         ha=dict(name="Akku-Temp Min", unit_of_measurement="°C", device_class="temperature", state_class="measurement"),
         transform=_kelvin),

    dict(path=["measurements", "temperatureBatteryStatus", "temperatureHvBatteryMax_K"],
         subtopic="battery_temp_max_c",
         ha_type="sensor",
         ha=dict(name="Akku-Temp Max", unit_of_measurement="°C", device_class="temperature", state_class="measurement"),
         transform=_kelvin),

    # --- Klimatisierung ---
    dict(path=["climatisation", "climatisationStatus", "climatisationState"],
         subtopic="climatisation_state",
         ha_type="sensor",
         ha=dict(name="Klimatisierung", icon="mdi:air-conditioner"),
         transform=_enum),

    dict(path=["climatisation", "climatisationSettings", "targetTemperature_C"],
         subtopic="target_temp_c",
         ha_type="sensor",
         ha=dict(name="Zieltemperatur Klima", unit_of_measurement="°C", device_class="temperature")),
]


def parse_args():
    p = argparse.ArgumentParser(description="VW ID.3 → MQTT mit HA Discovery")
    p.add_argument("--username",       default=os.environ.get("VW_USERNAME"),               metavar="EMAIL")
    p.add_argument("--password",       default=os.environ.get("VW_PASSWORD"),               metavar="PASS")
    p.add_argument("--vin",            default=os.environ.get("VW_VIN"),                    help="VIN (optional)")
    p.add_argument("--broker",         default=os.environ.get("MQTT_BROKER", "localhost"))
    p.add_argument("--port",    type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    p.add_argument("--base-topic",     default=os.environ.get("MQTT_BASE_TOPIC", "car/id3"), dest="base_topic")
    p.add_argument("--mqtt-user",      default=os.environ.get("MQTT_USER"))
    p.add_argument("--mqtt-password",  default=os.environ.get("MQTT_PASSWORD"))
    p.add_argument("-v", "--verbose",  action="store_true")
    return p.parse_args()


def fetch_data(username, password, target_vin=None):
    from weconnect.weconnect import WeConnect

    wc = WeConnect(username=username, password=password,
                   updateAfterLogin=False, loginOnInit=False)
    wc.login()
    wc.update()

    for vin, vehicle in wc.vehicles.items():
        if target_vin and vin != target_vin:
            continue

        readings = {}
        for sensor in SENSORS:
            try:
                obj = vehicle.domains
                for key in sensor["path"][:-1]:
                    obj = obj[key]
                attr = getattr(obj, sensor["path"][-1])
                value = attr.value
                if value is None:
                    continue
                transform = sensor.get("transform")
                readings[sensor["subtopic"]] = transform(value) if transform else value
            except (KeyError, AttributeError) as e:
                log.debug("Nicht verfügbar: %s — %s", "/".join(sensor["path"]), e)

        if readings:
            return vin, readings

    return None, {}


def build_messages(vin, readings, base_topic):
    object_id_base = f"vw_id3_{vin.lower()}"
    device = {
        "identifiers": [vin],
        "name": "VW ID.3",
        "model": "ID.3",
        "manufacturer": "Volkswagen",
    }

    msgs = []
    for sensor in SENSORS:
        subtopic = sensor["subtopic"]
        if subtopic not in readings:
            continue

        state_topic = f"{base_topic}/{subtopic}"
        object_id   = f"{object_id_base}_{subtopic}"
        ha_type     = sensor["ha_type"]

        config = {
            **sensor["ha"],
            "unique_id":   object_id,
            "state_topic": state_topic,
            "device":      device,
        }
        config_topic = f"homeassistant/{ha_type}/{object_id}/config"

        msgs.append({"topic": config_topic, "payload": json.dumps(config), "retain": True})
        msgs.append({"topic": state_topic,  "payload": str(readings[subtopic]), "retain": True})

    return msgs


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.username or not args.password:
        sys.exit("Fehler: VW_USERNAME und VW_PASSWORD erforderlich")

    vin, readings = fetch_data(args.username, args.password, args.vin)

    if not readings:
        sys.exit("Fehler: Keine Fahrzeugdaten gefunden")

    print(f"VIN {vin} — {len(readings)} Werte:")
    for subtopic, value in readings.items():
        print(f"  {args.base_topic}/{subtopic} = {value}")

    msgs = build_messages(vin, readings, args.base_topic)

    import paho.mqtt.publish as publish
    auth = {"username": args.mqtt_user, "password": args.mqtt_password or ""} if args.mqtt_user else None
    publish.multiple(msgs, hostname=args.broker, port=args.port, auth=auth)

    print(f"\n→ {len(msgs) // 2} Topics + Discovery-Configs an {args.broker}:{args.port} published")


if __name__ == "__main__":
    main()
