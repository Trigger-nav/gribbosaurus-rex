#!/usr/bin/env python3
"""ICON-2I end-to-end smoke — real fetcher path, no arbiter pass.

Finds the newest available run, fetches both wind files through
Icon2iFetcher (download -> crop to the Palermo-Montecarlo union), and
decodes them. Confirms the whole path before the arbiter picks it up.

  sudo -u gribbo /opt/gribbo/venv/bin/python /opt/gribbo/app/scripts/live_smoke_icon2i.py
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
from gribbosaurus_rex.fetch.icon2i import Icon2iFetcher  # noqa: E402


def main() -> int:
    f = Icon2iFetcher()
    fc = fetch_config(load_fleet())
    fc = replace(fc, max_lead_hours=72)

    print("finding the newest available ICON-2I run…")
    cycle = next((c for c in f.candidate_cycles() if f.is_available(c)), None)
    if cycle is None:
        print("no available run found — send me this output")
        return 1
    print(f"newest available run: {cycle:%Y-%m-%dT%H:00Z}")

    with tempfile.TemporaryDirectory() as td:
        res = f.fetch(cycle, fc, Path(td))
        print(f"fetched + cropped: {len(res.files)} files, "
              f"{res.nbytes/1e6:.1f} MB total")
        import cfgrib
        have_u = have_v = False
        for path in res.files:
            for ds in cfgrib.open_datasets(str(path),
                                           backend_kwargs={"indexpath": ""}):
                print(f"  {path.name}: vars={list(ds.data_vars)} "
                      f"dims={dict(ds.sizes)} "
                      f"lat.ndim={ds['latitude'].ndim if 'latitude' in ds else '?'}")
                have_u |= any(v in ds.data_vars for v in ("u10", "10u"))
                have_v |= any(v in ds.data_vars for v in ("v10", "10v"))
                ds.close()
        ok = have_u and have_v
        print("\n✅ ICON-2I wind present and decodes — safe in prod"
              if ok else "\n❌ wind missing — send me the output")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
