"""Offline unit tests — no network, no GRIB libraries needed.

Covers: config loading/validation, run store lifecycle, cycle
candidate arithmetic, fetcher URL construction, wind maths, scoring
metrics.

Run: python -m pytest tests/ -q   (or plain `python tests/test_offline.py`)
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from gribbosaurus_rex.config import BBox, load_config  # noqa: E402
from gribbosaurus_rex.core.scoring import (  # noqa: E402
    direction_error, distance_weight, wind_vector_error)
from gribbosaurus_rex.core.wind import to_speed_dir, to_uv  # noqa: E402
from gribbosaurus_rex.fetch.base import cycle_db, cycle_iso  # noqa: E402
from gribbosaurus_rex.fetch.ecmwf_open import (  # noqa: E402
    AifsFetcher, EcmwfOpenFetcher)
from gribbosaurus_rex.fetch.gfs import GfsFetcher  # noqa: E402
from gribbosaurus_rex.fetch.icon import IconEuFetcher  # noqa: E402
from gribbosaurus_rex.store.runs import RunStore  # noqa: E402

UTC = timezone.utc


def test_config_loads():
    cfg = load_config(REPO / "configs" / "balearics.yaml")
    assert cfg.name == "balearics-summer"
    assert cfg.bbox.contains(39.5, 2.6)
    assert "ifs" in cfg.models
    assert cfg.db_path.name == "gribbo.sqlite"


def test_bbox_validation():
    try:
        BBox(50, 40, 0, 1)  # inverted lats
        raise AssertionError("should have raised")
    except ValueError:
        pass
    assert BBox(38, 40, 1, 3).padded(1.0).lat_min == 37


def test_run_store_lifecycle():
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "t.sqlite")
        cyc = "2026-07-13T00:00:00+00:00"

        store.start("ifs", cyc, str(Path(td) / "grib"))
        assert store.get("ifs", cyc).status == "fetching"
        assert not store.has_complete("ifs", cyc)

        store.complete("ifs", cyc, n_files=33, nbytes=5_000_000)
        assert store.has_complete("ifs", cyc)
        assert store.latest_complete("ifs").n_files == 33

        # newer failed run must not become "latest complete"
        cyc2 = "2026-07-13T06:00:00+00:00"
        store.start("ifs", cyc2, str(Path(td) / "grib2"))
        store.fail("ifs", cyc2, "boom")
        assert store.latest_complete("ifs").cycle == cyc

        # re-attempt resets the failed row
        store.start("ifs", cyc2, str(Path(td) / "grib2"))
        store.complete("ifs", cyc2, 33, 1)
        assert store.latest_complete("ifs").cycle == cyc2

        # prune keeps newest only
        pruned = store.prune("ifs", keep=1)
        assert pruned == [cyc]
        assert store.get("ifs", cyc) is None


def test_candidate_cycles_newest_first_and_lagged():
    f = GfsFetcher.__new__(GfsFetcher)  # skip __init__ (no HTTP session)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    cands = f.candidate_cycles(now)
    assert cands == sorted(cands, reverse=True)
    # GFS lag is 3.5h -> at 12:00 the 12z run can't be a candidate, 06z is
    assert cands[0] == datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    assert all((now - c).total_seconds() <= 30 * 3600 for c in cands)


def test_ifs_stream_and_steps():
    f = EcmwfOpenFetcher.__new__(EcmwfOpenFetcher)
    c00 = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    c06 = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    assert f.stream(c00) == "oper" and f.stream(c06) == "scda"
    # 06z capped at 90h even if config asks for more
    assert max(f._steps_for(c06, 240)) == 90
    assert max(f._steps_for(c00, 240)) == 240
    steps96 = f._steps_for(c00, 96)
    assert steps96[0] == 0 and steps96[-1] == 96 and 3 in steps96

    url = f._base_url(c00, 24)
    assert url == ("https://data.ecmwf.int/forecasts/20260713/00z/ifs/0p25/"
                   "oper/20260713000000-24h-oper-fc.grib2")
    # the index sidecar REPLACES .grib2 (verified live 2026-07-13):
    # …-fc.grib2.index does NOT exist, …-fc.index does
    assert f._index_url(c00, 24).endswith("20260713000000-24h-oper-fc.index")
    assert ".grib2.index" not in f._index_url(c00, 24)


def test_aifs_urls_and_steps():
    f = AifsFetcher.__new__(AifsFetcher)
    c06 = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    assert f.stream(c06) == "oper"  # AIFS is always oper
    assert f.steps(96) == list(range(0, 97, 6))
    assert "/aifs-single/0p25/oper/20260713060000-48h-oper-fc.grib2" \
        in f._base_url(c06, 48)


def test_gfs_filter_params():
    from gribbosaurus_rex.config import RaceConfig

    f = GfsFetcher.__new__(GfsFetcher)
    cfg = RaceConfig(name="t", bbox=BBox(38.0, 40.5, 0.5, 3.5))
    c = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    p = f._params(c, 7, cfg)
    assert p["file"] == "gfs.t06z.pgrb2.0p25.f007"
    assert p["dir"] == "/gfs.20260713/06/atmos"
    assert p["var_UGRD"] == "on" and p["leftlon"] == 0.5 and p["toplat"] == 40.5
    # hourly to 120 then 3-hourly
    s = f.steps(130)
    assert 119 in s and 120 in s and 121 not in s and 123 in s


def test_icon_urls_and_domain_check():
    from gribbosaurus_rex.config import RaceConfig

    f = IconEuFetcher.__new__(IconEuFetcher)
    c = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    url = f._url(c, 5, "u_10m", "U_10M")
    assert url == ("https://opendata.dwd.de/weather/nwp/icon-eu/grib/00/u_10m/"
                   "icon-eu_europe_regular-lat-lon_single-level_"
                   "2026071300_005_U_10M.grib2.bz2")
    f._check_domain(RaceConfig(name="t", bbox=BBox(38.0, 40.5, 0.5, 3.5)))
    try:  # Caribbean is outside ICON-EU
        f._check_domain(RaceConfig(name="t", bbox=BBox(12.0, 18.0, -65.0, -59.0)))
        raise AssertionError("should have raised")
    except RuntimeError:
        pass


def test_cycle_ids():
    c = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    assert cycle_iso(c) == "20260713T06Z"
    assert cycle_db(c) == "2026-07-13T06:00:00+00:00"


def test_wind_roundtrip_and_convention():
    # northerly (FROM 0°) blows toward south: u=0, v negative
    u, v = to_uv(10.0, 0.0)
    assert abs(u) < 1e-9 and v < 0
    for ws, wd in [(5, 0), (12.3, 47), (20, 180), (7, 359)]:
        s, d = to_speed_dir(*to_uv(ws, wd))
        assert abs(s - ws) < 1e-9
        assert min(abs(d - wd), 360 - abs(d - wd)) < 1e-6


def test_scoring_metrics():
    # same speed, 90° apart -> vector error = speed * sqrt(2)
    err = wind_vector_error(10, 0, 10, 90)
    assert abs(err - 10 * np.sqrt(2)) < 1e-9
    assert direction_error(350, 10) == 20
    assert abs(distance_weight(0) - 1.0) < 1e-12
    assert abs(distance_weight(30) - 0.5) < 1e-12
    assert distance_weight(90) < 0.13


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
