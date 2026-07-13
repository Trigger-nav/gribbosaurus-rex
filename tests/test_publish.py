"""scores.json publisher tests — contract schema 1.0 compliance, offline.

Run: python tests/test_publish.py   (or pytest)
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402
from gribbosaurus_rex.publish import (LEAD_BUCKETS, PUBLISH_NAMES,  # noqa: E402
                                      build_scores, etag_for, publish,
                                      scores_path)

UTC = timezone.utc


def _iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _seed(store: ObsStore, now, *, model="ifs", source="metar",
          station="LEPA", lead_h=6.0, err=1.0, age_h=1.0, dir_err=10.0):
    t = _iso(now - timedelta(hours=age_h))
    store.insert_obs(source=source, station=station, lat=39.5, lon=2.6,
                     time_iso=t, wind_speed_ms=6, wind_dir_deg=70)
    ob = [o for o in store.recent_obs(1e6) if o.station == station][0]
    store.insert_verification(
        obs_id=ob.id, model=model, cycle=_iso(now - timedelta(hours=age_h + lead_h)),
        lead_hours=lead_h, fc_wind_speed=6 + err, fc_wind_dir=70 + dir_err,
        fc_pressure=None, err_vector_ms=err, err_speed_ms=err,
        err_dir_deg=dir_err, err_press_hpa=None)
    return ob


def _cfg(td, models=("ifs", "gfs")):
    return RaceConfig(name="t", bbox=BBox(38.0, 40.5, 0.5, 3.5),
                      models=models, data_dir=Path(td))


def test_schema_and_model_name_mapping():
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(td)
        store = ObsStore(cfg.db_path)
        _seed(store, now, model="ifs", station="A", lead_h=3, err=1.0)
        _seed(store, now, model="gfs", station="B", lead_h=3, err=2.0)

        doc = build_scores(cfg, store, now=now)
        assert doc["schema_version"] == "1.0"
        assert doc["generated_at"].endswith("Z")
        assert len(doc["scores"]) == 2

        by_model = {e["model"]: e for e in doc["scores"]}
        # contract requirement 1: Stingray source names, never internal keys
        assert set(by_model) == {"ecmwf_ifs", "nomads_gfs_ww3"}
        e = by_model["ecmwf_ifs"]
        assert e["lead_h_bucket"] == [0, 12]
        assert e["region"]["lat_min"] == 38.0 and e["region"]["lon_max"] == 3.5
        assert 0 <= e["score"] <= 1
        assert e["obs_count"] == 1
        assert e["obs_window_h"] == 24
        assert "wind_rmse_ms" in e["metrics"]
        assert "hs_rmse_m" not in e["metrics"]  # no invented wave numbers
        # better model scores higher
        assert by_model["ecmwf_ifs"]["score"] > by_model["nomads_gfs_ww3"]["score"]


def test_lead_bucket_resolution():
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(td, models=("ifs",))
        store = ObsStore(cfg.db_path)
        # same model: sharp at 6h lead, poor at 30h lead
        _seed(store, now, station="A", lead_h=6, err=0.5)
        _seed(store, now, station="B", lead_h=30, err=4.0)

        doc = build_scores(cfg, store, now=now)
        buckets = {tuple(e["lead_h_bucket"]): e for e in doc["scores"]}
        assert set(buckets) == {(0, 12), (24, 48)}  # empty 12-24 absent
        assert buckets[(0, 12)]["score"] > buckets[(24, 48)]["score"]
        assert list(LEAD_BUCKETS) == [(0, 12), (12, 24), (24, 48)]


def test_excluded_sources_and_honest_low_obs():
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(td, models=("ifs",))
        store = ObsStore(cfg.db_path)
        # only excluded sources -> no entries at all (absence, not zeros)
        _seed(store, now, station="T1", source="test", err=0.1)
        _seed(store, now, station="O1", source="openmeteo", err=0.1)
        assert build_scores(cfg, store, now=now)["scores"] == []

        # one real obs -> published with honest obs_count=1, not suppressed
        _seed(store, now, station="R1", source="metar", err=1.0)
        doc = build_scores(cfg, store, now=now)
        assert len(doc["scores"]) == 1
        assert doc["scores"][0]["obs_count"] == 1


def test_atomic_write_and_etag():
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as td:
        cfg = _cfg(td, models=("ifs",))
        store = ObsStore(cfg.db_path)
        _seed(store, now, station="A", err=1.0)

        path = publish(cfg, store)
        assert path == scores_path(cfg) and path.exists()
        doc = json.loads(path.read_text())
        assert doc["schema_version"] == "1.0"
        assert not path.with_suffix(".json.part").exists()

        # ETag: content-derived, quoted, stable
        e1 = etag_for(path.read_bytes())
        e2 = etag_for(path.read_bytes())
        assert e1 == e2 and e1.startswith('"') and e1.endswith('"')
        assert etag_for(b"other") != e1


def test_publish_names_cover_all_registry_models():
    from gribbosaurus_rex.fetch.registry import FETCHERS

    for key in FETCHERS:
        assert key in PUBLISH_NAMES, f"no Stingray publish name for '{key}'"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
