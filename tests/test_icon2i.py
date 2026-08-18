"""ICON-2I fetcher tests (pure logic, no network). Offline.

Pinned to the live discovery of 2026-08-17: open-data URL layout, run-id
format, per-variable level suffixes, 72 h step cap, domain guard,
registry wiring.

Run: python tests/test_icon2i.py   (or pytest)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.fetch.icon2i import (BASE, WIND_VARS,  # noqa: E402
                                           Icon2iFetcher)
from gribbosaurus_rex.fetch.registry import FETCHERS, get_fetcher  # noqa: E402


def test_run_id_and_url():
    cyc = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    rid = Icon2iFetcher._run_id(cyc)
    assert rid == "2026081712"
    url = Icon2iFetcher._url(BASE, rid, "U_10M", "heightAboveGround-10")
    # exact live filename observed in discovery
    assert url.endswith(
        "/2026081712/U_10M/"
        "ICON_2I_SURFACE_PRESSURE_LEVELS_2026081712_heightAboveGround-10.grib")


def test_wind_vars_levels():
    assert WIND_VARS == {"U_10M": "heightAboveGround-10",
                         "V_10M": "heightAboveGround-10"}


def test_steps_cap():
    f = Icon2iFetcher()
    assert f.steps(240)[-1] == 72       # file carries 0-72 h
    assert f.steps(48)[-1] == 48
    assert f.steps(240)[0] == 0
    assert f.cycle_hours == (0, 12)     # observed runs


def test_domain_guard():
    f = Icon2iFetcher()
    pm = BBox(lat_min=37.7, lat_max=44.2, lon_min=6.5, lon_max=14.5)
    f._check_domain(RaceConfig(name="pm", bbox=pm, models=("icon_2i",)))
    channel = BBox(lat_min=49.2, lat_max=51.3, lon_min=-5.2, lon_max=2.1)
    raised = False
    try:
        f._check_domain(RaceConfig(name="ec", bbox=channel,
                                   models=("icon_2i",)))
    except RuntimeError:
        raised = True
    assert raised                        # Channel is outside Italian waters


def test_concat_merges_and_cleans_up():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a, b = td / "_u.part.grib", td / "_v.part.grib"
        a.write_bytes(b"GRIB-u-messages")
        b.write_bytes(b"GRIB-v-messages")
        out = td / "icon_2i_wind.grib2"
        Icon2iFetcher._concat([a, b], out)
        assert out.read_bytes() == b"GRIB-u-messagesGRIB-v-messages"
        assert not a.exists() and not b.exists()      # parts cleaned up
        assert not out.with_suffix(".grib2.part").exists()


def test_registry():
    assert "icon_2i" in FETCHERS
    f = get_fetcher("icon_2i")
    assert f.name == "icon_2i" and f.crop_on_fetch is True


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
