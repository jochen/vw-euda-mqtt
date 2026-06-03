# weconnect-to-mqtt

> # ⛔ BROKEN / EOL — does not work anymore (since 2026-06-01)
>
> Volkswagen shut down the reverse-engineered login this tool relied on. **There is no fix**, and migrating to another library does **not** help — `weconnect`, `volkswagencarnet`, `CarConnectivity` and `evcc` all fail the same way.
>
> - **2026-05-27** — VW (CARIAD) migrated the token endpoint (`/login/v1/idk/token` → `/auth/v1/idk/oidc/token`).
> - **2026-06-01** — VW retired the classic VW-ID OAuth client (`a24fba63-…`). The IdP at `identity.vwgroup.io/oidc/v1/authorize` now returns **HTTP 403** (`Missing 'Location' header in authorization response`).
> - VW additionally requires a per-device **email one-time code (2FA) every ~30 days**, which is fundamentally incompatible with a headless cron job.
> - ⚠️ **Repeated login attempts can get your VW account locked** (rate limiting). Disable any cron job — do not let it keep retrying.
>
> The maintainers of `volkswagencarnet` and `evcc` confirm VW *"has closed all loopholes"* and that this will **probably not be fixed**.
>
> ### The only way forward: EU Data Act portal
>
> Under EU Regulation 2023/2854 (in force since 2025-09-12) VW must provide your vehicle data in a machine-readable format. It is published via the **EU Data Act portal → https://eu-data-act.drivesomethinggreater.com/** as **JSON on a 15-minute interval** (read-only, no remote control). You log in once, link your car and enable the continuous export. Other integrations are migrating to this source (e.g. [ioBroker.vw-connect](https://github.com/Gaspode69/ioBroker.vw-connect) skips the classic login entirely since 2026-06-01).
>
> Everything below is kept for historical reference and no longer functions.

---

Reads live data from a **VW ID.3** (and other WeConnect-enabled VW vehicles) via the [WeConnect Python library](https://github.com/tillsteinbach/WeConnect-python) and publishes **15 sensors** to an MQTT broker with full **Home Assistant MQTT Discovery** support.

Works without the car being plugged in — WeConnect provides the last known state from VW's cloud.

## Features

- State of Charge (SOC), range, charging state & power, plug status
- Door lock state, odometer, inspection countdown
- HV battery temperature (min/max, converted from Kelvin to °C)
- Climatisation state and target temperature
- Auto-discovered in Home Assistant as a single **VW ID.3** device
- Reads credentials from a `.env` file — no `export` or `set -a` needed
- Designed to run as a cron job on a Raspberry Pi

## Requirements

- Python 3.9+
- A myVolkswagen / WeConnect account
- An MQTT broker reachable from the Pi (e.g. Mosquitto)
- Home Assistant with the MQTT integration enabled

> **Note:** `weconnect` is officially end-of-life and **no longer authenticates** (see the banner at the top). Its successor [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity-python) is affected by the same VW shutdown and has no fix either.

## Setup

```bash
git clone https://github.com/jochen-zerties/weconnect-to-mqtt.git
cd weconnect-to-mqtt

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in your credentials
```

## Configuration

Copy `.env.example` to `.env` and edit:

```ini
VW_USERNAME=your@email.de
VW_PASSWORD=yourpassword
# VW_VIN=WVWZZZE1ZMP000000   # optional — only needed with multiple vehicles

MQTT_BROKER=192.168.1.x
MQTT_PORT=1883
MQTT_BASE_TOPIC=car/id3
# MQTT_USER=mqttuser
# MQTT_PASSWORD=mqttpass
```

## Usage

```bash
./venv/bin/python soc_to_mqtt.py
```

Example output:

```
VIN WVWZZZE1ZMP000000 — 15 Werte:
  car/id3/soc = 80
  car/id3/range_km = 277
  car/id3/charging_state = NOT_READY_FOR_CHARGING
  car/id3/charge_power_kw = 0.0
  car/id3/plug_connected = CONNECTED
  ...

→ 15 Topics + Discovery-Configs an 192.168.1.x:1883 published
```

Add `-v` for verbose WeConnect debug output.

## Cron job (every 15 minutes)

```bash
crontab -e
```

```
*/15 * * * * /home/pi/weconnect-to-mqtt/venv/bin/python /home/pi/weconnect-to-mqtt/soc_to_mqtt.py >> /home/pi/weconnect-to-mqtt/soc.log 2>&1
```

## Home Assistant

Once the script runs, Home Assistant auto-discovers a **VW ID.3** device under **Settings → Devices & Services → MQTT** with these entities:

| Entity | Type | Device class |
|--------|------|-------------|
| SOC | sensor | battery |
| Reichweite | sensor | distance |
| Ladestatus | sensor | — |
| Ladeleistung | sensor | power |
| Ladekabel | binary_sensor | plug |
| Externe Stromversorgung | binary_sensor | power |
| Ziel-SOC | sensor | battery |
| Verriegelt | binary_sensor | lock |
| Fahrzeugstatus | sensor | — |
| Kilometerstand | sensor | distance |
| Inspektion in | sensor | — |
| Akku-Temp Min | sensor | temperature |
| Akku-Temp Max | sensor | temperature |
| Klimatisierung | sensor | — |
| Zieltemperatur Klima | sensor | temperature |

No changes to `configuration.yaml` needed.

## Known issues

- **Login fails with HTTP 403 since 2026-06-01** — VW shut down the reverse-engineered auth flow. This is unfixable, see the banner at the top of this README. Disable any cron job to avoid getting your VW account locked.
- _(historical)_ `weconnect==0.60.8` had a `MissingSchema` bug due to an earlier VW auth API change; `>= 0.60.9` worked until the 2026-06-01 shutdown.
- _(historical)_ A few `WARNING: Unknown attribute windowOpen_pct` lines appeared in the output — harmless, weconnect didn't know this field.

## License

MIT
