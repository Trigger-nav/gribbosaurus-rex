#!/usr/bin/env python3
"""MeteoHub WRF discovery — run BEFORE building a wrf fetcher.

Same open-data endpoint as ICON-2I (no auth), layout live-confirmed
2026-10-15:

  {BASE}/WRF/{YYYYMMDDHH}/{VAR}/WRF_{YYYYMMDDHH}_{level}.grib
  runs 00Z/12Z; variable dirs include 10u/ 10v/ prmsl/ gust/;
  the 10u file is one multi-step GRIB, ~135 MB.

THE question this script answers: grid type. WRF runs natively on a
Lambert grid, which our pipeline does NOT handle (regular lat-lon only —
the reason the UKV order was placed as mo-uk-latlon). If MeteoHub
publishes resampled regular_ll (as it does for ICON-2I), the fetcher is
a clone of icon2i.py with new names; if it's Lambert (lat.ndim == 2),
we stop and say so honestly.

  sudo -u gribbo /opt/gribbo/venv/bin/python /opt/gribbo/app/scripts/wrf_discover.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import requests

BASE = "https://meteohub.agenziaitaliameteo.it/nwp/WRF"
WANT = ["10u", "10v", "prmsl", "gust"]


def listing(url: str) -> list[str]:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return sorted(set(re.findall(r'href="([^"?/][^"]*)"', r.text)))


def main() -> int:
    runs = [e.rstrip("/") for e in listing(BASE + "/")
            if re.fullmatch(r"\d{10}/", e)]
    print(f"runs: {len(runs)}, newest: {runs[-3:]}")
    run = runs[-1]

    vdirs = [e.rstrip("/") for e in listing(f"{BASE}/{run}/") if e.endswith("/")]
    print(f"wanted vars present: "
          f"{[v for v in WANT if v in vdirs]} (of {WANT})")

    files = {}
    for v in [v for v in WANT if v in vdirs]:
        entries = [e for e in listing(f"{BASE}/{run}/{v}/")
                   if e.endswith(".grib")]
        print(f"  {v}: {entries}")
        if entries:
            files[v] = entries[0]
    if "10u" not in files:
        print("no 10u file — send me this output")
        return 1

    print("\ndownloading 10u (~135 MB)…")
    import cfgrib

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "u10.grib"
        with requests.get(f"{BASE}/{run}/10u/{files['10u']}",
                          stream=True, timeout=900) as r:
            r.raise_for_status()
            with out.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        print(f"  downloaded {out.stat().st_size/1e6:.1f} MB")
        for ds in cfgrib.open_datasets(str(out), backend_kwargs={"indexpath": ""}):
            lat = ds.get("latitude")
            nd = lat.ndim if lat is not None else "?"
            print(f"  vars={list(ds.data_vars)} dims={dict(ds.sizes)} "
                  f"lat.ndim={nd}")
            for v in ds.data_vars:
                gt = ds[v].attrs.get("GRIB_gridType", "?")
                print(f"  {v}: gridType={gt}")
            if lat is not None and nd == 1:
                la, lo = ds["latitude"].values, ds["longitude"].values
                print(f"  extent: lat {la.min():.2f}..{la.max():.2f} "
                      f"lon {lo.min():.2f}..{lo.max():.2f} "
                      f"dlat~{abs(la[1]-la[0]):.4f}")
            tdim = "time" if "time" in ds.sizes else "step"
            if tdim in ds.sizes:
                print(f"  steps: {ds.sizes[tdim]}")
            ds.close()

    print("\nlat.ndim==1 + regular extent -> pipeline-ready, I'll clone "
          "the icon2i fetcher.\nlat.ndim==2 or gridType lambert -> native "
          "grid, NOT a drop-in — send the output either way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
