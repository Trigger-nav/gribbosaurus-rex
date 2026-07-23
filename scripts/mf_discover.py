#!/usr/bin/env python3
"""One-off: dump Météo-France package descriptions to pin the valid time-range
tokens for ARPEGE and AROME-OM (AROME's 6h tokens already work).

    export METEOFRANCE_API_KEY=...        # the apikey that worked in the smoke
    python scripts/mf_discover.py

Paste the whole output back. Prints, per model/grid:
  * the available package ids (SP1/SP2/…)
  * the package description JSON, which lists the valid `time` tokens
  * one real 400 body so we can read the error verbatim
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.fetch.base import http_session  # noqa: E402

ROOT = "https://public-api.meteofrance.fr/previnum"
KEY = os.environ.get("METEOFRANCE_API_KEY")
if not KEY:
    print("set METEOFRANCE_API_KEY first")
    raise SystemExit(2)
H = {"apikey": KEY}
http = http_session()

# (service, model, grid, product-suffix)
TARGETS = [
    ("DPPaquetAROME", "AROME", "0.025", "productARO"),       # works — reference
    ("DPPaquetARPEGE", "ARPEGE", "0.1", "productARP"),
    ("DPPaquetARPEGE", "ARPEGE", "0.25", "productARP"),
    ("DPPaquetAROME-OM", "AROME-OM-ANTIL", "0.025", "productOMAN"),
]


def get(url):
    try:
        r = http.get(url, headers=H, timeout=40)
        return r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        return "ERR", str(e)


for service, model, grid, product in TARGETS:
    base = f"{ROOT}/{service}/v1/models/{model}/grids/{grid}"
    print("=" * 72)
    print(f"{model} {grid}")
    print("=" * 72)

    code, body = get(f"{base}/packages")
    print(f"[packages] HTTP {code}")
    pkgs = []
    try:
        j = json.loads(body)
        for lnk in j.get("links", []):
            href = lnk.get("href", "")
            if "/packages/" in href:
                pid = href.split("/packages/")[-1].split("/")[0]
                if pid and pid not in pkgs:
                    pkgs.append(pid)
        print("  package ids:", pkgs or "(none parsed — raw below)")
        if not pkgs:
            print(body[:800])
    except Exception:
        print(body[:800])

    # describe each package (this lists valid time tokens / products)
    for pid in (pkgs or ["SP1"]):
        code, body = get(f"{base}/packages/{pid}")
        print(f"  [{pid}] HTTP {code}")
        if code == 200:
            print("   ", body[:2500])
        else:
            print("   ", body[:400])

    # one real productXXX 400 body, verbatim
    code, body = get(f"{base}/packages/SP1/{product}"
                     f"?referencetime=2026-07-23T00:00:00Z&time=00H06H&format=grib2")
    print(f"  [{product} SP1 time=00H06H] HTTP {code}")
    print("   ", body[:500])
    print()
