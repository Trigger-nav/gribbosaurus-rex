#!/usr/bin/env python3
"""Validate the base-fleet fetch-side crop (ICON-EU, IFS) — no full pass.

For each model: fetch a single forecast step UNcropped, then run the same
slim_crop_file the fetcher now applies, and report the size / message drop
and that 10 m wind still decodes. ~1-2 minutes. Run on the server:

    python scripts/crop_bench.py

Expect a clear size reduction (ICON-EU especially) and decoded vars with
u10/v10. If wind is missing or nothing shrinks, DON'T deploy — paste output.
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
from gribbosaurus_rex.export import slim_crop_file  # noqa: E402
from gribbosaurus_rex.fetch.base import model_crop_bbox  # noqa: E402
from gribbosaurus_rex.fetch.registry import get_fetcher  # noqa: E402


def _count(path: Path) -> int:
    import eccodes as ec
    n = 0
    with open(path, "rb") as fh:
        while True:
            gid = ec.codes_grib_new_from_file(fh)
            if gid is None:
                break
            n += 1
            ec.codes_release(gid)
    return n


def bench(model: str, fc) -> None:
    print("=" * 64)
    print(model)
    f = get_fetcher(model)
    f.crop_on_fetch = False           # fetch uncropped for the "before" figure
    bbox = model_crop_bbox(model, fc)
    step0_cfg = replace(fc, max_lead_hours=0)   # just the first step

    got = False
    for cyc in f.candidate_cycles():
        with tempfile.TemporaryDirectory() as td:
            try:
                res = f.fetch(cyc, step0_cfg, Path(td))
            except Exception as e:  # noqa: BLE001
                print(f"  {cyc:%Y-%m-%dT%HZ} not available ({e.__class__.__name__})")
                continue
            out = res.files[0]
            before_mb = out.stat().st_size / 1e6
            before_n = _count(out)
            kept, total = slim_crop_file(out, bbox)
            after_mb = out.stat().st_size / 1e6
            ratio = (before_mb / after_mb) if after_mb else 0
            print(f"  cycle {cyc:%Y-%m-%dT%HZ}, crop bbox {bbox}")
            print(f"  before: {before_mb:.2f} MB, {before_n} msgs")
            print(f"  after : {after_mb:.2f} MB, {kept}/{total} msgs  "
                  f"({ratio:.0f}x smaller)")
            import cfgrib
            allvars = set()
            for ds in cfgrib.open_datasets(str(out),
                                           backend_kwargs={"indexpath": ""}):
                allvars |= set(ds.data_vars)
            print(f"  decoded vars: {sorted(allvars)}")
            ok = any(v in allvars for v in ("u10", "10u", "u")) and \
                any(v in allvars for v in ("v10", "10v", "v"))
            print("  ✅ wind present" if ok else "  ❌ WIND MISSING — do not deploy")
            got = True
        break
    if not got:
        print("  no cycle available to test")


def main() -> int:
    fc = fetch_config(load_fleet())
    for model in ("icon_eu", "ifs"):
        if model in fc.models:
            bench(model, fc)
    print("\nDone. If both show a size drop and ✅ wind, deploy is safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
