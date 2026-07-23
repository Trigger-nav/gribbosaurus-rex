"""Race-area GRIB export — the racing tool's "download this model" button.

Takes a model's newest complete run (per-step .grib2 files on disk),
crops every message to the race bbox, and concatenates them into one
multi-message GRIB2 file that routing software (Expedition, Adrena,
qtVlm...) loads directly.

Cropping is done with eccodes on regular_ll grids: slice the values
array and rewrite the grid-description keys. Anything unexpected —
non-regular grid, wrapped longitudes, missing keys — falls back to
passing that message through uncropped: a bigger file is annoying, a
broken one is dangerous.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import numpy as np

from gribbosaurus_rex.config import BBox, RaceConfig
from gribbosaurus_rex.store.runs import RunRecord, RunStore

log = logging.getLogger("gribbo.export")

CROP_PAD_DEG = 0.5  # margin so routers can interpolate at the box edge


def _axis(first: float, last: float, n: int) -> np.ndarray:
    """Simple first..last axis (latitudes: no wrap, either direction)."""
    return np.linspace(first, last, n)


def _norm_lon(lon):
    return ((np.asarray(lon) + 180.0) % 360.0) - 180.0


def _lon_axis(lon0: float, di: float, ni: int,
              scans_negatively: bool) -> np.ndarray:
    """Longitude axis in scan order, normalized to [-180, 180).

    GRIB encodes longitudes 0..360 and the first point can sit anywhere
    on the circle (ECMWF global: first=180 means -180; ICON-EU: first=
    336.5 means -23.5), so the axis must be built modularly from the
    increment — NEVER by interpolating first..last. Pure; unit-tested.
    """
    step = -di if scans_negatively else di
    return _norm_lon((lon0 + step * np.arange(ni)) % 360.0)


def _unwrap_roll(lons_norm: np.ndarray) -> int | None:
    """Columns to np.roll a wrapped-but-ascending longitude axis by so it
    becomes monotonic ascending (global 0..360 grids normalize to
    -180..180 with one jump). None = already monotonic or unhandleable.

    Pure function — unit-tested offline.
    """
    diffs = np.diff(lons_norm)
    if np.all(diffs > 0):
        return 0  # already fine
    jumps = np.nonzero(diffs < 0)[0]
    if len(jumps) != 1:
        return None  # descending or genuinely weird — caller passes through
    shift = -(int(jumps[0]) + 1)
    if not np.all(np.diff(np.roll(lons_norm, shift)) > 0):
        return None
    return shift


def _index_range(coords: np.ndarray, lo: float, hi: float) -> tuple[int, int] | None:
    """(start, stop) covering [lo, hi] on a monotonic axis, else None.

    Pure function — unit-tested offline. `coords` may ascend or descend;
    the returned slice indexes the ORIGINAL order.
    """
    asc = coords[0] <= coords[-1]
    c = coords if asc else coords[::-1]
    inside = np.nonzero((c >= lo) & (c <= hi))[0]
    if len(inside) < 2:
        return None
    start, stop = int(inside[0]), int(inside[-1]) + 1
    if not asc:
        n = len(coords)
        start, stop = n - stop, n - start
    return start, stop


def _crop_message(msg_id, bbox: BBox) -> bytes | None:
    """Crop one GRIB message to bbox; None means 'pass through as-is'."""
    import eccodes as ec

    try:
        if ec.codes_get(msg_id, "gridType") != "regular_ll":
            return None
        ni = ec.codes_get(msg_id, "Ni")
        nj = ec.codes_get(msg_id, "Nj")
        lat0 = ec.codes_get(msg_id, "latitudeOfFirstGridPointInDegrees")
        lat1 = ec.codes_get(msg_id, "latitudeOfLastGridPointInDegrees")
        lon0 = ec.codes_get(msg_id, "longitudeOfFirstGridPointInDegrees")
        di = ec.codes_get(msg_id, "iDirectionIncrementInDegrees")
        i_neg = bool(ec.codes_get(msg_id, "iScansNegatively"))

        lats = _axis(lat0, lat1, nj)          # scan order; no wrap possible
        lons = _lon_axis(lon0, di, ni, i_neg)  # scan order, modular

        values = np.asarray(ec.codes_get_values(msg_id)).reshape(nj, ni)

        if np.all(np.diff(lons) < 0):
            return None  # descending longitudes — rare, pass through
        shift = _unwrap_roll(lons)
        if shift is None:
            return None
        if shift:
            # axis crosses ±180 mid-grid: rotate so it ascends monotonic
            lons = np.roll(lons, shift)
            values = np.roll(values, shift, axis=1)

        b = bbox.padded(CROP_PAD_DEG)
        jr = _index_range(lats, b.lat_min, b.lat_max)
        ir = _index_range(lons, b.lon_min, b.lon_max)
        if jr is None or ir is None:
            return None
        j0, j1 = jr
        i0, i1 = ir

        sub = values[j0:j1, i0:i1]

        clone = ec.codes_clone(msg_id)
        try:
            ec.codes_set(clone, "Ni", sub.shape[1])
            ec.codes_set(clone, "Nj", sub.shape[0])
            ec.codes_set(clone, "latitudeOfFirstGridPointInDegrees",
                         float(lats[j0]))
            ec.codes_set(clone, "latitudeOfLastGridPointInDegrees",
                         float(lats[j1 - 1]))
            ec.codes_set(clone, "longitudeOfFirstGridPointInDegrees",
                         float(lons[i0] % 360.0))
            ec.codes_set(clone, "longitudeOfLastGridPointInDegrees",
                         float(lons[i1 - 1] % 360.0))
            ec.codes_set_values(clone, sub.ravel())
            return ec.codes_get_message(clone)
        finally:
            ec.codes_release(clone)
    except Exception:  # noqa: BLE001 — any surprise -> uncropped fallback
        log.exception("crop failed — passing message through uncropped")
        return None


def slim_crop_file(path: Path, bbox: BBox, *, keep=None) -> tuple[int, int]:
    """Rewrite a downloaded GRIB in place: keep only messages `keep(msg_id)`
    accepts, each cropped to `bbox` (regular_ll via _crop_message). This is
    what makes full-domain high-res packages (Météo-France) cheap to decode
    — an ~800k-point all-variable AROME file becomes a small few-field one.

    SAFE BY DESIGN: on ANY error the original file is left exactly as-is
    (worst case = slow, never broken). Returns (messages_written, total).
    If nothing is kept (a too-strict `keep`), the original is preserved.
    """
    import eccodes as ec

    tmp = path.with_suffix(path.suffix + ".slim")
    n_total = n_written = 0
    try:
        with open(path, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                msg_id = ec.codes_grib_new_from_file(fin)
                if msg_id is None:
                    break
                try:
                    n_total += 1
                    if keep is not None and not keep(msg_id):
                        continue
                    cropped = _crop_message(msg_id, bbox)
                    fout.write(cropped if cropped is not None
                               else ec.codes_get_message(msg_id))
                    n_written += 1
                finally:
                    ec.codes_release(msg_id)
    except Exception:  # noqa: BLE001 — leave the original untouched
        log.exception("slim_crop_file failed for %s — keeping original", path.name)
        tmp.unlink(missing_ok=True)
        return (0, n_total)

    if n_written:
        tmp.replace(path)          # cropped (and field-filtered) file wins
    else:
        tmp.unlink(missing_ok=True)  # kept nothing -> keep the original
    return (n_written, n_total)


def build_race_grib(cfg: RaceConfig, model: str,
                    run_store: RunStore | None = None) -> tuple[bytes, str]:
    """(grib_bytes, filename) for a model's newest run, cropped to the race.

    Raises FileNotFoundError when the model has no complete run on disk.
    """
    import eccodes as ec

    store = run_store or RunStore(cfg.db_path)
    rec: RunRecord | None = store.latest_complete(model)
    if rec is None:
        raise FileNotFoundError(f"No complete {model} run on disk")

    out = io.BytesIO()
    n_msgs = n_cropped = 0
    for step_file in sorted(Path(rec.path).glob("*.grib2")):
        with open(step_file, "rb") as f:
            while True:
                msg_id = ec.codes_grib_new_from_file(f)
                if msg_id is None:
                    break
                try:
                    cropped = _crop_message(msg_id, cfg.bbox)
                    if cropped is not None:
                        out.write(cropped)
                        n_cropped += 1
                    else:
                        out.write(ec.codes_get_message(msg_id))
                    n_msgs += 1
                finally:
                    ec.codes_release(msg_id)

    if n_msgs == 0:
        raise FileNotFoundError(f"Run directory {rec.path} contains no messages")

    cycle_tag = rec.cycle.replace(":", "").replace("-", "")[:11]  # 20260713T12
    fname = f"gribbo_{cfg.name}_{model}_{cycle_tag}Z.grib2"
    log.info("export %s: %d messages (%d cropped), %.1f MB",
             fname, n_msgs, n_cropped, out.tell() / 1e6)
    return out.getvalue(), fname
