"""DEPRECATED — placeholder ingest replaced by real GRIB pipeline.

The old fetch_model("IFS", lat, lon) returned placeholder constants.
It now proxies to the newest downloaded GRIB run so old callers keep
working. New code should use gribbosaurus_rex.extract directly.

The Open-Meteo client (ingest/ecmwf.py) is kept: it returns *served*
model data, which Phase 2 will reuse as a cross-check source.
"""

from __future__ import annotations

_ALIASES = {
    "IFS": "ifs",
    "AIFS": "aifs",
    "GFS": "gfs",
    "UKV": "icon_eu",   # UKV placeholder never had real data; nearest live
    "ICON_EU": "icon_eu",  # equivalent is the ICON-EU high-res regional model
}


def fetch_model(model_name: str, lat: float, lon: float):
    from gribbosaurus_rex.config import load_config
    from gribbosaurus_rex.extract import point_timeseries
    from gribbosaurus_rex.store.runs import RunStore

    key = _ALIASES.get(model_name, model_name.lower())
    cfg = load_config()
    rec = RunStore(cfg.db_path).latest_complete(key)
    if rec is None:
        raise RuntimeError(
            f"No downloaded runs for '{key}'. "
            "Run: python -m gribbosaurus_rex fetch-once")
    return point_timeseries(rec, lat, lon, bbox=cfg.bbox)
