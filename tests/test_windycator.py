"""Windycator source tests — parsing against a REAL captured payload
(tests/fixtures/windycator_sample.json, captured live 2026-07-14 from
weather.ianmillard.com). Offline: no network.

Run: python tests/test_windycator.py   (or pytest)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.obs.windycator import KN_TO_MS, _num, parse_station  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "windycator_sample.json").read_text())
# "now" chosen just after the capture time so freshness logic is realistic
NOW = datetime(2026, 7, 14, 8, 15, tzinfo=timezone.utc)  # 09:15 BST


def _by_id(station_id):
    rec = next(r for r in FIXTURE["data"] if r.get("id") == station_id)
    return parse_station(rec, NOW)


def test_num_defensive_parse():
    assert _num(13.4) == 13.4
    assert _num("15.2") == 15.2
    assert _num(None) is None
    assert _num("") is None
    assert _num(-1) is None          # sentinel
    assert _num("garbage") is None


def test_bramble_portlog_parses_to_si_utc():
    o = _by_id("bramble_portlog")
    assert o is not None
    assert o["source"] == "windycator" and o["station"] == "bramble_portlog"
    assert abs(o["lat"] - 50.790167) < 1e-9
    # 09:05 +01:00 -> 08:05 UTC
    assert o["time_iso"] == "2026-07-14T08:05:00+00:00"
    assert abs(o["wind_speed_ms"] - 13.4 * KN_TO_MS) < 1e-9
    assert o["wind_dir_deg"] == 37.0
    assert abs(o["gust_ms"] - 14.6 * KN_TO_MS) < 1e-9
    assert o["pressure_hpa"] is None


def test_string_numerics_cco_pier():
    o = _by_id("sandown_pier_cco")
    assert o is not None                       # lat/lng/values all strings
    assert abs(o["lat"] - 50.65111) < 1e-6
    assert abs(o["wind_speed_ms"] - 15.2 * KN_TO_MS) < 1e-9
    assert o["wind_dir_deg"] == 40.0


def test_barometer_captured_when_present():
    o = _by_id("spinnaker_sc")
    assert o is not None
    assert abs(o["pressure_hpa"] - 1018.96) < 1e-9


def test_sentinels_and_stale_stations_skipped():
    assert _by_id("bramble_sotonvts") is None   # wind_avg -1 AND months old
    assert _by_id("trafalgar_shipyard") is None  # all-null values
    assert _by_id("needles") is None             # metoffice entry weeks stale
    assert _by_id("brading") is None             # 2 days stale


def test_fresh_station_count_matches_expectation():
    parsed = [parse_station(r, NOW) for r in FIXTURE["data"]]
    kept = [p for p in parsed if p is not None]
    # exactly the four live stations in the fixture survive
    assert {p["station"] for p in kept} == {
        "bramble_portlog", "lymington_platform", "sandown_pier_cco",
        "spinnaker_sc"}
    # all wind speeds plausible SI values for a 13-16kn day
    for p in kept:
        assert 0 < p["wind_speed_ms"] < 12


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
