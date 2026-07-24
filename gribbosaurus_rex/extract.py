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

    Assumes 1D regular latitude/longitude coordinates. Grids with 2D
    coordinate arrays (e.g. UKV's native Lambert projection) are not
    handled here — those are reprojected upstream before this is called.
    """
    if bbox is None:
        return ds
    if ds.latitude.ndim != 1 or ds.longitude.ndim != 1:
        return ds  # 2D-coordinate grid — cropping happens elsewhere
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


# non-dimension coords we deliberately keep while stripping level scalars
# (heightAboveGround, surface, number, ...) so single/multi-step assembly works
_KEEP_COORDS = ("latitude", "longitude", "step", "valid_time", "time")


def _clean_da(da):
    """Strip level/ensemble scalar coords but preserve valid_time and step.

    reset_coords(drop=True) would also drop valid_time (a derived, non-index
    coordinate), which we need to build the time axis — so we drop explicitly.
    """
    drop = [c for c in da.coords if c not in da.dims and c != "valid_time"]
    return da.drop_vars(drop, errors="ignore")


def _normalize_lon(ds):
    """Shift longitudes from 0..360 to -180..180 and re-sort (GFS grids)."""
    if ds.longitude.ndim == 1 and float(ds.longitude.max()) > 180:
        ds = ds.assign_coords(
            longitude=(((ds.longitude + 180) % 360) - 180))
        ds = ds.sortby("longitude")
    return ds


def _to_time_indexed(ds):
    """Re-index a per-file dataset onto a `time` dimension from valid_time.

    Handles both layouts cfgrib produces:
      * single forecast step  -> scalar valid_time -> length-1 time axis
      * multi-step file        -> valid_time along `step` -> full time axis
    Météo-France "packages" and Met Office orders deliver the multi-step
    form (many lead times per GRIB); ECMWF/GFS/ICON here are one step/file.
    """
    if "valid_time" not in ds.coords:
        raise RuntimeError("dataset has no valid_time coordinate")
    vt = ds["valid_time"]
    if vt.ndim == 0:
        t = pd.Timestamp(np.asarray(vt.values).item())
        ds = ds.drop_vars([c for c in ("valid_time", "step")
                           if c in ds.coords], errors="ignore")
        return ds.expand_dims(time=[t])
    # multi-step: valid_time varies along its (single) dimension, usually 'step'
    dim = vt.dims[0]
    ds = ds.assign_coords(
        time=(dim, pd.to_datetime(np.asarray(vt.values))))
    ds = ds.swap_dims({dim: "time"})
    drop = [c for c in ("step", "valid_time") if c in ds.coords]
    return ds.drop_vars(drop, errors="ignore")


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

    per_file = []
    for f in files:
        # cfgrib/eccodes hold file handles + buffers per open; a full
        # multi-model pass opens dozens of these and the memory creeps up
        # (OOM risk on small boxes). Close every source dataset the moment
        # we've .load()ed the cropped values we need out of it.
        dsets = cfgrib.open_datasets(str(f), backend_kwargs={"indexpath": ""})
        try:
            u = v = p = None
            for ds in dsets:
                u = u if u is not None else _pick(ds.data_vars, U_NAMES)
                v = v if v is not None else _pick(ds.data_vars, V_NAMES)
                p = p if p is not None else _pick(ds.data_vars, P_NAMES)
            if u is None or v is None:
                log.warning("skipping %s: missing wind fields", f.name)
                continue
            pieces = {"u10": _clean_da(u), "v10": _clean_da(v)}
            if p is not None:
                pieces["msl"] = _clean_da(p)
            step_ds = xr.Dataset(pieces)

            # normalize longitude to [-180, 180] (GFS grids use 0..360),
            # then crop BEFORE loading values — this is where the win is
            step_ds = _normalize_lon(step_ds)
            step_ds = _crop(step_ds, bbox)

            # build the time axis — one step per file (ECMWF/GFS/ICON) or
            # many steps per file (Météo-France packages, Met Office orders).
            # .load() detaches values from the source datasets so they can
            # be closed immediately below.
            step_ds = _to_time_indexed(step_ds).load()
            per_file.append(step_ds)
        finally:
            for ds in dsets:
                try:
                    ds.close()
                except Exception:  # noqa: BLE001 — best-effort handle release
                    pass

    if not per_file:
        raise RuntimeError(f"No usable GRIB messages found in {run_dir}")

    # concat, then drop any duplicate valid times (overlapping package ranges)
    out = xr.concat(per_file, dim="time").sortby("time")
    _, keep = np.unique(out["time"].values, return_index=True)
    if len(keep) != out.sizes["time"]:
        out = out.isel(time=keep)
    return out


# Sized to hold a whole race's verification working set resident at once.
# verify_pass loops obs -> model -> run, so within one race the distinct
# datasets touched are (models x complete-runs-in-window): ~7 models x a
# couple of runs each. If maxsize is smaller than that set, every single
# observation re-decodes the heavy multi-file runs (icon_eu ~93 files,
# gfs ~129 files) from cold — turning an O(models x runs) pass into
# O(obs x models x runs) and blowing past the arbiter's time budget on the
# obs-dense Channel/Fastnet races. 32 covers 7 models x ~4 runs with
# headroom; entries are small in-memory cropped arrays (handles already
# closed in _open_run_dataset), so the memory cost stays modest and LRU
# evicts the previous race's datasets as the pass moves on.
@lru_cache(maxsize=32)
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
