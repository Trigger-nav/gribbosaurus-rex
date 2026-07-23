#!/usr/bin/env python3
"""Enumerate the valid ARPEGE time-range tokens straight from the API.

The per-run package listing (/packages/SP1?referencetime=<cycle>) links to
every available product, whose hrefs carry the exact `time=` tokens. This
prints them for ARPEGE 0.1 (404'd on our guess) and 0.25 (worked), so the
0.1 windows get pinned from ground truth.

    python scripts/mf_probe.py          # METEOFRANCE_API_KEY exported
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.fetch.base import http_session  # noqa: E402

ROOT = "https://public-api.meteofrance.fr/previnum"
H = {"apikey": os.environ["METEOFRANCE_API_KEY"]}
http = http_session()
TOKEN_RE = re.compile(r"time=(\d{2,3}H(?:\d{2,3}H)?)")
REF_RE = re.compile(r"referencetime=([0-9T:\-Z]+)")


def dump(service: str, model: str, grid: str) -> None:
    base = f"{ROOT}/{service}/v1/models/{model}/grids/{grid}"
    r = http.get(f"{base}/packages/SP1", headers=H, timeout=30)
    refs = sorted({m.group(1) for m in REF_RE.finditer(r.text)})
    ref = refs[-1] if refs else None
    print("=" * 64)
    print(f"{model} {grid}  newest run = {ref}")
    if not ref:
        print("  (no referencetime found)\n", r.text[:400])
        return
    r2 = http.get(f"{base}/packages/SP1?referencetime={ref}", headers=H, timeout=40)
    print(f"  /packages/SP1?referencetime={ref} -> HTTP {r2.status_code}")
    toks = sorted({m.group(1) for m in TOKEN_RE.finditer(r2.text)})
    if toks:
        print(f"  {len(toks)} time tokens:")
        print("   ", toks)
    else:
        print("  no time= tokens in body; first 700 chars:")
        print("   ", r2.text[:700])
    print()


dump("DPPaquetARPEGE", "ARPEGE", "0.1")
dump("DPPaquetARPEGE", "ARPEGE", "0.25")
