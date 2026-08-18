#!/usr/bin/env python3
"""Météo-France DPObs discovery — run BEFORE building the mf_obs source.

Goal: real coastal + buoy observations for Palermo-Montecarlo (Corsica
stations sit right on the course) and extra French-coast obs for the
Channel races. Docs (2026-08-18) name these under
https://public-api.meteofrance.fr/public/DPObs (API version
vv2 — live-confirmed 2026-08-18: v2 paths + apikey header answer 200;
v1 paths return 900908, Bearer returns 401) :

  liste-stations            all ground stations (id, lat, lon)
  station/horaire           hourly obs per station (dd/ff/fxi10/pmer/t)
  liste-bouees + bouees     marine buoys, hourly

Exact path forms and field units need pinning live, so this script tries
the plausible variants and prints what answers. Uses the SAME credential
as the AROME/ARPEGE fetchers (env in /etc/gribbo/env). NB the portal
scopes keys per-API: if everything below is 401/403, add the (free)
"Donnees d'Observation" subscription to your application on
portail-api.meteofrance.fr and re-run.

  sudo -u gribbo /opt/gribbo/venv/bin/python /opt/gribbo/app/scripts/mf_obs_discover.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.fetch.meteofrance import AromeFranceFetcher  # noqa: E402

DPOBS = "https://public-api.meteofrance.fr/public/DPObs"
PM_BOX = (37.7, 44.2, 6.5, 14.5)      # palermo-montecarlo
EC_BOX = (49.2, 51.3, -5.2, 2.1)      # english-channel


def in_box(lat, lon, box):
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def try_get(http, headers, paths, params=None, label=""):
    for path in paths:
        url = f"{DPOBS}{path}"
        r = http.get(url, headers=headers, params=params or {}, timeout=60)
        print(f"  {label} GET {path} -> {r.status_code}")
        if r.status_code == 200:
            return path, r
        print(f"    {r.text[:200]!r}")
    return None, None


def main() -> int:
    f = AromeFranceFetcher()
    headers = f._auth_headers()
    http = f.http

    print("== station list ==")
    path, r = try_get(http, headers,
                      ["/v2/liste-stations"],
                      {"format": "json"}, "stations")
    if r is None:
        print("!! no station list — likely missing the DPObs subscription "
              "on the portal application. Add it (free) and re-run.")
        return 1
    stations = r.json()
    if isinstance(stations, dict):   # some responses wrap the list
        for v in stations.values():
            if isinstance(v, list):
                stations = v
                break
    print(f"  {len(stations)} stations; first: "
          f"{json.dumps(stations[0])[:220]}")

    def coords(s):
        for la, lo in (("Latitude", "Longitude"), ("latitude", "longitude"),
                       ("lat", "lon")):
            if la in s and lo in s:
                return float(s[la]), float(s[lo])
        return None, None

    pm = [s for s in stations if in_box(*coords(s), PM_BOX)
          if coords(s)[0] is not None]
    ec = [s for s in stations if in_box(*coords(s), EC_BOX)
          if coords(s)[0] is not None]
    print(f"  in Palermo-Montecarlo box: {len(pm)}  |  in Channel box: {len(ec)}")
    for s in pm[:8]:
        print(f"    PM: {json.dumps(s)[:160]}")

    if pm:
        sid = (pm[0].get("Id_station") or pm[0].get("id_station")
               or pm[0].get("id") or pm[0].get("ID"))
        print(f"\n== hourly obs for sample station {sid} ==")
        path, r = try_get(http, headers,
                          ["/v2/station/horaire", "/v2/station-horaire",
                           "/v2/station/infrahoraire-6m"],
                          {"id_station": str(sid), "format": "json"}, "obs")
        if r is not None:
            rows = r.json()
            print(f"  {json.dumps(rows if isinstance(rows, dict) else rows[:2])[:600]}")

    print("\n== buoys ==")
    path, r = try_get(http, headers,
                      ["/v2/liste-bouees"],
                      {"format": "json"}, "buoy-list")
    if r is not None:
        buoys = r.json()
        blist = buoys if isinstance(buoys, list) else next(
            (v for v in buoys.values() if isinstance(v, list)), [])
        print(f"  {len(blist)} buoys; first 3: {json.dumps(blist[:3])[:400]}")
        bid = None
        if blist:
            b0 = blist[0]
            bid = (b0.get("Id_bouee") or b0.get("id_bouee") or b0.get("id")
                   or b0.get("ID") or b0.get("numer_sta"))
        pth, r2 = try_get(http, headers, ["/v2/bouees", "/v2/paquet/bouees"],
                          ({"id_bouee": str(bid), "format": "json"}
                           if bid else {"format": "json"}), "buoy-obs")
        if r2 is not None:
            print(f"  {r2.text[:500]}")

    print("\nSend me ALL of this output — it pins the station ids, field "
          "names and units for the mf_obs source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
