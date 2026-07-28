"""Yacht HTTPS push — validation for POST /obs/yacht (pure logic).

The boat side (scripts/stingray_sender.py on the Expedition PC) batches
observations and POSTs them over TLS through Caddy:

    POST /obs/yacht
    X-Gribbo-Token: <GRIBBO_YACHT_TOKEN>
    {"station": "stingray",
     "obs": [{"time": "2026-08-01T13:05:00+00:00", "lat": 50.76,
              "lon": -1.30, "tws_kn": 14.2, "twd_deg": 225,
              "baro_hpa": 1013.2}, ...]}

Wind speed may be sent as `tws_kn` (Expedition/NMEA convention) or
`wind_speed_ms` (SI); stored SI. Position-only obs (no wind) are accepted
— they can't be scored but a fresh yacht fix re-anchors the distance
weighting (verify.compute_scores), which is valuable on its own.

Everything here is FastAPI-free so it unit-tests offline; the endpoint in
api/main.py is a thin wrapper. Duplicates are handled downstream by the
obs table's UNIQUE(source, station, time) — the sender retries freely.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone

KN_TO_MS = 0.514444

MAX_BATCH = 500                      # obs per POST
MAX_AGE = timedelta(days=7)          # allow post-race catch-up uploads
MAX_FUTURE = timedelta(minutes=10)   # boat clock slack
TOKEN_ENV = "GRIBBO_YACHT_TOKEN"


def token_ok(provided: str | None) -> bool:
    """Constant-time check against GRIBBO_YACHT_TOKEN. False when the
    server has no token configured — the endpoint stays closed until one
    is set in /etc/gribbo/env."""
    want = os.environ.get(TOKEN_ENV) or ""
    if not want or not provided:
        return False
    return hmac.compare_digest(provided, want)


def token_configured() -> bool:
    return bool(os.environ.get(TOKEN_ENV))


def _clean_time(v, now: datetime) -> str | None:
    try:
        ts = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)   # boat times are UTC
    ts = ts.astimezone(timezone.utc)
    if ts > now + MAX_FUTURE or ts < now - MAX_AGE:
        return None
    return ts.isoformat(timespec="seconds")


def _num(v, lo: float, hi: float) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def validate_ob(d: dict, now: datetime) -> tuple[dict | None, str | None]:
    """One raw obs dict -> (insert_obs kwargs, None) or (None, reason)."""
    if not isinstance(d, dict):
        return None, "not an object"
    t = _clean_time(d.get("time"), now)
    if t is None:
        return None, f"bad/stale time: {d.get('time')!r}"
    lat = _num(d.get("lat"), -90.0, 90.0)
    lon = _num(d.get("lon"), -180.0, 180.0)
    if lat is None or lon is None:
        return None, f"bad position: {d.get('lat')!r},{d.get('lon')!r}"

    speed_ms = None
    if d.get("wind_speed_ms") is not None:
        speed_ms = _num(d.get("wind_speed_ms"), 0.0, 62.0)   # ~120 kn
        if speed_ms is None:
            return None, f"bad wind_speed_ms: {d.get('wind_speed_ms')!r}"
    elif d.get("tws_kn") is not None:
        kn = _num(d.get("tws_kn"), 0.0, 120.0)
        if kn is None:
            return None, f"bad tws_kn: {d.get('tws_kn')!r}"
        speed_ms = kn * KN_TO_MS

    twd = None
    if d.get("twd_deg") is not None:
        twd = _num(d.get("twd_deg"), 0.0, 360.0)
        if twd is None:
            return None, f"bad twd_deg: {d.get('twd_deg')!r}"
        twd = twd % 360.0

    baro = None
    if d.get("baro_hpa") is not None:
        baro = _num(d.get("baro_hpa"), 850.0, 1100.0)
        if baro is None:
            return None, f"bad baro_hpa: {d.get('baro_hpa')!r}"

    gust_ms = None
    if d.get("gust_kn") is not None:
        g = _num(d.get("gust_kn"), 0.0, 150.0)
        gust_ms = g * KN_TO_MS if g is not None else None

    return {"time_iso": t, "lat": lat, "lon": lon,
            "wind_speed_ms": (round(speed_ms, 3)
                              if speed_ms is not None else None),
            "wind_dir_deg": twd, "gust_ms": gust_ms,
            "pressure_hpa": baro}, None


def parse_payload(payload, now: datetime | None = None
                  ) -> tuple[str, list[dict], list[str]]:
    """Full request body -> (station, valid insert kwargs, reject reasons).

    Raises ValueError on a structurally unusable body (wrong type,
    oversized batch) — the endpoint maps that to HTTP 422.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    raw = payload.get("obs")
    if not isinstance(raw, list) or not raw:
        raise ValueError("'obs' must be a non-empty list")
    if len(raw) > MAX_BATCH:
        raise ValueError(f"batch too large ({len(raw)} > {MAX_BATCH})")
    station = str(payload.get("station") or "stingray")[:40]

    rows, rejected = [], []
    for d in raw:
        row, why = validate_ob(d, now)
        if row is not None:
            rows.append(row)
        else:
            rejected.append(why)
    return station, rows, rejected
