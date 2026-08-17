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
from gribbosaurus_rex.extract import open_run
from gribbosaurus_rex.store.runs import RunStore

log = logging.getLogger("gribbo.pipeline")

# Prior weights, used until enough verification data exists to earn real
# confidence scores (verify.blend_weights takes over automatically).
STATIC_WEIGHTS = {
    "ifs": 0.35, "aifs": 0.2, "gfs": 0.25, "icon_eu": 0.2,
    # High-res priors: competitive but not dominant until they earn scores.
    # These are per-race priors, renormalized over each race's model list.
    "mf_arome": 0.3, "mf_arpege": 0.25,
    "mf_arpege_global": 0.2, "mf_arome_antilles": 0.3,
    "ukmo_ukv": 0.3, "icon_2i": 0.3,
}


def current_weights(cfg: RaceConfig) -> tuple[dict[str, float], str]:
    """(weights, source) — earned confidence scores if any, else priors."""
    from gribbosaurus_rex.obs.store import ObsStore
    from gribbosaurus_rex.verify import blend_weights

    try:
        earned = blend_weights(cfg, ObsStore(cfg.db_path))
    except Exception:  # noqa: BLE001
        log.exception("score lookup failed — falling back to priors")
        earned = None
    if earned:
        return earned, "confidence"
    total = sum(STATIC_WEIGHTS.get(m, 0.1) for m in cfg.models)
    return ({m: STATIC_WEIGHTS.get(m, 0.1) / total for m in cfg.models},
            "prior")


def resolve_weights(cfg: RaceConfig, override: dict | None = None
                    ) -> tuple[dict[str, float], str]:
    """Blend weights for a run: a user override if given, else earned/prior.

    The override (dashboard sliders, or any API caller passing ?weights=)
    is filtered to the race's models, must contain at least one positive
    finite value, and is normalized here. Invalid overrides raise
    ValueError so the API can 422 rather than silently blending wrong.
    Manual weights are per-request only — they never touch the published
    scores.json or the stored confidence history.
    """
    if override is None:
        return current_weights(cfg)
    if not isinstance(override, dict):
        raise ValueError("weights must be an object of {model: number}")
    usable = {}
    for m, v in override.items():
        if m not in cfg.models:
            continue           # unknown/other-race models are ignored
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"weight for {m!r} is not a number: {v!r}")
        if not np.isfinite(x) or x < 0:
            raise ValueError(f"weight for {m!r} must be finite and >= 0")
        usable[m] = x
    total = sum(usable.values())
    if not usable or total <= 0:
        raise ValueError(
            f"no positive weight for any model of this race "
            f"(models: {list(cfg.models)})")
    return {m: v / total for m, v in usable.items()}, "manual"


def generate_grid(cfg: RaceConfig, step: float = 0.25):
    lats = np.arange(cfg.bbox.lat_min, cfg.bbox.lat_max + 1e-9, step)
    lons = np.arange(cfg.bbox.lon_min, cfg.bbox.lon_max + 1e-9, step)
    return lats, lons


def run(cfg: RaceConfig | None = None, valid_time=None, step: float = 0.25,
        weights_override: dict | None = None) -> pd.DataFrame:
    """Blend the newest complete run of each configured model onto a grid.

    Returns lat, lon, speed_ms (SI), direction (deg true, FROM),
    uncertainty_ms (mean abs model-to-blend wind-speed spread), n_models.
    Display layers convert to knots at the boundary.
    weights_override: manual {model: weight} for this request only (see
    resolve_weights) — the dashboard's user-configurable blend.
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
            ds = open_run(rec, bbox=cfg.bbox)
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

    weights, weight_source = resolve_weights(cfg, weights_override)
    w_total = sum(weights.get(m, 0.05) for m in fields)
    u_blend = np.zeros((len(lats), len(lons)))
    v_blend = np.zeros_like(u_blend)
    for m, (u, v) in fields.items():
        w = weights.get(m, 0.05) / w_total
        u_blend += w * u
        v_blend += w * v
    log.debug("blend weights (%s): %s", weight_source,
              {m: round(weights.get(m, 0.05) / w_total, 3) for m in fields})

    speed_ms, direction = to_speed_dir(u_blend, v_blend)

    # spread of member wind speeds around the blend = cheap uncertainty proxy
    spreads = [np.abs(np.hypot(u, v) - np.hypot(u_blend, v_blend))
               for u, v in fields.values()]
    uncertainty_ms = np.mean(spreads, axis=0)

    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")
    return pd.DataFrame({
        "lat": lat_grid.ravel(),
        "lon": lon_grid.ravel(),
        "speed_ms": speed_ms.ravel(),
        "direction": direction.ravel(),
        "uncertainty_ms": uncertainty_ms.ravel(),
        "n_models": len(fields),
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
