"""Météo-France DPObs observations — marine buoys + curated ground stations.

Live-discovered 2026-08-18 (scripts/mf_obs_discover.py), API version vv2:

  base   https://public-api.meteofrance.fr/public/DPObs
  auth   header  apikey: <METEOFRANCE_OBS_API_KEY>   (Bearer -> 401;
         the portal scopes subscriptions per-API, so the obs key is its
         own credential — falls back to METEOFRANCE_API_KEY. Current key
         expires ~2029-08-18, portal max 94672800 s.)

  GET /v2/bouees                     ALL ~830 buoys' latest hourly obs in
                                     ONE call (JSON) — the fleet-wide win.
                                     BOUEE_AZUR + BOUEE_PACA sit right on
                                     the Palermo-Montecarlo course line.
  GET /v2/station/horaire?id_station=  hourly obs for ONE ground station
                                     (JSON). 2151 stations exist but many
                                     inland RADOME ones carry no wind, and
                                     it's a request per station — so
                                     stations are config-CURATED
                                     (obs.mf_stations ids), default none.
  GET /v2/liste-stations             CSV catalogue (curation only).

Units (pinned from live rows): ff/rafper/raf = m/s, dd = degrees true,
pmer/pres = Pa (store hPa), t = Kelvin (not stored), validity_time =
ISO8601 obs time. Wind-less rows are skipped — an obs with no wind can't
be scored and buoy positions are fixed, so it adds nothing.
"""

from __future__ import annotations

import logging
import os

import requests

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.obs.store import ObsStore

log = logging.getLogger("gribbo.obs.mf")

BASE = os.environ.get("MF_OBS_BASE",
                      "https://public-api.meteofrance.fr/public/DPObs")

_session = requests.Session()
_session.headers["User-Agent"] = "gribbosaurus-rex"


def _auth_headers() -> dict:
    key = (os.environ.get("METEOFRANCE_OBS_API_KEY")
           or os.environ.get("METEOFRANCE_API_KEY"))
    if not key:
        raise RuntimeError(
            "No Météo-France obs credential: set METEOFRANCE_OBS_API_KEY "
            "(the DPObs-subscribed key) in /etc/gribbo/env.")
    return {"apikey": key}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def parse_row(row: dict, station_fallback: str = "") -> dict | None:
    """One DPObs JSON row (buoy or station) -> insert_obs kwargs, or None
    if it can't be scored (no wind speed / position / time). Pure —
    offline-tested against rows captured live 2026-08-18."""
    lat, lon = _num(row.get("lat")), _num(row.get("lon"))
    t_iso = row.get("validity_time")
    ff = _num(row.get("ff"))
    if lat is None or lon is None or not t_iso or ff is None:
        return None
    dd = _num(row.get("dd"))
    gust = _num(row.get("rafper"))
    if gust is None:
        gust = _num(row.get("raf"))
    pmer = _num(row.get("pmer"))
    if pmer is None:
        pmer = _num(row.get("pres"))
    station = (row.get("name") or row.get("geo_id_wmo")
               or row.get("geo_id_insee") or station_fallback or "mf")
    return {"station": str(station)[:40], "lat": lat, "lon": lon,
            "time_iso": str(t_iso),
            "wind_speed_ms": ff, "wind_dir_deg": dd, "gust_ms": gust,
            "pressure_hpa": (round(pmer / 100.0, 1)
                             if pmer is not None else None)}


def fetch_mf_buoys(cfg: RaceConfig, store: ObsStore) -> int:
    """All MF-redistributed GTS buoys, one request; keep those in the race
    box. Returns new rows."""
    r = _session.get(f"{BASE}/v2/bouees", headers=_auth_headers(),
                     params={"format": "json"}, timeout=60)
    r.raise_for_status()
    box = cfg.bbox.padded(0.3)
    new = kept = 0
    for row in r.json():
        kw = parse_row(row)
        if kw is None or not box.contains(kw["lat"], kw["lon"]):
            continue
        kept += 1
        if store.insert_obs(source="mf_buoy", **kw):
            new += 1
    log.info("mf_buoy: %d new obs (%d buoys in box)", new, kept)
    return new


def fetch_mf_stations(cfg: RaceConfig, store: ObsStore) -> int:
    """Hourly obs for the race's CURATED station ids (obs.mf_stations).
    One request per station — keep the list short and coastal."""
    new = 0
    for sid in cfg.obs.mf_stations:
        try:
            r = _session.get(f"{BASE}/v2/station/horaire",
                             headers=_auth_headers(),
                             params={"id_station": str(sid),
                                     "format": "json"}, timeout=60)
            r.raise_for_status()
            for row in r.json():
                kw = parse_row(row, station_fallback=str(sid))
                if kw is not None and store.insert_obs(source="mf_station",
                                                       **kw):
                    new += 1
        except Exception:  # noqa: BLE001 — one bad station must not kill the rest
            log.exception("mf_station %s failed", sid)
    if cfg.obs.mf_stations:
        log.info("mf_station: %d new obs (%d stations)", new,
                 len(cfg.obs.mf_stations))
    return new
