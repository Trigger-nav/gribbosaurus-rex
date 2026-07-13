"""Command-line interface.

  python -m gribbosaurus_rex status                 # model/run status table
  python -m gribbosaurus_rex fetch-once             # one polling pass
  python -m gribbosaurus_rex watch                  # poll forever
  python -m gribbosaurus_rex point 39.5 2.6         # forecasts at lat lon
  python -m gribbosaurus_rex serve                  # API + poller (uvicorn)

All commands accept --config path/to/race.yaml (or set GRIBBO_CONFIG).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from gribbosaurus_rex.config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gribbosaurus_rex")
    p.add_argument("--config", help="race config YAML", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("fetch-once")
    sub.add_parser("watch")
    sp = sub.add_parser("point")
    sp.add_argument("lat", type=float)
    sp.add_argument("lon", type=float)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8000)
    si = sub.add_parser("import-log")
    si.add_argument("csv", help="Expedition CSV log file")
    si.add_argument("--boat", default="yacht")
    sub.add_parser("verify-once")
    sub.add_parser("scores")
    sub.add_parser("arbiter-once")  # fetch + verify + publish scores.json

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = load_config(args.config)

    if args.cmd == "status":
        from gribbosaurus_rex.store.runs import RunStore

        store = RunStore(cfg.db_path)
        rows = store.list_runs(limit=30)
        if not rows:
            print(f"[{cfg.name}] no runs fetched yet — try: fetch-once")
            return 0
        print(f"[{cfg.name}] recent runs:")
        for r in rows:
            size = f"{r.bytes / 1e6:6.1f}MB" if r.bytes else "      -"
            print(f"  {r.model:8s} {r.cycle}  {r.status:9s} "
                  f"{r.n_files:3d} files {size}  {r.message or ''}")
        return 0

    if args.cmd == "fetch-once":
        from gribbosaurus_rex.scheduler import check_all
        from gribbosaurus_rex.store.runs import RunStore

        fetched = check_all(cfg, RunStore(cfg.db_path))
        for model, cycle in fetched.items():
            print(f"  {model:8s} {'fetched ' + cycle if cycle else 'nothing new'}")
        return 0

    if args.cmd == "watch":
        from gribbosaurus_rex.scheduler import watch

        watch(cfg)
        return 0

    if args.cmd == "point":
        from gribbosaurus_rex.extract import MS_TO_KN, latest_point_forecasts

        df = latest_point_forecasts(cfg, args.lat, args.lon)
        if df.empty:
            print("No model runs on disk yet — run fetch-once first.")
            return 1
        df = df.copy()
        df["wind_kn"] = (df["wind_speed_ms"] * MS_TO_KN).round(1)  # display
        cols = ["time", "wind_kn", "wind_speed_ms", "wind_dir", "pressure",
                "model", "cycle"]
        print(df[cols].groupby("model").head(8).to_string(index=False))
        return 0

    if args.cmd == "serve":
        import uvicorn

        if args.config:
            os.environ["GRIBBO_CONFIG"] = args.config
        os.environ.setdefault("GRIBBO_WATCH", "1")
        uvicorn.run("gribbosaurus_rex.api.main:app", host="127.0.0.1",
                    port=args.port)
        return 0

    if args.cmd == "import-log":
        from gribbosaurus_rex.obs.expedition import import_log
        from gribbosaurus_rex.obs.store import ObsStore

        n = import_log(args.csv, ObsStore(cfg.db_path), boat=args.boat)
        print(f"imported {n} yacht obs from {args.csv}")
        return 0

    if args.cmd == "verify-once":
        from gribbosaurus_rex.scheduler import obs_and_verify_pass
        from gribbosaurus_rex.store.runs import RunStore

        result = obs_and_verify_pass(cfg, RunStore(cfg.db_path))
        print(f"new obs:           {result['new_obs']}")
        print(f"new verifications: {result['new_verifications']}")
        for m, s in sorted(result["scores"].items()):
            print(f"  confidence {m:8s} {s:.3f}")
        return 0

    if args.cmd == "scores":
        from gribbosaurus_rex.obs.store import ObsStore

        store = ObsStore(cfg.db_path)
        latest = store.latest_scores()
        if not latest:
            print("No scores yet — run verify-once after some obs exist.")
            return 0
        for m, s in sorted(latest.items()):
            print(f"  {m:8s} {s:.3f}")
        return 0

    if args.cmd == "arbiter-once":
        # The Stingray arbiter's cron entrypoint: fetch new runs, fetch
        # obs, verify, publish scores.json. One pass, then exit.
        from gribbosaurus_rex.scheduler import check_all, obs_and_verify_pass
        from gribbosaurus_rex.store.runs import RunStore

        run_store = RunStore(cfg.db_path)
        fetched = check_all(cfg, run_store)
        result = obs_and_verify_pass(cfg, run_store)
        new_runs = {m: c for m, c in fetched.items() if c}
        print(f"runs fetched:      {new_runs or 'none new'}")
        print(f"new obs:           {result['new_obs']}")
        print(f"new verifications: {result['new_verifications']}")
        print(f"published:         {result.get('published', 'FAILED')}")
        return 0 if result.get("published") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
