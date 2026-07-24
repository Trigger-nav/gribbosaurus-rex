#!/usr/bin/env python3
"""UKV end-to-end smoke — runs the real fetcher path, no arbiter pass.

Finds the newest available run in the ukv-channel order, fetches its wind
file through UkvFetcher (order API -> download -> crop), and decodes it.
Confirms the whole path works before enabling UKV in prod.

    export DATAHUB_API_KEY=...
    python scripts/live_smoke_ukv.py
"""

from __future__ import annotations

import sys
import tempfile
import warnings
from dataclasses import replace
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import fetch_config, load_fleet  # noqa: E402
from gribbosaurus_rex.fetch.ukmo import UkvFetcher  # noqa: E402


def main() -> int:
    f = UkvFetcher()
    fc = fetch_config(load_fleet())          # carries per-model crop bbox
    fc = replace(fc, max_lead_hours=48)

    print("finding the newest available UKV run…")
    cycle = None
    for cyc in f.candidate_cycles():
        if f.is_available(cyc):
            cycle = cyc
            break
    if cycle is None:
        print("no available run found — is the order Active and the key valid?")
        return 1
    print(f"newest available run: {cycle:%Y-%m-%dT%H:00Z}")

    with tempfile.TemporaryDirectory() as td:
        res = f.fetch(cycle, fc, Path(td))
        out = res.files[0]
        print(f"fetched + cropped: {out.name}, {out.stat().st_size/1e6:.2f} MB")
        import cfgrib
        for ds in cfgrib.open_datasets(str(out), backend_kwargs={"indexpath": ""}):
            print(f"  vars={list(ds.data_vars)} dims={dict(ds.sizes)} "
                  f"lat.ndim={ds['latitude'].ndim if 'latitude' in ds else '?'}")
        # confirm wind is there
        import cfgrib as _c  # noqa: F401
        ok = False
        for ds in cfgrib.open_datasets(str(out), backend_kwargs={"indexpath": ""}):
            if any(v in ds.data_vars for v in ("u10", "10u")) and \
               any(v in ds.data_vars for v in ("v10", "10v")):
                ok = True
        print("\n✅ UKV wind present and decodes — safe to enable in prod"
              if ok else "\n❌ wind missing — send me the output")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
