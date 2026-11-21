#!/usr/bin/env python3
"""VW EU Data Act → MQTT daemon (Home Assistant Discovery).

Long-running asyncio service: keeps one authenticated portal session (cookie
jar persisted to disk), polls the data-delivery list on an adaptive ~15-minute
cadence, downloads each new dataset and republishes the curated readings to
MQTT. Replaces the dead weconnect-based soc_to_mqtt.py.

Lockout-safe: the session is reused across polls; we only re-login when the
portal actively rejects a request (401/403), never in a tight loop.

New in this version:
- Persistent async MQTT client via aiomqtt (publish + subscribe in one loop).
- evcc feedback loop: subscribes to evcc loadpoint topics to track charging
  state and session energy.
- SOC interpolation: estimates live SOC between VW's 15-minute portal updates
  and publishes it to <base_topic>/soc_live.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import aiohttp
import aiomqtt

sys.path.insert(0, str(Path(__file__).parent))
from euda import api as euda_api  # noqa: E402
from euda.api import ApiError, AuthError, EudaApiClient  # noqa: E402
from euda.data import Dataset  # noqa: E402
from euda.mqtt import build_messages, soc_live_discovery, soc_live_state  # noqa: E402
from euda.soc_estimator import SocEstimator  # noqa: E402

log = logging.getLogger("euda_service")

HERE = Path(__file__).parent
COOKIE_FILE = HERE / "euda_cookies.pickle"
STATE_FILE = HERE / "euda_state.json"
ESTIMATOR_FILE = HERE / "euda_estimator.json"
# Raw datasets are archived here (gzip, per month). Empty VW_ARCHIVE_DIR = off.
_archive_env = os.environ.get("VW_ARCHIVE_DIR", str(HERE / "archive"))
ARCHIVE_DIR = Path(_archive_env) if _archive_env else None

NO_CONTENT = "_no_content_found.zip"
DATASET_INTERVAL = 15 * 60       # seconds
POST_DATASET_BUFFER = 45         # seconds
RETRY_INTERVAL = 60              # seconds (not-ready / transient)
AUTH_RETRY_INITIAL = 300         # seconds (AuthError: VW sperrt Accounts bei
AUTH_RETRY_MAX = 4 * 3600        #   Login-Stürmen — exponentiell zurückhalten)
MIN_INTERVAL = 30                # seconds
MQTT_RECONNECT_DELAY = 10        # seconds (initial backoff on MqttError)
MQTT_RECONNECT_MAX = 120         # seconds (max backoff)
MQTT_KEEPALIVE_INTERVAL = 30     # seconds (availability ping — surfaces dead links)
AVAILABILITY_SUBTOPIC = "availability"


# ---------------------------------------------------------------------------
# Shared mutable state between tasks
# ---------------------------------------------------------------------------

@dataclass
class Hub:
    """Mutable shared state between the concurrent tasks (single event loop,
    so no locking)."""
    est: SocEstimator = field(default_factory=SocEstimator)

    # VIN (set once after first successful poll).
    vin: Optional[str] = None

    # ---- evcc state (updated by the evcc listener task) ------------------
    charging: bool = False
    connected: bool = False
    charged_energy_wh: float = 0.0   # current session energy in Wh

    last_hv_soc: Optional[float] = None   # last battery_level_HV.value seen
    last_published_soc_live: Optional[float] = None
    stale_logged: bool = False


# ---------------------------------------------------------------------------
# Dataset timestamp helpers
# ---------------------------------------------------------------------------

def _filename_timestamp(name: str) -> datetime | None:
    stem = name.rsplit(".", 1)[0]
    for part in reversed(stem.split("_")):
        try:
            return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _created_on(entry: dict) -> datetime | None:
    raw = entry.get("createdOn")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return _filename_timestamp(entry.get("name", ""))


def _next_delay(listing: list[dict]) -> float:
    """Seconds until the next poll: ~15 min after the newest known dataset,
    else a short retry when the next drop is already overdue."""
    timestamps = [ts for e in listing if (ts := _created_on(e))]
    if timestamps:
        target = max(timestamps).timestamp() + DATASET_INTERVAL + POST_DATASET_BUFFER
        delta = target - datetime.now(timezone.utc).timestamp()
        if delta > MIN_INTERVAL:
            return delta
    return RETRY_INTERVAL


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log.warning("State konnte nicht gespeichert werden: %s", e)


def _load_estimator(est: SocEstimator) -> None:
    """Restore anchor + energy history so a restart doesn't lose the charged
    energy since the last VW reading (otherwise soc_live drops back)."""
    try:
        data = json.loads(ESTIMATOR_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.warning("Schätzer-Zustand nicht ladbar (%s) — starte leer", e)
        return
    est.load_dict(data)
    if est.vw_soc is not None and est.vw_soc_time is not None:
        log.info("Schätzer-Zustand geladen: VW-SOC %.0f%% (gemessen %s), %d Energiepunkte",
                 est.vw_soc, datetime.fromtimestamp(est.vw_soc_time, timezone.utc).isoformat(),
                 len(est.energy.times))


def _save_estimator(est: SocEstimator) -> None:
    tmp = ESTIMATOR_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(est.to_dict()), encoding="utf-8")
        tmp.replace(ESTIMATOR_FILE)
    except OSError as e:
        log.warning("Schätzer-Zustand konnte nicht gespeichert werden: %s", e)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self) -> None:
        self.email = os.environ.get("EUDA_EMAIL") or os.environ.get("VW_USERNAME")
        self.password = os.environ.get("EUDA_PASSWORD") or os.environ.get("VW_PASSWORD")
        self.vin = os.environ.get("VW_VIN") or os.environ.get("EUDA_VIN")
        self.country = os.environ.get("EUDA_COUNTRY")
        self.language = os.environ.get("EUDA_LANGUAGE")
        self.broker = os.environ.get("MQTT_BROKER", "localhost")
        self.port = int(os.environ.get("MQTT_PORT", "1883"))
        self.base_topic = os.environ.get("MQTT_BASE_TOPIC", "ohm/car/id3")
        self.mqtt_user = os.environ.get("MQTT_USER")
        self.mqtt_password = os.environ.get("MQTT_PASSWORD")

        # evcc / interpolation
        self.evcc_base_topic = os.environ.get("EVCC_BASE_TOPIC", "ohm/evcc")
        self.evcc_loadpoint = os.environ.get("EVCC_LOADPOINT", "1")
        self.vehicle_capacity_kwh = float(os.environ.get("VEHICLE_CAPACITY_KWH", "58"))
        self.soc_live_subtopic = os.environ.get("SOC_LIVE_SUBTOPIC", "soc_live")
        # Manual SOC (e.g. read from the VW app, sent by Gaston): publish a
        # number (non-retained) here → becomes the new anchor immediately.
        # The outcome is published retained as JSON on <base>/soc_manual/result.
        self.soc_manual_topic = os.environ.get(
            "SOC_MANUAL_TOPIC", f"{self.base_topic}/soc_manual/set")
        self.soc_manual_result_topic = self.soc_manual_topic.rsplit("/", 1)[0] + "/result"
        self.interp_interval_s = float(os.environ.get("INTERP_INTERVAL_S", "60"))
        self.charge_efficiency = float(os.environ.get("CHARGE_EFFICIENCY", "1.0"))
        # Withhold soc_live when the portal has not delivered for this long
        # (3 missed 15-min drops) — evcc's MQTT timeout then takes over.
        self.soc_stale_after_s = float(os.environ.get("SOC_STALE_AFTER_S", "2700"))
        # soc_live feeds evcc → keep it non-retained so a stale value can't
        # linger and make evcc charge on after this service stops.
        self.soc_live_retain = os.environ.get("SOC_LIVE_RETAIN", "false").lower() in (
            "1", "true", "yes",
        )

    def evcc_topic(self, field: str) -> str:
        return f"{self.evcc_base_topic}/loadpoints/{self.evcc_loadpoint}/{field}"


# ---------------------------------------------------------------------------
# MQTT publish helper (uses the shared aiomqtt client)
# ---------------------------------------------------------------------------

async def _async_publish(client: aiomqtt.Client, messages) -> None:
    """Publish a list of MqttMessage objects via the shared aiomqtt client."""
    for m in messages:
        await client.publish(m.topic, payload=m.payload, retain=m.retain)


# ---------------------------------------------------------------------------
# Task 1: VW poll loop
# ---------------------------------------------------------------------------

async def vw_poll_task(
    cfg: Config,
    hub: Hub,
    mqtt_client_ref: list,   # [aiomqtt.Client] — populated by mqtt_task
    stop: asyncio.Event,
) -> None:
    """Polls the VW EU-Data-Act portal and publishes curated sensor data."""
    if cfg.country and cfg.language:
        brand = euda_api.OIDC_STATE.split("__")[-1]
        euda_api.OIDC_STATE = f"{cfg.country}__{cfg.language}__{brand}"
        log.info("OIDC state -> %s", euda_api.OIDC_STATE)

    jar = aiohttp.CookieJar()
    if COOKIE_FILE.exists():
        try:
            jar.load(COOKIE_FILE)
            log.info("Session-Cookies geladen")
        except Exception as e:  # noqa: BLE001
            log.warning("Cookies nicht ladbar (%s)", e)

    state = _load_state()

    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        client = EudaApiClient(session, cfg.email, cfg.password)
        if COOKIE_FILE.exists():
            client._logged_in = True  # noqa: SLF001  reuse; re-login on 401/403

        # Startup-Login darf den Service nicht crashen (systemd-Restart-Loop
        # = Login-Sturm): bei AuthError mit Backoff weiterprobieren.
        auth_backoff = AUTH_RETRY_INITIAL
        vin = identifier = None
        while vin is None and not stop.is_set():
            try:
                vin, identifier = await _resolve_target(client, cfg)
            except AuthError as e:
                log.error("Auth fehlgeschlagen: %s — Retry in %ds", e, auth_backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=auth_backoff)
                except asyncio.TimeoutError:
                    pass
                auth_backoff = min(auth_backoff * 2, AUTH_RETRY_MAX)
        if vin is None:
            return
        jar.save(COOKIE_FILE)
        hub.vin = vin
        log.info("Ziel: VIN %s, Identifier %s", vin, identifier)

        auth_backoff = AUTH_RETRY_INITIAL
        while not stop.is_set():
            delay = RETRY_INTERVAL
            try:
                mqtt_client = mqtt_client_ref[0] if mqtt_client_ref else None
                identifier, delay = await _poll_once(
                    client, cfg, vin, identifier, state, hub, mqtt_client
                )
                jar.save(COOKIE_FILE)
                auth_backoff = AUTH_RETRY_INITIAL
            except AuthError as e:
                delay = auth_backoff
                auth_backoff = min(auth_backoff * 2, AUTH_RETRY_MAX)
                log.error("Auth fehlgeschlagen: %s — Retry in %ds", e, delay)
            except ApiError as e:
                log.warning("API-Fehler: %s — Retry in %ds", e, RETRY_INTERVAL)
            except Exception:  # noqa: BLE001
                log.exception("Unerwarteter Fehler — Retry in %ds", RETRY_INTERVAL)

            log.debug("Nächster Poll in %.0fs", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


async def _resolve_target(client: EudaApiClient, cfg: Config) -> tuple[str, str]:
    vehicles = await client.async_list_vehicles()
    if not vehicles:
        raise ApiError("Keine Fahrzeuge im Portal verknüpft")
    vin = cfg.vin or vehicles[0]["vin"]
    meta = await client.async_get_metadata(vin)
    identifier = meta.get("Identifier") or meta.get("identifier")
    if not identifier:
        raise ApiError(f"Kein Identifier in Metadata (keys={list(meta)[:8]})")
    return vin, identifier


async def _poll_once(
    client: EudaApiClient,
    cfg: Config,
    vin: str,
    identifier: str,
    state: dict,
    hub: Hub,
    mqtt_client: Optional[aiomqtt.Client],
) -> tuple[str, float]:
    """One list→download→publish cycle. Returns (identifier, next_delay_seconds)."""
    try:
        listing = await client.async_list_datasets(vin, identifier)
    except ApiError as e:
        msg = str(e)
        if "HTTP 400" in msg or "HTTP 404" in msg or "HTTP 500" in msg:
            new_id = await _maybe_refresh_identifier(client, vin, identifier)
            if new_id:
                return new_id, RETRY_INTERVAL
            log.info("Data-Delivery noch nicht bereit (%s) — warte %ds",
                     msg.split("->")[-1].strip(), RETRY_INTERVAL)
            return identifier, RETRY_INTERVAL
        raise

    hub.est.portal_ok(time.time())
    content = [e for e in listing if e.get("name") and not e["name"].endswith(NO_CONTENT)]
    if not content:
        new_id = await _maybe_refresh_identifier(client, vin, identifier)
        if new_id:
            return new_id, RETRY_INTERVAL
        log.info("Noch keine Datasets mit Inhalt (%d gelistet)", len(listing))
        return identifier, _next_delay(listing)

    newest = max(content, key=lambda e: _created_on(e) or datetime.min.replace(tzinfo=timezone.utc))
    # After a restart the estimator is empty: re-read the newest dataset once
    # even if it was already published, so soc_live is available right away.
    if newest["name"] == state.get("last_dataset") and hub.est.vw_soc is not None:
        log.debug("Kein neues Dataset (%s)", newest["name"])
        return identifier, _next_delay(listing)

    log.info("Neues Dataset: %s", newest["name"])
    payload = await client.async_download_dataset(vin, identifier, newest["name"])
    _archive_dataset(newest["name"], payload)
    ds = Dataset.from_json(payload)
    messages = build_messages(ds, cfg.base_topic)

    if messages:
        if mqtt_client is not None:
            await _async_publish(mqtt_client, messages)
        else:
            log.warning("MQTT-Client noch nicht bereit; Sensordaten verworfen")
        log.info("→ %d Sensoren an %s:%d (%s) publiziert (captured_at=%s)",
                 len(messages) // 2, cfg.broker, cfg.port, cfg.base_topic, ds.captured_at)
    else:
        log.warning("Dataset enthielt keine kuratierten Felder")

    # Offer the VW SOC to the estimator. It only re-anchors when the value
    # changed — datasets keep repeating the car's last battery report.
    picked = _select_soc(ds, hub)
    if picked is not None:
        new_soc, source = picked
        measured = _reading_time(ds, newest)
        if hub.est.vw_reading(new_soc, measured):
            log.info("Neuer VW-SOC %.0f%% aus %s (gemessen ~%s) → Anker gesetzt",
                     new_soc, source, datetime.fromtimestamp(measured, timezone.utc).isoformat())
        else:
            log.info("VW-SOC %.0f%% aus %s unverändert (Wiederholung) → Anker bleibt",
                     new_soc, source)

    state["last_dataset"] = newest["name"]
    _save_state(state)
    return identifier, _next_delay(listing)


def _select_soc(ds: Dataset, hub: Hub) -> tuple[float, str] | None:
    """Pick the SOC reading from a dataset.

    Primary: battery_state_report.soc (pinned current-SOC key).
    Fallback: battery_level_HV.value (state VALID) — but only if it *changed*
    since we last saw it. It runs ~0–2 % off the primary (18.11.: 42 vs 44),
    so an unchanged HV value carries no news and must not pull the anchor
    down; a changed one is a fresh report (21.11. 10:56: primary missing,
    HV 36 → 34 just before charging).
    """
    hv = ds.by_field("battery_level_HV.value")
    hv_state = ds.by_field("battery_level_HV.state")
    hv_val = None
    if hv is not None and hv.value is not None and (
            hv_state is None or str(hv_state.value).upper() == "VALID"):
        try:
            hv_val = float(hv.value)
        except (TypeError, ValueError):
            hv_val = None
    prev_hv, hub.last_hv_soc = hub.last_hv_soc, (hv_val if hv_val is not None else hub.last_hv_soc)

    dp = ds.by_field("battery_state_report.soc")
    if dp is not None and dp.value is not None:
        return float(dp.value), "battery_state_report"
    if hv_val is not None and hv_val != prev_hv:
        return hv_val, "battery_level_HV (Fallback)"
    return None


def _archive_dataset(name: str, payload: dict) -> None:
    """Keep every downloaded raw dataset (evidence + later analysis)."""
    if not ARCHIVE_DIR:
        return
    try:
        stamp = _filename_timestamp(name) or datetime.now(timezone.utc)
        d = ARCHIVE_DIR / stamp.strftime("%Y-%m")
        d.mkdir(parents=True, exist_ok=True)
        f = d / (name.rsplit(".", 1)[0] + ".json.gz")
        if not f.exists():
            with gzip.open(f, "wt", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
    except OSError as e:
        log.warning("Dataset-Archiv: %s nicht gespeichert: %s", name, e)


def _reading_time(ds: Dataset, entry: dict) -> float:
    """Best guess for when the car measured the values in ``ds``: the newest
    car capture time, but never later than the dataset's creation."""
    created = _created_on(entry) or datetime.now(timezone.utc)
    t = ds.captured_at if ds.captured_at and ds.captured_at <= created else created
    return t.timestamp()


