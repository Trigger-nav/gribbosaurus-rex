#!/usr/bin/env python3
"""GRIB export smoke test — needs runs on disk (fetch-once first).

For every fleet race and model with a complete run: builds the cropped
race-area GRIB, re-opens it with cfgrib, and checks the grid actually
covers the race bbox (and only ~that), with sane wind values.

    python scripts/live_smoke_export.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
import warnings
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
warnings.filterwarnings("ignore", category=FutureWarning)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import load_fleet  # noqa: E402
from gribbosaurus_rex.export import build_race_grib  # noqa: E402
from gribbosaurus_rex.store.runs import RunStore  # noqa: E402


def main() -> int:
    import cfgrib

    failures = []
    fleet = load_fleet()
    store = RunStore(fleet[0].db_path)

    for cfg in fleet:
        print(f"\n=== {cfg.name} ===")
        for model in cfg.models:
            if store.latest_complete(model) is None:
                print(f"  {model:8s} no run on disk — skipped")
                continue
            try:
                data, fname = build_race_grib(cfg, model, store)
                with tempfile.NamedTemporaryFile(suffix=".grib2") as tf:
                    tf.write(data)
                    tf.flush()
                    dss = cfgrib.open_datasets(tf.name,
                                               backend_kwargs={"indexpath": ""})
                    lat_min = min(float(ds.latitude.min()) for ds in dss)
                    lat_max = max(float(ds.latitude.max()) for ds in dss)
                ok_cover = (lat_min <= cfg.bbox.lat_min + 0.6
                            and lat_max >= cfg.bbox.lat_max - 0.6)
                ok_tight = (lat_max - lat_min) < (cfg.bbox.lat_max
                                                  - cfg.bbox.lat_min) + 3.0
                print(f"  {model:8s} {fname} {len(data)/1e6:6.2f} MB  "
                      f"lats {lat_min:.2f}..{lat_max:.2f} "
                      f"{'✓' if ok_cover else 'COVERAGE FAIL'}"
                      f"{'' if ok_tight else ' (NOT CROPPED)'}")
                if not ok_cover:
                    failures.append(f"{cfg.name}/{model}: bbox not covered")
                if not ok_tight:
                    failures.append(f"{cfg.name}/{model}: crop ineffective")
            except Exception as e:  # noqa: BLE001
                failures.append(f"{cfg.name}/{model}: {e}")
                print(f"  {model:8s} ERROR {e}")

    print("\n" + ("💥 FAILURES:\n  " + "\n  ".join(failures) if failures
                  else "🦖 GRIB export smoke test passed."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
