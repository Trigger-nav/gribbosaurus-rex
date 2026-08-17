"""Staleness alarm tests (offline, temp sqlite, no network).

The alarm exists because ukmo_ukv died silently for 15 days (2026-08-02
DataHub lapse). Covers: fresh model quiet, stale model loud, no-runs
loud, threshold math for 6h- and 12h-cycle models, unknown model name.

Run: python tests/test_staleness.py   (or pytest)
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.scheduler import staleness_report  # noqa: E402
from gribbosaurus_rex.store.runs import RunStore  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _cfg(*models):
    return RaceConfig(name="t",
                      bbox=BBox(lat_min=49.0, lat_max=52.0,
                                lon_min=-6.0, lon_max=2.0),
                      models=tuple(models))


def _store_with(td, runs):
    rs = RunStore(Path(td) / "t.sqlite")
    for model, age_h in runs:
        cyc = (NOW - timedelta(hours=age_h)).isoformat()
        rs.start(model, cyc, path=str(Path(td) / model))
        rs.complete(model, cyc, n_files=1, nbytes=1)
    return rs


def test_fresh_model_is_quiet():
    # icon_eu: 6h cycles, lag 3h -> threshold 15h; a 7h-old run is fine
    with tempfile.TemporaryDirectory() as td:
        rs = _store_with(td, [("icon_eu", 7.0)])
        assert staleness_report(_cfg("icon_eu"), rs, NOW) == []


def test_stale_model_is_loud():
    with tempfile.TemporaryDirectory() as td:
        rs = _store_with(td, [("icon_eu", 40.0)])
        (w,) = staleness_report(_cfg("icon_eu"), rs, NOW)
        assert "icon_eu" in w and "40h old" in w
        assert "check the feed/credentials" in w


def test_threshold_scales_with_cycle_interval():
    # icon_2i cycles every 12h (00/12), lag 2h -> threshold 27h:
    # a 20h-old run is FINE for icon_2i but stale for a 6h-cycle model
    with tempfile.TemporaryDirectory() as td:
        rs = _store_with(td, [("icon_2i", 20.0), ("icon_eu", 20.0)])
        warns = staleness_report(_cfg("icon_2i", "icon_eu"), rs, NOW)
        assert len(warns) == 1 and "icon_eu" in warns[0]


def test_no_runs_and_unknown_model():
    with tempfile.TemporaryDirectory() as td:
        rs = _store_with(td, [])
        warns = staleness_report(_cfg("ukmo_ukv", "not_a_model"), rs, NOW)
        assert any("ukmo_ukv: no complete runs yet" in w for w in warns)
        assert any("not_a_model: no fetcher registered" in w for w in warns)


def test_fifteen_day_ukv_scenario_fires():
    # the real incident: 6h-cycle model, 363h stale — must be loud
    with tempfile.TemporaryDirectory() as td:
        rs = _store_with(td, [("ukmo_ukv", 363.0)])
        (w,) = staleness_report(_cfg("ukmo_ukv"), rs, NOW)
        assert "ukmo_ukv" in w and "363h old" in w


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