async def _maybe_refresh_identifier(client: EudaApiClient, vin: str, identifier: str) -> str | None:
    try:
        meta = await client.async_get_metadata(vin)
    except ApiError as e:
        log.debug("Identifier-Refresh fehlgeschlagen: %s", e)
        return None
    new_id = meta.get("Identifier") or meta.get("identifier")
    if new_id and new_id != identifier:
        log.warning("Identifier geändert (%s -> %s) — Subscription wohl neu angelegt",
                    identifier, new_id)
        return new_id
    return None


# ---------------------------------------------------------------------------
# Task 2: evcc listener (subscribes to evcc loadpoint topics)
# ---------------------------------------------------------------------------

async def evcc_listener_task(
    cfg: Config,
    hub: Hub,
    stop: asyncio.Event,
) -> None:
    """Subscribe to evcc MQTT topics and feed charging state/energy into the hub."""
    lp = cfg.evcc_loadpoint
    evcc_base = cfg.evcc_base_topic

    topics = {
        cfg.evcc_topic("charging"): "_on_charging",
        cfg.evcc_topic("connected"): "_on_connected",
        cfg.evcc_topic("chargedEnergy"): "_on_charged_energy",
        cfg.evcc_topic("vehicleSoc"): "_on_vehicle_soc",
    }

    backoff = MQTT_RECONNECT_DELAY
    while not stop.is_set():
        try:
            async with aiomqtt.Client(
                hostname=cfg.broker,
                port=cfg.port,
                username=cfg.mqtt_user,
                password=cfg.mqtt_password,
                identifier=f"vweuda-evcc-listener-{lp}",
            ) as client:
                backoff = MQTT_RECONNECT_DELAY  # reset on success
                for topic in topics:
                    await client.subscribe(topic)
                await client.subscribe(cfg.soc_manual_topic)
                log.info("evcc listener: abonniert unter %s/loadpoints/%s/... und %s",
                         evcc_base, lp, cfg.soc_manual_topic)

                # aiomqtt 2.x: `client.messages` is an async-generator property,
                # iterated directly (no `.messages()` call / context manager).
                async for message in client.messages:
                    if stop.is_set():
                        break
                    t = str(message.topic)
                    payload = message.payload
                    raw = (payload.decode(errors="replace") if isinstance(payload, bytes)
                           else str(payload)).strip()
                    if t == cfg.soc_manual_topic:
                        if message.retain:
                            # A retained "set" would re-apply an old value on
                            # every reconnect — refuse it.
                            log.warning("Manueller SOC als retained-Nachricht ignoriert: %r", raw)
                            continue
                        result = _handle_manual_soc(hub, raw)
                        _save_estimator(hub.est)
                        await client.publish(cfg.soc_manual_result_topic,
                                             json.dumps(result), retain=True)
                        continue
                    _handle_evcc_message(hub, t, raw, cfg)

        except aiomqtt.MqttError as e:
            if stop.is_set():
                break
            log.warning("evcc listener MQTT-Fehler: %s — reconnect in %ds", e, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MQTT_RECONNECT_MAX)
        except Exception:  # noqa: BLE001
            if stop.is_set():
                break
            log.exception("evcc listener: unerwarteter Fehler — retry in %ds", backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MQTT_RECONNECT_MAX)


