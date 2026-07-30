"""Expedition live-capture parsing tests (offline).

Uses sentences captured VERBATIM from Stingray's Expedition instrument
monitor 2026-07-30 (Cowes): Expedition emits the position as an
RMC-shaped payload under the RMB sentence id ($ECRMB,hhmmss,A,lat,...).
Both parsers (server obs/nmea.py and the boat-side sender) must accept
that shape — and must still reject a REAL RMB (waypoint nav data).

Run: python tests/test_nmea_expedition.py   (or pytest)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.obs.nmea import NmeaState  # noqa: E402

# verbatim from the Expedition monitor screenshot (checksums as sent)
EXP_MWD = "$ECMWD,275.96,T,275.11,M,7.10,N,3.65,M*4B"
EXP_RMB = "$ECRMB,091251.00,A,5045.729,N,00117.711,W,0.08,222.32,300726,0.85,E*6A"


def _nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


# a REAL RMB (waypoint navigation) — must NOT be read as a position fix
REAL_RMB = _nmea("GPRMB,A,0.66,L,003,004,4917.24,N,12309.57,W,001.3,052.5,000.5,V")


def _check_state(st):
    assert st.lat is not None, "position not parsed from Expedition RMB"
    assert abs(st.lat - 50.76215) < 1e-4
    assert abs(st.lon - (-1.29518)) < 1e-4          # Cowes, W hemisphere
    assert st.tws_kn == 7.10 and st.twd == 275.96


def test_server_parser_accepts_expedition_capture():
    st = NmeaState()
    st.handle(EXP_RMB)
    st.handle(EXP_MWD)
    _check_state(st)
    assert st.snapshot() is not None                # would emit an obs


def test_sender_parser_accepts_expedition_capture():
    spec = importlib.util.spec_from_file_location(
        "stingray_sender", REPO / "scripts" / "stingray_sender.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    st = m.NmeaState()
    st.handle(EXP_RMB)
    st.handle(EXP_MWD)
    _check_state(st)
    ob = st.snapshot()
    assert ob is not None and ob["tws_kn"] == 7.1


def test_real_rmb_still_rejected():
    st = NmeaState()
    st.handle(REAL_RMB)
    assert st.lat is None                           # waypoint nav != position


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
