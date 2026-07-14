"""Gribbosaurus Rex API — fleet-aware (multiple race areas, one service).

Endpoints
  GET /races           enabled race areas (bbox, models, description)
  GET /models/status   per-model: latest run, freshness (fleet fetch domain)
  GET /runs            recent run history (the "new GRIB available" feed)
  GET /point?race=     multi-model forecast at lat/lon (race auto-detected
                       from the point when omitted)
  GET /grid?race=      blended wind field over a race area
  GET /obs             recent observations (yacht, METAR, buoys)
  GET /scores?race=    latest confidence per model + current blend weights
  GET /scores/history?race=
  GET /scores.json     the Stingray artefact: ALL regions, ETag/Last-Modified
  POST /fetch          trigger one polling pass now (runs + obs + verify)

Set GRIBBO_WATCH=1 to run the background poller inside the API process;
the NMEA listener starts too when enabled in any race config.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request, Response

from gribbosaurus_rex.config import fetch_config, load_fleet
from gribbosaurus_rex.fetch.base import cycle_db
from gribbosaurus_rex.fetch.registry import get_fetcher
from gribbosaurus_rex.obs.store import ObsStore
from gribbosaurus_rex.scheduler import Poller, check_all, obs_and_verify_pass
from gribbosaurus_rex.store.runs import RunStore

RACES = load_fleet()
BY_NAME = {r.name: r for r in RACES}
fleet_cfg = fetch_config(RACES)  # union bbox / union models / max horizon
_poller: Poller | None = None
_nmea = None


def resolve_race(race: str | None, lat: float | None = None,
                 lon: float | None = None):
    """Race by name; else the first race containing (lat, lon); else the
    first enabled race."""
    if race:
        if race not in BY_NAME:
            raise HTTPException(404, f"Unknown race '{race}'. "
                                     f"Enabled: {sorted(BY_NAME)}")
        return BY_NAME[race]
    if lat is not None and lon is not None:
        for r in RACES:
            if r.bbox.padded(1.0).contains(lat, lon):
                return r
        raise HTTPException(
            422, f"({lat},{lon}) is outside every enabled race area")
    return RACES[0]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller, _nmea
    if os.environ.get("GRIBBO_WATCH") == "1":
        _poller = Poller(RACES)
        _poller.start()
    nmea_race = next((r for r in RACES if r.obs.nmea.enabled), None)
    if nmea_race:
        from gribbosaurus_rex.obs.nmea import NmeaListener

        _nmea = NmeaListener(nmea_race, ObsStore(nmea_race.db_path))
        _nmea.start()
    yield
    if _poller:
        _poller.stop()
    if _nmea:
        _nmea.stop()


app = FastAPI(title="Gribbosaurus Rex API", lifespan=lifespan)


@app.get("/races")
def races():
    return [{
        "name": r.name,
        "description": r.description,
        "bbox": vars(r.bbox),
        "models": list(r.models),
        "max_lead_hours": r.max_lead_hours,
    } for r in RACES]


@app.get("/models/status")
def models_status():
    store = RunStore(fleet_cfg.db_path)
    now = datetime.now(timezone.utc)
    out = []
    for model in fleet_cfg.models:
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
    return {"race": fleet_cfg.name,
            "races": [r.name for r in RACES],
            "time": now.isoformat(timespec="seconds"),
            "models": out}


@app.get("/runs")
def runs(model: str | None = None, limit: int = 40):
    store = RunStore(fleet_cfg.db_path)
    return [vars(r) for r in store.list_runs(model=model, limit=limit)]


@app.get("/point")
def point(lat: float = Query(...), lon: float = Query(...),
          race: str | None = None):
    from gribbosaurus_rex.extract import latest_point_forecasts

    rc = resolve_race(race, lat, lon)
    if not rc.bbox.padded(1.0).contains(lat, lon):
        raise HTTPException(422, f"({lat},{lon}) is outside race area '{rc.name}'")
    df = latest_point_forecasts(rc, lat, lon)
    if df.empty:
        raise HTTPException(
            503, "No model runs on disk yet — POST /fetch or run fetch-once.")
    df["time"] = df["time"].astype(str)
    return df.to_dict(orient="records")


@app.get("/grid")
def grid(valid_time: str | None = None, race: str | None = None):
    from gribbosaurus_rex.pipeline import run as run_pipeline

    try:
        df = run_pipeline(resolve_race(race), valid_time=valid_time)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return df.to_dict(orient="records")


@app.get("/obs")
def obs(window_h: float = 24, source: str | None = None):
    store = ObsStore(fleet_cfg.db_path)
    return [vars(o) for o in store.recent_obs(window_h, source=source)]


@app.get("/scores")
def scores(race: str | None = None):
    from gribbosaurus_rex.pipeline import current_weights

    rc = resolve_race(race)
    store = ObsStore(rc.db_path)
    weights, source = current_weights(rc)
    return {
        "race": rc.name,
        "latest": store.latest_scores(race=rc.name),
        "blend_weights": weights,
        "weight_source": source,   # "confidence" once verification has data
    }


@app.get("/scores/history")
def scores_history(model: str | None = None, limit: int = 500,
                   race: str | None = None):
    store = ObsStore(fleet_cfg.db_path)
    rc = resolve_race(race)
    return [dict(r) for r in store.score_history(model=model, limit=limit,
                                                 race=rc.name)]


@app.get("/scores.json")
def scores_json(request: Request):
    """The Stingray artefact (contract schema 1.0): ETag/Last-Modified,
    honors If-None-Match. Regenerated by every arbiter/poller pass."""
    from email.utils import formatdate

    from gribbosaurus_rex.publish import etag_for, scores_path

    path = scores_path(fleet_cfg)
    if not path.exists():
        raise HTTPException(
            503, "scores.json not generated yet — run: "
                 "python -m gribbosaurus_rex arbiter-once")
    content = path.read_bytes()
    etag = etag_for(content)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=content, media_type="application/json",
        headers={
            "ETag": etag,
            "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
            "Cache-Control": "no-cache",
        })


@app.get("/grib/{model}")
def grib_download(model: str, race: str | None = None):
    """Latest run of one model as a single race-area GRIB2 file — for
    routing software (Expedition, Adrena, qtVlm)."""
    from gribbosaurus_rex.export import build_race_grib

    rc = resolve_race(race)
    if model not in rc.models:
        raise HTTPException(404, f"Model '{model}' not configured for "
                                 f"race '{rc.name}' ({list(rc.models)})")
    try:
        data, fname = build_race_grib(rc, model)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from e
    return Response(
        content=data, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/fetch")
def fetch_now():
    store = RunStore(fleet_cfg.db_path)
    fetched = check_all(fleet_cfg, store)
    phase2 = obs_and_verify_pass(RACES, store)
    return {"fetched": fetched, **phase2}
