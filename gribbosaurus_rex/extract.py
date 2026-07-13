"""Read downloaded GRIB runs into xarray and extract point time series.

This is the bridge between raw GRIB files on disk and the rest of the
system (blending, scoring, API). Output schema is always:

    time (UTC) | wind_speed (kn) | wind_dir (deg FROM) | pressure (hPa)

plus `model` and `cycle` columns, matching the original ingest schema.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.core.wind import to_speed_dir
from gribbosaurus_rex.store.runs import RunRecord, RunStore

log = logging.getLogger("gribbo.extract")

MS_TO_KN = 1.943844

# cfgrib short-name candidates across models (ECMWF / GFS / ICON)
U_NAMES = ("u10", "10u", "u")
V_NAMES = ("v10", "10v", "v")
P_NAMES = ("msl", "prmsl", "pmsl", "pres")


def _pick(ds_vars: dict, names: tuple[str, ...]):
    for n in names:
        if n in ds_vars:
            return ds_vars[n]
    return None


def _open_run_dataset(run_dir: Path):
    """Open all per-step GRIB files of a run as one dataset on valid_time.

    Uses cfgrib.open_datasets per file (a file may mix level types, e.g.
    10m winds + MSL pressure), normalizes names, then concatenates.
    """
    import warnings

    import cfgrib
    import xarray as xr

    # cfgrib's internal xr.merge tickles a noisy xarray FutureWarning
    warnings.filterwarnings("ignore", category=FutureWarning, module="cfgrib")

    files = sorted(run_dir.glob("*.grib2"))
    if not files:
        raise FileNotFoundError(f"No GRIB files in {run_dir}")

    per_step = []
    for f in files:
        u = v = p = None
        for ds in cfgrib.open_datasets(str(f), backend_kwargs={"indexpath": ""}):
            u = u if u is not None else _pick(ds.data_vars, U_NAMES)
            v = v if v is not None else _pick(ds.data_vars, V_NAMES)
            p = p if p is not None else _pick(ds.data_vars, P_NAMES)
        if u is None or v is None:
            log.warning("skipping %s: missing wind fields", f.name)
            continue
        pieces = {"u10": u.reset_coords(drop=True),
                  "v10": v.reset_coords(drop=True)}
        if p is not None:
            pieces["msl"] = p.reset_coords(drop=True)
        step_ds = xr.Dataset(pieces)
        # promote valid_time to an indexable dimension
        vt = pd.Timestamp(np.asarray(u.valid_time.values).item())
        step_ds = step_ds.expand_dims(time=[vt])
        per_step.append(step_ds)

    if not per_step:
        raise RuntimeError(f"No usable GRIB messages found in {run_dir}")

    ds = xr.concat(per_step, dim="time").sortby("time")

    # normalize longitude to [-180, 180] (GFS grids use 0..360)
    if float(ds.longitude.max()) > 180:
        ds = ds.assign_coords(longitude=(((ds.longitude + 180) % 360) - 180))
        ds = ds.sortby("longitude")
    return ds


@lru_cache(maxsize=8)
def _cached_run_dataset(run_dir: str, mtime_key: float):
    return _open_run_dataset(Path(run_dir))


def open_run(rec: RunRecord):
    """Open a run's dataset with a small cache keyed on (path, mtime)."""
    run_dir = Path(rec.path)
    mtime = max((f.stat().st_mtime for f in run_dir.glob("*.grib2")), default=0.0)
    return _cached_run_dataset(str(run_dir), mtime)


def point_timeseries(rec: RunRecord, lat: float, lon: float) -> pd.DataFrame:
    """Full forecast time series at one location, from one model run."""
    ds = open_run(rec)
    pt = ds.interp(latitude=lat, longitude=lon, method="linear")

    u = pt["u10"].values
    v = pt["v10"].values
    speed_ms = np.sqrt(u ** 2 + v ** 2)
    _, direction = to_speed_dir(u, v)

    out = pd.DataFrame({
        "time": pd.to_datetime(pt["time"].values),
        "wind_speed": speed_ms * MS_TO_KN,
        "wind_dir": direction,
        "pressure": (pt["msl"].values / 100.0) if "msl" in pt else np.nan,
    })
    out["model"] = rec.model
    out["cycle"] = rec.cycle
    return out


def value_at(rec: RunRecord, lat: float, lon: float, when: pd.Timestamp) -> dict:
    """Model value interpolated to a single time/place (for verification)."""
    ds = open_run(rec)
    when = pd.Timestamp(when)
    if when.tzinfo is not None:
        when = when.tz_convert("UTC").tz_localize(None)
    pt = ds.interp(latitude=lat, longitude=lon, time=when, method="linear")
    u = float(pt["u10"]); v = float(pt["v10"])
    speed, direction = to_speed_dir(u, v)
    return {
        "wind_speed": speed * MS_TO_KN,
        "wind_dir": float(direction),
        "pressure": float(pt["msl"]) / 100.0 if "msl" in pt else float("nan"),
        "model": rec.model,
        "cycle": rec.cycle,
    }


def latest_point_forecasts(cfg: RaceConfig, lat: float, lon: float) -> pd.DataFrame:
    """Newest complete run of every configured model, extracted at a point."""
    store = RunStore(cfg.db_path)
    frames = []
    for model in cfg.models:
        rec = store.latest_complete(model)
        if rec is None:
            log.info("no complete run yet for %s", model)
            continue
        try:
            frames.append(point_timeseries(rec, lat, lon))
        except Exception:  # noqa: BLE001
            log.exception("extraction failed for %s %s", model, rec.cycle)
    if not frames:
        return pd.DataFrame(
            columns=["time", "wind_speed", "wind_dir", "pressure", "model", "cycle"])
    return pd.concat(frames, ignore_index=True)
