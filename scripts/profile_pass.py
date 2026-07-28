#!/usr/bin/env python3
"""Profile ONE race's verify+score to find where the arbiter's CPU goes.

Runs verify_pass + compute_scores for a single race in a fresh process
(cold dataset cache — exactly what each arbiter pass faces), after first
printing the loop dimensions (obs in window, complete runs per model,
table sizes). The dimensions alone usually explain a blow-up; the cProfile
top-N pinpoints the hot function (decode vs interp vs sqlite).

    sudo -u gribbo /opt/gribbo/venv/bin/python scripts/profile_pass.py english-channel
"""

from __future__ import annotations

import cProfile
import pstats
import resource
import sqlite3
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
REPO = Path("/opt/gribbo/app")
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import load_fleet          # noqa: E402
from gribbosaurus_rex.obs.store import ObsStore          # noqa: E402
from gribbosaurus_rex.store.runs import RunStore         # noqa: E402
from gribbosaurus_rex.verify import compute_scores, verify_pass  # noqa: E402


def main() -> int:
    race = sys.argv[1] if len(sys.argv) > 1 else "english-channel"
    races = load_fleet()
    cfg = next((r for r in races if r.name == race), None)
    if cfg is None:
        print(f"no such race {race}; have {[r.name for r in races]}")
        return 1

    rs = RunStore(cfg.db_path)
    obs_store = ObsStore(cfg.db_path)

    # ---- dimensions (cheap; flush before the slow part) ----------------
    obs = obs_store.recent_obs(cfg.scoring.window_h)
    print(f"race={cfg.name}")
    print(f"bbox={cfg.bbox}")
    print(f"models={list(cfg.models)}")
    print(f"max_lead_hours={cfg.max_lead_hours}  keep_runs={cfg.keep_runs}")
    print(f"scoring.window_h={cfg.scoring.window_h}  obs_in_window={len(obs)}")
    total_pairs = 0
    for m in cfg.models:
        runs = [r for r in rs.list_runs(model=m, limit=cfg.keep_runs * 2)
                if r.status == "complete"]
        total_pairs += len(obs) * len(runs)
        print(f"  {m:20s} complete_runs={len(runs)}")
    print(f"worst-case (obs x models x runs) value_at candidates ~ {total_pairs:,}")
    c = sqlite3.connect(cfg.db_path)
    print("verification rows total:",
          c.execute("SELECT COUNT(*) FROM verification").fetchone()[0])
    print("obs rows total:",
          c.execute("SELECT COUNT(*) FROM obs").fetchone()[0])
    print("--- indexes on verification ---")
    for r in c.execute("PRAGMA index_list('verification')"):
        print("  ", tuple(r))
    sys.stdout.flush()

    # ---- profile one verify + score -----------------------------------
    print("\nprofiling verify_pass + compute_scores (cold cache)…")
    sys.stdout.flush()
    t0 = time.time()
    pr = cProfile.Profile()
    pr.enable()
    n = verify_pass(cfg, rs, obs_store)
    compute_scores(cfg, obs_store)
    pr.disable()
    dt = time.time() - t0
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    print(f"\nDONE wall={dt:.1f}s  new_verifications={n}  peak_rss={peak_mb:.0f}MB")
    print("\n=== TOP 25 by self time (tottime) ===")
    pstats.Stats(pr).sort_stats("tottime").print_stats(25)
    print("\n=== TOP 25 by cumulative ===")
    pstats.Stats(pr).sort_stats("cumulative").print_stats(25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
