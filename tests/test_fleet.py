"""Fleet (multi-race) tests — config loading, union bbox, per-race scores,
multi-region scores.json. Offline.

Run: python tests/test_fleet.py   (or pytest)
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import (BBox, RaceConfig, fetch_config,  # noqa: E402
                                     load_fleet, union_bbox)
from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402
from gribbosaurus_rex.publish import build_scores  # noqa: E402
from gribbosaurus_rex.verify import compute_scores  # noqa: E402

UTC = timezone.utc


def _iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def test_repo_fleet_loads():
    fleet = load_fleet(REPO / "configs")
    names = {r.name for r in fleet}
    assert {"balearics-summer", "solent", "fastnet-2027"} <= names
    solent = next(r for r in fleet if r.name == "solent")
    assert solent.bbox.contains(50.76, -1.30)          # Cowes
    fastnet = next(r for r in fleet if r.name == "fastnet-2027")
    assert fastnet.bbox.contains(51.39, -9.60)          # Fastnet Rock
    assert fastnet.bbox.contains(49.65, -1.62)          # Cherbourg
    assert fastnet.max_lead_hours == 144
    # all races share one data_dir (enforced)
    assert len({r.data_dir for r in fleet}) == 1


def test_union_bbox_and_fetch_config():
    fleet = load_fleet(REPO / "configs")
    u = union_bbox(fleet)
    # spans Med to Celtic Sea
    assert u.lat_min <= 38.0 and u.lat_max >= 52.2
    assert u.lon_min <= -10.8 and u.lon_max >= 3.5

    fc = fetch_config(fleet)
    assert fc.name == "fleet"
    assert fc.max_lead_hours == max(r.max_lead_hours for r in fleet)
    assert set(fc.models) >= {"ifs", "gfs"}
    # every race area fits inside the fetch domain
    for r in fleet:
        assert fc.bbox.contains(r.bbox.lat_min, r.bbox.lon_min)
        assert fc.bbox.contains(r.bbox.lat_max, r.bbox.lon_max)


def test_fleet_rejects_duplicate_names_and_split_dirs():
    import yaml

    with tempfile.TemporaryDirectory() as td:
        base = {"name": "same", "bbox": {"lat_min": 1, "lat_max": 2,
                                         "lon_min": 3, "lon_max": 4}}
        for i in range(2):
            (Path(td) / f"r{i}.yaml").write_text(yaml.safe_dump(base))
        try:
            load_fleet(td)
            raise AssertionError("should have raised on duplicate names")
        except ValueError as e:
            assert "Duplicate" in str(e)


def _seed_region(store, now, *, station, lat, lon, model="ifs", err=1.0):
    t = _iso(now - timedelta(hours=1))
    store.insert_obs(source="metar", station=station, lat=lat, lon=lon,
                     time_iso=t, wind_speed_ms=6, wind_dir_deg=200)
    ob = [o for o in store.recent_obs(48) if o.station == station][0]
    store.insert_verification(
        obs_id=ob.id, model=model, cycle=_iso(now - timedelta(hours=7)),
        lead_hours=6, fc_wind_speed=6 + err, fc_wind_dir=200,
        fc_pressure=None, err_vector_ms=err, err_speed_ms=err,
        err_dir_deg=5.0, err_press_hpa=None)


def test_per_race_scores_and_multi_region_publish():
    now = datetime.now(UTC)
    med = RaceConfig(name="med", bbox=BBox(38.0, 40.5, 0.5, 3.5),
                     models=("ifs",))
    uk = RaceConfig(name="uk", bbox=BBox(48.8, 52.2, -10.8, -1.0),
                    models=("ifs",))
    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        # model is sharp in the Med, poor in the Channel
        _seed_region(store, now, station="LEPA", lat=39.5, lon=2.7, err=0.5)
        _seed_region(store, now, station="EGHI", lat=50.9, lon=-1.4, err=4.0)

        s_med = compute_scores(med, store)
        s_uk = compute_scores(uk, store)
        assert s_med["ifs"] > s_uk["ifs"]  # regions scored independently

        # per-race score history is separable
        assert store.latest_scores(race="med")["ifs"] == round(s_med["ifs"], 4)
        assert store.latest_scores(race="uk")["ifs"] == round(s_uk["ifs"], 4)
        assert store.latest_scores(race="") == {}

        # one scores.json, two regions, no cross-contamination
        doc = build_scores([med, uk], store, now=now)
        assert len(doc["scores"]) == 2
        by_region = {e["region"]["lat_min"]: e for e in doc["scores"]}
        assert by_region[38.0]["score"] > by_region[48.8]["score"]
        assert by_region[38.0]["obs_count"] == 1
        assert all(e["model"] == "ecmwf_ifs" for e in doc["scores"])


def test_regions_only_see_their_own_obs():
    """An obs outside a race's bbox contributes nothing to its entries."""
    now = datetime.now(UTC)
    uk = RaceConfig(name="uk", bbox=BBox(48.8, 52.2, -10.8, -1.0),
                    models=("ifs",))
    with tempfile.TemporaryDirectory() as td:
        store = ObsStore(Path(td) / "t.sqlite")
        _seed_region(store, now, station="LEPA", lat=39.5, lon=2.7, err=0.5)
        doc = build_scores([uk], store, now=now)
        assert doc["scores"] == []  # Med obs never leaks into the UK region


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
