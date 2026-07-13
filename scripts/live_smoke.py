#!/usr/bin/env python3
"""Live smoke test — verifies the whole Phase 1 chain against real servers.

Run this on a machine with internet access:

    python scripts/live_smoke.py

It will, using a tiny 2h-horizon config so downloads stay small:
  1. probe run availability for every model (URL schema check)
  2. fetch the newest published cycle of each
  3. decode the GRIBs and extract a point forecast
  4. print the run store status

Exit code 0 = Phase 1 works end-to-end.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.fetch.registry import FETCHERS, get_fetcher  # noqa: E402
from gribbosaurus_rex.scheduler import check_model  # noqa: E402
from gribbosaurus_rex.store.runs import RunStore  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(message)s")

POINT = (39.5, 2.6)  # Palma Bay


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as td:
        cfg = RaceConfig(
            name="smoke",
            bbox=BBox(38.0, 40.5, 0.5, 3.5),
            models=tuple(FETCHERS),
            max_lead_hours=2,     # keep the test download tiny
            keep_runs=1,
            data_dir=Path(td),
        )
        store = RunStore(cfg.db_path)

        print("\n=== 1. run availability probes ===")
        for model in cfg.models:
            f = get_fetcher(model)
            cands = f.candidate_cycles()
            avail = next(
                (c for c in cands if f.is_available(c, cfg.max_lead_hours)), None)
            status = f"newest published: {avail:%Y-%m-%d %HZ}" if avail \
                else "NO published cycle found (URL schema wrong?)"
            print(f"  {model:8s} {status}")
            if not avail:
                failures.append(f"{model}: availability probe failed")

        print("\n=== 2. fetch newest cycles ===")
        for model in cfg.models:
            try:
                cycle = check_model(model, cfg, store)
                rec = store.latest_complete(model)
                if rec:
                    print(f"  {model:8s} fetched {cycle or rec.cycle} "
                          f"({rec.n_files} files, {rec.bytes/1e6:.2f} MB)")
                else:
                    failures.append(f"{model}: nothing fetched")
                    print(f"  {model:8s} FAILED — see log above")
            except Exception as e:  # noqa: BLE001
                failures.append(f"{model}: {e}")
                print(f"  {model:8s} ERROR {e}")

        print("\n=== 3. decode + point extraction ===")
        from gribbosaurus_rex.extract import latest_point_forecasts

        df = latest_point_forecasts(cfg, *POINT)
        if df.empty:
            failures.append("extraction returned no data")
        else:
            print(df.groupby("model").head(2).to_string(index=False))
            bad = df[(df.wind_speed_ms < 0) | (df.wind_speed_ms > 75)]
            if not bad.empty:
                failures.append("implausible wind speeds decoded")

        print("\n=== 4. run store ===")
        for r in store.list_runs():
            print(f"  {r.model:8s} {r.cycle} {r.status} {r.message or ''}")

    print("\n" + ("💥 FAILURES:\n  " + "\n  ".join(failures) if failures
                  else "🦖 Phase 1 smoke test passed end-to-end."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
