#!/usr/bin/env python3
"""Météo-France AROME / ARPEGE discovery + smoke test.

Run this ON A MACHINE WITH THE KEY (your Mac or the server) once
METEOFRANCE_API_KEY is set. The dev sandbox has no outbound network, so the
fetcher's path/query tokens (PRODUCT suffix, TIME range groupings, the
overseas AROME service/model ids) are documented best-guesses until this
script confirms them against the live API.

What it does, printing everything so we can pin the constants:
  1. Confirms which auth scheme works  (apikey header vs Bearer token).
  2. Lists the catalogue: grids, then packages for each model.
  3. HEAD-probes the constructed package URLs for recent cycles.
  4. Downloads ONE AROME package and decodes it with cfgrib — proving the
     multi-step file assembles and carries 10m wind.

    export METEOFRANCE_API_KEY=...        # from the portal
    # optional: export METEOFRANCE_AUTH=bearer
    python scripts/live_smoke_meteofrance.py

Paste the output back and I'll correct any token that the API rejects.
"""

from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import requests  # noqa: E402

from gribbosaurus_rex.fetch.base import http_session  # noqa: E402
from gribbosaurus_rex.fetch.meteofrance import (  # noqa: E402
    AromeAntillesFetcher,
    AromeFranceFetcher,
    ArpegeFetcher,
    ArpegeGlobalFetcher,
)

FETCHERS = [AromeFranceFetcher, ArpegeFetcher, ArpegeGlobalFetcher,
            AromeAntillesFetcher]


def _headers(mode: str) -> dict:
    key = os.environ["METEOFRANCE_API_KEY"]
    return ({"Authorization": f"Bearer {key}"} if mode == "bearer"
            else {"apikey": key})


def _probe(http, url: str, headers: dict) -> str:
    try:
        r = http.get(url, headers={**headers, "Range": "bytes=0-0"},
                     timeout=30, stream=True)
        return f"HTTP {r.status_code} ({r.headers.get('Content-Type','?')})"
    except requests.RequestException as e:
        return f"ERR {e.__class__.__name__}"


def main() -> int:
    if not (os.environ.get("METEOFRANCE_APPLICATION_ID")
            or os.environ.get("METEOFRANCE_API_KEY")):
        print("Set METEOFRANCE_APPLICATION_ID (recommended) or "
              "METEOFRANCE_API_KEY — register on the portal first.")
        return 2

    http = http_session()
    f0 = AromeFranceFetcher()
    cat = f"{f0._service_url()}/models/{f0.model_id}/grids"

    print("=" * 68)
    print("1. AUTH SCHEME")
    print("=" * 68)
    if os.environ.get("METEOFRANCE_APPLICATION_ID"):
        # exercise the real token-exchange path the server will use
        try:
            H = f0._auth_headers()
            print(f"  APPLICATION_ID -> token exchange OK, "
                  f"Authorization header present: {'Authorization' in H}")
            print(f"  {cat}\n           {_probe(http, cat, H)}")
        except Exception as e:  # noqa: BLE001
            print(f"  APPLICATION_ID token exchange FAILED: {e}")
            return 1
    else:
        working_mode = None
        for mode in ("apikey", "bearer"):
            status = _probe(http, cat, _headers(mode))
            print(f"  {mode:7s} -> {cat}\n           {status}")
            if working_mode is None and status.startswith("HTTP 2"):
                working_mode = mode
        if working_mode is None:
            working_mode = os.environ.get("METEOFRANCE_AUTH", "apikey")
            print(f"  (no 2xx — continuing with '{working_mode}'; check the key)")
        else:
            print(f"  --> using '{working_mode}'"
                  f"  (set METEOFRANCE_AUTH={working_mode} in /etc/gribbo/env)")
        H = _headers(working_mode)

    print("\n" + "=" * 68)
    print("2. CATALOGUE — grids & packages per model")
    print("=" * 68)
    for cls in FETCHERS:
        f = cls()
        print(f"\n--- {f.name}  (service={f.service} model={f.model_id}) ---")
        for suffix in (f"/models/{f.model_id}/grids",
                       f"/models/{f.model_id}/grids/{f.grid}/packages"):
            url = f._service_url() + suffix
            try:
                r = http.get(url, headers=H, timeout=30)
                body = r.text[:400].replace("\n", " ")
                print(f"  GET {suffix}\n    HTTP {r.status_code}: {body}")
            except requests.RequestException as e:
                print(f"  GET {suffix}\n    ERR {e.__class__.__name__}: {e}")

    print("\n" + "=" * 68)
    print("3. PACKAGE URL PROBES — recent cycles x first two time ranges")
    print("=" * 68)
    for cls in FETCHERS:
        f = cls()
        print(f"\n--- {f.name} ---")
        for cycle in f.candidate_cycles()[:3]:
            for _end, token in f.ranges[:2]:
                url = f._package_url(cycle, token)
                print(f"  {cycle:%Y-%m-%dT%HZ} {token:>10s} -> "
                      f"{_probe(http, url, H)}")

    print("\n" + "=" * 68)
    print("4. REAL DOWNLOAD + DECODE — first AROME package that is available")
    print("=" * 68)
    os.environ["METEOFRANCE_AUTH"] = working_mode
    f = AromeFranceFetcher()
    got = False
    for cycle in f.candidate_cycles():
        end, token = f.ranges[0]
        url = f._package_url(cycle, token)
        if not _probe(http, url, H).startswith("HTTP 2"):
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "arome.grib2"
            try:
                n = f.download(url, out, headers=H, timeout=300)
            except requests.RequestException as e:
                print(f"  download failed for {cycle:%Y%m%dT%HZ}: {e}")
                continue
            print(f"  downloaded {token} of {cycle:%Y-%m-%dT%HZ}: "
                  f"{n/1e6:.1f} MB")
            try:
                import cfgrib
                dss = cfgrib.open_datasets(str(out),
                                           backend_kwargs={"indexpath": ""})
                for ds in dss:
                    dims = dict(ds.sizes)
                    print(f"    dataset vars={list(ds.data_vars)} dims={dims}")
                got = True
            except Exception as e:  # noqa: BLE001
                print(f"    cfgrib decode failed: {e}")
        break
    if not got:
        print("  no AROME package downloaded — read the section-3 statuses "
              "above to see which token/cycle the API actually offers.")

    print("\nDone. Send me this whole output to pin the constants.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
