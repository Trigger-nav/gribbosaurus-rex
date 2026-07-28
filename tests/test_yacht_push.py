"""Yacht HTTPS push tests (offline): payload validation, token auth,
store round-trip with duplicate handling, and the boat-side sender's
vendored NMEA parse + queue. No network, no FastAPI needed.

Run: python tests/test_yacht_push.py   (or pytest)
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402
from gribbosaurus_rex.obs.yacht_push import (KN_TO_MS, parse_payload,  # noqa: E402
                                             token_configured, token_ok,
                                             validate_ob)

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _load_sender():
    spec = importlib.util.spec_from_file_location(
        "stingray_sender", REPO / "scripts" / "stingray_sender.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


def test_token():
    os.environ.pop("GRIBBO_YACHT_TOKEN", None)
    assert not token_configured()
    assert not token_ok("anything")          # unset server -> closed
    os.environ["GRIBBO_YACHT_TOKEN"] = "sekrit"
    try:
        assert token_configured()
        assert token_ok("sekrit")
        assert not token_ok("wrong")
        assert not token_ok(None)
        assert not token_ok("")
    finally:
        os.environ.pop("GRIBBO_YACHT_TOKEN", None)


def test_validate_ob_full():
    row, why = validate_ob({"time": "2026-08-01T11:59:00+00:00",
                            "lat": 50.76, "lon": -1.30, "tws_kn": 14.2,
                            "twd_deg": 225.0, "baro_hpa": 1013.2}, NOW)
    assert why is None
    assert row["time_iso"] == "2026-08-01T11:59:00+00:00"
    assert abs(row["wind_speed_ms"] - 14.2 * KN_TO_MS) < 5e-4  # 3dp rounding
    assert row["wind_dir_deg"] == 225.0 and row["pressure_hpa"] == 1013.2


def test_validate_ob_variants():
    ok, why = validate_ob({"time": "2026-08-01T11:00:00",   # naive -> UTC
                           "lat": 50.0, "lon": -1.0}, NOW)
    assert why is None and ok["wind_speed_ms"] is None      # position-only ok
    ok, _ = validate_ob({"time": "2026-08-01T11:00:00+00:00", "lat": 50.0,
                         "lon": -1.0, "wind_speed_ms": 7.5}, NOW)
    assert ok["wind_speed_ms"] == 7.5                       # SI passthrough
    ok, _ = validate_ob({"time": "2026-08-01T11:00:00+00:00", "lat": 50.0,
                         "lon": -1.0, "twd_deg": 360.0}, NOW)
    assert ok["wind_dir_deg"] == 0.0                        # 360 wraps


def test_validate_ob_rejects():
    bad = [
        {"time": "garbage", "lat": 50.0, "lon": -1.0},
        {"time": (NOW + timedelta(hours=1)).isoformat(), "lat": 50, "lon": -1},
        {"time": (NOW - timedelta(days=8)).isoformat(), "lat": 50, "lon": -1},
        {"time": NOW.isoformat(), "lat": 95.0, "lon": -1.0},
        {"time": NOW.isoformat(), "lat": 50.0, "lon": -1.0, "tws_kn": 400},
        {"time": NOW.isoformat(), "lat": 50.0, "lon": -1.0, "baro_hpa": 500},
        "not a dict",
    ]
    for d in bad:
        row, why = validate_ob(d, NOW)
        assert row is None and why, f"should reject: {d!r}"


def test_parse_payload():
    good = {"time": "2026-08-01T11:59:00+00:00", "lat": 50.76, "lon": -1.3,
            "tws_kn": 12.0, "twd_deg": 200.0}
    station, rows, rejected = parse_payload(
        {"station": "stingray", "obs": [good, {"time": "junk"}]}, NOW)
    assert station == "stingray" and len(rows) == 1 and len(rejected) == 1
    station, _, _ = parse_payload({"obs": [good]}, NOW)
    assert station == "stingray"                            # default station
    for bad in [[], {"obs": []}, {"obs": "x"}, "x",
                {"obs": [good] * 501}]:
        try:
            parse_payload(bad, NOW)
            raise AssertionError(f"should raise: {bad!r:.40}")
        except ValueError:
            pass


def test_store_roundtrip_and_dedup():
    with tempfile.TemporaryDirectory() as td:
        st = ObsStore(Path(td) / "t.sqlite")
        _, rows, _ = parse_payload({"obs": [
            {"time": "2026-08-01T11:58:00+00:00", "lat": 50.7, "lon": -1.3,
             "tws_kn": 10.0, "twd_deg": 180.0},
            {"time": "2026-08-01T11:59:00+00:00", "lat": 50.71, "lon": -1.31,
             "tws_kn": 11.0, "twd_deg": 185.0}]}, NOW)
        new = sum(st.insert_obs(source="yacht", station="stingray", **r)
                  for r in rows)
        assert new == 2
        dup = sum(st.insert_obs(source="yacht", station="stingray", **r)
                  for r in rows)
        assert dup == 0                                     # retry-safe
        got = st.recent_obs(window_h=24 * 365, source="yacht")
        assert len(got) == 2 and got[0].wind_speed_ms is not None
        assert st.yacht_latest(max_age_h=24 * 365 * 10) is not None


def test_sender_nmea_and_queue():
    m = _load_sender()
    st = m.NmeaState()
    st.handle(_nmea("GPRMC,120000,A,5045.600,N,00118.000,W,6.0,220.0,010826,,"))
    st.handle(_nmea("WIMWD,225.0,T,221.0,M,14.2,N,7.3,M"))
    st.handle(_nmea("WIMDA,29.92,I,1.0132,B,15.0,C,,,,,,,,,,,,,,"))
    ob = st.snapshot()
    assert ob is not None
    assert abs(ob["lat"] - (50 + 45.6 / 60)) < 1e-6
    assert abs(ob["lon"] + (1 + 18.0 / 60)) < 1e-6           # W -> negative
    assert ob["tws_kn"] == 14.2 and ob["twd_deg"] == 225.0
    assert ob["baro_hpa"] == 1013.2
    # corrupt checksum is ignored
    st2 = m.NmeaState()
    st2.handle("$GPRMC,120000,A,5045.600,N,00118.000,W,6.0,220.0,010826,,*00")
    assert st2.snapshot() is None
    # disk queue: push/pop/ack survives reload
    with tempfile.TemporaryDirectory() as td:
        q = m.Queue(Path(td) / "q.jsonl")
        for i in range(5):
            q.push({"time": f"t{i}", "lat": 1.0, "lon": 2.0})
        assert len(q.pop_batch(3)) == 3
        q.ack(3)
        q2 = m.Queue(Path(td) / "q.jsonl")                   # reload from disk
        assert [o["time"] for o in q2.items] == ["t3", "t4"]


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
