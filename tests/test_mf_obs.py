"""Météo-France DPObs source tests (offline). Fixtures are VERBATIM rows
captured from the live v2 API on 2026-08-18 (mf_obs_discover.py output):
a full buoy row (BOUEE_AZUR), a wind-less inland station row (CASTELLANE
— must be skipped), and unit conversions (pmer Pa -> hPa).

Run: python tests/test_mf_obs.py   (or pytest)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import DEFAULT_TRUST, BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.obs.mf_obs import parse_row  # noqa: E402
from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402

# verbatim from live discovery 2026-08-18
BUOY_AZUR = {"lat": 43.36, "lon": 7.83, "geo_id_wmo": "6100001",
             "geo_id_wigos": "0-22000-16-6100001", "name": "BOUEE_AZUR",
             "reference_time": "2026-08-17T21:08:03Z",
             "insert_time": "2026-08-17T21:03:30Z",
             "validity_time": "2026-08-17T21:00:00Z",
             "t": 300.15, "td": 296.15, "u": 79.0, "dd": 50, "ff": 8.9,
             "pmer": 100990, "tmer": 301.95, "haut_vag": 1.2,
             "per_moy_vag": 5.0, "dir_vag": 221.0, "rafper": 11.8}

STATION_NO_WIND = {"lat": 43.849167, "lon": 6.508,
                   "geo_id_insee": "04039001",
                   "reference_time": "2026-08-18T08:10:09Z",
                   "insert_time": "2026-08-18T08:02:43Z",
                   "validity_time": "2026-08-18T08:00:00Z",
                   "t": 297.25, "td": None, "tx": 297.25, "tn": 292.95,
                   "u": None, "dd": None, "ff": None, "dxy": None,
                   "fxy": None, "ddraf": None, "raf": None, "rr1": 0.0,
                   "pres": None, "pmer": None}


def test_parse_buoy_row():
    kw = parse_row(BUOY_AZUR)
    assert kw is not None
    assert kw["station"] == "BOUEE_AZUR"
    assert kw["lat"] == 43.36 and kw["lon"] == 7.83
    assert kw["time_iso"] == "2026-08-17T21:00:00Z"
    assert kw["wind_speed_ms"] == 8.9            # already m/s
    assert kw["wind_dir_deg"] == 50.0
    assert kw["gust_ms"] == 11.8                 # rafper
    assert kw["pressure_hpa"] == 1009.9          # Pa -> hPa


def test_windless_row_skipped():
    assert parse_row(STATION_NO_WIND) is None    # ff None -> unscorable


def test_partial_rows():
    r = dict(BUOY_AZUR, dd=None)                 # speed without direction
    kw = parse_row(r)
    assert kw is not None and kw["wind_dir_deg"] is None
    r = dict(BUOY_AZUR, pmer=None)               # no pressure
    assert parse_row(r)["pressure_hpa"] is None
    r = dict(BUOY_AZUR)
    del r["name"]                                # falls back to WMO id
    assert parse_row(r)["station"] == "6100001"
    assert parse_row({"lat": 1, "lon": 2}) is None   # no time/wind at all


def test_store_roundtrip_and_bbox_logic():
    pm = RaceConfig(name="pm",
                    bbox=BBox(lat_min=37.7, lat_max=44.2,
                              lon_min=6.5, lon_max=14.5),
                    models=("ifs",))
    assert pm.bbox.padded(0.3).contains(43.36, 7.83)   # AZUR in P-M box
    assert not pm.bbox.padded(0.3).contains(45.21, -4.98)  # GASCOGNE not
    with tempfile.TemporaryDirectory() as td:
        st = ObsStore(Path(td) / "t.sqlite")
        kw = parse_row(BUOY_AZUR)
        assert st.insert_obs(source="mf_buoy", **kw) is True
        assert st.insert_obs(source="mf_buoy", **kw) is False  # dedup
        got = st.recent_obs(window_h=24 * 400, source="mf_buoy")
        assert len(got) == 1 and got[0].wind_speed_ms == 8.9


def test_trust_defaults():
    assert DEFAULT_TRUST["mf_buoy"] == 0.9
    assert DEFAULT_TRUST["mf_station"] == 0.85
    cfg = RaceConfig(name="t", bbox=BBox(0, 1, 0, 1), models=("ifs",))
    assert cfg.trust_for("mf_buoy") == 0.9


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
