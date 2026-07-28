"""Tests for the run-major vectorized verification path (offline, no
xarray/cfgrib needed): _naive_utc, _build_rows row assembly and NaN
handling, and the ObsStore batch methods (verified_obs_ids /
insert_verifications) against a real temp sqlite db.

Run: python tests/test_verify_batch.py   (or pytest)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.obs.store import Obs, ObsStore  # noqa: E402
from gribbosaurus_rex.verify import _build_rows, _naive_utc  # noqa: E402


def _ob(i, speed=8.0, direction=270.0, press=1013.0):
    return Obs(id=i, source="windycator", station=f"s{i}", lat=50.5, lon=-1.3,
               time="2026-07-28T06:00:00+00:00", wind_speed_ms=speed,
               wind_dir_deg=direction, gust_ms=None, pressure_hpa=press,
               created_at="2026-07-28T06:00:05+00:00")


def test_naive_utc():
    a = _naive_utc("2026-07-28T06:00:00+00:00")
    b = _naive_utc("2026-07-28T06:00:00")          # naive == same instant
    c = _naive_utc("2026-07-28T08:00:00+02:00")    # CEST == same instant
    assert a == b == c
    lead = (a - _naive_utc("2026-07-28T00:00:00+00:00")) / np.timedelta64(1, "h")
    assert float(lead) == 6.0


def test_build_rows_basic():
    sub = [_ob(1), _ob(2)]
    rows = _build_rows(sub, "ukmo_ukv", "2026-07-28T00:00:00+00:00",
                       lead_h=np.array([6.0, 6.0]),
                       fc_speed=np.array([7.5, 8.5]),
                       fc_dir=np.array([265.0, 280.0]),
                       fc_press=np.array([1012.0, np.nan]))
    assert len(rows) == 2
    obs_id, model, cycle, lead, fs, fd, fp, ev, es, ed, ep = rows[0]
    assert (obs_id, model) == (1, "ukmo_ukv")
    assert lead == 6.0 and fs == 7.5 and fd == 265.0 and fp == 1012.0
    assert ev is not None and ev > 0            # vector error computed
    assert abs(es - (7.5 - 8.0)) < 1e-9         # signed speed error
    assert ed == 5.0                            # circular dir error
    assert abs(ep - (1012.0 - 1013.0)) < 1e-9
    assert rows[1][6] is None and rows[1][10] is None   # NaN press -> None


def test_build_rows_nan_forecast_skipped():
    sub = [_ob(1), _ob(2)]
    rows = _build_rows(sub, "m", "2026-07-28T00:00:00+00:00",
                       lead_h=np.array([1.0, 2.0]),
                       fc_speed=np.array([np.nan, 5.0]),   # ob 1: off-grid
                       fc_dir=np.array([np.nan, 180.0]),
                       fc_press=np.array([np.nan, np.nan]))
    assert len(rows) == 1 and rows[0][0] == 2


def test_build_rows_obs_without_direction():
    sub = [_ob(1, direction=None)]
    rows = _build_rows(sub, "m", "2026-07-28T00:00:00+00:00",
                       lead_h=np.array([1.0]), fc_speed=np.array([5.0]),
                       fc_dir=np.array([200.0]), fc_press=np.array([np.nan]))
    assert len(rows) == 1
    _, _, _, _, _, _, _, ev, es, ed, ep = rows[0]
    assert ev is None and ed is None            # no obs dir -> no vector err
    assert es is not None                       # speed error still recorded


def test_store_batch_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        st = ObsStore(Path(td) / "t.sqlite")
        for i in range(3):
            st.insert_obs(source="windycator", station=f"s{i}", lat=50.0,
                          lon=-1.0, time_iso=f"2026-07-28T0{i}:00:00+00:00",
                          wind_speed_ms=8.0, wind_dir_deg=270.0)
        cyc = "2026-07-28T00:00:00+00:00"
        rows = [(1, "ukmo_ukv", cyc, 1.0, 7.5, 265.0, None, 0.9, -0.5, 5.0, None),
                (2, "ukmo_ukv", cyc, 2.0, 7.0, 260.0, None, 1.2, -1.0, 10.0, None)]
        assert st.insert_verifications(rows) == 2
        assert st.insert_verifications(rows) == 0        # OR IGNORE dedups
        assert st.insert_verifications([]) == 0
        assert st.verified_obs_ids("ukmo_ukv", cyc) == {1, 2}
        assert st.verified_obs_ids("ukmo_ukv", "2026-07-28T06:00:00+00:00") == set()
        assert st.verified_obs_ids("mf_arome", cyc) == set()
        # batch rows must be readable through the existing window query
        got = st.verifications_window(window_h=48.0)
        assert len(got) == 2
        assert {r["obs_id"] for r in got} == {1, 2}
        assert got[0]["err_vector_ms"] is not None
        # the new index exists
        idx = {r[1] for r in
               st._conn.execute("PRAGMA index_list('verification')")}
        assert "idx_verif_model_cycle" in idx


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
