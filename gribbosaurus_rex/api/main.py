"""Gribbosaurus Rex API.

Endpoints
  GET /models/status   per-model: latest run, freshness, next expected cycle
  GET /runs            recent run history (the "new GRIB available" feed)
  GET /point           multi-model forecast time series at lat/lon
  GET /grid            blended wind field over the race area
  POST /fetch          trigger one polling pass now

Set GRIBBO_WATCH=1 to run the background poller inside the API process.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from gribbosaurus_rex.config import load_config
from gribbosaurus_rex.fetch.base import cycle_db
from gribbosaurus_rex.fetch.registry import get_fetcher
from gribbosaurus_rex.scheduler import Poller, check_all
from gribbosaurus_rex.store.runs import RunStore

cfg = load_config()
_poller: Poller | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller
    if os.environ.get("GRIBBO_WATCH") == "1":
        _poller = Poller(cfg)
        _poller.start()
    yield
    if _poller:
        _poller.stop()


app = FastAPI(title="Gribbosaurus Rex API", lifespan=lifespan)


@app.get("/models/status")
def models_status():
    store = RunStore(cfg.db_path)
    now = datetime.now(timezone.utc)
    out = []
    for model in cfg.models:
        fetcher = get_fetcher(model)
        latest = store.latest_complete(model)
        candidates = fetcher.candidate_cycles(now)
        newest_candidate = cycle_db(candidates[0]) if candidates else None
        age_h = None
        if latest:
            age_h = round((now - datetime.fromisoformat(latest.cycle))
                          .total_seconds() / 3600, 1)
        out.append({
            "model": model,
            "latest_cycle": latest.cycle if latest else None,
            "fetched_at": latest.finished_at if latest else None,
            "cycle_age_hours": age_h,
            "n_files": latest.n_files if latest else 0,
            "size_mb": round(latest.bytes / 1e6, 1) if latest else 0,
            "newest_possible_cycle": newest_candidate,
            "up_to_date": bool(latest and latest.cycle == newest_candidate),
        })
    return {"race": cfg.name, "time": now.isoformat(timespec="seconds"),
            "models": out}


@app.get("/runs")
def runs(model: str | None = None, limit: int = 40):
    store = RunStore(cfg.db_path)
    return [vars(r) for r in store.list_runs(model=model, limit=limit)]


@app.get("/point")
def point(lat: float = Query(...), lon: float = Query(...)):
    from gribbosaurus_rex.extract import latest_point_forecasts

    if not cfg.bbox.padded(1.0).contains(lat, lon):
        raise HTTPException(422, f"({lat},{lon}) is outside race area '{cfg.name}'")
    df = latest_point_forecasts(cfg, lat, lon)
    if df.empty:
        raise HTTPException(
            503, "No model runs on disk yet — POST /fetch or run fetch-once.")
    df["time"] = df["time"].astype(str)
    return df.to_dict(orient="records")


@app.get("/grid")
def grid(valid_time: str | None = None):
    from gribbosaurus_rex.pipeline import run as run_pipeline

    try:
        df = run_pipeline(cfg, valid_time=valid_time)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return df.to_dict(orient="records")


@app.post("/fetch")
def fetch_now():
    store = RunStore(cfg.db_path)
    return {"fetched": check_all(cfg, store)}