def _parse_manual_soc(raw: str) -> float:
    """Accept '50', '50%', '50,5' or JSON {"soc": 50}."""
    s = raw.strip()
    if s.startswith("{"):
        s = str(json.loads(s).get("soc", ""))
    s = s.rstrip("%").strip().replace(",", ".")
    soc = float(s)
    if not 0.0 <= soc <= 100.0:
        raise ValueError(f"außerhalb 0–100: {soc}")
    return soc


def _handle_manual_soc(hub: Hub, raw: str) -> dict:
    """Apply a manually reported SOC; returns the result published as JSON."""
    now = time.time()
    try:
        soc = _parse_manual_soc(raw)
    except (ValueError, TypeError, AttributeError) as e:
        log.warning("Manueller SOC ungültig (%r): %s", raw, e)
        return {"ok": False, "error": f"ungültiger Wert: {raw!r}", "ts": now}
    r = hub.est.manual_reading(soc, now)
    hub.stale_logged = False
    log.info("Manueller SOC %.1f%% übernommen → Anker gesetzt (vorher geschätzt %s%%, "
             "seit letztem Anker %s Wh, gemessene Effizienz %s; eingestellt %.2f)",
             soc, r["previous_estimate"], r["delta_wh"], r["efficiency"], hub.est.efficiency)
    return {"ok": True, **r, "configured_efficiency": hub.est.efficiency,
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat()}


