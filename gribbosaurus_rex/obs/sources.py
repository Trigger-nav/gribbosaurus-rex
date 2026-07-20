"""Shore-side observation sources: METAR and NDBC buoys.

Both are polled by the scheduler and written into the ObsStore. All
values normalized to knots / degrees-FROM / hPa, times to ISO8601 UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.obs.store import ObsStore

log = logging.getLogger("gribbo.obs")

METAR_API = "https://aviationweather.gov/api/data/metar"
NDBC_RT2 = "https://www.ndbc.noaa.gov/data/realtime2"

KN_TO_MS = 0.514444  # METAR wspd is knots; internal storage is SI (m/s)

_session = requests.Session()
_session.headers["User-Agent"] = "gribbosaurus-rex/0.2 (marine verification)"


def _iso_from_epoch(epoch: int | float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc) \
        .isoformat(timespec="seconds")


# --------------------------------------------------------------------- METAR

def fetch_metar(cfg: RaceConfig, store: ObsStore) -> int:
    """All METARs inside the race bbox (padded a little). Returns new rows."""
    b = cfg.bbox.padded(0.5)
    params = {
        "bbox": f"{b.lat_min},{b.lon_min},{b.lat_max},{b.lon_max}",
        "format": "json",
    }
    r = _session.get(METAR_API, params=params, timeout=30)
    r.raise_for_status()
    reports = r.json()
    if not isinstance(reports, list):
        log.warning("unexpected METAR payload: %r", str(reports)[:200])
        return 0

    new = 0
    for rep in reports:
        try:
            station = rep.get("icaoId") or rep.get("station_id")
            lat, lon = float(rep["lat"]), float(rep["lon"])
            if "obsTime" in rep and rep["obsTime"]:
                t = _iso_from_epoch(rep["obsTime"])
            else:  # e.g. "2026-07-13 12:20:00"
                t = rep["reportTime"].replace(" ", "T") + "+00:00"

            wdir = rep.get("wdir")
            wdir = None if wdir in (None, "VRB", "") else float(wdir)
            wspd = rep.get("wspd")          # knots in this API -> store m/s
            wspd = None if wspd is None else float(wspd) * KN_TO_MS
            gust = rep.get("wgst")
            gust = None if gust in (None, "") else float(gust) * KN_TO_MS
            # altim is hPa in the JSON API; slp (sea-level pressure) preferred
            press = rep.get("slp") or rep.get("altim")
            press = None if press in (None, "") else float(press)
            if press is not None and press > 2000:   # some feeds give Pa*10
                press = press / 10.0

            if wspd is None and press is None:
                continue
            new += store.insert_obs(
                source="metar", station=str(station), lat=lat, lon=lon,
                time_iso=t, wind_speed_ms=wspd, wind_dir_deg=wdir,
                gust_ms=gust, pressure_hpa=press)
        except (KeyError, TypeError, ValueError) as e:
            log.debug("skipping METAR record (%s): %r", e, str(rep)[:200])
    log.info("metar: %d new obs", new)
    return new


# ---------------------------------------------------------------- NDBC buoys

def parse_station_location(location_field: str) -> tuple[float, float] | None:
    """NDBC station_table LOCATION field -> (lat, lon).

    Format: `50.103 N 6.100 W (50°6'10" N ...)` — value THEN hemisphere
    letter as separate tokens. Pure; unit-tested.
    """
    t = location_field.split()
    try:
        lat = float(t[0]) * (1 if t[1].upper() == "N" else -1)
        lon = float(t[2]) * (1 if t[3].upper() == "E" else -1)
    except (ValueError, IndexError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _ndbc_positions(station_ids: tuple[str, ...]) -> dict:
    """Positions for the requested stations — ONE station-table download
    per pass (the table is ~1MB; never fetch it per buoy)."""
    if not station_ids:
        return {}
    wanted = {str(s).lower() for s in station_ids}
    out: dict = {}
    try:
        r = _session.get(
            "https://www.ndbc.noaa.gov/data/stations/station_table.txt",
            timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("ndbc station table unavailable: %s", e)
        return {}
    for line in r.text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) > 6 and parts[0].lower() in wanted:
            pos = parse_station_location(parts[6])
            if pos:
                out[parts[0].lower()] = pos
    return out


def fetch_ndbc(cfg: RaceConfig, store: ObsStore) -> int:
    """NDBC realtime2 text feed for the configured buoy ids.

    Column layout: YY MM DD hh mm WDIR WSPD GST WVHT ... PRES ...
    WSPD/GST in m/s, PRES in hPa, MM = missing.
    """
    positions = _ndbc_positions(tuple(cfg.obs.ndbc_stations))
    new = 0
    for sid in cfg.obs.ndbc_stations:
        pos = positions.get(str(sid).lower())
        if pos is None:
            log.warning("ndbc %s: no position in station table — skipping", sid)
            continue
        lat, lon = pos
        try:
            r = _session.get(f"{NDBC_RT2}/{sid}.txt", timeout=30)
            if r.status_code == 404:
                log.warning("ndbc %s: not found", sid)
                continue
            r.raise_for_status()
            lines = r.text.splitlines()
            header = lines[0].lstrip("#").split()
            idx = {name: i for i, name in enumerate(header)}

            for line in lines[2:26]:  # newest ~24 rows (data is newest-first)
                p = line.split()
                if len(p) < 6:
                    continue

                def val(col, scale=1.0):
                    i = idx.get(col)
                    if i is None or i >= len(p) or p[i] == "MM":
                        return None
                    return float(p[i]) * scale

                t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]),
                             int(p[4]), tzinfo=timezone.utc)
                new += store.insert_obs(
                    source="ndbc", station=str(sid), lat=lat, lon=lon,
                    time_iso=t.isoformat(timespec="seconds"),
                    wind_speed_ms=val("WSPD"),   # NDBC is already m/s
                    wind_dir_deg=val("WDIR"),
                    gust_ms=val("GST"),
                    pressure_hpa=val("PRES"))
        except requests.RequestException as e:
            log.warning("ndbc %s: %s", sid, e)
    if cfg.obs.ndbc_stations:
        log.info("ndbc: %d new obs", new)
    return new


def fetch_all(cfg: RaceConfig, store: ObsStore) -> int:
    """One obs-polling pass over all enabled shore sources."""
    new = 0
    if cfg.obs.metar:
        try:
            new += fetch_metar(cfg, store)
        except Exception:  # noqa: BLE001
            log.exception("metar fetch failed")
    if cfg.obs.windycator:
        from gribbosaurus_rex.obs.windycator import fetch_windycator

        try:
            new += fetch_windycator(cfg, store)
        except Exception:  # noqa: BLE001
            log.exception("windycator fetch failed")
    if cfg.obs.ndbc_stations:
        try:
            new += fetch_ndbc(cfg, store)
        except Exception:  # noqa: BLE001
            log.exception("ndbc fetch failed")
    return new
