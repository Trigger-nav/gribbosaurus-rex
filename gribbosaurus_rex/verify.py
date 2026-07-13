"""Verification + confidence scoring — where forecasts meet reality.

One verification pass:
  1. take every observation in the scoring window
  2. for each configured model, find complete runs whose forecast covers
     the obs time
  3. interpolate the model to the obs time/place, compute errors
     (wind vector error is the primary skill metric), store one
     verification row per (obs, model, run)
  4. recompute each model's rolling confidence score with observations
     weighted by distance-from-yacht, forecast lead, recency and source
     trust — and persist the score time series for the dashboard

Confidence mapping: score = exp(-weighted_rmse_vector / err_scale_kn),
so 0kn error -> 1.0, err_scale_kn -> 0.37, and scores are comparable
across models because every model is judged on the same obs set.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.core.scoring import (direction_error, distance_weight,
                                           wind_vector_error)
from gribbosaurus_rex.obs.store import Obs, ObsStore
from gribbosaurus_rex.store.runs import RunStore

log = logging.getLogger("gribbo.verify")

EARTH_R_NM = 3440.065


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_NM * math.asin(math.sqrt(a))


def _half_life_weight(x: float, half: float) -> float:
    return 0.5 ** (max(0.0, x) / half) if half > 0 else 1.0


# ------------------------------------------------------------- verification

def verify_pass(cfg: RaceConfig, run_store: RunStore, obs_store: ObsStore) -> int:
    """Verify window obs against all covering runs. Returns rows added."""
    from gribbosaurus_rex.extract import value_at

    added = 0
    observations = obs_store.recent_obs(cfg.scoring.window_h)
    if not observations:
        return 0

    runs_by_model = {m: [r for r in run_store.list_runs(model=m, limit=cfg.keep_runs * 2)
                         if r.status == "complete"]
                     for m in cfg.models}

    for ob in observations:
        if ob.source == "test":
            continue  # smoke/loopback data: stored, never scored
        if ob.wind_speed_ms is None:
            continue
        if not cfg.bbox.padded(0.5).contains(ob.lat, ob.lon):
            continue
        t_obs = datetime.fromisoformat(ob.time)

        for model, runs in runs_by_model.items():
            for rec in runs:
                t_cycle = datetime.fromisoformat(rec.cycle)
                lead_h = (t_obs - t_cycle).total_seconds() / 3600.0
                if lead_h < 0 or lead_h > cfg.max_lead_hours:
                    continue
                if obs_store.has_verification(ob.id, model, rec.cycle):
                    continue
                try:
                    fc = value_at(rec, ob.lat, ob.lon,
                                  pd.Timestamp(t_obs).tz_convert("UTC"))
                except Exception:  # noqa: BLE001
                    log.exception("value_at failed: %s %s", model, rec.cycle)
                    continue
                if fc["wind_speed_ms"] is None or np.isnan(fc["wind_speed_ms"]):
                    continue

                err_vec = err_dir = None
                if ob.wind_dir_deg is not None:
                    err_vec = float(wind_vector_error(
                        fc["wind_speed_ms"], fc["wind_dir"],
                        ob.wind_speed_ms, ob.wind_dir_deg))
                    err_dir = float(direction_error(fc["wind_dir"],
                                                    ob.wind_dir_deg))
                err_spd = float(fc["wind_speed_ms"] - ob.wind_speed_ms)
                err_prs = None
                if ob.pressure_hpa is not None and not np.isnan(fc["pressure"]):
                    err_prs = float(fc["pressure"] - ob.pressure_hpa)

                obs_store.insert_verification(
                    obs_id=ob.id, model=model, cycle=rec.cycle,
                    lead_hours=round(lead_h, 2),
                    fc_wind_speed=round(fc["wind_speed_ms"], 3),
                    fc_wind_dir=round(fc["wind_dir"], 1),
                    fc_pressure=(None if np.isnan(fc["pressure"])
                                 else round(fc["pressure"], 1)),
                    err_vector_ms=err_vec, err_speed_ms=err_spd,
                    err_dir_deg=err_dir, err_press_hpa=err_prs)
                added += 1

    if added:
        log.info("verification: %d new forecast-vs-obs comparisons", added)
    return added


# ---------------------------------------------------------------- confidence

def compute_scores(cfg: RaceConfig, obs_store: ObsStore,
                   now: datetime | None = None) -> dict[str, float]:
    """Rolling weighted confidence per model; persists a score snapshot."""
    now = now or datetime.now(timezone.utc)

    # anchor for distance weighting: fresh yacht fix beats configured focus
    yacht = obs_store.yacht_latest()
    anchor = (yacht.lat, yacht.lon) if yacht else cfg.anchor()

    rows = obs_store.verifications_window(cfg.scoring.window_h)
    per_model: dict[str, list[tuple[float, float, float, float]]] = {}
    for r in rows:
        if r["err_vector_ms"] is None or r["source"] == "test":
            continue
        d_nm = haversine_nm(anchor[0], anchor[1], r["lat"], r["lon"])
        age_h = (now - datetime.fromisoformat(r["obs_time"])) \
            .total_seconds() / 3600.0
        w = (cfg.trust_for(r["source"])
             * distance_weight(d_nm, cfg.scoring.half_weight_nm)
             * _half_life_weight(r["lead_hours"], cfg.scoring.lead_half_h)
             * _half_life_weight(age_h, cfg.scoring.recency_half_h))
        per_model.setdefault(r["model"], []).append(
            (w, r["err_vector_ms"], r["err_dir_deg"] or 0.0,
             r["err_press_hpa"] if r["err_press_hpa"] is not None else np.nan))

    t_iso = now.isoformat(timespec="seconds")
    scores: dict[str, float] = {}
    for model in cfg.models:
        samples = per_model.get(model, [])
        if not samples:
            continue
        w = np.array([s[0] for s in samples])
        ev = np.array([s[1] for s in samples])
        ed = np.array([s[2] for s in samples])
        ep = np.array([s[3] for s in samples])
        if w.sum() <= 0:
            continue
        rmse = float(np.sqrt(np.sum(w * ev ** 2) / w.sum()))
        score = float(np.exp(-rmse / cfg.scoring.err_scale_ms))
        press_bias = (float(np.nansum(w * ep) / w.sum())
                      if not np.all(np.isnan(ep)) else None)
        scores[model] = score
        obs_store.insert_score(
            time_iso=t_iso, model=model, score=round(score, 4),
            n_obs=len(samples), rmse_vector_ms=round(rmse, 3),
            mean_dir_err=round(float(np.sum(w * ed) / w.sum()), 1),
            mean_press_bias=(round(press_bias, 2)
                             if press_bias is not None else None))

    if scores:
        log.info("confidence: %s (anchor=%s)",
                 {m: round(s, 3) for m, s in scores.items()},
                 "yacht" if yacht else "focus")
    return scores


def blend_weights(cfg: RaceConfig, obs_store: ObsStore,
                  floor: float = 0.05) -> dict[str, float] | None:
    """Normalized blend weights from the latest scores (None if no scores).

    A small floor keeps every model in the blend — a model at score 0
    still carries signal, and scores recover as its forecasts improve.
    """
    latest = obs_store.latest_scores()
    usable = {m: max(latest[m], floor) for m in cfg.models if m in latest}
    if not usable:
        return None
    total = sum(usable.values())
    return {m: v / total for m, v in usable.items()}
