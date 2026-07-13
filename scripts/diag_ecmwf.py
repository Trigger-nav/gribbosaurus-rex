#!/usr/bin/env python3
"""Diagnose ECMWF open-data URL layout.

Probes a matrix of candidate URL shapes for IFS and AIFS on
data.ecmwf.int and prints the HTTP status for each, so we can see
exactly which path/product/step naming the server accepts today.

Run:  python scripts/diag_ecmwf.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

S = requests.Session()
S.headers["User-Agent"] = "gribbosaurus-rex/diag"

ROOT = "https://data.ecmwf.int/forecasts"


def probe(url: str) -> str:
    out = []
    try:
        r = S.head(url, timeout=20, allow_redirects=True)
        out.append(f"HEAD={r.status_code}")
        if r.status_code not in (200, 206):
            g = S.get(url, timeout=20, headers={"Range": "bytes=0-64"})
            out.append(f"rangedGET={g.status_code}")
            if g.status_code in (200, 206):
                out.append(f"body[:24]={g.content[:24]!r}")
    except requests.RequestException as e:
        out.append(f"ERR {type(e).__name__}: {e}")
    return " ".join(out)


def main() -> None:
    now = datetime.now(timezone.utc)
    # two candidate cycles well past any publication lag
    cycles = []
    for back_h in (12, 24):
        c = (now - timedelta(hours=back_h)).replace(minute=0, second=0,
                                                    microsecond=0)
        c = c.replace(hour=(c.hour // 12) * 12)  # snap to 00/12
        if c not in cycles:
            cycles.append(c)

    products = [
        ("ifs", "0p25", "oper"),
        ("aifs-single", "0p25", "oper"),
        ("aifs", "0p25", "oper"),          # older product name
    ]
    steps = [0, 24, 144, 240, 360]

    print(f"UTC now: {now:%Y-%m-%d %H:%M}\n")

    # 1) can we see the top-level listing at all?
    for u in (f"{ROOT}/", f"{ROOT}"):
        print(f"{u}\n  -> {probe(u)}")

    for cyc in cycles:
        d, h = cyc.strftime("%Y%m%d"), cyc.strftime("%H")
        print(f"\n=== cycle {d} {h}z ===")
        # directory-ish probes
        for p, res, stream in products:
            base_dir = f"{ROOT}/{d}/{h}z/{p}/{res}/{stream}/"
            print(f"[dir] {p}/{res}/{stream}/ -> {probe(base_dir)}")
        # file probes
        for p, res, stream in products:
            for step in steps:
                fname = f"{d}{h}0000-{step}h-{stream}-fc.grib2"
                url = f"{ROOT}/{d}/{h}z/{p}/{res}/{stream}/{fname}"
                print(f"[{p:11s} s{step:3d}] .index -> {probe(url + '.index')}")
                if step == 0:
                    print(f"[{p:11s} s{step:3d}] .grib2 -> {probe(url)}")

    print("\nDone — paste this whole output back to Claude.")


if __name__ == "__main__":
    main()
