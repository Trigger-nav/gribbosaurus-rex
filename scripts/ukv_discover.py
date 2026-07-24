#!/usr/bin/env python3
"""Met Office DataHub UKV — discover the order's /latest structure.

Order-based API: list the files in the `ukv-channel` order's latest run, then
download + decode one so we can see how fileIds encode run/step/param and
confirm it's a regular lat-lon grid with 10 m wind. Run with the key set:

    export DATAHUB_API_KEY=...        # your DataHub apikey
    python scripts/ukv_discover.py

Paste the whole output back and I'll finalise the fetcher parsing. The
script never prints your key.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.fetch.base import http_session  # noqa: E402

BASE = os.environ.get(
    "DATAHUB_BASE",
    "https://data.hub.api.metoffice.gov.uk/atmospheric-models/1.0.0")
ORDER = os.environ.get("DATAHUB_ORDER_ID", "ukv-channel")
KEY = os.environ.get("DATAHUB_API_KEY")
if not KEY:
    print("set DATAHUB_API_KEY first")
    raise SystemExit(2)
H = {"apikey": KEY}
http = http_session()


def main() -> int:
    print("=" * 68)
    print("1. GET /orders  (confirm the order id + auth)")
    print("=" * 68)
    r = http.get(f"{BASE}/orders", headers=H, timeout=30)
    print("  HTTP", r.status_code)
    print(" ", r.text[:1200])

    print("\n" + "=" * 68)
    print(f"2. GET /orders/{ORDER}/latest  (the file list for the newest run)")
    print("=" * 68)
    r = http.get(f"{BASE}/orders/{ORDER}/latest", headers=H, timeout=60)
    print("  HTTP", r.status_code)
    body = r.text
    print("  first 3000 chars:")
    print(" ", body[:3000])
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            print("\n  top-level keys:", list(j.keys()))
    except Exception:  # noqa: BLE001
        pass

    # fileIds are shape-agnostic — regex them out
    ids = re.findall(r'"fileId"\s*:\s*"([^"]+)"', body)
    print(f"\n  found {len(ids)} fileId(s); first few:")
    for fid in ids[:8]:
        print("   ", fid)
    if not ids:
        print("  (no fileIds parsed — the JSON shape above tells us the key)")
        return 0

    print("\n" + "=" * 68)
    print("3. DOWNLOAD + DECODE one file")
    print("=" * 68)
    fid = ids[0]
    url = f"{BASE}/orders/{ORDER}/latest/{quote(fid, safe='')}/data"
    r = http.get(url, headers={**H, "Accept": "application/x-grib"},
                 timeout=180, allow_redirects=True)
    print(f"  GET .../{fid[:40]}.../data -> HTTP {r.status_code}, "
          f"{len(r.content)/1e6:.2f} MB, {r.headers.get('Content-Type','?')}")
    if r.status_code != 200:
        print("  body:", r.text[:400])
        return 1
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "ukv.grib2"
        f.write_bytes(r.content)
        try:
            import cfgrib
            for ds in cfgrib.open_datasets(str(f), backend_kwargs={"indexpath": ""}):
                gt = ds.attrs.get("GRIB_gridType", "?")
                latdim = ds["latitude"].ndim if "latitude" in ds else "?"
                print(f"    vars={list(ds.data_vars)} dims={dict(ds.sizes)} "
                      f"gridType={gt} lat.ndim={latdim}")
        except Exception as e:  # noqa: BLE001
            print("    cfgrib decode failed:", e)
    print("\nDone. Send me all of this — especially the section-2 JSON and the "
          "gridType/vars in section 3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
