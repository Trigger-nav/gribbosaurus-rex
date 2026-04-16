import requests

def fetch_ukv(api_key, bbox):
    url = "https://api-metoffice.apiconnect.ibmcloud.com/metoffice/production/v0/..."

    headers = {"X-IBM-Client-Id": api_key}

    params = {
        "bbox": bbox,
        "parameters": "wind_speed,wind_direction,msl_pressure",
        "format": "netcdf"
    }

    r = requests.get(url, headers=headers, params=params)
    return r.content