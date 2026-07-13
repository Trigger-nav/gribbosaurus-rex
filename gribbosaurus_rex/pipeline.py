"""Blended wind field over the race area, built from the newest GRIB runs.

Phase 1: model weights are still static (set below). In Phase 2 they
become dynamic confidence scores earned from observation verification.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from gribbosaurus_rex.config import RaceConfig, load_config
from gribbosaurus_rex.core.wind import to_speed_dir
from gribbosaurus_rex.extract import MS_TO_KN, open_run
from gribbosaurus_rex.store.runs import RunStore

log = logging.getLogger("gribbo.pipeline")

# Static prior weights until verification-based scoring lands (Phase 2).
STATIC_WEIGHTS = {"ifs": 0.35, "aifs": 0.2, "gfs": 0.25, "icon_eu": 0.2}


def generate_grid(cfg: RaceConfig, step: float = 0.25):
    lats = np.arange(cfg.bbox.lat_min, cfg.bbox.lat_max + 1e-9, step)
    lons = np.arange(cfg.bbox.lon_min, cfg.bbox.lon_max + 1e-9, step)
    return lats, lons


def run(cfg: RaceConfig | None = None, valid_time=None, step: float = 0.25) -> pd.DataFrame:
    """Blend the newest complete run of each configured model onto a grid.

    Returns lat, lon, speed (kn), direction (deg FROM), uncertainty (kn,
    mean abs model-to-blend wind-speed spread), n_models.
    """
    import xarray as xr

    cfg = cfg or load_config()
    store = RunStore(cfg.db_path)
    lats, lons = generate_grid(cfg, step)

    lat_da = xr.DataArray(lats, dims="lat")
    lon_da = xr.DataArray(lons, dims="lon")

    # One common valid time for every model (default: now), so the blend
    # never mixes different forecast times across models.
    when = pd.Timestamp(valid_time) if valid_time is not None \
        else pd.Timestamp.now(tz="UTC").floor("10min")
    if when.tzinfo is not None:
        when = when.tz_convert("UTC").tz_localize(None)

    fields = {}  # model -> (u, v) arrays on (lat, lon)
    for model in cfg.models:
        rec = store.latest_complete(model)
        if rec is None:
            continue
        try:
            ds = open_run(rec)
            pt = ds.interp(time=when, latitude=lat_da, longitude=lon_da,
                           method="linear")
            if np.isnan(pt["u10"].values).all():
                log.warning("%s run does not cover %s — skipped", model, when)
                continue
            fields[model] = (pt["u10"].values, pt["v10"].values)
        except Exception:  # noqa: BLE001
            log.exception("grid extraction failed for %s", model)

    if not fields:
        raise RuntimeError(
            "No complete model runs on disk yet — run "
            "`python -m gribbosaurus_rex fetch-once` first.")

    w_total = sum(STATIC_WEIGHTS.get(m, 0.1) for m in fields)
    u_blend = np.zeros((len(lats), len(lons)))
    v_blend = np.zeros_like(u_blend)
    for m, (u, v) in fields.items():
        w = STATIC_WEIGHTS.get(m, 0.1) / w_total
        u_blend += w * u
        v_blend += w * v

    speed_ms, direction = to_speed_dir(u_blend, v_blend)

    # spread of member wind speeds around the blend = cheap uncertainty proxy
    spreads = [np.abs(np.hypot(u, v) - np.hypot(u_blend, v_blend))
               for u, v in fields.values()]
    uncertainty_ms = np.mean(spreads, axis=0)

    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")
    return pd.DataFrame({
        "lat": lat_grid.ravel(),
        "lon": lon_grid.ravel(),
        "speed": (speed_ms * MS_TO_KN).ravel(),
        "direction": direction.ravel(),
        "uncertainty": (uncertainty_ms * MS_TO_KN).ravel(),
        "n_models": len(fields),
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
