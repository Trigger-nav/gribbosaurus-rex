from fastapi import FastAPI
import pandas as pd

from gribbosaurus_rex.pipeline import run

app = FastAPI(title="🦖 Gribbosaurus Rex API")


# ----------------------------
# Example grid (replace later)
# ----------------------------
def generate_grid():
    lat_range = [38.5, 39.0, 39.5]
    lon_range = [1.5, 2.0, 2.5]

    return [(lat, lon) for lat in lat_range for lon in lon_range]


@app.get("/")
def root():
    return {"status": "Gribbosaurus Rex online 🦖"}


@app.get("/wind")
def get_wind():
    grid = generate_grid()

    df = run(grid)

    return {
        "points": df.to_dict(orient="records")
    }