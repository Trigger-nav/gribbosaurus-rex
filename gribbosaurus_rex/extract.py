"""Read downloaded GRIB runs into xarray and extract point time series.

This is the bridge between raw GRIB files on disk and the rest of the
system (blending, scoring, API). Output schema is always SI (contract
convention alignment — knots exist only at display boundaries):

    time (UTC) | wind_speed_ms | wind_dir (deg true, FROM) | pressure (hPa)

plus `model` and `cycle` columns.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from gribbosaurus_rex.config import BBox, RaceConfig
from gribbosaurus_rex.core.wind import to_speed_dir
from gribbosaurus_rex.store.runs import RunRecord, RunStore

log = logging.getLogger("gribbo.extract")

MS_TO_KN = 1.943844  # for DISPLAY conversions only — internals are SI

# cfgrib short-name candidates across models (ECMWF / GFS / ICON)
U_NAMES = ("u10", "10u", "u")
V_NAMES = ("v10", "10v", "v")
P_NAMES = ("msl", "prmsl", "pmsl", "pres")


def _pick(ds_vars: dict, names: tuple[str, ...]):
    for n in names:
        if n in ds_vars:
            return ds_vars[n]
    return None


def _crop(ds, bbox):
    """Crop a normalized-longitude dataset to a padded bbox.

    Critical for performance: ICON-EU files span all of Europe at 0.0625°
    (~720k points/field) — decoding that for every step of every run
    pins the CPU for minutes. Cropping to the race area first makes the
    load ~1000x smaller. Handles ascending or descending latitude order.
    """
    if bbox is None:
        return ds
    b = bbox.padded(0.75)
    lat_slice = (slice(b.lat_min, b.lat_max)
                 if float(ds.latitude[0]) <= float(ds.latitude[-1])
                 else slice(b.lat_max, b.lat_min))
    lon_slice = (slice(b.lon_min, b.lon_max)
                 if float(ds.longitude[0]) <= float(ds.longitude[-1])
                 else slice(b.lon_max, b.lon_min))
    out = ds.sel(latitude=lat_slice, longitude=lon_slice)
    # never return an empty grid — a bbox outside the file's domain is a bug
    if out.sizes.get("latitude", 0) < 2 or out.sizes.get("longitude", 0) < 2:
        return ds
    return out


def _open_run_dataset(run_dir: Path, bbox=None):
    """Open all per-step GRIB files of a run as one dataset on valid_time.

    Uses cfgrib.open_datasets per file (a file may mix level types, e.g.
    10m winds + MSL pressure), normalizes names + longitudes, crops to
    the race bbox, then concatenates.
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

        # normalize longitude to [-180, 180] (GFS grids use 0..360),
        # then crop BEFORE loading values — this is where the win is
        if float(step_ds.longitude.max()) > 180:
            step_ds = step_ds.assign_coords(
                longitude=(((step_ds.longitude + 180) % 360) - 180))
            step_ds = step_ds.sortby("longitude")
        step_ds = _crop(step_ds, bbox)

        # promote valid_time to an indexable dimension
        vt = pd.Timestamp(np.asarray(u.valid_time.values).item())
        step_ds = step_ds.expand_dims(time=[vt]).load()
        per_step.append(step_ds)

    if not per_step:
        raise RuntimeError(f"No usable GRIB messages found in {run_dir}")

    return xr.concat(per_step, dim="time").sortby("time")


@lru_cache(maxsize=8)
def _cached_run_dataset(run_dir: str, mtime_key: float, bbox_key):
    bbox = BBox(*bbox_key) if bbox_key else None
    return _open_run_dataset(Path(run_dir), bbox=bbox)


def open_run(rec: RunRecord, bbox=None):
    """Open a run's dataset with a small cache keyed on (path, mtime, bbox).

    ALWAYS pass the race bbox when you have it — it makes opening large-
    domain models (ICON-EU especially) orders of magnitude cheaper, and
    in fleet mode it is what crops the shared files to the right race.
    bbox=None decodes the file's full domain (slow; test/debug only).
    """
    run_dir = Path(rec.path)
    mtime = max((f.stat().st_mtime for f in run_dir.glob("*.grib2")), default=0.0)
    bbox_key = ((bbox.lat_min, bbox.lat_max, bbox.lon_min, bbox.lon_max)
                if bbox else None)
    return _cached_run_dataset(str(run_dir), mtime, bbox_key)


def point_timeseries(rec: RunRecord, lat: float, lon: float,
                     bbox=None) -> pd.DataFrame:
    """Full forecast time series at one location, from one model run."""
    ds = open_run(rec, bbox=bbox)
    pt = ds.interp(latitude=lat, longitude=lon, method="linear")

    u = pt["u10"].values
    v = pt["v10"].values
    speed_ms, direction = to_speed_dir(u, v)

    out = pd.DataFrame({
        "time": pd.to_datetime(pt["time"].values),
        "wind_speed_ms": speed_ms,
        "wind_dir": direction,
        "pressure": (pt["msl"].values / 100.0) if "msl" in pt else np.nan,
    })
    out["model"] = rec.model
    out["cycle"] = rec.cycle
    return out


def value_at(rec: RunRecord, lat: float, lon: float, when: pd.Timestamp,
             bbox=None) -> dict:
    """Model value interpolated to a single time/place (for verification)."""
    ds = open_run(rec, bbox=bbox)
    when = pd.Timestamp(when)
    if when.tzinfo is not None:
        when = when.tz_convert("UTC").tz_localize(None)
    pt = ds.interp(latitude=lat, longitude=lon, time=when, method="linear")
    u = float(pt["u10"]); v = float(pt["v10"])
    speed_ms, direction = to_speed_dir(u, v)
    return {
        "wind_speed_ms": float(speed_ms),
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
            frames.append(point_timeseries(rec, lat, lon, bbox=cfg.bbox))
        except Exception:  # noqa: BLE001
            log.exception("extraction failed for %s %s", model, rec.cycle)
    if not frames:
        return pd.DataFrame(
            columns=["time", "wind_speed_ms", "wind_dir", "pressure",
                     "model", "cycle"])
    return pd.concat(frames, ignore_index=True)
