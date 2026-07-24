"""Met Office DataHub UKV fetcher tests (pure logic; no network). Offline.

Exercises the order-API parsing against the real /latest shape captured on
2026-07-24: newest-run selection, wind-file picking, fileId URL-encoding,
auth header, domain guard, registry.

Run: python tests/test_ukmo.py   (or pytest)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.fetch.registry import FETCHERS, get_fetcher  # noqa: E402
from gribbosaurus_rex.fetch.ukmo import BASE, UkvFetcher  # noqa: E402

# minimal slice of a real /latest response (two runs, wind + msl each)
LATEST = {"orderDetails": {"order": {"orderId": "ukv-channel"}, "files": [
    {"fileId": "agl_u-component-of-wind+v-component-of-wind_10_+00",
     "surfaceId": "agl", "runDateTime": "2026-07-24T00:00:00Z", "run": "0"},
    {"fileId": "meansea_pressure-reduced-to-msl_+00",
     "surfaceId": "meansea", "runDateTime": "2026-07-24T00:00:00Z", "run": "0"},
    {"fileId": "agl_u-component-of-wind+v-component-of-wind_10_+06",
     "surfaceId": "agl", "runDateTime": "2026-07-24T06:00:00Z", "run": "6"},
    {"fileId": "meansea_pressure-reduced-to-msl_+06",
     "surfaceId": "meansea", "runDateTime": "2026-07-24T06:00:00Z", "run": "6"},
]}}


def test_files_and_newest_run():
    files = UkvFetcher._files(LATEST)
    assert len(files) == 4
    assert UkvFetcher._newest_run(files) == "2026-07-24T06:00:00Z"
    assert UkvFetcher._newest_run([]) is None


def test_pick_wind():
    files = UkvFetcher._files(LATEST)
    w = UkvFetcher._pick_wind(files, "2026-07-24T06:00:00Z")
    assert w is not None and w["surfaceId"] == "agl"
    assert w["fileId"].endswith("_+06")          # not the meansea file
    # a run with no wind file -> None
    assert UkvFetcher._pick_wind(files, "2026-07-24T12:00:00Z") is None


def test_data_url_encoding():
    fid = "agl_u-component-of-wind+v-component-of-wind_10_+06"
    url = UkvFetcher._data_url(BASE, "ukv-channel", fid)
    assert url.startswith(BASE + "/orders/ukv-channel/latest/")
    assert url.endswith("/data")
    assert "%2B" in url          # the '+' chars must be percent-encoded
    assert "+" not in url.split("/latest/")[1]  # none left raw in the path


def test_auth_headers():
    f = UkvFetcher()
    os.environ.pop("DATAHUB_API_KEY", None)
    raised = False
    try:
        f._auth_headers()
    except RuntimeError as e:
        raised = "DATAHUB_API_KEY" in str(e)
    assert raised
    os.environ["DATAHUB_API_KEY"] = "KEY123"
    try:
        assert f._auth_headers() == {"apikey": "KEY123"}
    finally:
        os.environ.pop("DATAHUB_API_KEY", None)


def test_domain_and_steps():
    f = UkvFetcher()
    ec = BBox(lat_min=49.2, lat_max=51.3, lon_min=-5.2, lon_max=2.1)
    f._check_domain(RaceConfig(name="ec", bbox=ec, models=("ukmo_ukv",)))
    carib = BBox(lat_min=15.5, lat_max=18.8, lon_min=-63.8, lon_max=-60.8)
    raised = False
    try:
        f._check_domain(RaceConfig(name="cb", bbox=carib, models=("ukmo_ukv",)))
    except RuntimeError:
        raised = True
    assert raised
    assert f.steps(240)[-1] == 48        # order caps at 48h


def test_registry():
    assert "ukmo_ukv" in FETCHERS
    assert get_fetcher("ukmo_ukv").name == "ukmo_ukv"
    assert get_fetcher("ukmo_ukv").crop_on_fetch is True


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
