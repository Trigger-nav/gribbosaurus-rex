"""DWD ICON-EU fetcher (0.0625° regular lat-lon, Europe domain).

Source: https://opendata.dwd.de/weather/nwp/icon-eu/grib/{HH}/{var}/
Files:  icon-eu_europe_regular-lat-lon_single-level_{YYYYMMDDHH}_{FFF}_{VAR}.grib2.bz2

One bz2-compressed GRIB per variable per step. We download u/v 10m wind
and mean-sea-level pressure, decompress, and concatenate the three
messages into a single .grib2 per step (matching the other fetchers'
one-file-per-step layout).

ICON-EU domain: 23.5W–62.5E, 29.5N–70.5N. The global icosahedral ICON
needs grid remapping and is deliberately not used here.
"""

from __future__ import annotations

import bz2
import logging
from datetime import datetime, timedelta
from pathlib import Path

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import BaseFetcher, FetchResult

log = logging.getLogger("gribbo.fetch.icon")

ROOT = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
VARS = {"u_10m": "U_10M", "v_10m": "V_10M", "pmsl": "PMSL"}

DOMAIN = dict(lat_min=29.5, lat_max=70.5, lon_min=-23.5, lon_max=62.5)


class IconEuFetcher(BaseFetcher):
    name = "icon_eu"
    min_publish_lag = timedelta(hours=2, minutes=30)

    def steps(self, max_lead_hours: int) -> list[int]:
        out = list(range(0, min(max_lead_hours, 78) + 1, 1))
        out += list(range(81, min(max_lead_hours, 120) + 1, 3))
        return out

    def _url(self, cycle: datetime, step: int, var_dir: str, var: str) -> str:
        return (f"{ROOT}/{cycle:%H}/{var_dir}/"
                f"icon-eu_europe_regular-lat-lon_single-level_"
                f"{cycle:%Y%m%d%H}_{step:03d}_{var}.grib2.bz2")

    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        last = self.steps(max_lead_hours or 120)[-1]
        return self.head_ok(self._url(cycle, last, "u_10m", "U_10M"))

    def _check_domain(self, cfg: RaceConfig) -> None:
        b = cfg.bbox
        if not (DOMAIN["lat_min"] <= b.lat_min and b.lat_max <= DOMAIN["lat_max"]
                and DOMAIN["lon_min"] <= b.lon_min and b.lon_max <= DOMAIN["lon_max"]):
            raise RuntimeError(
                f"Race bbox {b} is outside the ICON-EU domain; "
                "remove icon_eu from the config for this venue.")

    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        self._check_domain(cfg)
        files: list[Path] = []
        nbytes = 0
        for step in self.steps(cfg.max_lead_hours):
            out = dest / f"{self.name}_{step:03d}.grib2"
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".grib2.part")
            with open(tmp, "wb") as f:
                for var_dir, var in VARS.items():
                    r = self.http.get(self._url(cycle, step, var_dir, var),
                                      timeout=120)
                    r.raise_for_status()
                    data = bz2.decompress(r.content)
                    f.write(data)
                    nbytes += len(data)
            tmp.rename(out)
            files.append(out)
        log.info("icon_eu %s: %d files, %.1f MB", cycle, len(files), nbytes / 1e6)
        return FetchResult(files=files, nbytes=nbytes)
