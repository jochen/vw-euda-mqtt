# vw-euda-mqtt

A standalone Python asyncio daemon that pulls **VW ID.3** vehicle data from the
[VW EU Data Act portal](https://eu-data-act.vwgroup-datahub.com/) (moved from drivesomethinggreater.com on 2026-11-16) and
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
[EU Data Act portal](https://eu-data-act.vwgroup-datahub.com/) delivers
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

3. **SOC interpolation** — VW's SOC only changes when the car sends a new
   battery report; every 15-minute dataset in between *repeats* the old value.
   The estimator therefore only takes a VW reading when its value changes,
   dates it by the dataset's car capture time, and adds the energy evcc
   charged **since that moment** (from a cumulative, timestamped history of
   evcc's per-session `chargedEnergy`):

   ```
   soc_live = vw_soc + (E(now) - E(t_measured)) / (capacity_kWh × 1000) × 100 × efficiency
   ```

   Repeated or late readings therefore never throw `soc_live` back during
   charging. If the portal has not delivered for `SOC_STALE_AFTER_S`, `soc_live`
   is withheld so evcc's MQTT timeout takes over. The raw VW SOC on
   `<base_topic>/soc` is published unchanged.

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

1. Log in at https://eu-data-act.vwgroup-datahub.com/ with your VW-ID
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
| `CHARGE_EFFICIENCY` | `1.0` | Charging efficiency factor for interpolation (real data: 0.98 on 2026-11-18, 0.81 on 2026-11-21; production uses 0.85) |
| `SOC_STALE_AFTER_S` | `2700` | Stop publishing `soc_live` when the portal has not delivered for this long |
| `SOC_MANUAL_TOPIC` | `<base>/soc_manual/set` | Manually reported SOC (e.g. from the VW app); becomes the new anchor. Result JSON (retained) on `<base>/soc_manual/result` |

**Manual SOC.** Publish a number (`62`, `62%`, `62,5` or `{"soc": 62}`, *not*
retained) to `ohm/car/id3/soc_manual/set`. It immediately becomes the anchor for
`soc_live`; only a *newer* portal reading replaces it. The result
(`previous_estimate`, measured `efficiency` if ≥ 3 kWh were charged since the
last anchor) is published retained on `ohm/car/id3/soc_manual/result`;
calibrations are kept in `euda_estimator.json`.

**Fallback `battery_level_HV.value`.** If a dataset lacks the current-SOC key,
`battery_level_HV.value` (state `VALID`) is used — only when it changed since
the previous dataset, because it can sit 1–2 % off the primary value.

**Raw dataset archive.** Every downloaded dataset is stored gzip-compressed in
`archive/YYYY-MM/<name>.json.gz` (`VW_ARCHIVE_DIR`, empty = off; ~8 KB each).

**Persistence.** Anchor and energy history are saved to `euda_estimator.json`
every tick, so a restart no longer resets `soc_live` to the last VW value.

**Pinned SOC key.** Three data points are called `battery_state_report.soc`;
only `506cb83e-…` is the current SOC (the others are "SOC when charging
started"). See `PINNED_FIELD_KEYS` in `euda/data.py`.

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

Point evcc at **`ohm/car/id3/soc_live`** via a custom MQTT vehicle. This topic
is **non-retained** and republished every `INTERP_INTERVAL_S` (60 s), so a short
evcc `timeout` reliably flags the SOC as stale if this service stops — evcc then
won't keep charging against a frozen value. Use the live topic, not the raw
`soc` (which is retained for HA display and would linger after an outage).

```yaml
mqtt:
  broker: 192.168.1.10:1883

vehicles:
  - name: id3
    type: custom
    title: VW ID.3
    capacity: 58              # kWh (ID.3 Pro)
    soc:
      source: mqtt
      topic: ohm/car/id3/soc_live   # non-retained, refreshed every 60 s
      timeout: 10m                  # short: soc_live is fresh; older ⇒ service down
```

No extra publishing is required — MQTT is pub/sub and the daemon already
publishes the topic.

### Start value, trust rules and measured cap (since 2026-11-21)

`soc_live = anchor + charged × CHARGE_EFFICIENCY − km since anchor × DRIVE_KWH_PER_KM`

- **Driving.** The portal SOC often lags 12–24 h, but `mileage.value` comes in
  almost every dataset. Driving since the anchor is subtracted, so a SOC
  reported at unplug (manual, e.g. via Gaston) is still a good start value at
  the next plug-in. Pure-driving intervals ≥ 10 km between two anchors are
  logged as consumption samples (`drive_calibrations` in the state file).
- **Trust.** A manual SOC locks the anchor for `MANUAL_LOCK_H` (24 h) or until
  `MANUAL_LOCK_KM` (30 km) are driven. In that window VW values only *confirm*
  (within `SOC_TOLERANCE_PCT`) or *conflict* — they never replace it. Outside
  the lock, a VW value more than the tolerance **below** the model after
  charging, with the car not moved, is a stale pre-charge report and is
  rejected. Conflicts are published retained as JSON on
  `ohm/car/id3/soc_conflict` (`SOC_CONFLICT_TOPIC`), e.g. for Gaston to ask
  for the app value.
- **Measured cap.** The Elli Connect reports no measurements over EEBUS; evcc's
  `chargePower` is offered current × 230 V × phases and keeps counting when the
  car stops drawing. The service caps it at the measured site consumption
  (evcc `site/pvPower + grid/power + battery/power`, i.e. the Fronius) minus
  `HOME_BASE_LOAD_W` and subtracts the excess from the session energy. Without
  fresh site values (e.g. Modbus down) nothing is capped. A real meter in the
  wallbox supply would make this unnecessary.

---

## License

MIT
