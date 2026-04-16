import openmeteo_requests
import requests_cache
from retry_requests import retry
import pandas as pd

cache = requests_cache.CachedSession(".cache", expire_after=3600)
client = openmeteo_requests.Client(session=retry(cache, retries=5))

def fetch_ecmwf(lat, lon, model="ecmwf_ifs"):
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ["wind_speed_10m", "wind_direction_10m", "pressure_msl"],
        "models": model,
        "wind_speed_unit": "kn",
        "timezone": "UTC"
    }

    r = client.weather_api(url, params=params)[0]
    h = r.Hourly()

    return pd.DataFrame({
        "wind_speed": h.Variables(0).ValuesAsNumpy(),
        "wind_dir": h.Variables(1).ValuesAsNumpy(),
        "pressure": h.Variables(2).ValuesAsNumpy()
    })