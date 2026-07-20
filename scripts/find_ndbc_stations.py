#!/usr/bin/env python3
"""Discover NDBC/GTS stations inside each fleet race area.

NDBC's realtime feed relays the global GTS marine network — UK Met Office
lightvessels, Irish Marine Institute M-buoys, Météo-France buoys and more,
not just US moorings. This tool finds what's actually alive in YOUR race
boxes:

  1. downloads the station table (once)
  2. keeps stations inside any enabled race bbox (padded 0.2°)
  3. probes each one's realtime2 feed and reports data freshness
  4. prints ready-to-paste `ndbc_stations:` lists per race

    python scripts/find_ndbc_stations.py

Run it again whenever a race is added — then copy the fresh station ids
into that race's config.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import load_fleet  # noqa: E402
from gribbosaurus_rex.obs.sources import parse_station_location  # noqa: E402

TABLE = "https://www.ndbc.noaa.gov/data/stations/station_table.txt"
RT2 = "https://www.ndbc.noaa.gov/data/realtime2"
FRESH_H = 6.0   # newest row younger than this = station counts as live

session = requests.Session()
session.headers["User-Agent"] = "gribbosaurus-rex/0.2 (station discovery)"


def newest_obs_age_h(sid: str) -> tuple[float | None, str]:
    """(age_hours, note) for a station's newest realtime2 row."""
    try:
        r = session.get(f"{RT2}/{sid}.txt", timeout=20)
        if r.status_code == 404:
            return None, "no realtime2 feed"
        r.raise_for_status()
    except requests.RequestException as e:
        return None, f"error: {e}"
    for line in r.text.splitlines()[2:6]:
        p = line.split()
        if len(p) < 5:
            continue
        try:
            t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]),
                         int(p[4]), tzinfo=timezone.utc)
        except ValueError:
            continue
        age = (datetime.now(timezone.utc) - t).total_seconds() / 3600
        has_wind = len(p) > 6 and p[6] != "MM"
        return age, ("wind ok" if has_wind else "NO WIND DATA")
    return None, "no parseable rows"


def main() -> int:
    fleet = load_fleet()
    print("downloading NDBC station table...")
    r = session.get(TABLE, timeout=60)
    r.raise_for_status()

    stations = []  # (sid, name, lat, lon)
    for line in r.text.splitlines():
        if line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 7:
            continue
        pos = parse_station_location(parts[6])
        if pos is None:
            continue
        stations.append((parts[0], parts[4] or parts[0], pos[0], pos[1]))
    print(f"{len(stations)} stations worldwide\n")

    for cfg in fleet:
        b = cfg.bbox.padded(0.2)
        inside = [s for s in stations
                  if b.lat_min <= s[2] <= b.lat_max
                  and b.lon_min <= s[3] <= b.lon_max]
        print(f"=== {cfg.name} ===")
        live_ids = []
        if not inside:
            print("  (no stations in the table for this area)")
        for sid, name, lat, lon in sorted(inside):
            age, note = newest_obs_age_h(sid)
            if age is None:
                status = note
            elif age <= FRESH_H:
                status = f"LIVE ({age:.1f}h old, {note})"
                if "wind ok" in note:
                    live_ids.append(sid)
            else:
                status = f"stale ({age:.0f}h old)"
            print(f"  {sid:7s} {name[:38]:38s} {lat:7.3f},{lon:8.3f}  {status}")
        if live_ids:
            print(f"\n  paste into configs/{cfg.name}.yaml:")
            print(f"  ndbc_stations: {live_ids}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
