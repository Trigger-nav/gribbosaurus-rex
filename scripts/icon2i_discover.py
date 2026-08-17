#!/usr/bin/env python3
"""ICON-2I (MeteoHub open data) discovery — run BEFORE building the fetcher.

MeteoHub publishes ICON-2I as open data, no auth (found 2026-08-17):

  https://meteohub.agenziaitaliameteo.it/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS/
      {YYYYMMDDHH}/                       runs, cycles 00 and 12
      {VAR}/ICON_2I_SURFACE_PRESSURE_LEVELS_{YYYYMMDDHH}_heightAboveGround-10.grib
                                          one multi-step GRIB per variable

This script confirms the unknowns the fetcher design depends on:
  1. available runs + which variable dirs exist (U_10M / V_10M / PMSL names)
  2. the exact filename in each variable dir (level suffix varies by var)
  3. GRID TYPE — regular lat-lon (pipeline-ready) or icosahedral/rotated
     (would need regridding — different design)
  4. steps in one file, extent, resolution

Run on the server (or any machine with the venv):
  sudo -u gribbo /opt/gribbo/venv/bin/python /opt/gribbo/app/scripts/icon2i_discover.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import requests

BASE = "https://meteohub.agenziaitaliameteo.it/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS"
WANT_VARS = ["U_10M", "V_10M", "PMSL"]


def listing(url: str) -> list[str]:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return sorted(set(re.findall(r'href="([^"?/][^"]*)"', r.text)))


def main() -> int:
    print("== runs ==")
    runs = [e.rstrip("/") for e in listing(BASE + "/")
            if re.fullmatch(r"\d{10}/", e)]
    print(f"  {len(runs)} runs, newest: {runs[-3:]}")
    run = runs[-1]

    print(f"\n== variable dirs in {run} ==")
    vdirs = [e.rstrip("/") for e in listing(f"{BASE}/{run}/") if e.endswith("/")]
    print(f"  {vdirs}")
    missing = [v for v in WANT_VARS if v not in vdirs]
    if missing:
        print(f"  !! wanted vars missing: {missing} — send me this output")

    files = {}
    for v in [v for v in WANT_VARS if v in vdirs]:
        entries = [e for e in listing(f"{BASE}/{run}/{v}/") if e.endswith(".grib")]
        print(f"  {v}: {entries}")
        if entries:
            files[v] = entries[0]

    print("\n== decode check (downloads U_10M once, ~85 MB) ==")
    if "U_10M" not in files:
        print("no U_10M file — stopping; send me the output above")
        return 1
    url = f"{BASE}/{run}/U_10M/{files['U_10M']}"
    import cfgrib

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "u10.grib"
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with out.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        print(f"  downloaded {out.stat().st_size/1e6:.1f} MB")
        for ds in cfgrib.open_datasets(str(out), backend_kwargs={"indexpath": ""}):
            gtype = ds.attrs.get("GRIB_gridType", "?")
            lat = ds.get("latitude")
            print(f"  vars={list(ds.data_vars)} dims={dict(ds.sizes)}")
            print(f"  gridType={gtype}  lat.ndim={lat.ndim if lat is not None else '?'}")
            if lat is not None and lat.ndim == 1:
                lats, lons = ds["latitude"].values, ds["longitude"].values
                print(f"  extent: lat {lats.min():.2f}..{lats.max():.2f} "
                      f"lon {lons.min():.2f}..{lons.max():.2f} "
                      f"dlat~{abs(lats[1]-lats[0]):.3f}")
            tdim = "time" if "time" in ds.sizes else "step"
            if tdim in ds.sizes:
                print(f"  steps in file: {ds.sizes[tdim]}")
            ds.close()

    print("\nIf gridType is regular_ll and lat.ndim==1: pipeline-ready — "
          "send me this output and I'll build the fetcher.\n"
          "If it says unstructured/rotated or lat.ndim==2: send it too — "
          "different design needed (regrid or 2D-crop).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
