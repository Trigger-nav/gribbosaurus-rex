#!/usr/bin/env python3
"""Fast validation of the Météo-France slim+crop, no full arbiter pass.

Fetches ONE AROME package, runs slim_crop_file (field filter + spatial crop
to the English Channel box), and reports the size / message-count drop, then
decodes the result with cfgrib to confirm 10 m wind survived. ~1 minute.

    export METEOFRANCE_API_KEY=...   METEOFRANCE_AUTH=apikey
    python scripts/mf_crop_test.py

Expect: a large size reduction (tens of MB -> a few MB) and decoded vars
including u10/v10 (+ prmsl). If wind is missing, DON'T enable high-res in
prod — paste me the output.
"""

from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import BBox  # noqa: E402
from gribbosaurus_rex.export import slim_crop_file  # noqa: E402
from gribbosaurus_rex.fetch.meteofrance import (  # noqa: E402
    AromeFranceFetcher,
    _mf_keep,
)


def _count_messages(path: Path) -> int:
    import eccodes as ec
    n = 0
    with open(path, "rb") as fh:
        while True:
            gid = ec.codes_grib_new_from_file(fh)
            if gid is None:
                break
            n += 1
            ec.codes_release(gid)
    return n


def main() -> int:
    f = AromeFranceFetcher()
    headers = f._auth_headers()
    channel = BBox(lat_min=49.2, lat_max=51.3, lon_min=-5.2, lon_max=2.1)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "arome_test.grib2"
        token = f.ranges[0][1]  # 00H06H
        got = False
        for cycle in f.candidate_cycles():
            url = f._package_url(cycle, token)
            try:
                f.download(url, out, headers=headers, timeout=300)
                got = True
                print(f"downloaded {token} of {cycle:%Y-%m-%dT%HZ}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {cycle:%Y-%m-%dT%HZ} not available: {e}")
        if not got:
            print("no AROME package available to test")
            return 1

        before_mb = out.stat().st_size / 1e6
        before_n = _count_messages(out)
        print(f"before: {before_mb:.1f} MB, {before_n} messages")

        kept, total = slim_crop_file(out, channel.padded(1.0), keep=_mf_keep)
        after_mb = out.stat().st_size / 1e6
        print(f"after slim+crop: {after_mb:.2f} MB, {kept}/{total} messages kept "
              f"({before_mb/after_mb:.0f}x smaller)")

        import cfgrib
        allvars = set()
        for ds in cfgrib.open_datasets(str(out), backend_kwargs={"indexpath": ""}):
            allvars |= set(ds.data_vars)
            dims = dict(ds.sizes)
        print(f"decoded vars: {sorted(allvars)}  grid dims: {dims}")
        has_u = any(v in allvars for v in ("u10", "10u", "u"))
        has_v = any(v in allvars for v in ("v10", "10v", "v"))
        if has_u and has_v:
            print("\n✅ wind present and decodes — safe to enable high-res in prod")
            return 0
        print("\n❌ wind missing after slim — do NOT enable; send me this output")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
