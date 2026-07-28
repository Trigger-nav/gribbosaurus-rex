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

PERFORMANCE — the loop MUST stay run-major (model -> run -> all obs at
once). The obvious obs-major loop (for ob: for model: for run:) is
catastrophically slow at fleet scale and took the arbiter from ~2 min to
45-min systemd kills: with ~4-5k obs in the window and 7 models x 8 runs
(= 56 datasets, more than any sane cache holds) it (a) cycles the dataset
cache in LRU's worst-case pattern so nearly every access re-decodes a
multi-file GRIB, (b) issues one has_verification point-query per
(obs, model, run) ~= hundreds of thousands per race, and (c) does one
scalar xarray interp per pair. Run-major does <=56 dataset opens, one
verified-set query per run, and ONE vectorized interp per run. Profiled
on the prod box 2026-07-28; see scripts/profile_pass.py.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.core.scoring import (direction_error, distance_weight,
                                           wind_vector_error)
from gribbosaurus_rex.core.wind import to_speed_dir
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

def _naive_utc(iso: str) -> np.datetime64:
    """ISO8601 (naive or tz-aware) -> tz-naive UTC numpy datetime64."""
    ts = pd.Timestamp(iso)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return np.datetime64(ts)


def _build_rows(sub: list[Obs], model: str, cycle: str, lead_h: np.ndarray,
                fc_speed: np.ndarray, fc_dir: np.ndarray,
                fc_press: np.ndarray) -> list[tuple]:
    """Assemble insert_verifications() rows from vectorized forecast arrays.

    Pure numpy/python (no xarray) so it's unit-testable offline. Skips
    points where the forecast is NaN (obs outside the grid, or a valid
    time past the run's last step). NaN errors -> None columns, matching
    the old scalar path.
    """
    ob_speed = np.array([o.wind_speed_ms for o in sub], dtype=float)
    ob_dir = np.array([o.wind_dir_deg if o.wind_dir_deg is not None
                       else np.nan for o in sub], dtype=float)
    ob_press = np.array([o.pressure_hpa if o.pressure_hpa is not None
                         else np.nan for o in sub], dtype=float)

    with np.errstate(invalid="ignore"):
        err_vec = wind_vector_error(fc_speed, fc_dir, ob_speed, ob_dir)
        err_dir = direction_error(fc_dir, ob_dir)
        err_spd = fc_speed - ob_speed
        err_prs = fc_press - ob_press

    def _opt(x: float, nd: int | None = None) -> float | None:
        if np.isnan(x):
            return None
        return round(float(x), nd) if nd is not None else float(x)

    rows = []
    for k, o in enumerate(sub):
        if np.isnan(fc_speed[k]):
            continue
        rows.append((o.id, model, cycle, round(float(lead_h[k]), 2),
                     round(float(fc_speed[k]), 3), round(float(fc_dir[k]), 1),
                     _opt(fc_press[k], 1), _opt(err_vec[k]),
                     float(err_spd[k]), _opt(err_dir[k]), _opt(err_prs[k])))
    return rows


def verify_pass(cfg: RaceConfig, run_store: RunStore, obs_store: ObsStore) -> int:
    """Verify window obs against all covering runs. Returns rows added.

    Run-major + vectorized — see the module docstring for why this shape
    is load-bearing. Do not revert to a per-observation loop.
    """
    import xarray as xr

    from gribbosaurus_rex.extract import open_run

    observations = obs_store.recent_obs(cfg.scoring.window_h)
    box = cfg.bbox.padded(0.5)
    cand = [ob for ob in observations
            if ob.source != "test"            # smoke/loopback: never scored
            and ob.wind_speed_ms is not None
            and box.contains(ob.lat, ob.lon)]
    if not cand:
        return 0
    obs_times = np.array([_naive_utc(ob.time) for ob in cand])

    added = 0
    for model in cfg.models:
        runs = run_store.list_runs(model=model, limit=cfg.keep_runs * 2)
        for rec in runs:
            if rec.status != "complete":
                continue
            if not Path(rec.path).is_dir():
                # pruned from disk but still 'complete' in the DB — skip
                # cheaply here rather than exploding per-obs downstream
                log.debug("skipping pruned run %s %s", model, rec.cycle)
                continue
            lead_h = ((obs_times - _naive_utc(rec.cycle))
                      / np.timedelta64(1, "h")).astype(float)
            mask = (lead_h >= 0.0) & (lead_h <= cfg.max_lead_hours)
            if not mask.any():
                continue
            done = obs_store.verified_obs_ids(model, rec.cycle)
            idx = [int(i) for i in np.nonzero(mask)[0] if cand[i].id not in done]
            if not idx:
                continue
            sub = [cand[i] for i in idx]
            try:
                ds = open_run(rec, bbox=cfg.bbox)
                pt = ds.interp(
                    latitude=xr.DataArray([o.lat for o in sub], dims="pt"),
                    longitude=xr.DataArray([o.lon for o in sub], dims="pt"),
                    time=xr.DataArray(obs_times[idx], dims="pt"),
                    method="linear")
                fc_u = np.asarray(pt["u10"].values, dtype=float)
                fc_v = np.asarray(pt["v10"].values, dtype=float)
                fc_press = (np.asarray(pt["msl"].values, dtype=float) / 100.0
                            if "msl" in pt else np.full(len(sub), np.nan))
            except Exception:  # noqa: BLE001 — once per run, not per obs
                log.exception("verification interp failed: %s %s",
                              model, rec.cycle)
                continue
            fc_speed, fc_dir = to_speed_dir(fc_u, fc_v)
            rows = _build_rows(sub, model, rec.cycle, lead_h[idx],
                               np.asarray(fc_speed, dtype=float),
                               np.asarray(fc_dir, dtype=float), fc_press)
            added += obs_store.insert_verifications(rows)

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
            time_iso=t_iso, model=model, race=cfg.name, score=round(score, 4),
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
    """Normalized blend weights from this race's latest scores (None if
    no scores yet).

    A small floor keeps every model in the blend — a model at score 0
    still carries signal, and scores recover as its forecasts improve.
    """
    latest = obs_store.latest_scores(race=cfg.name)
    usable = {m: max(latest[m], floor) for m in cfg.models if m in latest}
    if not usable:
        return None
    total = sum(usable.values())
    return {m: v / total for m, v in usable.items()}
