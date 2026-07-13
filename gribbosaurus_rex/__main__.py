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
        from gribbosaurus_rex.extract import latest_point_forecasts

        df = latest_point_forecasts(cfg, args.lat, args.lon)
        if df.empty:
            print("No model runs on disk yet — run fetch-once first.")
            return 1
        with_opts = df.groupby("model").head(8)
        print(with_opts.to_string(index=False))
        return 0

    if args.cmd == "serve":
        import uvicorn

        if args.config:
            os.environ["GRIBBO_CONFIG"] = args.config
        os.environ.setdefault("GRIBBO_WATCH", "1")
        uvicorn.run("gribbosaurus_rex.api.main:app", host="127.0.0.1",
                    port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
