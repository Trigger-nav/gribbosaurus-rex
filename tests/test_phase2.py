"""Phase 2 offline tests — obs store, NMEA parsing, Expedition import,
verification weighting and confidence maths. No network, no GRIB files.

Run: python tests/test_phase2.py   (or pytest)
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from gribbosaurus_rex.config import BBox, RaceConfig, load_config  # noqa: E402
from gribbosaurus_rex.obs.nmea import NmeaState, checksum_ok, _dm_to_deg  # noqa: E402
from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402
from gribbosaurus_rex.verify import (_half_life_weight, blend_weights,  # noqa: E402
                                     compute_scores, haversine_nm)

UTC = timezone.utc


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


# ------------------------------------------------------------------- config

def test_phase2_config_loads():
    cfg = load_config(REPO / "configs" / "balearics.yaml")
    assert cfg.obs.metar is True
    assert cfg.obs.nmea.port == 10110
    assert cfg.trust_for("yacht") == 1.0
    assert cfg.trust_for("unknown-source") == 0.5
    assert abs(cfg.anchor()[0] - 39.45) < 1e-9  # configured focus point
    assert cfg.scoring.err_scale_ms == 2.5
    assert cfg.scoring.publish_window_h == 24


# ---------------------------------------------------------------- obs store

def test_obs_store_roundtrip_and_dedup():
    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        t = _iso(datetime.now(UTC) - timedelta(minutes=10))
        assert store.insert_obs(source="metar", station="LEPA", lat=39.55,
                                lon=2.74, time_iso=t, wind_speed_ms=12,
                                wind_dir_deg=70, pressure_hpa=1013)
        # exact duplicate must be ignored
        assert not store.insert_obs(source="metar", station="LEPA", lat=39.55,
                                    lon=2.74, time_iso=t, wind_speed_ms=12,
                                    wind_dir_deg=70, pressure_hpa=1013)
        assert len(store.recent_obs(1.0)) == 1
        assert store.recent_obs(1.0, source="yacht") == []


def test_yacht_latest_freshness():
    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        stale = _iso(datetime.now(UTC) - timedelta(hours=12))
        store.insert_obs(source="yacht", station="stingray", lat=39.4, lon=2.5,
                         time_iso=stale, wind_speed_ms=10, wind_dir_deg=60)
        assert store.yacht_latest(max_age_h=6) is None
        fresh = _iso(datetime.now(UTC) - timedelta(minutes=5))
        store.insert_obs(source="yacht", station="stingray", lat=39.5, lon=2.6,
                         time_iso=fresh, wind_speed_ms=11, wind_dir_deg=65)
        y = store.yacht_latest(max_age_h=6)
        assert y is not None and abs(y.lat - 39.5) < 1e-9


# -------------------------------------------------------------------- NMEA

def test_nmea_checksum():
    good = nmea("WIMWD,70.0,T,67.9,M,12.0,N,6.2,M")
    assert checksum_ok(good)
    assert not checksum_ok(good[:-1] + "0")     # corrupted checksum
    assert not checksum_ok("WIMWD,70.0,T")      # no $, no checksum


def test_dm_to_deg():
    assert abs(_dm_to_deg("3927.0000", "N") - 39.45) < 1e-6
    assert abs(_dm_to_deg("00236.0000", "W") - (-2.6)) < 1e-6


def test_nmea_state_machine():
    s = NmeaState()
    s.handle(nmea("GPRMC,120000.00,A,3927.0000,N,00236.0000,E,7.5,60.0,130726,2.1,E,A"))
    s.handle(nmea("WIMWD,70.0,T,67.9,M,12.5,N,6.4,M"))
    s.handle(nmea("WIMDA,29.9110,I,1.0128,B,21.5,C,,,,,,,,,,,,,,"))
    snap = s.snapshot()
    assert snap is not None
    lat, lon, tws, twd, press = snap
    assert abs(lat - 39.45) < 1e-6 and abs(lon - 2.6) < 1e-6
    assert abs(tws - 12.5) < 1e-9 and abs(twd - 70.0) < 1e-9
    assert abs(press - 1012.8) < 1e-6

    # corrupted sentence must not change state
    s.handle("$WIMWD,999.0,T,67.9,M,50.0,N,25.7,M*00")
    assert abs(s.twd - 70.0) < 1e-9

    # MWV(T) + heading path
    s2 = NmeaState()
    s2.handle(nmea("GPRMC,120000.00,A,3927.0000,N,00236.0000,E,7.5,60.0,130726,2.1,E,A"))
    s2.handle(nmea("HEHDT,60.0,T"))
    s2.handle(nmea("WIMWV,30.0,T,12.0,N,A"))
    assert abs(s2.twd - 90.0) < 1e-9


# -------------------------------------------------------------- expedition

def test_expedition_import():
    import pandas as pd

    from gribbosaurus_rex.obs.expedition import import_log

    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        # Excel-serial Utc, 30s cadence -> resampled to 60s
        base = 46212.5  # some day in 2026
        rows = [{"Utc": base + i * (30 / 86400), "Lat": 39.4 + i * 1e-4,
                 "Lon": 2.5, "Tws": 11 + i * 0.1, "Twd": 65, "Baro": 1013.1}
                for i in range(10)]
        csv = Path(td) / "log.csv"
        pd.DataFrame(rows).to_csv(csv, index=False)
        n = import_log(csv, store, boat="stingray")
        assert n == 5  # every other 30s row at 60s resample

        obs = store.recent_obs(window_h=1e6, source="yacht")
        assert len(obs) == 5
        assert obs[0].station == "stingray"
        assert abs(obs[0].pressure_hpa - 1013.1) < 1e-9

        # missing required column
        bad = Path(td) / "bad.csv"
        pd.DataFrame([{"Utc": base, "Lat": 1, "Lon": 2}]).to_csv(bad, index=False)
        try:
            import_log(bad, store)
            raise AssertionError("should have raised")
        except ValueError:
            pass


# ------------------------------------------------------- scoring/confidence

def test_haversine_and_halflife():
    # Palma to Ibiza town is ~70nm
    d = haversine_nm(39.57, 2.65, 38.91, 1.43)
    assert 65 < d < 80
    assert _half_life_weight(0, 24) == 1.0
    assert abs(_half_life_weight(24, 24) - 0.5) < 1e-12
    assert _half_life_weight(5, 0) == 1.0  # disabled half-life


def _seeded_store(td: Path, err_good: float, err_bad: float) -> ObsStore:
    """Two models verified against the same obs: one accurate, one not."""
    store = ObsStore(td / "t.sqlite")
    now = datetime.now(UTC)
    for i in range(6):
        t = _iso(now - timedelta(hours=i))
        store.insert_obs(source="metar", station=f"S{i}", lat=39.5, lon=2.6,
                         time_iso=t, wind_speed_ms=12, wind_dir_deg=70,
                         pressure_hpa=1013)
        ob = store.recent_obs(cfg_window := 48)[0]
        for model, err in (("ifs", err_good), ("gfs", err_bad)):
            store.insert_verification(
                obs_id=ob.id, model=model, cycle=_iso(now - timedelta(hours=12)),
                lead_hours=12 - i, fc_wind_speed=12 + err, fc_wind_dir=70,
                fc_pressure=1013, err_vector_ms=err, err_speed_ms=err,
                err_dir_deg=0.0, err_press_hpa=0.0)
    return store


def test_confidence_orders_models_and_blend_weights():
    cfg = RaceConfig(name="t", bbox=BBox(38.0, 40.5, 0.5, 3.5),
                     models=("ifs", "gfs"))
    with tempfile.TemporaryDirectory() as td:
        store = _seeded_store(Path(td), err_good=1.0, err_bad=6.0)
        scores = compute_scores(cfg, store)
        assert set(scores) == {"ifs", "gfs"}
        assert scores["ifs"] > scores["gfs"]          # accurate model wins
        assert 0 < scores["gfs"] < scores["ifs"] <= 1

        # persisted + turned into normalized blend weights
        w = blend_weights(cfg, store)
        assert abs(sum(w.values()) - 1.0) < 1e-9
        assert w["ifs"] > w["gfs"]

        # score history exists for the dashboard
        assert len(store.score_history()) == 2


def test_confidence_distance_weighting():
    """An obs far from the anchor influences the score less."""
    cfg = RaceConfig(name="t", bbox=BBox(35.0, 45.0, -5.0, 9.0),
                     models=("ifs",))
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        # near obs, small error; far obs (~300nm away), huge error
        for station, lat, lon, err in (("NEAR", 40.0, 2.0, 1.0),
                                       ("FAR", 44.5, 8.0, 10.0)):
            t = _iso(now - timedelta(minutes=30))
            store.insert_obs(source="metar", station=station, lat=lat, lon=lon,
                             time_iso=t, wind_speed_ms=10, wind_dir_deg=90)
            ob = [o for o in store.recent_obs(48) if o.station == station][0]
            store.insert_verification(
                obs_id=ob.id, model="ifs", cycle=_iso(now - timedelta(hours=6)),
                lead_hours=5.5, fc_wind_speed=10 + err, fc_wind_dir=90,
                fc_pressure=None, err_vector_ms=err, err_speed_ms=err,
                err_dir_deg=0.0, err_press_hpa=None)
        cfg_focus = RaceConfig(
            name="t", bbox=cfg.bbox, models=("ifs",),
            obs=__import__("gribbosaurus_rex.config", fromlist=["ObsConfig"])
            .ObsConfig(focus_lat=40.0, focus_lon=2.0))
        scores = compute_scores(cfg_focus, store)
        # weighted rmse should sit near the NEAR error (1 m/s), not the mean
        assert scores["ifs"] > np.exp(-2.0 / 2.5)


def test_test_source_never_scored():
    """source='test' obs are stored but excluded from confidence."""
    cfg = RaceConfig(name="t", bbox=BBox(38.0, 40.5, 0.5, 3.5), models=("ifs",))
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        t = _iso(now - timedelta(minutes=10))
        store.insert_obs(source="test", station="smoke-boat", lat=39.5,
                         lon=2.6, time_iso=t, wind_speed_ms=50, wind_dir_deg=0)
        ob = store.recent_obs(1.0, source="test")[0]
        # even if a verification row sneaks in, scoring ignores it
        store.insert_verification(
            obs_id=ob.id, model="ifs", cycle=_iso(now - timedelta(hours=3)),
            lead_hours=3, fc_wind_speed=5, fc_wind_dir=0, fc_pressure=None,
            err_vector_ms=45.0, err_speed_ms=-45.0, err_dir_deg=0.0,
            err_press_hpa=None)
        assert compute_scores(cfg, store) == {}
        # and purge_station removes both rows
        assert store.purge_station("smoke-boat") == 1
        assert store.verifications_window(48) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
