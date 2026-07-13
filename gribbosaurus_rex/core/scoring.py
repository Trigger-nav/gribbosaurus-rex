"""Model verification scoring (Phase 2 — interface stub).

NOTE: this file previously contained an accidental copy of blend.py.
The real implementation lands with observation ingestion. The intended
contract, so other code can build against it now:

    score_forecast(forecast, obs) -> per-obs errors
    confidence(errors, weights)   -> 0..1 score per model

Errors combine wind-vector RMSE, circular direction error and pressure
bias; each observation is weighted by distance from the yacht, recency,
and source trust (yacht instruments > buoys/METAR > served APIs).
"""

from __future__ import annotations

import numpy as np

from gribbosaurus_rex.core.wind import circular_error, to_uv


def wind_vector_error(ws_fc, wd_fc, ws_ob, wd_ob):
    """Magnitude of the vector difference between forecast and observed
    wind (same units as input speeds). This is the primary skill metric:
    it penalizes speed and direction errors together, correctly."""
    u_f, v_f = to_uv(np.asarray(ws_fc, dtype=float), np.asarray(wd_fc, dtype=float))
    u_o, v_o = to_uv(np.asarray(ws_ob, dtype=float), np.asarray(wd_ob, dtype=float))
    return np.hypot(u_f - u_o, v_f - v_o)


def direction_error(wd_fc, wd_ob):
    """Circular direction error in degrees (wrap-around safe)."""
    return circular_error(np.asarray(wd_fc, dtype=float),
                          np.asarray(wd_ob, dtype=float))


def distance_weight(dist_nm, half_weight_nm: float = 30.0):
    """Observation weight by distance from the yacht: 1 at 0nm, 0.5 at
    half_weight_nm, decaying exponentially."""
    return 0.5 ** (np.asarray(dist_nm, dtype=float) / half_weight_nm)
