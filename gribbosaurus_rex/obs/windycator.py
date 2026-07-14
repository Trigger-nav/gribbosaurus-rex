"""Windycator observation source — Ian Millard's UK live-wind aggregator.

https://weather.ianmillard.com/windycator/ combines coastal stations and
buoys that UK sailors actually trust: Southampton VTS portlog (Bramble
Post, Calshot, Nab Tower), weatherfile.com platforms (Lymington, Hurst,
Yarmouth Pier), Channel Coastal Observatory piers, NCI lookouts and club
anemometers.

Feed (discovered from the site, 2026-07-14):
    GET /windycator/latest.php?timestamp=0&coords=LONMIN,LATMIN,LONMAX,LATMAX
    -> {"timestamp": ..., "data": [ {station...}, ... ]}

Per-station quirks handled here:
  * wind_avg / wind_gust are KNOTS (weatherfile links say wt=KTS) -> m/s
  * numerics arrive as numbers OR strings ("15.2"); wind_dir may be null
  * wind_avg -1 is a sentinel for "no data"
  * timestamp is local time with offset (+01:00) -> stored as UTC
  * some stations are weeks stale — rows are stored (deduped) and the
    scoring window ignores them naturally; obviously-dead entries are
    skipped by a staleness cut to keep the table tidy
  * "barometer" (hPa) present on some station types

Courtesy: it's a personal aggregation service — we identify ourselves in
the User-Agent, poll at the fleet cadence (default 10 min; the site's own
upstream polls are 1–5 min), and only for race bboxes that enable it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.obs.store import ObsStore

log = logging.getLogger("gribbo.obs.windycator")

URL = "https://weather.ianmillard.com/windycator/latest.php"
KN_TO_MS = 0.514444
MAX_AGE_H = 3.0   # skip stations whose latest report is older than this

_session = requests.Session()
_session.headers["User-Agent"] = \
    "gribbosaurus-rex/0.2 (model verification; personal use; polite polling)"


def _num(v) -> float | None:
    """Defensive numeric parse: handles floats, ints, numeric strings,
    None, empty strings and the -1 'no data' sentinel."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f < 0 else f


def parse_station(rec: dict, now: datetime) -> dict | None:
    """One feed record -> insert_obs kwargs (SI), or None to skip."""
    try:
        lat = float(rec["lat"])
        lon = float(rec["lng"])
        station = str(rec.get("id") or rec["name"])
        ts = datetime.fromisoformat(rec["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        return None  # no offset -> ambiguous; refuse rather than guess
    ts_utc = ts.astimezone(timezone.utc)
    if now - ts_utc > timedelta(hours=MAX_AGE_H):
        return None  # dead/stale station

    wind_kn = _num(rec.get("wind_avg"))
    gust_kn = _num(rec.get("wind_gust"))
    wdir = _num(rec.get("wind_dir"))
    baro = _num(rec.get("barometer"))
    if baro is not None and not (850 <= baro <= 1100):
        baro = None
    if wind_kn is None and baro is None:
        return None

    return dict(
        source="windycator", station=station, lat=lat, lon=lon,
        time_iso=ts_utc.isoformat(timespec="seconds"),
        wind_speed_ms=(wind_kn * KN_TO_MS) if wind_kn is not None else None,
        wind_dir_deg=(wdir % 360.0) if wdir is not None else None,
        gust_ms=(gust_kn * KN_TO_MS) if gust_kn is not None else None,
        pressure_hpa=baro,
    )


def fetch_windycator(cfg: RaceConfig, store: ObsStore) -> int:
    """All Windycator stations inside the race bbox. Returns new rows."""
    b = cfg.bbox.padded(0.2)
    params = {
        "timestamp": 0,
        "coords": f"{b.lon_min},{b.lat_min},{b.lon_max},{b.lat_max}",
    }
    r = _session.get(URL, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    records = payload.get("data", [])
    if not isinstance(records, list):
        log.warning("unexpected windycator payload: %r", str(payload)[:200])
        return 0

    now = datetime.now(timezone.utc)
    new = skipped = 0
    for rec in records:
        kwargs = parse_station(rec, now)
        if kwargs is None:
            skipped += 1
            continue
        new += store.insert_obs(**kwargs)
    log.info("windycator: %d new obs (%d stations skipped: stale/no data)",
             new, skipped)
    return new