def _handle_evcc_message(hub: Hub, topic: str, raw: str, cfg: Config) -> None:
    """Parse one evcc MQTT message and update hub state."""
    lp = cfg.evcc_loadpoint
    suffix = topic.split(f"/loadpoints/{lp}/", 1)[-1] if f"/loadpoints/{lp}/" in topic else ""

    if suffix == "charging":
        hub.charging = raw.lower() == "true"
        log.debug("evcc charging: %s", hub.charging)

    elif suffix == "connected":
        hub.connected = raw.lower() == "true"
        log.debug("evcc connected: %s", hub.connected)

    elif suffix == "chargedEnergy":
        try:
            new_energy = float(raw)
        except ValueError:
            log.warning("chargedEnergy: ungültiger Wert %r", raw)
            return
        hub.charged_energy_wh = new_energy
        hub.est.evcc_energy(time.time(), new_energy)
        log.debug("evcc chargedEnergy: %.0f Wh", new_energy)

    elif suffix == "vehicleSoc":
        try:
            log.debug("evcc vehicleSoc: %s (ignoriert)", raw)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Task 3: interpolation loop
# ---------------------------------------------------------------------------

async def interpolation_task(
    cfg: Config,
    hub: Hub,
    mqtt_client_ref: list,   # [aiomqtt.Client]
    stop: asyncio.Event,
) -> None:
    """Periodic SOC interpolation tick; publishes soc_live when changed."""
    interval = cfg.interp_interval_s
    discovery_sent = False

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break

        now = time.time()
        _save_estimator(hub.est)
        soc_live = hub.est.estimate(now)
        if soc_live is None:
            if hub.est.vw_soc is not None and hub.est.is_stale(now):
                if not hub.stale_logged:
                    log.warning("Portal-Daten veraltet (> %.0f s ohne Erfolg) — "
                                "soc_live wird NICHT publiziert (evcc-timeout greift)",
                                hub.est.stale_after_s)
                    hub.stale_logged = True
            else:
                log.debug("interpolation: noch kein realer VW-SOC — überspringe")
            continue
        if hub.stale_logged:
            log.info("Portal-Daten wieder aktuell — soc_live wird wieder publiziert")
            hub.stale_logged = False

        mqtt_client = mqtt_client_ref[0] if mqtt_client_ref else None
        if mqtt_client is None:
            log.debug("interpolation: MQTT-Client noch nicht bereit")
            continue

        vin = hub.vin
        if vin is None:
            log.debug("interpolation: VIN noch nicht bekannt")
            continue

        rounded = round(soc_live, 1)
        changed = hub.last_published_soc_live != rounded
        try:
            msgs = []
            if not discovery_sent:
                msgs.append(soc_live_discovery(vin, cfg.base_topic, cfg.soc_live_subtopic))
            # Republish the (non-retained) state EVERY tick so evcc's timeout
            # always sees a fresh value; the moment we stop, evcc invalidates it
            # instead of charging on against a frozen retained SOC.
            msgs.append(soc_live_state(
                cfg.base_topic, cfg.soc_live_subtopic, rounded, cfg.soc_live_retain))
            await _async_publish(mqtt_client, msgs)
            discovery_sent = True
            hub.last_published_soc_live = rounded
            log.log(
                logging.INFO if changed else logging.DEBUG,
                "soc_live=%.1f%% (charging=%s, vw_soc=%s, session=%.0f Wh)",
                rounded, hub.charging,
                f"{hub.est.vw_soc:.0f}%" if hub.est.vw_soc is not None else "None",
                hub.charged_energy_wh,
            )
        except aiomqtt.MqttError as e:
            log.warning("interpolation: Publish-Fehler: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("interpolation: unerwarteter Fehler")


# ---------------------------------------------------------------------------
# Task 4 (outer): persistent aiomqtt publish client wrapper
# ---------------------------------------------------------------------------

async def mqtt_task(
    cfg: Config,
    hub: Hub,
    mqtt_client_ref: list,
    stop: asyncio.Event,
) -> None:
    """Manage a persistent aiomqtt.Client for publishing; exposes it via mqtt_client_ref."""
    backoff = MQTT_RECONNECT_DELAY
    while not stop.is_set():
        try:
            availability = f"{cfg.base_topic}/{AVAILABILITY_SUBTOPIC}"
            async with aiomqtt.Client(
                hostname=cfg.broker,
                port=cfg.port,
                username=cfg.mqtt_user,
                password=cfg.mqtt_password,
                identifier="vweuda-publisher",
                will=aiomqtt.Will(availability, payload="offline", retain=True),
            ) as client:
                backoff = MQTT_RECONNECT_DELAY
                mqtt_client_ref.clear()
                mqtt_client_ref.append(client)
                log.info("MQTT publisher verbunden mit %s:%d", cfg.broker, cfg.port)
                # Ping the availability topic instead of parking on stop.wait():
                # aiomqtt only raises MqttError from an *active* call, so a link
                # that dies while this task idles goes unnoticed, the dead client
                # stays in mqtt_client_ref, and every later publish fails with
                # "client is not currently connected" until the daemon restarts.
                while not stop.is_set():
                    await client.publish(availability, payload="online", retain=True)
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=MQTT_KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        pass
                # Clean shutdown: the will only fires on an unexpected drop.
                await client.publish(availability, payload="offline", retain=True)

        except aiomqtt.MqttError as e:
            mqtt_client_ref.clear()
            if stop.is_set():
                break
            log.warning("MQTT publisher Fehler: %s — reconnect in %ds", e, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MQTT_RECONNECT_MAX)
        except Exception:  # noqa: BLE001
            mqtt_client_ref.clear()
            if stop.is_set():
                break
            log.exception("MQTT publisher: unerwarteter Fehler — retry in %ds", backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MQTT_RECONNECT_MAX)

    mqtt_client_ref.clear()


# ---------------------------------------------------------------------------
# Main entry point: wire tasks via asyncio.gather
# ---------------------------------------------------------------------------

async def run(cfg: Config, stop: asyncio.Event) -> None:
    hub = Hub(est=SocEstimator(
        capacity_kwh=cfg.vehicle_capacity_kwh,
        efficiency=cfg.charge_efficiency,
        stale_after_s=cfg.soc_stale_after_s,
    ))
    _load_estimator(hub.est)
    # Shared mutable reference to the active aiomqtt.Client.
    # A list is used so tasks can check/replace it without a closure.
    mqtt_client_ref: list = []

    tasks = [
        asyncio.create_task(mqtt_task(cfg, hub, mqtt_client_ref, stop), name="mqtt"),
        asyncio.create_task(evcc_listener_task(cfg, hub, stop), name="evcc"),
        asyncio.create_task(vw_poll_task(cfg, hub, mqtt_client_ref, stop), name="vw-poll"),
        asyncio.create_task(interpolation_task(cfg, hub, mqtt_client_ref, stop), name="interp"),
    ]
    # Wait until either a stop signal arrives or any task exits unexpectedly.
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _pending = await asyncio.wait(
        [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
    )
    # Shut down: the evcc listener blocks in `async for`, so cancel explicitly.
    stop.set()
    stop_task.cancel()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, stop_task, return_exceptions=True)

    # If a worker task crashed (rather than a clean stop signal), propagate the
    # exception so the process exits non-zero and systemd's Restart=on-failure
    # brings it back up.
    for t in done:
        if t is stop_task or t.cancelled():
            continue
        exc = t.exception()
        if exc is not None:
            raise exc


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("EUDA_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config()
    if not cfg.email or not cfg.password:
        sys.exit("Fehler: EUDA_EMAIL/EUDA_PASSWORD (oder VW_USERNAME/VW_PASSWORD) nötig")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop = asyncio.Event()

    def _request_stop(*_):
        log.info("Stop-Signal erhalten")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, _request_stop)

    try:
        loop.run_until_complete(run(cfg, stop))
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
