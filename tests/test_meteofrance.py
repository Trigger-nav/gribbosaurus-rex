"""Météo-France AROME/ARPEGE fetcher + multi-step extractor tests. Offline.

The pure-logic tests (URLs, auth, ranges, domain, registry) run anywhere.
The multi-step extractor test needs xarray and is skipped if it's absent
(e.g. the network-less dev sandbox) — it runs in the real venv.

Run: python tests/test_meteofrance.py   (or pytest)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.fetch.meteofrance import (  # noqa: E402
    AROME_RANGES,
    AROMEOM_HOURS,
    ARPEGE_EU_RANGES,
    AromeAntillesFetcher,
    AromeFranceFetcher,
    ArpegeFetcher,
    ArpegeGlobalFetcher,
    _hours,
    _ranges,
)
from gribbosaurus_rex.fetch.registry import FETCHERS, get_fetcher  # noqa: E402

UTC = timezone.utc


def _race(bbox, models=("mf_arome",), lead=48):
    return RaceConfig(name="t", bbox=bbox, models=models, max_lead_hours=lead)


def test_range_tokens():
    # AROME: 2-digit 6-hour ranges (\d{2}H\d{2}H)
    assert _ranges((0, 6, 12, 18, 24), width=2) == [
        (6, "00H06H"), (12, "07H12H"), (18, "13H18H"), (24, "19H24H")]
    assert AROME_RANGES[0] == (6, "00H06H")
    assert AROME_RANGES[-1] == (48, "43H48H")
    # ARPEGE 0.1: 3-digit 12-hour ranges (\d{3}H\d{3}H), nine windows to 102h
    assert ARPEGE_EU_RANGES[0] == (12, "000H012H")
    assert ARPEGE_EU_RANGES[1] == (24, "013H024H")
    assert ARPEGE_EU_RANGES[-1] == (102, "097H102H")
    assert len(ARPEGE_EU_RANGES) == 9
    # AROME-OM: single 3-digit hours (\d{3}H)
    assert AROMEOM_HOURS[0] == (0, "000H")
    assert AROMEOM_HOURS[6] == (6, "006H")
    assert AROMEOM_HOURS[-1] == (48, "048H")
    assert _hours(3) == [(0, "000H"), (1, "001H"), (2, "002H"), (3, "003H")]


def test_package_url():
    f = AromeFranceFetcher()
    cyc = datetime(2026, 7, 23, 0, tzinfo=UTC)
    url = f._package_url(cyc, "00H06H")
    assert "DPPaquetAROME/v1/models/AROME/grids/0.025/packages/SP1/productARO" in url
    assert "referencetime=2026-07-23T00:00:00Z" in url
    assert "time=00H06H" in url and "format=grib2" in url

    ua = ArpegeFetcher()._package_url(cyc, "000H024H")
    assert "DPPaquetARPEGE/v1/models/ARPEGE/grids/0.1/packages/SP1/productARP" in ua
    assert "time=000H024H" in ua

    uo = AromeAntillesFetcher()._package_url(cyc, "006H")
    assert ("DPPaquetAROME-OM/v1/models/AROME-OM-ANTIL/grids/0.025"
            "/packages/SP1/productOMAN") in uo


def test_auth_headers():
    f = AromeFranceFetcher()
    for k in ("METEOFRANCE_AUTH", "METEOFRANCE_API_KEY",
              "METEOFRANCE_APPLICATION_ID"):
        os.environ.pop(k, None)
    os.environ["METEOFRANCE_API_KEY"] = "ABC123"
    try:
        assert f._auth_headers() == {"apikey": "ABC123"}
        os.environ["METEOFRANCE_AUTH"] = "bearer"
        assert f._auth_headers() == {"Authorization": "Bearer ABC123"}
        os.environ.pop("METEOFRANCE_AUTH", None)

        # APPLICATION_ID takes precedence and uses the token exchange
        os.environ["METEOFRANCE_APPLICATION_ID"] = "base64appid"
        f._bearer_from_application_id = lambda: "TOK"   # avoid network
        assert f._auth_headers() == {"Authorization": "Bearer TOK"}
    finally:
        for k in ("METEOFRANCE_AUTH", "METEOFRANCE_API_KEY",
                  "METEOFRANCE_APPLICATION_ID"):
            os.environ.pop(k, None)
    raised = False
    try:
        AromeFranceFetcher()._auth_headers()
    except RuntimeError as e:
        raised = "METEOFRANCE_APPLICATION_ID" in str(e)
    assert raised, "no credential must raise"


def test_needed_ranges_and_steps():
    f = AromeFranceFetcher()
    assert [t for _, t in f._needed_ranges(12)] == ["00H06H", "07H12H"]
    assert [t for _, t in f._needed_ranges(48)] == [t for _, t in AROME_RANGES]
    assert f._needed_ranges(1)[0][1] == "00H06H"     # always ≥1 window
    assert f.steps(240)[-1] == 48                     # capped at last range
    assert ArpegeFetcher().steps(240)[-1] == 102


def test_domain_overlap():
    f = AromeFranceFetcher()
    channel = BBox(lat_min=49.2, lat_max=51.3, lon_min=-5.2, lon_max=2.1)
    f._check_domain(_race(channel))                  # overlaps -> no raise
    carib = BBox(lat_min=15.5, lat_max=18.8, lon_min=-63.8, lon_max=-60.8)
    raised = False
    try:
        f._check_domain(_race(carib))
    except RuntimeError:
        raised = True
    assert raised, "AROME France must reject a Caribbean-only bbox"
    # global model never rejects; Antilles accepts the Caribbean
    ArpegeGlobalFetcher()._check_domain(_race(carib))
    AromeAntillesFetcher()._check_domain(_race(carib))


def test_registry():
    for n in ("mf_arome", "mf_arpege", "mf_arpege_global", "mf_arome_antilles"):
        assert n in FETCHERS
        assert get_fetcher(n).name == n


# --------------------------------------------------------------------------
# Multi-step extractor assembly — needs xarray (skipped where unavailable)
# --------------------------------------------------------------------------

def test_multistep_time_assembly():
    try:
        import numpy as np
        import pandas as pd
        import xarray as xr
    except ImportError:
        print("  (skipped test_multistep_time_assembly — xarray not installed)")
        return
    from gribbosaurus_rex.extract import _to_time_indexed

    lat = np.array([50.0, 50.1]); lon = np.array([-2.0, -1.9])
    ref = np.datetime64("2026-07-23T00:00:00")

    # multi-step file: valid_time along 'step'
    steps = np.array([np.timedelta64(h, "h") for h in (0, 1, 2)])
    vt = ref + steps
    u = xr.DataArray(np.ones((3, 2, 2)), dims=("step", "latitude", "longitude"),
                     coords={"step": steps, "latitude": lat, "longitude": lon,
                             "valid_time": ("step", vt)})
    ds = xr.Dataset({"u10": u, "v10": u})
    out = _to_time_indexed(ds)
    assert "time" in out.dims and "step" not in out.dims
    assert out.sizes["time"] == 3
    assert list(pd.to_datetime(out["time"].values)) == list(pd.to_datetime(vt))

    # single-step file: scalar valid_time -> length-1 time axis
    u1 = xr.DataArray(np.ones((2, 2)), dims=("latitude", "longitude"),
                      coords={"latitude": lat, "longitude": lon,
                              "valid_time": ref})
    out1 = _to_time_indexed(xr.Dataset({"u10": u1, "v10": u1}))
    assert out1.sizes["time"] == 1
    assert pd.to_datetime(out1["time"].values[0]) == pd.Timestamp(ref)
    print("  multi-step + single-step assembly OK")


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
