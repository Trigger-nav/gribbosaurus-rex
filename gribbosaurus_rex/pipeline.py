import pandas as pd

from gribbosaurus_rex.ingest.ecmwf import fetch_ecmwf
from gribbosaurus_rex.core.blend import blend
from gribbosaurus_rex.core.wind import to_uv


# ----------------------------
# GRID GENERATOR
# ----------------------------
def generate_grid(lat_min=38.5, lat_max=40.0, lon_min=1.0, lon_max=3.0, step=0.25):
    lats = []
    lons = []

    lat = lat_min
    while lat <= lat_max:
        lats.append(round(lat, 3))
        lat += step

    lon = lon_min
    while lon <= lon_max:
        lons.append(round(lon, 3))
        lon += step

    return [(lat, lon) for lat in lats for lon in lons]


# ----------------------------
# MAIN PIPELINE
# ----------------------------
def run(grid_points=None):

    if grid_points is None:
        grid_points = generate_grid()

    results = []

    for lat, lon in grid_points:

        # Fetch model data (IFS only for now)
        ifs = fetch_ecmwf(lat, lon, "ecmwf_ifs")

        # fallback (AIFS not available in Open-Meteo)
        aifs = ifs.copy()

        weights = {"IFS": 0.7, "AIFS": 0.5}

        ws_ifs = ifs["wind_speed"].iloc[0]
        wd_ifs = ifs["wind_dir"].iloc[0]

        ws_aifs = aifs["wind_speed"].iloc[0]
        wd_aifs = aifs["wind_dir"].iloc[0]

        u_ifs, v_ifs = to_uv(ws_ifs, wd_ifs)
        u_aifs, v_aifs = to_uv(ws_aifs, wd_aifs)

        models = {
            "IFS": {"u": u_ifs, "v": v_ifs, "w": weights["IFS"]},
            "AIFS": {"u": u_aifs, "v": v_aifs, "w": weights["AIFS"]},
        }

        speed, direction = blend(models)

        results.append({
            "lat": lat,
            "lon": lon,
            "speed": speed,
            "direction": direction
        })

    return pd.DataFrame(results)