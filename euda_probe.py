#!/usr/bin/env python3
"""One-shot probe for the VW EU Data Act portal.

Logs in ONCE, lists vehicles, resolves the data-request identifier, lists the
available datasets and — if a content dataset exists — downloads the newest one
and prints the curated readings. This validates the whole read path before we
build the MQTT daemon, with a single login (account-lockout-safe).

Usage:
    ./venv/bin/python euda_probe.py [-v]

Credentials are read from .env (EUDA_EMAIL/EUDA_PASSWORD, falling back to the
old VW_USERNAME/VW_PASSWORD). Optional: VW_VIN, EUDA_COUNTRY, EUDA_LANGUAGE.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from euda import api as euda_api  # noqa: E402
from euda.api import ApiError, AuthError, EudaApiClient  # noqa: E402
from euda.const import BASE_URL  # noqa: E402
from euda.data import CURATED_BINARY, CURATED_SENSORS, Dataset  # noqa: E402

log = logging.getLogger("euda_probe")

NO_CONTENT = "_no_content_found.zip"
COOKIE_FILE = Path(__file__).parent / "euda_cookies.pickle"


def _maybe_override_state() -> None:
    """Allow overriding the OIDC state country/language via env (DE fallback)."""
    country = os.environ.get("EUDA_COUNTRY")
    language = os.environ.get("EUDA_LANGUAGE")
    if country and language:
        brand = euda_api.OIDC_STATE.split("__")[-1]
        euda_api.OIDC_STATE = f"{country}__{language}__{brand}"
        log.info("OIDC state overridden -> %s", euda_api.OIDC_STATE)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--fresh", action="store_true",
                   help="ignoriere gespeicherte Cookies, immer neu einloggen")
    p.add_argument("--dump-metadata", action="store_true",
                   help="komplette Metadata-JSON ausgeben (Debug)")
    p.add_argument("--list-retries", type=int, default=1,
                   help="Anzahl Versuche für die Dataset-Liste bei HTTP 500 (default 1)")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    email = os.environ.get("EUDA_EMAIL") or os.environ.get("VW_USERNAME")
    password = os.environ.get("EUDA_PASSWORD") or os.environ.get("VW_PASSWORD")
    target_vin = os.environ.get("VW_VIN") or os.environ.get("EUDA_VIN")
    if not email or not password:
        sys.exit("Fehler: EUDA_EMAIL/EUDA_PASSWORD (oder VW_USERNAME/VW_PASSWORD) nötig")

    _maybe_override_state()

    jar = aiohttp.CookieJar()
    reused = False
    if COOKIE_FILE.exists() and not args.fresh:
        try:
            jar.load(COOKIE_FILE)
            reused = True
        except Exception as e:  # noqa: BLE001
            log.warning("Cookies konnten nicht geladen werden (%s) -> Neu-Login", e)

    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        client = EudaApiClient(session, email, password)

        if reused:
            # Cookies wiederverwenden; bei 401/403 loggt der Client selbst neu ein.
            client._logged_in = True  # noqa: SLF001
            print("→ Bestehende Session aus Cookies wiederverwendet (kein neuer Login)\n")
        else:
            print("→ Login (einmalig) …")
            try:
                await client.async_login()
            except AuthError as e:
                sys.exit(f"✗ Login fehlgeschlagen: {e}")
            except ApiError as e:
                sys.exit(f"✗ API-/Netzwerkfehler beim Login: {e}")
            jar.save(COOKIE_FILE)
            print("✓ Login OK (Cookies gespeichert)\n")

        print("→ Fahrzeuge auflisten …")
        vehicles = await client.async_list_vehicles()
        if not vehicles:
            sys.exit("✗ Keine Fahrzeuge im Portal verknüpft")
        for v in vehicles:
            print(f"  • {v['vin']}  {v.get('nickname', '')}")
        vin = target_vin or vehicles[0]["vin"]
        print(f"\n→ Verwende VIN {vin}\n")

        print("→ Metadata / Identifier …")
        try:
            meta = await client.async_get_metadata(vin)
        except ApiError as e:
            sys.exit(f"✗ Metadata-Abruf fehlgeschlagen: {e}")
        if args.dump_metadata:
            print("--- Metadata (komplett) ---")
            print(json.dumps(meta, indent=2, ensure_ascii=False)[:4000])
            print("---------------------------")
        identifier = meta.get("Identifier") or meta.get("identifier")
        if not identifier:
            sys.exit(f"✗ Kein Identifier in Metadata: keys={list(meta)[:10]}")
        print(f"  Identifier = {identifier}")
        # weitere evtl. nützliche Metadata-Felder zeigen
        for k in ("status", "Status", "type", "Type", "dataRequestType", "state", "State"):
            if k in meta:
                print(f"  {k} = {meta[k]!r}")
        print()

        print("→ Datasets auflisten …")
        listing = None
        for attempt in range(1, max(1, args.list_retries) + 1):
            try:
                listing = await client.async_list_datasets(vin, identifier)
                break
            except ApiError as e:
                msg = str(e)
                if "HTTP 400" in msg:
                    sys.exit(
                        "✗ Data-Delivery noch nicht bereit (HTTP 400).\n"
                        "  Wenn du den kontinuierlichen 15-Min-Export gerade erst aktiviert\n"
                        "  hast, kann das Provisioning Stunden dauern. Später erneut testen."
                    )
                if "HTTP 500" in msg and attempt < args.list_retries:
                    log.warning("List HTTP 500 (Versuch %d/%d), 10s warten …",
                                attempt, args.list_retries)
                    await asyncio.sleep(10)
                    continue
                if "HTTP 500" in msg:
                    sys.exit(
                        f"✗ Dataset-Liste: {e}\n"
                        "  HTTP 500 trotz korrektem Header. Laut Referenz oft transient /\n"
                        "  Provisioning noch nicht fertig (kann Stunden dauern). Mit\n"
                        "  --dump-metadata den Datenrequest-Status prüfen, später erneut testen."
                    )
                sys.exit(f"✗ Dataset-Liste fehlgeschlagen: {e}")

        content = [e for e in listing if e.get("name") and not e["name"].endswith(NO_CONTENT)]
        print(f"  {len(listing)} Einträge gesamt, {len(content)} mit Inhalt")
        for e in listing[-5:]:
            mark = " " if e["name"].endswith(NO_CONTENT) else "*"
            print(f"   {mark} {e.get('name')}  ({e.get('size', '?')} B, {e.get('createdOn', '?')})")

        if not content:
            print("\n⚠ Noch keine Datasets mit Inhalt — Portal liefert vermutlich noch nichts.")
            print("  Login + API-Pfad funktionieren aber. Später erneut probieren.")
            return 0

        newest = sorted(content, key=lambda e: e.get("createdOn") or e["name"])[-1]
        print(f"\n→ Lade neuestes Dataset: {newest['name']}")
        payload = await client.async_download_dataset(vin, identifier, newest["name"])
        ds = Dataset.from_json(payload)
        print(f"✓ Geladen: VIN {ds.vin}, {len(ds.points)} Datenpunkte, "
              f"captured_at={ds.captured_at}\n")

        print("=== Kuratierte Werte ===")
        for cs in CURATED_SENSORS:
            dp = ds.by_field(cs.field_name)
            if dp is not None and dp.value is not None:
                unit = dp.unit or cs.unit or ""
                print(f"  {cs.name:28s} {dp.value!r:>14} {unit}   [{cs.field_name}]")
        for cb in CURATED_BINARY:
            dp = ds.by_field(cb.field_name)
            if dp is not None and dp.value is not None:
                print(f"  {cb.name:28s} {dp.value!r:>14}        [{cb.field_name}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
