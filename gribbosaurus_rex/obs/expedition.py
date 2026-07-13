"""Expedition log importer — batch-load yacht data for backtesting.

Expedition exports CSV logs with (at least) Utc, Lat, Lon, Tws, Twd and
usually Baro. Column names vary slightly between setups, so matching is
case-insensitive with a few aliases. Utc may be an Excel serial day
number (Expedition's native format) or an ISO-ish string — both work.

    python -m gribbosaurus_rex import-log path/to/log.csv
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from gribbosaurus_rex.obs.store import ObsStore

log = logging.getLogger("gribbo.expedition")

EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)
KN_TO_MS = 0.514444

ALIASES = {
    "time": ("utc", "time", "datetime", "date"),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "longitude", "long"),
    "tws": ("tws", "truewindspeed", "wind_speed"),
    "twd": ("twd", "truewinddir", "truewinddirection", "wind_dir"),
    "baro": ("baro", "barometer", "pressure", "mslp"),
}


def _find_col(df: pd.DataFrame, key: str) -> str | None:
    lookup = {c.lower().replace(" ", "").replace("_", ""): c for c in df.columns}
    for alias in ALIASES[key]:
        c = lookup.get(alias.replace("_", ""))
        if c is not None:
            return c
    return None


def _parse_time(value) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):        # Excel serial day number
        if not 20000 < float(value) < 80000:   # sanity: ~1954..2118
            return None
        return EXCEL_EPOCH + timedelta(days=float(value))
    try:
        t = pd.Timestamp(str(value))
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t.to_pydatetime()
    except ValueError:
        return None


def import_log(path: str | Path, store: ObsStore, boat: str = "yacht",
               resample_s: int = 60) -> int:
    """Import an Expedition CSV. Rows are thinned to one per `resample_s`
    seconds so a 1Hz log doesn't create 86k obs/day. Returns rows added."""
    df = pd.read_csv(path)
    cols = {k: _find_col(df, k) for k in ALIASES}
    missing = [k for k in ("time", "lat", "lon", "tws") if cols[k] is None]
    if missing:
        raise ValueError(
            f"Log {path} is missing columns {missing}; found: {list(df.columns)}")

    new = 0
    last_kept: datetime | None = None
    for _, row in df.iterrows():
        t = _parse_time(row[cols["time"]])
        if t is None:
            continue
        if last_kept is not None and (t - last_kept).total_seconds() < resample_s:
            continue
        try:
            lat, lon = float(row[cols["lat"]]), float(row[cols["lon"]])
            tws = float(row[cols["tws"]])
        except (TypeError, ValueError):
            continue
        if pd.isna(lat) or pd.isna(lon) or pd.isna(tws):
            continue
        twd = baro = None
        if cols["twd"] is not None and not pd.isna(row[cols["twd"]]):
            twd = float(row[cols["twd"]]) % 360
        if cols["baro"] is not None and not pd.isna(row[cols["baro"]]):
            baro = float(row[cols["baro"]])
            if baro < 2:          # bars
                baro *= 1000.0
            elif baro < 500:      # weird half-units — reject
                baro = None
        last_kept = t
        new += store.insert_obs(
            source="yacht", station=boat, lat=lat, lon=lon,
            time_iso=t.astimezone(timezone.utc).isoformat(timespec="seconds"),
            wind_speed_ms=tws * KN_TO_MS,  # Expedition Tws is knots; store SI
            wind_dir_deg=twd, pressure_hpa=baro)

    log.info("expedition import %s: %d obs added", path, new)
    return new
