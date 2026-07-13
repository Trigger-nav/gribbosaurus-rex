"""Thin service layer kept for backwards compatibility.

The API now calls extract/pipeline directly; these wrappers remain for
any external callers of the old interface.
"""

from gribbosaurus_rex.config import load_config
from gribbosaurus_rex.extract import latest_point_forecasts
from gribbosaurus_rex.pipeline import run


def get_point_forecast(lat: float, lon: float):
    df = latest_point_forecasts(load_config(), lat, lon)
    df["time"] = df["time"].astype(str)
    return df.to_dict(orient="records")


def get_grid_forecast():
    return run().to_dict(orient="records")
