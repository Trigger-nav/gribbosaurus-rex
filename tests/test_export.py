"""GRIB export + refetch tests — the pure axis/slice maths offline; the
eccodes re-encode path is covered by scripts/live_smoke_export.py.

The lon-axis cases mirror the real encodings found live 2026-07-13:
ECMWF global encodes -180 as first=180; ICON-EU encodes -23.5 as 336.5.
Interpolating first..last produced garbage axes — these tests pin the
modular construction.

Run: python tests/test_export.py   (or pytest)
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from gribbosaurus_rex.export import (_axis, _index_range, _lon_axis,  # noqa: E402
                                     _norm_lon, _unwrap_roll)


def test_lon_axis_ecmwf_global_first_180():
    # ECMWF 0.25 global: first=180.0 means the axis starts at -180
    lons = _lon_axis(180.0, 0.25, 1440, scans_negatively=False)
    assert abs(lons[0] - (-180.0)) < 1e-9
    assert abs(lons[-1] - 179.75) < 1e-9
    assert np.all(np.diff(lons) > 0)          # fully ascending, no roll needed
    assert _unwrap_roll(lons) == 0

    r = _index_range(lons, -10.8, -1.0)        # Fastnet box
    assert r is not None
    i0, i1 = r
    assert lons[i0] >= -10.8 and lons[i1 - 1] <= -1.0


def test_lon_axis_icon_eu_first_336_5():
    # ICON-EU: first=336.5 means -23.5, crossing Greenwich to +62.5
    ni = int(round((62.5 - (-23.5)) / 0.0625)) + 1
    lons = _lon_axis(336.5, 0.0625, ni, scans_negatively=False)
    assert abs(lons[0] - (-23.5)) < 1e-6
    assert abs(lons[-1] - 62.5) < 1e-6
    assert np.all(np.diff(lons) > 0)
    r = _index_range(lons, -2.1, -0.6)         # Solent box
    assert r is not None


def test_lon_axis_wrap_at_dateline_needs_roll():
    # a hypothetical Pacific grid crossing ±180 still unwraps correctly
    lons = _lon_axis(170.0, 0.25, 161, scans_negatively=False)  # 170..-149.75
    assert not np.all(np.diff(lons) > 0)       # jump at the dateline
    shift = _unwrap_roll(lons)
    assert shift not in (None, 0)
    rolled = np.roll(lons, shift)
    assert np.all(np.diff(rolled) > 0)


def test_index_range_ascending_and_descending():
    lons = np.arange(-12.0, 4.01, 0.25)
    r = _index_range(lons, -10.8, -1.0)
    assert r is not None
    i0, i1 = r
    assert lons[i0:i1].min() >= -10.8 and lons[i0:i1].max() <= -1.0
    assert _index_range(lons, 100.0, 120.0) is None

    lats = np.linspace(70.5, 29.5, 657)        # GRIB north->south scan
    r = _index_range(lats, 48.8, 52.2)
    assert r is not None
    j0, j1 = r
    sub = lats[j0:j1]
    assert sub.min() >= 48.8 and sub.max() <= 52.2
    assert sub[0] > sub[-1]                    # original order preserved


def test_axis_and_norm_lon():
    ax = _axis(90.0, -90.0, 721)
    assert abs(ax[1] - 89.75) < 1e-9
    assert abs(float(_norm_lon(350.0)) - (-10.0)) < 1e-9


def test_region_subset_refetch_when_fleet_grows():
    """A GFS run fetched for the Balearics box must be refetched once the
    fleet domain includes the UK — the exact live failure of 2026-07-13."""
    from gribbosaurus_rex.store.runs import RunStore

    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "t.sqlite")
        cyc = "2026-07-14T00:00:00+00:00"
        store.start("gfs", cyc, str(Path(td) / "g"), bbox="38.0,40.5,0.5,3.5")
        store.complete("gfs", cyc, 3, 140_000)
        rec = store.get("gfs", cyc)

        assert rec.bbox_covers(38.0, 40.5, 0.5, 3.5)          # Med ok
        assert not rec.bbox_covers(38.0, 52.2, -10.8, 3.5)    # fleet: refetch
        # pre-tracking rows (bbox NULL) always refetch for subset models
        store.start("gfs", "2026-07-13T18:00:00+00:00", "p")  # no bbox
        store.complete("gfs", "2026-07-13T18:00:00+00:00", 3, 1)
        assert not store.get("gfs", "2026-07-13T18:00:00+00:00") \
            .bbox_covers(38.0, 40.5, 0.5, 3.5)

        # non-subset models (ECMWF/ICON files are domain-independent)
        # never trigger refetch — flag lives on the fetcher
        from gribbosaurus_rex.fetch.registry import get_fetcher

        assert get_fetcher("gfs").region_subset is True
        assert get_fetcher("ifs").region_subset is False
        assert get_fetcher("icon_eu").region_subset is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
