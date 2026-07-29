"""resolve_weights (user-configurable blend) tests — offline, no xarray.

Run: python tests/test_blend_override.py   (or pytest)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox, RaceConfig  # noqa: E402
from gribbosaurus_rex.pipeline import resolve_weights  # noqa: E402

CFG = RaceConfig(name="t",
                 bbox=BBox(lat_min=50.45, lat_max=50.95,
                           lon_min=-2.05, lon_max=-0.7),
                 models=("ifs", "mf_arome", "ukmo_ukv"))


def test_manual_normalizes():
    w, src = resolve_weights(CFG, {"mf_arome": 60, "ukmo_ukv": 40})
    assert src == "manual"
    assert abs(w["mf_arome"] - 0.6) < 1e-9
    assert abs(w["ukmo_ukv"] - 0.4) < 1e-9
    assert "ifs" not in w                      # zero-weighted by omission
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_manual_single_model_and_scale_free():
    w, _ = resolve_weights(CFG, {"mf_arome": 7})
    assert w == {"mf_arome": 1.0}              # all-AROME view
    w2, _ = resolve_weights(CFG, {"ifs": 0.5, "mf_arome": 0.5, "ukmo_ukv": 0})
    assert abs(w2["ifs"] - 0.5) < 1e-9 and "ukmo_ukv" not in w2 \
        or w2.get("ukmo_ukv", 0) == 0          # zero weight contributes 0


def test_unknown_models_ignored():
    w, _ = resolve_weights(CFG, {"mf_arome": 1, "gfs": 99, "nonsense": 5})
    assert w == {"mf_arome": 1.0}              # gfs not in this race's models


def test_bad_overrides_raise():
    for bad in [{"mf_arome": "lots"}, {"mf_arome": float("nan")},
                {"mf_arome": -1}, {}, {"gfs": 1}, {"all": 0}, "x", 42,
                {"ifs": 0, "mf_arome": 0, "ukmo_ukv": 0}]:
        try:
            resolve_weights(CFG, bad)
            raise AssertionError(f"should raise: {bad!r}")
        except ValueError:
            pass


def test_none_falls_through_to_auto():
    w, src = resolve_weights(CFG, None)
    assert src in ("confidence", "prior")      # earned or priors, not manual
    assert abs(sum(w.values()) - 1.0) < 1e-6


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
