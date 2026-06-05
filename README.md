# vw-euda-mqtt

A standalone Python asyncio daemon that pulls **VW ID.3** vehicle data from the
[VW EU Data Act portal](https://eu-data-act.drivesomethinggreater.com/) and
publishes it to MQTT with **Home Assistant MQTT Discovery**.

Repo: https://github.com/jochen/vw-euda-mqtt

---

## Background

On **2026-06-01** Volkswagen retired the classic VW-ID OAuth client
(`a24fba63-…`). The identity provider at `identity.vwgroup.io` now returns
**HTTP 403** for every reverse-engineered login. `weconnect`,
`volkswagencarnet`, `CarConnectivity`, and `evcc` are all broken the same way —
there is no fix.

Under **EU Regulation 2023/2854** (EU Data Act, in force since 2025-09-12) VW
must provide vehicle data in a machine-readable format. The
[EU Data Act portal](https://eu-data-act.drivesomethinggreater.com/) delivers
read-only JSON snapshots on a 15-minute interval. This daemon uses that portal
as its data source.

The original WeConnect-based `soc_to_mqtt.py` is kept in the repository as
historical reference; it no longer works.

---

## What it does

The daemon runs three concurrent asyncio tasks:

1. **VW poll** — authenticates to the portal via OIDC, maintains a persisted
   cookie jar, polls the data-delivery list on an adaptive ~15-minute cadence,
   downloads each new dataset, and publishes curated sensors to MQTT with Home
   Assistant Discovery under `MQTT_BASE_TOPIC` (default `ohm/car/id3`).

2. **evcc listener** — subscribes to evcc's own MQTT topics
   (`charging`, `connected`, `chargedEnergy` on the configured loadpoint) to
   track real-time charging state and session energy.

3. **SOC interpolation** — between VW's 15-minute readings, estimates a live
   SOC anchored to the last real VW reading plus evcc's session energy:

   ```
   soc_live = anchor_soc + chargedEnergy_Wh / (capacity_kWh × 1000) × 100
   ```

   Published to `<base_topic>/soc_live` (configurable). The raw VW SOC on
   `<base_topic>/soc` is never modified. Each new VW dataset re-anchors the
   estimate, so drift self-corrects every ~15 minutes.

---

## Published MQTT topics and Home Assistant entities

All topics are under `MQTT_BASE_TOPIC` (default `ohm/car/id3`).
HA Discovery configs are published to `homeassistant/sensor/…` and
`homeassistant/binary_sensor/…`.

| Subtopic | HA name | Type | Device class |
|---|---|---|---|
| `soc` | SOC | sensor | battery |
| `target_soc` | Ziel-SOC | sensor | battery |
| `charge_power_kw` | Ladeleistung | sensor | power |
| `charge_rate` | Laderate | sensor | — |
| `charge_energy_kwh` | Geladene Energie | sensor | energy |
| `remaining_charge_time_s` | Restladezeit | sensor | duration |
| `charge_state` | Ladestatus | sensor | — |
| `charge_type` | Ladetyp | sensor | — |
| `odometer_km` | Kilometerstand | sensor | distance |
| `range_km` | Reichweite | sensor | distance |
| `battery_temp_min_c` | Akku-Temp Min | sensor | temperature |
| `battery_temp_max_c` | Akku-Temp Max | sensor | temperature |
| `locked` | Verriegelt | binary_sensor | lock |
| `parking_brake` | Handbremse | binary_sensor | — |
| `soc_live` | SOC (live) | sensor | battery |

All messages are published with `retain=True`.

---

## Requirements

- Python 3.9+
- A VW-ID / myVolkswagen account linked to the EU Data Act portal
- An MQTT broker (e.g. Mosquitto)
- Home Assistant with the MQTT integration enabled (optional, for Discovery)

---

## Portal one-time setup

This is a **manual, per-user** step done once in a browser:

1. Log in at https://eu-data-act.drivesomethinggreater.com/ with your VW-ID
   credentials.
2. Go to **Data clusters → Vehicle overview** and link your car.
3. Click **"Get customised data"**, choose **continuous** frequency and set the
   interval to **15 minutes**.
4. Submit. The first datasets can take **several hours** to appear.

---

## Installation

```bash
git clone https://github.com/jochen/vw-euda-mqtt.git
cd vw-euda-mqtt

python3 -m venv venv
./venv/bin/pip install -r requirements-euda.txt

cp .env.example .env
nano .env   # fill in credentials and MQTT settings
```

---

## Configuration

Copy `.env.example` to `.env`. All variables:

| Variable | Default | Description |
|---|---|---|
| `VW_USERNAME` | — | VW-ID email (alias for `EUDA_EMAIL`) |
| `VW_PASSWORD` | — | VW-ID password (alias for `EUDA_PASSWORD`) |
| `EUDA_EMAIL` | — | Portal email (takes precedence over `VW_USERNAME`) |
| `EUDA_PASSWORD` | — | Portal password (takes precedence over `VW_PASSWORD`) |
| `VW_VIN` / `EUDA_VIN` | first vehicle | VIN — only needed with multiple linked vehicles |
| `EUDA_COUNTRY` | `si` | OIDC state country code; try `de` if login fails |
| `EUDA_LANGUAGE` | `sl` | OIDC state language code; try `de` if login fails |
| `MQTT_BROKER` | `localhost` | MQTT broker hostname or IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_BASE_TOPIC` | `ohm/car/id3` | Root topic for all published values |
| `MQTT_USER` | — | MQTT username (optional) |
| `MQTT_PASSWORD` | — | MQTT password (optional) |
| `EVCC_BASE_TOPIC` | `ohm/evcc` | Base topic where evcc publishes |
| `EVCC_LOADPOINT` | `1` | evcc loadpoint number to subscribe to |
| `VEHICLE_CAPACITY_KWH` | `58` | Usable battery capacity in kWh (ID.3 Pro = 58) |
| `SOC_LIVE_SUBTOPIC` | `soc_live` | Subtopic for the interpolated SOC value |
| `INTERP_INTERVAL_S` | `60` | How often (seconds) to recalculate and publish `soc_live` |
| `CHARGE_EFFICIENCY` | `1.0` | Charging efficiency factor for interpolation (e.g. 0.92) |

> **Account lockout warning.** The daemon persists its session cookie and only
> re-authenticates on an explicit 401/403 response. Do **not** wrap it in a
> tight retry loop — repeated failed logins can lock your VW account.

---

## Running

### One-shot connectivity test

```bash
./venv/bin/python euda_probe.py
```

This logs in, lists vehicles, and prints the first available dataset — useful
to confirm credentials and portal access before starting the daemon.

### Foreground (manual test)

```bash
./venv/bin/python euda_service.py
```

### systemd user service (recommended)

See [`systemd/README.md`](systemd/README.md) for full instructions. Quick
summary:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/vw-euda-mqtt.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vw-euda-mqtt

# Persist across reboots without login:
loginctl enable-linger pi
```

Logs:

```bash
journalctl --user -u vw-euda-mqtt -f
```

---

## evcc integration

evcc can use `ohm/car/id3/soc` directly via a custom MQTT vehicle. To get the
interpolated value while charging, point it at `ohm/car/id3/soc_live` instead.

```yaml
mqtt:
  broker: 192.168.1.10:1883

vehicles:
  - name: id3
    type: custom
    title: VW ID.3
    capacity: 58            # kWh (ID.3 Pro)
    soc:
      source: mqtt
      topic: ohm/car/id3/soc   # or ohm/car/id3/soc_live for the interpolated value
      timeout: 90m
```

No extra publishing is required — MQTT is pub/sub and the daemon already
publishes both topics.

---

## License

MIT
