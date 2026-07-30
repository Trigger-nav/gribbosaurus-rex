"""Obs-source trust what-if tests (offline, sqlite only, no xarray).

Covers resolve_trust validation, compute_scores with trust_override
(excluding a source changes the score exactly as the math predicts),
and the persist=False guarantee (nothing written to the scores table).

Run: python tests/test_trust_preview.py   (or pytest)
"""

from __future__ import annotations

import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402
from gribbosaurus_rex.verify import compute_scores, resolve_trust  # noqa: E402

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
ANCHOR = (50.79, -1.29)

CFG = RaceConfig(name="t",
                 bbox=BBox(lat_min=50.45, lat_max=50.95,
                           lon_min=-2.05, lon_max=-0.7),
                 models=("ifs",))
# focus unset -> anchor = bbox centre; put obs AT the centre so distance
# weight is ~1 and the score math is exact.
CENTRE = ((50.45 + 50.95) / 2, (-2.05 + -0.7) / 2)


def _seed(st: ObsStore):
    """Two obs at the anchor, same time/lead: one windycator (err 2.0),
    one metar (err 4.0). Equal non-trust weights by construction."""
    t = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    st.insert_obs(source="windycator", station="w1", lat=CENTRE[0],
                  lon=CENTRE[1], time_iso=t, wind_speed_ms=8.0,
                  wind_dir_deg=270.0)
    st.insert_obs(source="metar", station="m1", lat=CENTRE[0],
                  lon=CENTRE[1], time_iso=t, wind_speed_ms=8.0,
                  wind_dir_deg=270.0)
    cyc = (NOW - timedelta(hours=7)).isoformat(timespec="seconds")
    st.insert_verifications([
        (1, "ifs", cyc, 6.0, 7.0, 265.0, None, 2.0, -1.0, 5.0, None),
        (2, "ifs", cyc, 6.0, 7.0, 265.0, None, 4.0, -1.0, 5.0, None)])


def _expected(err: float) -> float:
    return math.exp(-err / CFG.scoring.err_scale_ms)


def test_resolve_trust():
    f = resolve_trust(CFG, None)
    assert f("windycator") == 0.8 and f("nonsense") == 0.5
    g = resolve_trust(CFG, {"metar": 0.0, "yacht": 1.5})
    assert g("metar") == 0.0 and g("yacht") == 1.5
    assert g("windycator") == 0.8            # untouched sources fall through
    for bad in [{"metar": "high"}, {"metar": -0.1}, {"metar": 2.5},
                {"metar": float("nan")}, "x", 3]:
        try:
            resolve_trust(CFG, bad)
            raise AssertionError(f"should raise: {bad!r}")
        except ValueError:
            pass


def test_override_changes_scores_exactly():
    with tempfile.TemporaryDirectory() as td:
        st = ObsStore(Path(td) / "t.sqlite")
        _seed(st)
        base = compute_scores(CFG, st, NOW, persist=False)["ifs"]
        only_w = compute_scores(CFG, st, NOW, persist=False,
                                trust_override={"metar": 0.0})["ifs"]
        only_m = compute_scores(CFG, st, NOW, persist=False,
                                trust_override={"windycator": 0.0})["ifs"]
        assert abs(only_w - _expected(2.0)) < 1e-6   # windycator-only view
        assert abs(only_m - _expected(4.0)) < 1e-6   # metar-only view
        assert only_m < base < only_w                # blend sits between


def test_persist_flag():
    with tempfile.TemporaryDirectory() as td:
        st = ObsStore(Path(td) / "t.sqlite")
        _seed(st)
        compute_scores(CFG, st, NOW, persist=False)
        compute_scores(CFG, st, NOW, persist=False,
                       trust_override={"metar": 0.0})
        assert st.latest_scores(race=CFG.name) == {}     # nothing written
        compute_scores(CFG, st, NOW)                     # default persists
        assert "ifs" in st.latest_scores(race=CFG.name)


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
