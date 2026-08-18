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

        # Decode through the REAL pipeline entry (extract.open_run), not
        # per-file cfgrib — the 2026-08-18 per-variable-file bug decoded
        # fine per file but failed run assembly. Never weaken this check.
        from types import SimpleNamespace

        from gribbosaurus_rex.config import load_fleet as _lf
        from gribbosaurus_rex.extract import open_run
        pm = next(r for r in _lf() if r.name == "palermo-montecarlo")
        ds = open_run(SimpleNamespace(path=str(td)), bbox=pm.bbox)
        print(f"  assembled: vars={list(ds.data_vars)} dims={dict(ds.sizes)}")
        ok = ("u10" in ds.data_vars and "v10" in ds.data_vars
              and ds.sizes.get("time", 0) >= 70)
        print("\n✅ ICON-2I assembles through the pipeline — safe in prod"
              if ok else "\n❌ assembly failed — send me the output")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
